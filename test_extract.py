#!/usr/bin/env python3
"""Tests for the extraction edge. No network, no API key, no login by default.

    python3 test_extract.py                    # offline: validation, rejection, cache
    EXTRACT_LIVE=1 python3 test_extract.py     # also one real call against demo/posts.txt

The model is the only non-deterministic part of Signal Autopsy, so the rule under test is not
"does it parse well" but "can a bad answer reach the ledger". Every field is stubbed at the
envelope boundary, exactly as agy returns it, and every stub is restored on the way out — a stub
that leaks into the next test makes the next test measure the stub instead of the model.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import extract_calls as ex

HERE = Path(__file__).resolve().parent
REAL_MODEL_CALL = ex._model_call


def envelope(calls: list) -> dict:
    return dict(status="SUCCESS", response="", structured_output={"calls": calls},
                usage={"total_tokens": 1000})


@contextmanager
def stubbed(calls: list, counter: list | None = None):
    def _call(text: str, model: str) -> dict:
        if counter is not None:
            counter.append(1)
        return envelope(calls)
    ex._model_call = _call
    try:
        yield counter
    finally:
        ex._model_call = REAL_MODEL_CALL


GOOD = dict(coin="#BTC", direction="short", called_at="2026-11-19T00:33:23+00:00",
            evidence="[2026-11-19T00:33:23+00:00] #BTC Short")


def test_accepts_and_normalises():
    calls = [GOOD,
             dict(coin="IRYSUSDT", direction="bullish", called_at="2026-11-19T00:34:05Z",
                  evidence="[2026-11-19T00:34:05+00:00] #IRYS Bullish")]
    with tempfile.TemporaryDirectory() as d, stubbed(calls):
        got = ex.extract("x", cache_dir=Path(d))
    assert [c["coin"] for c in got["calls"]] == ["BTC", "IRYS"], got["calls"]
    assert got["calls"][1]["direction"] == "long", "bullish must map to long"
    assert got["calls"][1]["called_at"] == "2026-11-19T00:34:05+00:00", got["calls"][1]


def test_rejects_instead_of_repairing():
    calls = [GOOD,
             dict(GOOD, direction="sideways"),
             dict(GOOD, called_at="yesterday"),
             dict(GOOD, called_at="2026-11-19T00:33:23"),
             dict(GOOD, coin="")]
    with tempfile.TemporaryDirectory() as d, stubbed(calls):
        got = ex.extract("x", cache_dir=Path(d))
    assert len(got["calls"]) == 1, got["calls"]
    reasons = " | ".join(r["reason"] for r in got["rejected"])
    assert "not long/short" in reasons and "bad timestamp" in reasons and "no coin" in reasons, reasons
    assert len(got["rejected"]) == 4, got["rejected"]


def test_naive_timestamp_refused():
    with tempfile.TemporaryDirectory() as d, stubbed([dict(GOOD, called_at="2026-11-19T00:33:23")]):
        got = ex.extract("x", cache_dir=Path(d))
    assert not got["calls"] and got["rejected"], got


def test_duplicates_collapsed():
    calls = [GOOD, dict(GOOD), dict(GOOD, called_at="2026-11-19T00:40:00+00:00")]
    with tempfile.TemporaryDirectory() as d, stubbed(calls):
        got = ex.extract("x", cache_dir=Path(d))
    assert len(got["calls"]) == 2, got["calls"]
    assert got["duplicates"] == 1, got["duplicates"]


def test_cache_spends_one_call():
    counter: list = []
    with tempfile.TemporaryDirectory() as d, stubbed([GOOD], counter):
        first = ex.extract("same text", source="t.me/x", cache_dir=Path(d))
        second = ex.extract("same text", source="t.me/x", cache_dir=Path(d))
    assert len(counter) == 1, f"{len(counter)} model calls, expected 1"
    assert first["meta"]["cached"] is False and second["meta"]["cached"] is True
    assert first["calls"] == second["calls"]


def test_cache_key_covers_model_and_source():
    counter: list = []
    with tempfile.TemporaryDirectory() as d, stubbed([GOOD], counter):
        ex.extract("t", source="a", cache_dir=Path(d))
        ex.extract("t", source="b", cache_dir=Path(d))
        ex.extract("t", source="a", model="some-other-model", cache_dir=Path(d))
    assert len(counter) == 3, f"{len(counter)} calls; a different source or model must not reuse cache"


def test_refresh_bypasses_cache():
    counter: list = []
    with tempfile.TemporaryDirectory() as d, stubbed([GOOD], counter):
        ex.extract("t", cache_dir=Path(d))
        ex.extract("t", cache_dir=Path(d), refresh=True)
    assert len(counter) == 2, len(counter)


def test_empty_input_costs_nothing():
    counter: list = []
    with stubbed([GOOD], counter):
        for bad in ("", "   \n\t"):
            try:
                ex.extract(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("empty input accepted")
    assert not counter, "empty input must not spend a model call"


def test_non_success_envelope_refused():
    with tempfile.TemporaryDirectory() as d:
        ex._model_call = lambda text, model: dict(status="FAILED", structured_output=None)
        try:
            ex.extract("x", cache_dir=Path(d))
        except ex.ModelError:
            return
        finally:
            ex._model_call = REAL_MODEL_CALL
        raise AssertionError("FAILED envelope accepted")


def test_unresolvable_coin_rejected():
    def resolve(sym: str):
        if sym == "NOPE":
            raise RuntimeError("0 matches")
        return dict(id=1)

    calls = [GOOD, dict(GOOD, coin="NOPE")]
    with tempfile.TemporaryDirectory() as d, stubbed(calls):
        got = ex.extract("x", cache_dir=Path(d), resolve=resolve)
    assert [c["coin"] for c in got["calls"]] == ["BTC"], got["calls"]
    assert "does not resolve" in got["rejected"][0]["reason"], got["rejected"]


def test_live_against_labelled_posts():
    """One real call over the channel scrape, scored against demo/posts.labels.json.

    The labels, not demo/calls.json, are the ground truth: the hand-parsed file counts several
    commentary posts as calls and one EXIT as a long entry. Precision must be perfect — a call
    the model invented is worse than one it missed — and recall is printed, not hidden.
    Opt-in: costs one quota turn, and it must run against the real CLI, not a leftover stub.
    """
    if os.environ.get("EXTRACT_LIVE") != "1":
        print("  (skipped live extraction: set EXTRACT_LIVE=1)")
        return
    assert ex._model_call is REAL_MODEL_CALL, "a stubbed model call leaked into the live test"
    labels = json.loads((HERE / "demo" / "posts.labels.json").read_text())["posts"]
    expected = {(c[0], c[1], p["ts"]) for p in labels if (c := p.get("call"))}
    got_payload = ex.extract((HERE / "demo" / "posts.txt").read_text(),
                             source="t.me/binance_futures", refresh=True)
    got = {(c["coin"], c["direction"], c["called_at"]) for c in got_payload["calls"]}
    invented, missed = got - expected, expected - got
    print(f"  live: {len(got)} calls ({len(expected)} labelled) · invented {len(invented)} · "
          f"missed {len(missed)} · rejected {len(got_payload['rejected'])} · "
          f"tokens {got_payload['meta'].get('usage', {}).get('total_tokens')}")
    for item in sorted(invented)[:5]:
        print(f"    invented {item}")
    for item in sorted(missed)[:5]:
        print(f"    missed {item}")
    assert not invented, f"model called posts the labels say are not calls: {sorted(invented)[:3]}"
    recall = len(got & expected) / len(expected)
    assert recall >= 0.85, f"recall {recall:.2f} below 0.85"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print("\nall extraction assertions hold")
