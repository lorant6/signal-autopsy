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


def read_jsonl(path: Path, bad: list[int] | None = None) -> list[dict]:
    """Parse a JSONL file, skipping unreadable lines.

    Pass `bad` to collect the line numbers that did not parse: a caller about to rewrite the
    file must know it is holding an incomplete picture, or it will delete what it could not read.
    """
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if bad is not None:
                bad.append(n)
            continue                       # a torn line must not poison the whole history
    return rows


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def append_jsonl(path: Path, rows: list[dict]) -> None:
    """Append rows without gluing them to a torn last line.

    A crash mid-write leaves a partial line with no trailing newline; appending straight onto it
    fuses the fragment and the new record into one unparseable line, losing the new row too. The
    torn line is left in place as its own unreadable line, where read_jsonl(bad=...) reports it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stitch = path.exists() and path.stat().st_size > 0 and not _ends_with_newline(path)
    with path.open("a") as f:
        if stitch:
            print(f"forward: {path.name} does not end in a newline (torn write?) — "
                  f"starting a fresh line", file=sys.stderr)
            f.write("\n")
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


def _evidence_lines(root: Path) -> dict[str, list[str]]:
    """timestamp -> every post line published at that instant.

    Posts already in forward/posts.jsonl win over the demo scrape: they are what the channel
    published, in the form it published it. The demo file is the fallback because the 19 Nov
    posts had scrolled off the t.me/s preview page before the book was seeded.

    A timestamp is kept as a list, not a single line: message timestamps carry whole seconds, so
    two different posts can share one, and picking the first would attach one post's text to a
    call read from the other.
    """
    index: dict[str, list[str]] = {}
    for p in read_jsonl(root / "forward" / "posts.jsonl"):
        if p.get("ts") and p.get("text"):
            index.setdefault(_norm_ts(p["ts"]), []).append(f"[{p['ts']}] {p['text']}")
    demo = root / "demo" / "posts.txt"
    if demo.exists():
        for line in demo.read_text().splitlines():
            m = re.match(r"\[([^\]]+)\]\s*(\S.*)", line.strip())
            if m and not any(line.strip() == seen for seen in index.get(_norm_ts(m.group(1)), [])):
                index.setdefault(_norm_ts(m.group(1)), []).append(line.strip())
    return index


def _mentions(line: str, coin: str) -> bool:
    """Does this post line name the ticker? `#IRYS`, `IRYS`, `IRYSUSDT`, `Bless` all count.

    Matched against the raw line rather than an upper-cased copy: prose writes "etc." in lower
    case, so upper-casing first made ETC match "etc." and X match a URL segment. Two forms are
    accepted — the exact ticker in capitals, and its capitalised word form ("Bless" for BLESS,
    which is how that channel actually writes it). URL paths are excluded on both sides so a
    ticker is never inferred from a link.
    """
    c = coin.strip().upper()
    if not c:
        return False
    forms = [c]
    if c.isalpha() and len(c) >= 3:
        forms.append(c.capitalize())
    pattern = "|".join(re.escape(f) for f in forms)
    return bool(re.search(rf"(?<![A-Za-z0-9#/])#?(?:{pattern})(?:USDT|USDC|PERP)?(?![A-Za-z0-9/])",
                          line))


def _mention_kind(line: str, coin: str) -> str | None:
    """How the ticker is named: exactly (`strong`) or only as a capitalised word (`weak`).

    The capitalised form is what the channel writes for BLESS ("Buying Bless this week"), but
    it is also how English writes an ordinary word: "Link to the report", "Near the top",
    "Etc." at the start of a sentence. A strong match settles provenance on its own; a weak one
    only does so when nothing else shares that second, so callers can flag the difference
    instead of presenting a guess as an exact citation.
    """
    c = coin.strip().upper()
    if not c:
        return None
    tail = r"(?:USDT|USDC|PERP)?(?![A-Za-z0-9/])"
    head = r"(?<![A-Za-z0-9#/])#?"
    if re.search(head + re.escape(c) + tail, line):
        return "strong"
    cap = c.capitalize()
    if cap != c and re.search(head + re.escape(cap) + tail, line):
        return "weak"
    return None


def _pick_evidence(index: dict[str, list[str]], when: str,
                   coin: str) -> tuple[str | None, bool]:
    """The post line a call was read from, and whether the choice had to be guessed.

    Between two posts in the same second, the one that names the ticker in capitals wins. If
    several do, or the only candidate never mentions the ticker, the line is returned flagged:
    the evidence then rests on the timestamp alone, and saying so beats passing an inference off
    as exact provenance. A capitalised-word match that has to beat another post in the same
    second is flagged too — "Link to the report" is prose as often as it is a ticker.
    """
    candidates = index.get(_norm_ts(when)) or []
    if not candidates:
        return None, False
    kinds = [(_mention_kind(line, coin), line) for line in candidates]
    strong = [line for kind, line in kinds if kind == "strong"]
    weak = [line for kind, line in kinds if kind == "weak"]
    if len(strong) == 1:
        return strong[0], False
    if len(strong) > 1:
        return strong[0], True
    if len(weak) == 1:
        return weak[0], len(candidates) > 1
    if len(weak) > 1:
        return weak[0], True
    return candidates[0], True


def _backfill_evidence(root: Path, evidence: dict[str, list[str]]) -> int:
    """Fill in rows already in the book that were seeded before evidence was carried.

    Seeding twice is a no-op by design, so without this an old book keeps its empty evidence
    fields forever and the repair only ever applies to rows that do not exist yet. A file with
    an unreadable line is left alone: rewriting from what parsed would delete the rest.
    """
    path = root / "forward" / "calls.jsonl"
    bad: list[int] = []
    rows = read_jsonl(path, bad=bad)
    if bad:
        print(f"forward: {len(bad)} unreadable line(s) in {path.name} (first at line {bad[0]}) — "
              f"refusing to rewrite it, repair the file by hand first", file=sys.stderr)
        return 0
    fixed = 0
    for r in rows:
        if not r.get("evidence"):
            line, ambiguous = _pick_evidence(evidence, r.get("called_at", ""), r.get("coin", ""))
            if line:
                r["evidence"] = line[:300]
                if ambiguous:
                    r["evidence_ambiguous"] = True
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
    ambiguous = 0
    for c in json.loads((root / "demo" / "calls.json").read_text()):
        key = (c["coin"], c["direction"], c["called_at"])
        if key in have_calls:
            continue
        line, guessy = _pick_evidence(evidence, c["called_at"], c["coin"])
        if not line:
            unresolved.append(c["called_at"])
            continue
        have_calls.add(key)
        row = dict(coin=c["coin"], direction=c["direction"], called_at=c["called_at"],
                   evidence=line[:300], source=c.get("source") or SOURCE, seed=True,
                   added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if guessy:
            row["evidence_ambiguous"] = True
            ambiguous += 1
        rows.append(row)
    append_jsonl(calls_path, rows)
    print(f"seeded {len(rows)} call(s) with their post line, {len(scraped)} post id(s)"
          + (f", backfilled {backfilled}" if backfilled else "")
          + (f", {ambiguous} ambiguous timestamp(s)" if ambiguous else "")
          + (f", dropped {len(unresolved)} with no matching post" if unresolved else ""))
    for ts in unresolved:
        print(f"  dropped (no post line to point at): {ts}", file=sys.stderr)


def summary_text(summary: dict, source: str, new_calls: list[dict] | None = None) -> str:
    """The daily message. A day where nothing settled must say so, not crash on a missing mean.

    `report()` returns only n/failed/pending/note when no call could be scored; formatting the
    mean of an empty sample raises KeyError and used to kill the cycle after the ledger was
    written but before summary.txt was updated.
    """
    head = f"Signal Autopsy — forward book · {source}\n"
    if not summary.get("n"):
        return (head
                + f"nothing settled yet: {summary.get('pending', 0)} open · "
                  f"{summary.get('failed', 0)} unpriceable\n"
                + (summary.get("note") or "no call could be scored"))
    text = (head
            + f"{summary['n']} scored · {summary.get('pending', 0)} open · "
              f"{summary.get('failed', 0)} unpriceable\n"
            + f"mean {summary['net_mean']:+.2f}% net · median {summary['median']:+.2f}% · "
              f"sd {summary['stdev']:.2f} · t {summary['t_stat']:.2f}\n"
            + f"{summary['wins']}/{summary['n']} profitable · pre-call drift "
              f"{summary['pre_move_mean']:+.2f}%\n"
            + f"VERDICT: {summary['verdict']}")
    if new_calls:
        text += "\n\nnew: " + ", ".join(f"{c['coin']} {c['direction']}" for c in new_calls[:8])
    return text


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

    text = summary_text(summary, SOURCE, new_calls)

    SUMMARY.write_text("\n".join(f"{k} = {v}" for k, v in summary.items()) + "\n\n" + text + "\n")

    changed = bool(new_calls) or settled_now > 0
    if changed or a.force:
        print(text)
        msg_id = telegram(text)
        if msg_id:
            print(f"forward: sent to telegram, message {msg_id}")
    else:
        print(f"forward: {len(calls)} calls, nothing new, no message "
              f"(n={summary.get('n', 0)} scored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
