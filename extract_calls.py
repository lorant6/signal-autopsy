#!/usr/bin/env python3
"""Turn raw channel posts into scoreable calls — the one step a script cannot do.

Everything downstream of this file is deterministic arithmetic over CoinMarketCap quotes. The
step before it is not: a post says "#IRYS Bullish" and somebody has to decide that this is a
long on IRYS at 00:34:05 UTC, and that the next post is commentary rather than a call. This
module does that with a model, under a strict JSON schema, and then validates every field as if
it had been typed by a stranger:

  * direction must resolve to long/short (bullish/bear/long/short are mapped, anything else is
    rejected, never guessed);
  * the timestamp must parse as ISO-8601 with a timezone;
  * the ticker must resolve on CoinMarketCap when a key is available;
  * duplicates are collapsed (four posts re-announcing one call is one call);
  * every accepted call keeps the post line it came from, so a human can check it in seconds.

Rejections are reported with their reason and are never silently dropped. The model is the only
non-deterministic component in the tool, so its output is cached by content hash: a demo, a test
run, or a re-score costs no quota, and the same input reproduces the same ledger.

    python3 extract_calls.py demo/posts.txt --source t.me/binance_futures -o calls.json
    python3 extract_calls.py demo/posts.txt --refresh      # ignore the cache, one fresh call
    cat posts.txt | python3 extract_calls.py - --json      # machine-readable result
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AGY = os.environ.get("AGY_BIN") or str(Path.home() / ".local" / "bin" / "agy")
MODEL = os.environ.get("EXTRACT_MODEL", "gemini-4-neon-high")
TIMEOUT_S = int(os.environ.get("EXTRACT_TIMEOUT", "600"))
CACHE = Path.home() / ".cache" / "signal_autopsy" / "extract"

SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "coin": {"type": "string",
                             "description": "ticker only, uppercase, no # and no USDT suffix"},
                    "direction": {"type": "string", "enum": ["long", "short"]},
                    "called_at": {"type": "string",
                                  "description": "ISO-8601 UTC instant taken from the post's own "
                                                 "timestamp, e.g. 2026-11-19T00:34:05+00:00"},
                    "evidence": {"type": "string",
                                 "description": "the exact post line this call was read from"},
                },
                "required": ["coin", "direction", "called_at", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["calls"],
    "additionalProperties": False,
}

PROMPT = """You are converting a scrape of a crypto signal channel into a list of trade calls.

Read every line. A line is a CALL only when it names a coin AND states a direction (bullish /
long / bearish / short). Lines that are commentary, recaps, results, ads, greetings or price
chatter are NOT calls and must be omitted.

For each call emit:
  coin       ticker only, uppercase, strip the leading '#' and any USDT suffix (BTCUSDT -> BTC)
  direction  "long" for bullish/long/BUY, "short" for bearish/short/SELL
  called_at  the timestamp on that post, verbatim, as ISO-8601 with timezone
  evidence   the post line itself, copied exactly

Rules:
  * One entry per post. If the same call is re-posted, emit each post separately — downstream
    code collapses duplicates.
  * A call can be written in prose: "Buy more #EVAA", "Buying dip #ICNT", "Buying Bless this
    week" are long calls. The coin may be named without a '#'.
  * An EXIT is not an entry: "Close #BTC short", "closing", "took profit" mean the position is
    being shut, so omit that post entirely. Scoring an exit as an entry inverts the position.
  * A coin mentioned inside a list, a tutorial about pump shapes, or macro commentary is NOT a
    call unless the sentence itself says buy/long/short.
  * Never invent a post, a timestamp or a direction. If a line has no explicit direction, omit
    it.
  * If the scrape contains no calls at all, return {"calls": []}.

The scrape follows. Each line is "[timestamp] text".
"""

_DIRECTION = {"long": "long", "bullish": "long", "bull": "long", "buy": "long", "up": "long",
              "short": "short", "bearish": "short", "bear": "short", "sell": "short",
              "down": "short"}


class ModelError(RuntimeError):
    """The CLI answered, but not with something we can use."""


def _cache_key(text: str, source: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(b"v1\0")
    h.update(model.encode())
    h.update(b"\0")
    h.update(source.encode())
    h.update(b"\0")
    h.update(text.encode())
    h.update(b"\0")
    h.update(json.dumps(SCHEMA, sort_keys=True).encode())
    return h.hexdigest()[:32]


def _model_call(text: str, model: str) -> dict:
    """One agy turn. Returns the envelope; the caller decides what is usable.

    Separate and side-effect free so tests can stub it without a login or a quota spend.
    """
    if not os.path.isfile(AGY) or not os.access(AGY, os.X_OK):
        raise ModelError(f"agy not executable at {AGY} (set AGY_BIN)")
    cmd = [AGY, "--model", model, "--print", PROMPT + text,
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise ModelError(f"agy timed out after {TIMEOUT_S}s") from e
    out = (proc.stdout or "").strip()
    if not out:
        # agy can exit 0 with an empty stdout (auth, quota): exit code alone is not a verdict.
        raise ModelError(f"agy produced no output (exit {proc.returncode}): "
                         f"{(proc.stderr or '').strip()[:300]}")
    try:
        env = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError as e:
        raise ModelError(f"agy output is not JSON: {out[:300]}") from e
    if str(env.get("status", "")).upper() != "SUCCESS":
        raise ModelError(f"agy status {env.get('status')!r}: {out[:300]}")
    if not isinstance(env.get("structured_output"), dict):
        raise ModelError("agy returned SUCCESS without structured_output")
    return env


def _validated(raw_calls: list, *, resolve=None) -> tuple[list[dict], list[dict]]:
    """Accept what parses, reject the rest with a reason. Never repair a field silently."""
    calls: list[dict] = []
    rejected: list[dict] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            rejected.append(dict(reason="not an object", item=item))
            continue
        coin = str(item.get("coin") or "").strip().upper().lstrip("#")
        for suffix in ("USDT", "USDC", "PERP"):
            if coin.endswith(suffix) and len(coin) > len(suffix):
                coin = coin[: -len(suffix)]
        direction = _DIRECTION.get(str(item.get("direction") or "").strip().lower())
        when_raw = str(item.get("called_at") or "").strip()
        try:
            when = datetime.fromisoformat(when_raw.replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("no timezone")
            when = when.astimezone(timezone.utc)
        except ValueError as e:
            rejected.append(dict(reason=f"bad timestamp ({e})", item=item))
            continue
        if not coin:
            rejected.append(dict(reason="no coin", item=item))
            continue
        if not direction:
            rejected.append(dict(reason=f"direction {item.get('direction')!r} is not long/short",
                                 item=item))
            continue
        if resolve is not None:
            try:
                ident = resolve(coin)
            except Exception as e:                                    # noqa: BLE001
                rejected.append(dict(reason=f"{coin} does not resolve on CMC ({e})", item=item))
                continue
            if not ident:
                rejected.append(dict(reason=f"{coin} does not resolve on CMC", item=item))
                continue
        calls.append(dict(coin=coin, direction=direction,
                          called_at=when.isoformat(timespec="seconds"),
                          evidence=str(item.get("evidence") or "")[:400]))
    return calls, rejected


def _dedupe(calls: list[dict]) -> tuple[list[dict], int]:
    seen, out = set(), []
    for c in sorted(calls, key=lambda c: c["called_at"]):
        key = (c["coin"], c["direction"], c["called_at"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out, len(calls) - len(out)


def extract(text: str, *, source: str = "", model: str = MODEL, refresh: bool = False,
            resolve=None, cache_dir: Path | None = None) -> dict:
    """Posts -> validated calls. Cached by content; `refresh` forces exactly one new call."""
    if not text.strip():
        raise ValueError("empty scrape: nothing to extract")
    cache_dir = cache_dir or CACHE
    key = _cache_key(text, source, model)
    path = cache_dir / f"{key}.json"

    meta: dict = dict(model=model, source=source, cached=False, cache_key=key)
    if path.exists() and not refresh:
        payload = json.loads(path.read_text())
        payload["meta"] = {**meta, "cached": True,
                           "usage": payload["meta"].get("usage"),
                           "extracted_at": payload["meta"].get("extracted_at")}
        if resolve is not None:
            calls, rejected = _validated(payload.get("raw", []), resolve=resolve)
            calls, dupes = _dedupe(calls)
            payload.update(calls=calls, rejected=rejected, duplicates=dupes)
        return payload

    env = _model_call(text, model)
    raw = (env.get("structured_output") or {}).get("calls")
    if not isinstance(raw, list):
        raise ModelError("structured_output.calls is not a list")
    calls, rejected = _validated(raw, resolve=resolve)
    calls, dupes = _dedupe(calls)

    payload = dict(calls=calls, rejected=rejected, duplicates=dupes, raw=raw,
                   meta={**meta, "cached": False, "usage": env.get("usage"),
                         "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, path)
    except OSError as e:                     # a cache failure must not lose the extraction
        print(f"extract: cache write failed: {e}", file=sys.stderr)
    return payload


def _summary(payload: dict) -> str:
    m = payload["meta"]
    src = f" · {m['source']}" if m.get("source") else ""
    how = "cached" if m.get("cached") else "fresh call"
    line = (f"{len(payload['calls'])} calls · {len(payload['rejected'])} rejected · "
            f"{payload.get('duplicates', 0)} duplicates · {m['model']} · {how}{src}")
    return line


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract scoreable calls from raw channel posts.")
    ap.add_argument("path", help="scrape file, or - for stdin")
    ap.add_argument("--source", default="", help="label stored with every call")
    ap.add_argument("-o", "--out", help="write the calls here (JSON, loadable by signal_autopsy)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache, make one new call")
    ap.add_argument("--check-coins", action="store_true",
                    help="reject tickers CoinMarketCap cannot resolve (needs CMC_API_KEY)")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    a = ap.parse_args()

    text = sys.stdin.read() if a.path == "-" else Path(a.path).read_text()
    if not text.strip():
        print("extract: empty input", file=sys.stderr)
        raise SystemExit(2)

    resolve = None
    if a.check_coins:
        import cmc
        resolve = cmc.resolve_coin

    try:
        payload = extract(text, source=a.source, model=a.model, refresh=a.refresh, resolve=resolve)
    except ModelError as e:
        print(f"extract: {e}", file=sys.stderr)
        raise SystemExit(3)
    except ValueError as e:
        print(f"extract: {e}", file=sys.stderr)
        raise SystemExit(2)

    print(_summary(payload), file=sys.stderr)
    for r in payload["rejected"]:
        print(f"  rejected: {r['reason']} — {json.dumps(r['item'])[:160]}", file=sys.stderr)

    if a.out:
        Path(a.out).write_text(json.dumps(
            [dict(coin=c["coin"], direction=c["direction"], called_at=c["called_at"],
                  source=a.source or "extracted") for c in payload["calls"]], indent=1) + "\n")
        print(f"wrote {a.out}", file=sys.stderr)
    if a.json:
        print(json.dumps({k: payload[k] for k in ("calls", "rejected", "duplicates", "meta")},
                         indent=1))


if __name__ == "__main__":
    main()
