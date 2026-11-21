# Signal Autopsy

**Score any crypto signal source against CoinMarketCap data — with fees, slippage, and the
honest arithmetic that most signal channels never publish.**

Built for the *Build with CMC: API Hackathon, Round 2* (Nov 2026), track **AI Agents and
Automation**.

<img src="brand/logo.svg" width="72" align="right" alt="Signal Autopsy">

The mark is the argument: a call climbs, a marker lands on it, and what follows the marker falls
away as a dashed grey line — the part nobody publishes.

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

## Demo

A 21-second walkthrough — the live run, the settled ledger, and the MCP tools:
[`demo/signal-autopsy-demo.mp4`](demo/signal-autopsy-demo.mp4)
(rendered from `demo/demo_video.html`, no screen-recording tooling required)

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

Tools: `resolve_coin`, `quote_at`, `score_calls`, `extract_calls`. Verified against a live key: the
handshake and `tools/list` return without a network error, and each tool returns the same numbers
as the CLI.

## The reading step (where the model is)

Pricing and scoring are deterministic arithmetic — a judge can re-run them and get the same
ledger. The one step that is not computable is reading the channel: a post says
`#IRYS Bullish` and something has to decide that this is a long on IRYS at `00:34:05Z`, that the
next post is commentary, and that `Close #BTC short` is an exit rather than an entry.

`extract_calls.py` does that with a schema-constrained model and then treats its answer like
something typed by a stranger:

```bash
python3 extract_calls.py demo/posts.txt --source t.me/binance_futures -o calls.json
EXTRACT_LIVE=1 python3 test_extract.py    # scores extraction against demo/posts.labels.json
```

* direction must resolve to `long`/`short` (`bullish`, `bear`, `buy`, `shorting` are mapped;
  anything else is rejected, never guessed)
* the timestamp must parse as ISO-8601 **with** a timezone
* tickers are normalised (`#IRYSUSDT` → `IRYS`) and optionally checked against CMC's map
* duplicates collapse, and every accepted call keeps the post line it came from
* rejections are returned with reasons — the model cannot make a call appear by asserting it

On the 25-post scrape in `demo/posts.txt`, against hand labels in `demo/posts.labels.json`:
**17 of 18 calls recovered, 0 invented**, in one turn (~22k tokens). Missed: a post that says
*"Shorting BTC at top … Today bought BTC at 55555"* — it contradicts itself, and a tool that
guesses there would be lying.

The model is the only non-deterministic component, so its output is cached by content hash
(input + source + model + schema): a demo, a re-score or a test costs no quota, and the same
posts reproduce the same ledger. No login is needed for the deterministic path — only
`extract_calls` requires the CLI.

## Install

```bash
pip install -r requirements.txt
export CMC_API_KEY=...        # never in a file that is committed
python signal_autopsy.py score demo/calls.json --horizon 24
```

The scoring path needs nothing but Python 3.10+ and the key. The reading step is optional and
needs the Antigravity CLI (`agy`, installed at `~/.local/bin/agy`, logged in once):

```bash
python3 extract_calls.py demo/posts.txt --source t.me/binance_futures -o demo/calls.json
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
