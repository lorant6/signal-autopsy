#!/usr/bin/env python3
"""Score a list of signal calls against CoinMarketCap prices, with costs.

The output is a ledger: one row per call, priced at the moment it was made and again at the
end of the horizon, signed by the direction claimed, minus fees and slippage.

    python signal_autopsy.py score demo/calls.json --horizon 24
    python signal_autopsy.py score calls.json --horizon 4 --fee-bps 11 --slippage-bps 30 --json

Nothing here needs an opinion about whether the source is good. The arithmetic decides.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cmc


def load_calls(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("calls") or []
    out = []
    for c in data:
        coin = str(c.get("coin") or "").strip()
        when = str(c.get("called_at") or "").strip()
        side = str(c.get("direction") or "").strip().lower()
        if not coin or not when or side not in ("long", "short"):
            print(f"skip malformed call: {c}", file=sys.stderr)
            continue
        try:
            ts = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            print(f"skip unparseable timestamp: {when}", file=sys.stderr)
            continue
        out.append(dict(coin=coin, direction=side, called_at=ts, source=c.get("source", "")))
    return sorted(out, key=lambda c: c["called_at"])


def score(calls: list[dict], horizon_h: float, fee_bps: float, slippage_bps: float,
          pre_h: float = 1.0) -> list[dict]:
    rows = []
    for c in calls:
        try:
            ident = cmc.resolve_coin(c["coin"])
            entry = cmc.quote_at(ident["id"], c["called_at"])
            exit_ = cmc.quote_at(ident["id"], c["called_at"] + timedelta(hours=horizon_h))
            pre = cmc.quote_at(ident["id"], c["called_at"] - timedelta(hours=pre_h))
        except cmc.CMCError as exc:
            rows.append(dict(coin=c["coin"], error=str(exc), called_at=c["called_at"].isoformat()))
            continue

        sign = 1 if c["direction"] == "long" else -1
        gross = (exit_["price"] / entry["price"] - 1) * 100 * sign
        cost = (fee_bps + 2 * slippage_bps) / 100.0        # one round trip, slippage each side
        pre_move = (entry["price"] / pre["price"] - 1) * 100 * sign

        rows.append(dict(
            coin=c["coin"], cmc_id=ident["id"], direction=c["direction"],
            called_at=c["called_at"].isoformat(timespec="seconds"),
            entry=round(entry["price"], 8), exit=round(exit_["price"], 8),
            bucket_lag_s=entry["lag_seconds"],
            gross_pct=round(gross, 2), cost_pct=round(cost, 2), net_pct=round(gross - cost, 2),
            pre_move_pct=round(pre_move, 2), source=c.get("source", "")))
    return rows


def report(rows: list[dict], horizon_h: float) -> dict:
    ok = [r for r in rows if "net_pct" in r]
    failed = [r for r in rows if "error" in r]
    if not ok:
        return dict(n=0, failed=len(failed), note="no call could be scored")

    net = [r["net_pct"] for r in ok]
    gross = [r["gross_pct"] for r in ok]
    pre = [r["pre_move_pct"] for r in ok]
    mean, sd = st.mean(net), (st.stdev(net) if len(net) > 1 else 0.0)
    t = mean / (sd / len(net) ** 0.5) if sd and len(net) > 1 else 0.0
    return dict(
        n=len(ok), failed=len(failed), horizon_h=horizon_h,
        gross_mean=round(st.mean(gross), 2), net_mean=round(mean, 2),
        median=round(st.median(net), 2), stdev=round(sd, 2), t_stat=round(t, 2),
        wins=sum(1 for x in net if x > 0), hit_rate=round(100 * sum(1 for x in net if x > 0) / len(net), 1),
        pre_move_mean=round(st.mean(pre), 2),
        pre_move_positive=sum(1 for x in pre if x > 0),
        verdict=verdict(mean, t, len(net)))


def verdict(mean: float, t: float, n: int) -> str:
    if n < 8:
        return f"too few calls to judge ({n}); the arithmetic is real, the sample is not"
    if mean < 0:
        return "loses money after costs — the source is not a signal, whatever the screenshots say"
    if abs(t) < 2:
        return (f"positive but indistinguishable from luck at n={n} (t={t:.2f}) — "
                f"keep collecting before risking anything")
    return f"positive and statistically suggestive (t={t:.2f}) — still check pre-move drift"


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", help="score a calls file")
    sc.add_argument("path")
    sc.add_argument("--horizon", type=float, default=24.0, help="hours to hold, default 24")
    sc.add_argument("--fee-bps", type=float, default=11.0, help="round-trip fee, basis points")
    sc.add_argument("--slippage-bps", type=float, default=30.0, help="per side, basis points")
    sc.add_argument("--pre-hours", type=float, default=1.0, help="window for pre-call drift")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--ledger", help="write the per-call ledger as CSV here")
    a = ap.parse_args()

    if a.cmd == "score":
        calls = load_calls(a.path)
        rows = score(calls, a.horizon, a.fee_bps, a.slippage_bps, a.pre_hours)
        summary = report(rows, a.horizon)
        if a.ledger:
            import csv
            with open(a.ledger, "w", newline="") as fh:
                cols = [k for k in rows[0] if k != "error"] if rows else []
                w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
        if a.json:
            print(json.dumps(dict(summary=summary, calls=rows), indent=2))
            return
        print(f"\n{summary['n']} calls scored over {a.horizon:g}h"
              + (f" · {summary['failed']} unscoreable" if summary.get("failed") else ""))
        if summary["n"]:
            print(f"  gross {summary['gross_mean']:+.2f}%  ·  after costs {summary['net_mean']:+.2f}%"
                  f"  ·  median {summary['median']:+.2f}%")
            print(f"  {summary['wins']}/{summary['n']} profitable  ·  sd {summary['stdev']:.2f}"
                  f"  ·  t = {summary['t_stat']:.2f}")
            print(f"  move in the window BEFORE the call: {summary['pre_move_mean']:+.2f}%"
                  f" ({summary['pre_move_positive']}/{summary['n']} had already moved)")
            print(f"\n  VERDICT: {summary['verdict']}")


if __name__ == "__main__":
    main()
