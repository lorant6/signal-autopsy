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

Run against 24 real calls scraped from a live Telegram channel, priced by CMC on 21 Nov 2026:

```
24 calls in the file · 10 settled at 24h · 11 still open (horizon not elapsed) · 3 unscoreable

mean +6.62% net   median +2.56%   sd 15.52   t = 1.35
8/10 profitable after fees and slippage
move in the hour BEFORE the call, in the direction claimed: mean +5.51% (7 of 10)
```

Four findings that matter:

1. **The mean is carried by micro-caps.** IRYS alone pays +23.9% net on one call; the median
   call earns +2.56%. Judge a channel by the median or you are judging its best trade.
2. **t = 1.35 is not an edge.** At n=10 the tool refuses to call it one.
3. **The calls arrive after the move.** +5.51% of the move has already happened in the hour
   before the call is posted — the difference between a signal and a broadcast.
4. **Three calls are unpriceable at all.** CROSS is delisted on CMC, so the tool says so instead
   of quietly dropping it from the sample and flattering the average.

The tool reports the calls it cannot score and the calls whose horizon has not finished as
separate lines. Deleting them would be the easiest way to make the numbers look good, which is
exactly why the output refuses to.

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

Tools: `resolve_coin`, `quote_at`, `score_calls`. Verified against a live key: the handshake and
`tools/list` return without a network error, and each tool returns the same numbers as the CLI.

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

- `quotes/historical` at `interval=1h` works on a free key, but a single-id request returns the
  coin object at `data` while a multi-id request nests it under the id string. Assuming one shape
  makes every call look unscoreable with a 404 — a real bug this project hit and now handles.
- A request whose window ends in the future is rejected by CMC with a 400 rather than an empty
  result. The client now refuses future instants explicitly, and the scorer marks such calls
  *pending* instead of failed, so an unfinished holding period never colours the verdict.
- Historical quotes are bucketed per minute/day; a call at `00:34:07` resolves to the nearest
  bucket, which is honest but worth stating in the output rather than hiding.
- Micro-cap prices on CMC are aggregates across venues. Where a coin trades on one thin
  exchange, the "market" price is not the price you could have taken. The tool reports this
  rather than pretending otherwise.

## Licence

MIT.
