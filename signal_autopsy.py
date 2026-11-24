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
import traceback
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
    now = datetime.now(timezone.utc)
    for c in calls:
        # A call whose holding period has not finished cannot be scored yet. Calling that an
        # error would understate the sample; calling it a win would be a lie. It is pending.
        if c["called_at"] + timedelta(hours=horizon_h) > now:
            rows.append(dict(coin=c["coin"], called_at=c["called_at"].isoformat(timespec="seconds"),
                             direction=c["direction"], pending=True,
                             note=f"horizon not elapsed (exit "
                                  f"{(c['called_at'] + timedelta(hours=horizon_h)).isoformat(timespec='seconds')})",
                             source=c.get("source", "")))
            continue
        try:
            ident = cmc.resolve_coin(c["coin"])
            entry = cmc.quote_at(ident["id"], c["called_at"])
            exit_ = cmc.quote_at(ident["id"], c["called_at"] + timedelta(hours=horizon_h))
            pre = cmc.quote_at(ident["id"], c["called_at"] - timedelta(hours=pre_h))
        except cmc.CMCError as exc:
            # One unpriceable call must not end the run. The row is written as failed, every
            # other call keeps scoring, and the next cycle retries it because the ledger is
            # rebuilt from calls.jsonl each time. status 0 is transport-level (DNS, timeout,
            # malformed body), not a statement about the coin.
            rows.append(dict(coin=c["coin"], direction=c["direction"],
                             called_at=c["called_at"].isoformat(timespec="seconds"),
                             error=str(exc),
                             note=("transport failure — retryable, not a verdict on the call"
                                   if exc.status == 0 else "")))
            continue
        except Exception as exc:                                      # noqa: BLE001
            # A defect in this file must be visible. Swallowing it into a row makes every call
            # "unexpected per-call failure" and the ledger quietly empty, which is worse than a
            # crash; the traceback goes to stderr and the run still finishes.
            traceback.print_exc(file=sys.stderr)
            rows.append(dict(coin=c["coin"], direction=c["direction"],
                             called_at=c["called_at"].isoformat(timespec="seconds"),
                             error=f"{type(exc).__name__}: {exc}",
                             note="unexpected per-call failure — traceback on stderr"))
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
    pending = [r for r in rows if r.get("pending")]
    if not ok:
        return dict(n=0, failed=len(failed), pending=len(pending), note="no call could be scored")

    net = [r["net_pct"] for r in ok]
    gross = [r["gross_pct"] for r in ok]
    pre = [r["pre_move_pct"] for r in ok]
    mean, sd = st.mean(net), (st.stdev(net) if len(net) > 1 else 0.0)
    t = mean / (sd / len(net) ** 0.5) if sd and len(net) > 1 else 0.0
    return dict(
        n=len(ok), failed=len(failed), pending=len(pending), horizon_h=horizon_h,
        gross_mean=round(st.mean(gross), 2), net_mean=round(mean, 2),
        median=round(st.median(net), 2), stdev=round(sd, 2), t_stat=round(t, 2),
        wins=sum(1 for x in net if x > 0), hit_rate=round(100 * sum(1 for x in net if x > 0) / len(net), 1),
        pre_move_mean=round(st.mean(pre), 2),
        pre_move_positive=sum(1 for x in pre if x > 0),
        verdict=verdict(mean, t, len(net)))


def verdict(mean: float, t: float, n: int) -> str:
    """Both directions face the same bar.

    The first version let a NEGATIVE mean assert "the source is not a signal" at n=8 with no
    t-test, while demanding t>2 of a positive one. A tool that audits other people's claims cannot
    keep a thumb on one side of the scale: an underpowered negative is as meaningless as an
    underpowered positive, and saying otherwise is the exact behaviour this project exists to catch.
    """
    if n < 30:
        return (f"underpowered — n={n}, mean {mean:+.2f}%, t={t:.2f}; no direction claimed yet. "
                f"The arithmetic is real, the sample is not")
    if abs(t) < 2:
        return (f"no effect distinguishable from noise at n={n} (mean {mean:+.2f}%, t={t:.2f}) — "
                f"the costs are measurable, the edge is not")
    if mean < 0:
        return (f"loses money after costs at n={n} (mean {mean:+.2f}%, t={t:.2f}) — the forward "
                f"ledger contradicts the screenshots")
    return (f"positive and statistically suggestive (mean {mean:+.2f}%, t={t:.2f}) at n={n} — "
            f"still check pre-move drift before crediting the source")


def slippage_sensitivity(rows: list[dict], slippage_bps: float, mult: float) -> dict:
    """Mean net if slippage per side were `mult` times the flat assumption.

    The default 30bp is a venue-fee number, not a micro-cap number: on a thin listing the
    price you could have taken sits well below the aggregate CMC print. Scaling it is the
    cheapest way to see whether a positive mean survives a believable cost.
    """
    net = [r["net_pct"] for r in rows if "net_pct" in r]
    if not net:
        return {}
    extra = 2 * slippage_bps * (mult - 1) / 100.0        # slippage is charged on both sides
    adj = [x - extra for x in net]
    mean = st.mean(adj)
    sd = st.stdev(adj) if len(adj) > 1 else 0.0
    t = mean / (sd / len(adj) ** 0.5) if sd else 0.0
    return dict(slippage_x=mult, extra_cost_pct=round(extra, 2), net_mean=round(mean, 2),
                median=round(st.median(adj), 2), t_stat=round(t, 2),
                verdict=verdict(mean, t, len(adj)))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", help="score a calls file")
    sc.add_argument("path")
    sc.add_argument("--horizon", type=float, default=24.0, help="hours to hold, default 24")
    sc.add_argument("--fee-bps", type=float, default=11.0, help="round-trip fee, basis points")
    sc.add_argument("--slippage-bps", type=float, default=30.0, help="per side, basis points")
    sc.add_argument("--pre-hours", type=float, default=1.0, help="window for pre-call drift")
    sc.add_argument("--slippage-x", type=float, default=1.0,
                    help="scale the per-side slippage assumption (2 = double it), for sensitivity")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--ledger", help="write the per-call ledger as CSV here")
    a = ap.parse_args()

    if a.cmd == "score":
        # Fail on a missing key up front, not lazily: with a warm quote cache the run would
        # otherwise succeed with no key at all and mask a broken setup.
        cmc._key()
        calls = load_calls(a.path)
        rows = score(calls, a.horizon, a.fee_bps, a.slippage_bps, a.pre_hours)
        summary = report(rows, a.horizon)
        if a.ledger:
            import csv
            with open(a.ledger, "w", newline="") as fh:
                cols = ["coin", "cmc_id", "direction", "called_at", "entry", "exit", "bucket_lag_s",
                        "gross_pct", "cost_pct", "net_pct", "pre_move_pct", "source",
                        "pending", "note", "error"]
                w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
        if a.json:
            out = dict(summary=summary, calls=rows)
            if a.slippage_x != 1.0:
                out["slippage_sensitivity"] = slippage_sensitivity(rows, a.slippage_bps, a.slippage_x)
            print(json.dumps(out, indent=2))
            return
        print(f"\n{summary['n']} calls scored over {a.horizon:g}h"
              + (f" · {summary.get('pending', 0)} still open (horizon not elapsed)"
                 if summary.get("pending") else "")
              + (f" · {summary['failed']} unscoreable" if summary.get("failed") else ""))
        if summary["n"]:
            print(f"  gross {summary['gross_mean']:+.2f}%  ·  after costs {summary['net_mean']:+.2f}%"
                  f"  ·  median {summary['median']:+.2f}%")
            print(f"  {summary['wins']}/{summary['n']} profitable  ·  sd {summary['stdev']:.2f}"
                  f"  ·  t = {summary['t_stat']:.2f}")
            print(f"  move in the window BEFORE the call: {summary['pre_move_mean']:+.2f}%"
                  f" ({summary['pre_move_positive']}/{summary['n']} had already moved)")
            if a.slippage_x != 1.0:
                sens = slippage_sensitivity(rows, a.slippage_bps, a.slippage_x)
                print(f"  at {a.slippage_x:g}x slippage (cost {sens['extra_cost_pct']:+.2f}pp more):"
                      f" mean {sens['net_mean']:+.2f}%  ·  median {sens['median']:+.2f}%"
                      f"  ·  t {sens['t_stat']:.2f}")
            print(f"\n  VERDICT: {summary['verdict']}")


if __name__ == "__main__":
    main()
