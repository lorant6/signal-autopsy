# Signal Autopsy

**Score any crypto signal source against CoinMarketCap data — with fees, slippage, and the
honest arithmetic that most signal channels never publish.**

Built for the *Build with CMC: API Hackathon, Round 2* (Nov 2026), track **AI Agents and
Automation**.

---

## The problem

A signal channel posts `#IRYS Bullish` over a chart. No entry price. No stop. No target. No
record of results.

That is not a signal, it is an **unfalsifiable claim**: with no entry, no time horizon and no
published settlement, nothing it says can ever be shown to be wrong. This tool makes such
claims falsifiable.

Given a list of calls — coin, direction, timestamp — Signal Autopsy prices each one from
CoinMarketCap data at the moment it was made, holds it for a chosen horizon, charges fees and
slippage, and reports what it actually paid. The output is a forward ledger and a verdict, not
an opinion.

## Why this is not a toy

The same engine, run by hand on a live Telegram channel, produced these numbers:

```
15 calls · 12 settled at 24h
mean +5.54%   median +1.00%   sd 12.0   t = 1.60
move in the hour BEFORE the call, in the direction claimed: mean +4.86% (11 of 15)
```

Two findings that matter. The mean is carried by two micro-caps while the median call earns
about 1% — and after 0.11% fees and realistic slippage, roughly nothing. And the calls arrive
**after** the move has started, which is the difference between a signal and a broadcast.

## What it does

```
signal_autopsy score calls.json --horizon 24 --fee-bps 11 --slippage-bps 30
```

1. Resolves each ticker to a CoinMarketCap ID (`/v1/cryptocurrency/map`)
2. Prices the coin at the call timestamp (`/v1/cryptocurrency/quotes/historical`)
3. Prices it again at `timestamp + horizon`
4. Signs the move by the claimed direction, subtracts costs, and writes a ledger
5. Reports mean, median, hit rate, t-statistic, and the pre-call drift

Pre-call drift is the field most tools omit: it separates *"predicted"* from *"described"*.

## MCP server

The same engine is exposed as a Model Context Protocol server, so an LLM can score a source
mid-conversation:

```json
{"mcpServers": {"signal-autopsy": {"command": "python", "args": ["mcp_server.py"]}}}
```

Tools: `resolve_coin`, `quote_at`, `score_calls`, `score_source_file`.

## Install

```bash
pip install -r requirements.txt
export CMC_API_KEY=...        # never in a file that is committed
python signal_autopsy.py score demo/calls.json --horizon 24
```

## Calls file format

```json
[
  {"coin": "IRYS", "direction": "long",  "called_at": "2026-11-19T00:34:00Z", "source": "t.me/binance_futures"},
  {"coin": "CROSS", "direction": "long", "called_at": "2026-11-19T00:59:00Z", "source": "t.me/binance_futures"}
]
```

`direction` is `long` or `short`. `called_at` is ISO-8601 UTC — the moment the claim appeared,
not when it became convenient to measure.

## What the API made possible

- `quotes/historical` is what turns a timestamp into a price. Without it a claim can only be
  judged from "now", which is exactly the ambiguity signal channels rely on.
- `map` resolves the ticker ambiguity that makes naive scoring wrong — `CROSS` and `IRYS` are
  young listings, and symbol collisions are common.
- Startup-tier rate limits are enough to score a few hundred calls per run.

## Where it got in the way

- `quotes/historical` is not on the Basic tier, so a public demo needs an event-window key
  rather than a free one.
- Historical quotes are bucketed per minute/day; a call at `00:34:07` resolves to the nearest
  bucket, which is honest but worth stating in the output rather than hiding.
- Micro-cap prices on CMC are aggregates across venues. Where a coin trades on one thin
  exchange, the "market" price is not the price you could have taken. The tool reports this
  rather than pretending otherwise.

## Licence

MIT.
