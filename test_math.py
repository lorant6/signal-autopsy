"""Arithmetic tests for Signal Autopsy. No network, no API key, no test framework.

    python test_math.py

A scoring tool whose own maths is unchecked is just a nicer-looking opinion, so the numbers a
judge would question are asserted here rather than trusted.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import cmc
import signal_autopsy as sa

CALL = datetime(2026, 11, 19, 0, 34, tzinfo=timezone.utc)


def stub(prices: dict):
    cmc.resolve_coin = lambda s: dict(id=1, name=s, slug=s.lower(), symbol=s, candidates=1)
    cmc.quote_at = lambda cid, when, interval="1h": dict(
        price=prices[min(prices, key=lambda t: abs((t - when).total_seconds()))],
        bucket=when.isoformat(), lag_seconds=0)


def test_long_win():
    stub({CALL: 1.00, CALL + timedelta(hours=24): 1.25, CALL - timedelta(hours=1): 0.97})
    r = sa.score([dict(coin="T", direction="long", called_at=CALL, source="t")], 24, 11, 30)[0]
    assert abs(r["gross_pct"] - 25.0) < 0.01, r
    assert abs(r["cost_pct"] - 0.71) < 0.01, r            # 11bp fee + 2 x 30bp slippage
    assert abs(r["net_pct"] - 24.29) < 0.01, r
    assert abs(r["pre_move_pct"] - (1 / 0.97 - 1) * 100) < 0.01, r


def test_short_win_and_lose():
    stub({CALL: 1.00, CALL + timedelta(hours=24): 0.75, CALL - timedelta(hours=1): 1.00})
    r = sa.score([dict(coin="T", direction="short", called_at=CALL, source="t")], 24, 11, 30)[0]
    assert abs(r["gross_pct"] - 25.0) < 0.01, r           # price fell, short profits

    stub({CALL: 1.00, CALL + timedelta(hours=24): 1.25, CALL - timedelta(hours=1): 1.00})
    r = sa.score([dict(coin="T", direction="short", called_at=CALL, source="t")], 24, 11, 30)[0]
    assert abs(r["gross_pct"] + 25.0) < 0.01, r
    assert abs(r["net_pct"] + 25.71) < 0.01, r


def test_costs_always_applied():
    stub({CALL: 1.00, CALL + timedelta(hours=24): 1.00, CALL - timedelta(hours=1): 1.00})
    r = sa.score([dict(coin="T", direction="long", called_at=CALL, source="t")], 24, 11, 30)[0]
    assert r["gross_pct"] == 0.0, r
    assert r["net_pct"] < 0, "a flat trade must lose the fee, not break even"


def test_verdict_is_symmetric():
    """Both directions face the same bar.

    The first version of this test encoded the opposite rule: a negative mean asserted "loses
    money" at n=20 with no t-test, while a positive one needed t>2. A tool that audits other
    people's claims cannot hold a thumb on one side of the scale, so the policy is now
    n>=30 AND |t|>2 for either direction, and the test enforces it on both sides.
    """
    small = [dict(net_pct=1.0, gross_pct=1.0, pre_move_pct=0.0)] * 5
    assert "underpowered" in sa.report(small, 24)["verdict"]

    # enough trades to judge, negative and decisive -> allowed to name it
    losing = [dict(net_pct=-1.0 if i % 2 else -3.0, gross_pct=-1.0, pre_move_pct=0.0)
              for i in range(40)]
    assert "loses money" in sa.report(losing, 24)["verdict"]

    # same size, same decisiveness, positive -> also allowed to name it
    winning = [dict(net_pct=1.0 if i % 2 else 3.0, gross_pct=1.0, pre_move_pct=0.0)
               for i in range(40)]
    assert "positive and statistically suggestive" in sa.report(winning, 24)["verdict"]

    # a losing mean that is inside the noise band must NOT be called a loss
    small_losing = [dict(net_pct=-2.0, gross_pct=-1.0, pre_move_pct=0.0)] * 20
    v = sa.report(small_losing, 24)["verdict"]
    assert "loses money" not in v and "underpowered" in v, v

    # decisive sample, no direction
    noisy = [dict(net_pct=1.0 if i % 2 else -1.0, gross_pct=1.0, pre_move_pct=0.0)
             for i in range(40)]
    assert "no effect distinguishable" in sa.report(noisy, 24)["verdict"]


def test_unripe_call_is_pending_not_error():
    """A call whose 24h has not finished cannot be scored yet — and must not be called an error."""
    def boom(*a, **k):
        raise AssertionError("a pending call must not touch the network")
    cmc.resolve_coin, cmc.quote_at = boom, boom
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    rows = sa.score([dict(coin="T", direction="long", called_at=future, source="t")], 24, 11, 30)
    assert rows[0].get("pending") is True, rows
    assert "error" not in rows[0], rows
    assert "net_pct" not in rows[0], "an unripe call must not be reported as a result"


def test_counts_add_up():
    rows = ([dict(net_pct=1.0, gross_pct=1.0, pre_move_pct=0.0)] * 5
            + [dict(pending=True, coin="P")] * 3
            + [dict(error="inactive")] * 2)
    s = sa.report(rows, 24)
    assert (s["n"], s["pending"], s["failed"]) == (5, 3, 2), s


def test_quote_at_rejects_future_and_accepts_dict():
    import importlib
    importlib.reload(cmc)          # earlier tests monkeypatch cmc.quote_at; use the real one
    def boom(*a, **k):
        raise AssertionError("must fail before the request")
    cmc.get = boom
    try:
        cmc.quote_at(1, datetime.now(timezone.utc) + timedelta(hours=1))
    except cmc.CMCError as exc:
        assert "past" in str(exc), exc
    else:
        raise AssertionError("a future instant must be rejected, not silently cached as empty")
    assert cmc._coin_id({"id": 1027}) == 1027
    assert cmc._coin_id("1027") == 1027


def test_missing_key_fails_loudly():
    """No key must stop the run before it can pretend the cache answered for it."""
    import importlib
    import os
    importlib.reload(cmc)          # earlier tests monkeypatch cmc.get / quote_at
    saved = os.environ.pop("CMC_API_KEY", None)
    try:
        try:
            cmc._key()
        except SystemExit as exc:
            assert "CMC_API_KEY" in str(exc), exc
        else:
            raise AssertionError("a missing key must stop the run, not fall through to the cache")
    finally:
        if saved:
            os.environ["CMC_API_KEY"] = saved


def test_transport_errors_become_cmc_errors():
    """A DNS blip, a timeout or a truncated body must raise CMCError, not escape cmc.get."""
    import importlib
    import urllib.error
    importlib.reload(cmc)                       # earlier tests monkeypatch cmc.get / quote_at
    saved_key = os.environ.setdefault("CMC_API_KEY", "test-key-never-sent")
    saved_open = cmc.urllib.request.urlopen

    def boom(*a, **k):
        raise urllib.error.URLError("temporary DNS failure")

    cmc.urllib.request.urlopen = boom
    try:
        cmc.get("/v1/cryptocurrency/map")
    except cmc.CMCError as exc:
        assert exc.status == 0, exc.status
        assert "network failure" in str(exc), exc
    else:
        raise AssertionError("a transport failure escaped as something other than CMCError")
    finally:
        cmc.urllib.request.urlopen = saved_open
        if saved_key is None:
            os.environ.pop("CMC_API_KEY", None)


def test_one_failure_does_not_end_the_run():
    """A call that cannot be priced becomes a failed row; every other call still scores."""
    import importlib
    import signal_autopsy as sa2
    importlib.reload(cmc)
    importlib.reload(sa2)

    def resolve(sym: str):
        if sym == "IRYS":
            raise cmc.CMCError(0, "network failure, nothing received: temporary DNS failure")
        return dict(id=1)

    sa2.cmc.resolve_coin = resolve
    sa2.cmc.quote_at = lambda cid, when, interval="1h": dict(
        price=1.0, bucket=str(when), lag_seconds=0)
    calls = [dict(coin="IRYS", direction="long", called_at=CALL, source="t"),
             dict(coin="BTC", direction="short", called_at=CALL, source="t")]
    rows = sa2.score(calls, 24, 11, 30)
    assert len(rows) == 2, rows
    bad, good = rows
    assert "network failure" in bad["error"] and "retryable" in bad["note"], bad
    assert "net_pct" in good and "error" not in good, good
    summary = sa2.report(rows, 24)
    assert (summary["n"], summary["failed"]) == (1, 1), summary


def test_slippage_sensitivity_scales_both_sides():
    rows = [dict(net_pct=1.0, gross_pct=1.71, pre_move_pct=0.0)] * 10
    sens = sa.slippage_sensitivity(rows, 30, 2.0)
    assert abs(sens["extra_cost_pct"] - 0.6) < 0.01, sens      # 2 x 30bp charged twice
    assert abs(sens["net_mean"] - 0.4) < 0.01, sens
    assert sa.slippage_sensitivity([], 30, 2.0) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\nall maths assertions hold")
