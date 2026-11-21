#!/usr/bin/env python3
"""Forward collection: scrape the channel daily, read it with the model, settle at 24h.

This is the difference between a demo and a measurement. `demo/` is one snapshot; this keeps a
ledger that grows while the submission is being judged, so the number in the README is a forward
record rather than a backtest that happened once.

    python3 collect_forward.py            # one daily cycle; silent when nothing changed
    python3 collect_forward.py --dry      # fetch and report, write nothing, no model call
    python3 collect_forward.py --force    # send the Telegram message even if nothing changed

State lives in forward/:
    posts.jsonl    every post ever seen, keyed by post id (append-only)
    calls.jsonl    accepted calls from extract_calls, deduped (append-only)
    ledger.csv     the current per-call ledger, rewritten each cycle from calls.jsonl
    summary.txt    the last verdict, for reading without running anything

Rules that keep it honest: a post is never re-read twice, an unparseable batch is retried on the
next cycle instead of being dropped, a call already in the ledger is never re-added, and the
Telegram message fires only when a call was added or a position settled — a daily "nothing
happened" would train the reader to ignore the day that matters.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import extract_calls as ex
import signal_autopsy as sa

ROOT = Path(__file__).resolve().parent
FORWARD = ROOT / "forward"
POSTS = FORWARD / "posts.jsonl"
CALLS = FORWARD / "calls.jsonl"
LEDGER = FORWARD / "ledger.csv"
SUMMARY = FORWARD / "summary.txt"

CHANNEL = os.environ.get("CMCFWD_CHANNEL", "binance_futures")
SOURCE = f"t.me/{CHANNEL}"
HORIZON_H = float(os.environ.get("CMCFWD_HORIZON_H", "24"))
ENV = Path.home() / ".hermes" / "results_bot.env"     # trading numbers go to @bybitlorant_bot
CHAT_ID = "1143478179"


def get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_posts(raw: str) -> list[dict]:
    """(post_id, iso timestamp, text) from a t.me/s/ public preview page."""
    out = []
    for chunk in raw.split(f'data-post="{CHANNEL}/')[1:]:
        m = re.match(r"(\d+)", chunk)
        d = re.search(r'datetime="([^"]+)"', chunk)
        if not m or not d:
            continue
        body = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        txt = ""
        if body:
            txt = html.unescape(re.sub(r"<br\s*/?>", " ", body.group(1)))
            txt = re.sub(r"<[^>]+>", "", txt)
        txt = txt.strip()
        if not txt:
            continue                      # photo-only post: no text means no readable call
        out.append(dict(post_id=int(m.group(1)), ts=d.group(1), text=txt))
    return sorted(out, key=lambda p: p["post_id"])


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue                       # a torn line must not poison the whole history
    return rows


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def telegram(text: str) -> int | None:
    token = None
    try:
        for line in ENV.read_text().splitlines():
            if line.strip().startswith("RESULTS_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    if not token:
        print("forward: no RESULTS_BOT_TOKEN, summary not sent", file=sys.stderr)
        return None
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
            if not payload.get("ok"):
                print(f"forward: telegram refused: {payload}", file=sys.stderr)
                return None
            return int(payload["result"]["message_id"])
    except Exception as e:                                            # noqa: BLE001
        print(f"forward: telegram failed: {e}", file=sys.stderr)
        return None


def write_ledger(rows: list[dict]) -> None:
    cols = ["coin", "cmc_id", "direction", "called_at", "entry", "exit", "bucket_lag_s",
            "gross_pct", "cost_pct", "net_pct", "pre_move_pct", "source", "pending", "note",
            "error"]
    FORWARD.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _norm_ts(value: str) -> str:
    """Canonical UTC ISO instant, so a scrape and the demo file compare equal."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) \
            .astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


def _evidence_lines(root: Path) -> dict[str, str]:
    """timestamp -> the post line a call was read from.

    Posts already in forward/posts.jsonl win over the demo scrape: they are what the channel
    published, in the form it published it. The demo file is the fallback because the 19 Nov
    posts had scrolled off the t.me/s preview page before the book was seeded.
    """
    index: dict[str, str] = {}
    for p in read_jsonl(root / "forward" / "posts.jsonl"):
        if p.get("ts") and p.get("text"):
            index.setdefault(_norm_ts(p["ts"]), f"[{p['ts']}] {p['text']}")
    demo = root / "demo" / "posts.txt"
    if demo.exists():
        for line in demo.read_text().splitlines():
            m = re.match(r"\[([^\]]+)\]\s*(\S.*)", line.strip())
            if m:
                index.setdefault(_norm_ts(m.group(1)), line.strip())
    return index


def _backfill_evidence(root: Path, evidence: dict[str, str]) -> int:
    """Fill in rows already in the book that were seeded before evidence was carried.

    Seeding twice is a no-op by design, so without this an old book keeps its empty evidence
    fields forever and the repair only ever applies to rows that do not exist yet.
    """
    path = root / "forward" / "calls.jsonl"
    rows = read_jsonl(path)
    fixed = 0
    for r in rows:
        if not r.get("evidence"):
            line = evidence.get(_norm_ts(r.get("called_at", "")))
            if line:
                r["evidence"] = line[:300]
                fixed += 1
    if fixed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        os.replace(tmp, path)
    return fixed


def seed(scraped: list[dict], root: Path = ROOT) -> None:
    """Start the forward book from the 21 Nov snapshot.

    Without this the book begins empty and the README's number has nothing behind it for a
    fortnight. The seeded rows keep their real timestamps and carry the post line they were
    read from, so the book stays checkable against its own posts. A call whose post line
    cannot be found in either scrape is dropped and counted, never written with an empty
    evidence field: an untraceable row is the one thing an evidence book cannot afford.
    """
    forward = root / "forward"
    posts, calls_path = forward / "posts.jsonl", forward / "calls.jsonl"
    have_posts = {p["post_id"] for p in read_jsonl(posts)}
    append_jsonl(posts, [p for p in scraped if p["post_id"] not in have_posts])
    evidence = _evidence_lines(root)
    backfilled = _backfill_evidence(root, evidence)
    have_calls = {(c["coin"], c["direction"], c["called_at"]) for c in read_jsonl(calls_path)}
    rows: list[dict] = []
    unresolved: list[str] = []
    for c in json.loads((root / "demo" / "calls.json").read_text()):
        key = (c["coin"], c["direction"], c["called_at"])
        if key in have_calls:
            continue
        line = evidence.get(_norm_ts(c["called_at"]))
        if not line:
            unresolved.append(c["called_at"])
            continue
        have_calls.add(key)
        rows.append(dict(coin=c["coin"], direction=c["direction"], called_at=c["called_at"],
                         evidence=line[:300], source=c.get("source") or SOURCE, seed=True,
                         added_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))
    append_jsonl(calls_path, rows)
    print(f"seeded {len(rows)} call(s) with their post line, {len(scraped)} post id(s)"
          + (f", backfilled {backfilled}" if backfilled else "")
          + (f", dropped {len(unresolved)} with no matching post" if unresolved else ""))
    for ts in unresolved:
        print(f"  dropped (no post line to point at): {ts}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="fetch and report only")
    ap.add_argument("--force", action="store_true", help="message even when unchanged")
    ap.add_argument("--seed", action="store_true",
                    help="record the 21 Nov snapshot as the book's starting point, once")
    a = ap.parse_args()

    try:
        page = get(f"https://t.me/s/{CHANNEL}")
    except Exception as e:                                            # noqa: BLE001
        print(f"forward: cannot fetch {SOURCE}: {e}", file=sys.stderr)
        return 1
    scraped = parse_posts(page)
    if not scraped:
        print(f"forward: no posts parsed from {SOURCE} — layout change?", file=sys.stderr)
        return 1

    if a.seed:
        seed(scraped)

    known_posts = {p["post_id"] for p in read_jsonl(POSTS)}
    fresh = [p for p in scraped if p["post_id"] not in known_posts]
    new_calls: list[dict] = []

    if fresh and not a.dry:
        batch = "\n".join(f"[{p['ts']}] {p['text']}" for p in fresh)
        try:
            payload = ex.extract(batch, source=SOURCE)
        except (ex.ModelError, ValueError) as e:
            # Do not record the posts as seen: the next cycle retries this batch.
            print(f"forward: extraction failed, batch left pending: {e}", file=sys.stderr)
            return 1
        known_calls = {(c["coin"], c["direction"], c["called_at"]) for c in read_jsonl(CALLS)}
        for c in payload["calls"]:
            key = (c["coin"], c["direction"], c["called_at"])
            if key in known_calls:
                continue
            known_calls.add(key)
            new_calls.append(dict(coin=c["coin"], direction=c["direction"],
                                  called_at=c["called_at"], evidence=c["evidence"][:300],
                                  source=SOURCE,
                                  added_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))
        append_jsonl(POSTS, fresh)
        if new_calls:
            append_jsonl(CALLS, new_calls)
    elif fresh and a.dry:
        print(f"(dry) {len(fresh)} new post(s), not parsed")

    calls = read_jsonl(CALLS)
    if not calls:
        print(f"forward: no calls yet ({len(read_jsonl(POSTS))} posts seen)")
        return 0
    parsed = [dict(coin=c["coin"], direction=c["direction"],
                   called_at=datetime.fromisoformat(c["called_at"]), source=c.get("source", ""))
              for c in calls]
    rows = sa.score(parsed, HORIZON_H, 11.0, 30.0)
    summary = sa.report(rows, HORIZON_H)

    if a.dry:
        print(f"(dry) {len(calls)} calls, {summary}")
        return 0

    write_ledger(rows)
    prev = {}
    if SUMMARY.exists():
        for line in SUMMARY.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                prev[k.strip()] = v.strip()
    settled_now = int(summary.get("n", 0)) - int(prev.get("n", 0) or 0)

    text = (f"Signal Autopsy — forward book · {SOURCE}\n"
            f"{summary['n']} scored · {summary['pending']} open · {summary['failed']} unpriceable\n"
            f"mean {summary['net_mean']:+.2f}% net · median {summary['median']:+.2f}% · "
            f"sd {summary['stdev']:.2f} · t {summary['t_stat']:.2f}\n"
            f"{summary['wins']}/{summary['n']} profitable · pre-call drift "
            f"{summary['pre_move_mean']:+.2f}%\n"
            f"VERDICT: {summary['verdict']}")
    if new_calls:
        text += "\n\nnew: " + ", ".join(f"{c['coin']} {c['direction']}" for c in new_calls[:8])

    SUMMARY.write_text("\n".join(f"{k} = {v}" for k, v in summary.items()) + "\n\n" + text + "\n")

    changed = bool(new_calls) or settled_now > 0
    if changed or a.force:
        print(text)
        msg_id = telegram(text)
        if msg_id:
            print(f"forward: sent to telegram, message {msg_id}")
    else:
        print(f"forward: {len(calls)} calls, nothing new, no message "
              f"(n={summary['n']} scored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
