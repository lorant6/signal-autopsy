"""Arithmetic tests for Signal Autopsy. No network, no API key, no test framework.

    python test_math.py

A scoring tool whose own maths is unchecked is just a nicer-looking opinion, so the numbers a
judge would question are asserted here rather than trusted.
"""
from __future__ import annotations

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


def test_verdict_is_not_optimistic():
    rows = [dict(net_pct=1.0, gross_pct=1.0, pre_move_pct=0.0)] * 5
    assert "too few" in sa.report(rows, 24)["verdict"]
    losing = [dict(net_pct=-2.0, gross_pct=-1.0, pre_move_pct=0.0)] * 20
    assert "loses money" in sa.report(losing, 24)["verdict"]
    noisy = [dict(net_pct=1.0 if i % 2 else -1.0, gross_pct=1.0, pre_move_pct=0.0) for i in range(20)]
    assert "indistinguishable from luck" in sa.report(noisy, 24)["verdict"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\nall maths assertions hold")
