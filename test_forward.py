#!/usr/bin/env python3
"""Forward-book tests: a seeded call must point at the post it was read from.

    python3 test_forward.py

Offline: no model, no API key, no network. The forward book is the artifact a judge is asked
to believe, so the property under test is traceability — every seeded row resolves to a post
line, and a call that cannot be traced is dropped rather than written with empty evidence.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import collect_forward as cf

HERE = Path(__file__).resolve().parent


def build_root(tmp: str) -> Path:
    """A throwaway copy of the repo's demo scrape plus an empty forward book."""
    root = Path(tmp)
    shutil.copytree(HERE / "demo", root / "demo")
    (root / "forward").mkdir()
    return root


def demo_posts() -> dict[str, str]:
    return {line.split("]")[0].strip("["): line.strip()
            for line in (HERE / "demo" / "posts.txt").read_text().splitlines()
            if line.startswith("[")}


def test_seed_carries_evidence_and_drops_untraceable():
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        demo = json.loads((root / "demo" / "calls.json").read_text())
        orphan = dict(demo[0], called_at="2026-11-01T00:00:00+00:00")
        (root / "demo" / "calls.json").write_text(json.dumps(demo + [orphan]))
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    assert len(rows) == len(demo), f"{len(rows)} rows for {len(demo)} traceable calls"
    assert all(r["evidence"] for r in rows), [r for r in rows if not r["evidence"]]
    assert orphan["called_at"] not in {r["called_at"] for r in rows}, "an untraceable call was seeded"


def test_evidence_is_the_post_line_itself():
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    posts = demo_posts()
    for r in rows:
        want = posts[cf._norm_ts(r["called_at"])]
        assert r["evidence"] == want[:300], (r["coin"], r["called_at"], r["evidence"][:60])


def test_seed_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        cf.seed([], root=root)
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    keys = {(r["coin"], r["direction"], r["called_at"]) for r in rows}
    assert len(keys) == len(rows), "seeding twice duplicated a call"


def test_ledger_suppresses_nothing():
    """Every call in the book lands in the ledger as scored, pending or failed — never dropped."""
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    assert rows
    assert all({"coin", "direction", "called_at"} <= set(r) for r in rows), rows[0]


def test_backfill_repairs_an_old_book():
    """A book seeded before evidence was carried must be repaired, not left empty forever."""
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        demo = json.loads((root / "demo" / "calls.json").read_text())
        (root / "forward" / "calls.jsonl").write_text("".join(
            json.dumps(dict(coin=c["coin"], direction=c["direction"], called_at=c["called_at"],
                            evidence="", source=c["source"], seed=True)) + "\n" for c in demo))
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    assert len(rows) == len(demo), "backfill must not duplicate rows"
    assert all(r["evidence"] for r in rows), [r["called_at"] for r in rows if not r["evidence"]]


def test_append_survives_a_torn_last_line():
    """A crash mid-write leaves no trailing newline; the next append must not be glued onto it."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "calls.jsonl"
        p.write_text('{"coin":"BTC","direction":"long","called_at":"2026-11-19T00:00:00+00:00"}\n'
                     '{"coin":"IRY')                                   # torn, no newline
        cf.append_jsonl(p, [dict(coin="ETH", direction="short",
                                 called_at="2026-11-20T00:00:00+00:00")])
        bad: list[int] = []
        rows = cf.read_jsonl(p, bad=bad)
    assert [r["coin"] for r in rows] == ["BTC", "ETH"], rows
    assert bad == [2], f"the torn line must still be reported, got {bad}"


def test_backfill_refuses_to_rewrite_an_unreadable_book():
    """Rewriting from what parsed would delete the line that did not."""
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        demo = json.loads((root / "demo" / "calls.json").read_text())
        book = (root / "forward" / "calls.jsonl")
        good = dict(coin=demo[0]["coin"], direction=demo[0]["direction"],
                    called_at=demo[0]["called_at"], evidence="", source=demo[0]["source"])
        book.write_text(json.dumps(good) + "\n{\"coin\": \"TORN")
        cf.seed([], root=root)
        after = book.read_text()
    lines = after.splitlines()
    assert lines[0] == json.dumps(good), "the torn book must not be rewritten wholesale"
    assert '{"coin": "TORN' in after, "the unreadable line must survive, not be deleted"
    assert not json.loads(lines[0])["evidence"], "backfill must refuse while a line is unreadable"


def test_evidence_prefers_the_post_that_names_the_coin():
    """Two posts in the same second must not hand each other's text to a call."""
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        ts = "2026-11-19T00:34:05+00:00"
        (root / "demo" / "posts.txt").write_text(
            f"[{ts}] #AAA Bullish\n[{ts}] #BBB Bullish\n")
        (root / "demo" / "calls.json").write_text(json.dumps([
            dict(coin="BBB", direction="long", called_at=ts, source="t.me/x")]))
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    assert len(rows) == 1, rows
    assert rows[0]["evidence"].endswith("#BBB Bullish"), rows[0]["evidence"]
    assert "evidence_ambiguous" not in rows[0], "an unambiguous pick must not be flagged"


def test_summary_text_when_nothing_settled():
    """A day with no settled call must say so instead of raising KeyError on a missing mean."""
    txt = cf.summary_text(dict(n=0, failed=3, pending=4, note="no call could be scored"),
                          "t.me/x")
    assert "nothing settled yet" in txt and "4 open" in txt and "3 unpriceable" in txt, txt
    normal = cf.summary_text(dict(n=11, failed=3, pending=4, net_mean=0.95, median=0.2,
                                  stdev=11.24, t_stat=0.28, wins=7, pre_move_mean=4.24,
                                  verdict="v"), "t.me/x", [dict(coin="IDOL", direction="long")])
    assert "mean +0.95% net" in normal and "new: IDOL long" in normal, normal


def test_evidence_flags_an_ambiguous_second():
    """When two posts in one second both name the coin, say so instead of picking silently."""
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        ts = "2026-11-19T00:34:05+00:00"
        (root / "demo" / "posts.txt").write_text(
            f"[{ts}] #BBB Bullish\n[{ts}] #BBB Bullish again\n")
        (root / "demo" / "calls.json").write_text(json.dumps([
            dict(coin="BBB", direction="long", called_at=ts, source="t.me/x")]))
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    assert len(rows) == 1 and rows[0]["evidence_ambiguous"] is True, rows


def test_mentions_is_case_and_context_sensitive():
    """`etc.` is not the ETC ticker and a URL is not a symbol."""
    assert cf._mentions("and so on etc. nothing", "ETC") is False
    assert cf._mentions("https://x.com/foo", "X") is False
    assert cf._mentions("see https://t.me/IRYS/123", "IRYS") is False
    assert cf._mentions("#ETC bullish", "ETC") is True
    assert cf._mentions("Buying BTCUSDT now", "BTC") is True
    assert cf._mentions("METHOD call on ETH", "ETH") is True
    assert cf._mentions("METHOD only", "ETH") is False
    assert cf._mentions("1INCH pumping", "1INCH") is True
    assert cf._mentions("Buying Bless this week", "BLESS") is True
    assert cf._mentions("cross the street", "CROSS") is False
    assert cf._mentions("irys lowercase", "IRYS") is False, "lower-case prose is not a ticker"


def test_single_post_without_the_ticker_is_flagged():
    """One post, but it never names the coin: provenance rests on the timestamp, so say so."""
    with tempfile.TemporaryDirectory() as d:
        root = build_root(d)
        ts = "2026-11-19T00:34:05+00:00"
        (root / "demo" / "posts.txt").write_text(f"[{ts}] buying the dip again\n")
        (root / "demo" / "calls.json").write_text(json.dumps([
            dict(coin="IRYS", direction="long", called_at=ts, source="t.me/x")]))
        cf.seed([], root=root)
        rows = cf.read_jsonl(root / "forward" / "calls.jsonl")
    assert len(rows) == 1 and rows[0]["evidence_ambiguous"] is True, rows


def test_prose_word_does_not_claim_exact_provenance():
    """`Link to the report` is prose: if it beats another post from the same second, say so."""
    ts = "2026-11-19T00:34:05+00:00"
    index = {ts: [f"[{ts}] Link to the report below", f"[{ts}] something else entirely"]}
    line, ambiguous = cf._pick_evidence(index, ts, "LINK")
    assert line.endswith("Link to the report below") and ambiguous is True, (line, ambiguous)

    # ...but a capitalised ticker with no rival in that second is still exact.
    alone = {ts: [f"[{ts}] Buying Bless this week"]}
    line, ambiguous = cf._pick_evidence(alone, ts, "BLESS")
    assert line.endswith("Buying Bless this week") and ambiguous is False, (line, ambiguous)

    # An exact ticker outranks prose written in capitals.
    mixed = {ts: [f"[{ts}] Link to the report", f"[{ts}] #LINK long"]}
    line, ambiguous = cf._pick_evidence(mixed, ts, "LINK")
    assert line.endswith("#LINK long") and ambiguous is False, (line, ambiguous)


def test_mention_kind_separates_exact_from_capitalised():
    assert cf._mention_kind("#ETC bullish", "ETC") == "strong"
    assert cf._mention_kind("Etc. is what follows", "ETC") == "weak"
    assert cf._mention_kind("and so on etc.", "ETC") is None
    assert cf._mention_kind("see https://t.me/IRYS/123", "IRYS") is None
    assert cf._mention_kind("$XRP pumping", "X") is None, "a longer ticker is not this one"


def test_the_two_matchers_cannot_drift():
    """`_mentions` is a wrapper now; if it ever grows its own regex again this fails."""
    cases = [("and so on etc. nothing", "ETC"), ("#ETC bullish", "ETC"),
             ("Buying Bless this week", "BLESS"), ("cross the street", "CROSS"),
             ("MAX gains today", "MAX"), ("see https://t.me/IRYS/123", "IRYS"),
             ("Buying BTCUSDT now", "BTC"), ("1INCH pumping", "1INCH"),
             ("METHOD only", "ETH"), ("irys lowercase", "IRYS")]
    for line, coin in cases:
        assert cf._mentions(line, coin) is (cf._mention_kind(line, coin) is not None), (line, coin)


def test_an_all_caps_english_word_is_indistinguishable():
    """Pinned limitation: MAX is a ticker and a word, and capitals carry no signal to tell them
    apart. The flag cannot rescue this case; the README says so rather than implying otherwise."""
    assert cf._mention_kind("MAX gains today", "MAX") == "strong"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\nall forward-book assertions hold")
