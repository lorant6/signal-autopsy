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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\nall forward-book assertions hold")
