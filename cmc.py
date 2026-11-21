"""Minimal CoinMarketCap API client: resolve a ticker, price a coin at a moment.

Key comes from CMC_API_KEY in the environment. It is never read from a file in this repo and
never logged — a key in a log is a key that has leaked.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://pro-api.coinmarketcap.com"
CACHE = Path.home() / ".cache" / "signal_autopsy"


class CMCError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"CMC API {status}: {body[:200]}")
        self.status = status
        self.body = body


def _key() -> str:
    key = (os.environ.get("CMC_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "CMC_API_KEY is not set. Get a free key at https://coinmarketcap.com/api/ and\n"
            "export it in your shell. Do not commit it.")
    return key


def get(path: str, params: dict | None = None, cache_seconds: int = 0) -> dict:
    """GET a CMC endpoint. Optional on-disk cache keeps demo runs cheap and reproducible."""
    params = params or {}
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    cached = None
    if cache_seconds:
        CACHE.mkdir(parents=True, exist_ok=True)
        slug = urllib.parse.quote(url.replace(BASE, ""), safe="")[:150]
        cached = CACHE / f"{slug}.json"
        if cached.exists() and time.time() - cached.stat().st_mtime < cache_seconds:
            return json.loads(cached.read_text())

    req = urllib.request.Request(url, headers={
        "X-CMC_PRO_API_KEY": _key(),
        "Accept": "application/json",
        "User-Agent": "signal-autopsy/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
    except urllib.error.HTTPError as exc:            # 401/403/429 must not look like an empty result
        raise CMCError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A DNS blip, a dropped route or a slow key must not end a scoring run: the caller
        # writes the row as failed and carries on. status 0 marks it transport-level, which is
        # how a later cycle knows the failure was retryable rather than a delisting.
        reason = getattr(exc, "reason", exc)
        raise CMCError(0, f"network failure, nothing received: {reason}") from exc

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CMCError(0, f"malformed response, not JSON: {body[:120]!r}") from exc

    if cached is not None:
        cached.write_text(json.dumps(data))
    return data


def resolve_coin(symbol: str) -> dict:
    """Symbol -> {id, name, slug}. Prefers the highest-ranked active coin on a real market."""
    data = get("/v1/cryptocurrency/map", {"symbol": symbol.upper(), "listing_status": "active"},
               cache_seconds=86400)
    rows = (data.get("data") or [])
    if not rows:
        raise CMCError(404, f"no active coin for symbol {symbol!r}")
    rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 10**9))
    best = rows[0]
    return dict(id=best["id"], name=best.get("name", symbol), slug=best.get("slug", ""),
                symbol=best.get("symbol", symbol), candidates=len(rows))


def _coin_id(coin) -> int:
    """Accept an id int, a numeric string, or the dict resolve_coin returns."""
    if isinstance(coin, dict):
        coin = coin.get("id")
    try:
        return int(str(coin).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quote_at needs a numeric CMC id, got {coin!r}") from exc


def quote_at(coin_id, when: "datetime | str", interval: str = "1h") -> dict:
    """Price of a coin at (or immediately before) a UTC instant.

    CMC returns a window; we take the last bucket at or before `when` and say which bucket it
    was — a call at 00:34:07 priced from the 00:34 bucket is honest, and hiding it is not.
    """
    coin_id = _coin_id(coin_id)
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    when = when.astimezone(timezone.utc)
    span = 3 * 3600 if interval in ("1h", "5m", "15m", "1m") else 3 * 86400
    start = datetime.fromtimestamp(when.timestamp() - span, tz=timezone.utc)
    if when > datetime.now(timezone.utc):
        raise CMCError(400, f"quote_at needs an instant in the past: {when.isoformat()} is in the future")
    data = get("/v1/cryptocurrency/quotes/historical", {
        "id": coin_id,
        "time_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_end": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval": interval,
        "count": 100,
    }, cache_seconds=6 * 3600)

    # For a single id, CMC returns the coin object directly ({"data": {"id":1, ..., "quotes":[...]}});
    # for several ids it nests them under the id string. Accept both, or half the calls silently
    # look unscoreable.
    payload = data.get("data") or {}
    blob = payload
    if isinstance(payload, dict):
        nested = payload.get(str(coin_id)) or payload.get(coin_id)
        if isinstance(nested, dict):
            blob = nested
    quotes = (blob.get("quotes") if isinstance(blob, dict) else None) or []
    if not quotes:
        why = ("coin is inactive on CMC (delisted / no market) — its calls cannot be priced"
               if isinstance(blob, dict) and blob.get("is_active") == 0
               else f"no historical quotes for id {coin_id} near {when.isoformat()}")
        raise CMCError(404, why)

    best = None
    for q in quotes:
        ts = q.get("timestamp")
        if not ts:
            continue
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if t <= when and (best is None or t > best[0]):
            best = (t, q)
    if best is None:
        raise CMCError(404, f"no bucket at or before {when.isoformat()}")

    bucket, q = best
    price = ((q.get("quote") or {}).get("USD") or {}).get("price")
    return dict(price=float(price), bucket=bucket.isoformat(timespec="seconds"),
                lag_seconds=int((when - bucket).total_seconds()))
