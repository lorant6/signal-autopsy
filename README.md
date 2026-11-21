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

Run against a 25-post scrape of a live Telegram channel, parsed by `extract_calls.py` and priced
by CMC on 21 Nov 2026:

```
17 calls extracted from 25 posts · 11 settled at 24h · 3 still open · 3 unscoreable

mean +0.95% net   median +0.20%   sd 11.24   t = 0.28
7/11 profitable after fees and slippage
move in the hour BEFORE the call, in the direction claimed: mean +4.24% (7 of 11)
```

Those 17 calls are the demo extraction. The forward book below adds one live call (IDOL, posted
21 Nov 18:50), which is why the book reads 18 calls and 4 open where this snapshot reads 17 and
3 — `forward/summary.txt` is authoritative for the book, this block for the demo set.

Five findings that matter:

1. **The mean is one coin — remove IRYS and the book is negative.** IRYS pays +23.9% net on a
   single call and +8.39% across its four; the other seven calls average −3.31% (t = −1.16).
   What looks like a small positive edge is one micro-cap on one day, and it does not survive
   being removed. Judge the median, and then judge the book again without its best ticker.
2. **t = 0.28 is not an edge.** At n=11 the tool refuses to call it one.
3. **The calls arrive after the move.** +4.24% of the move has already happened in the hour
   before the call is posted — the difference between a signal and a broadcast.
4. **Three calls are unpriceable at all.** CROSS is delisted on CMC, so the tool says so instead
   of quietly dropping it from the sample and flattering the average.
5. **Parsing is part of the measurement.** A hand-parse of this same scrape counted ten
   commentary posts as calls and scored one `Close #BTC short` as a long entry; on that set the
   channel appeared to earn +6.62% (t = 1.35). Read carefully, it earns +0.95%. A tool that only
   audits the channel and not its own reading of the channel is doing half the job.

The tool reports the calls it cannot score and the calls whose horizon has not finished as
separate lines. Deleting them would be the easiest way to make the numbers look good, which is
exactly why the output refuses to.

### The same eleven calls, per coin

| coin | calls | mean net | median net | t |
|---|---|---|---|---|
| BTC | 3 | −0.58% | +0.20% | −0.60 |
| IRYS | 4 | **+8.39%** | +9.17% | +1.21 |
| ICNT | 2 | −9.92% | −9.92% | −0.98 |
| EVAA | 1 | +1.30% | +1.30% | — |
| ETH | 1 | −2.87% | −2.87% | — |

**Effective n.** Those eleven rows are five coins. Four of them are IRYS longs posted at 00:34,
00:53, 01:20 and 04:07 on 19 November, and their 24-hour windows overlap almost completely: one
bet on one micro-cap, booked four times. The t-statistic above assumes eleven independent
observations and has, at most, five — so it overstates even the little it claims. Collapsed to
one observation per coin the mean is **−0.73% (t = −0.25, n = 5)**. The tool still prints t
because a judge will ask for it; the honest reading of "positive but indistinguishable from
luck" is weaker than that.

**The cost assumption is flat on purpose, and it flatters the trades that matter.** 11bp fee
plus 30bp slippage per side is 0.71% round trip, applied identically to BTC at $62k and to IRYS
at $0.019. On a thin listing CMC's price is an aggregate across venues, and the price you could
actually have taken sits below it — so the four IRYS rows, the rows that carry the mean, are
exactly where the assumption is weakest. `--slippage-x 2` re-runs the same arithmetic at double
slippage: the mean falls from +0.95% to **+0.35%** and the median to **−0.40%**; at 3x it is
−0.25%. The flat number is reported because it is the one a rounded-off screenshot would show,
and the flag exists so nobody has to trust it.

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

`--slippage-x 2` repeats the same arithmetic at double slippage and prints the mean, median and
t beside the flat-cost result — a sensitivity check on the cost assumption, not a different
model.

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
**17 of 18 calls recovered, 0 invented**, in one turn (12.5k tokens; re-run from a clean cache
on 21 Nov). Missed: a post that says
*"Shorting BTC at top … Today bought BTC at 55555"* — it contradicts itself, and a tool that
guesses there would be lying.

The model is the only non-deterministic component, so its output is cached by content hash
(input + source + model + schema): a demo, a re-score or a test costs no quota, and the same
posts reproduce the same ledger. No login is needed for the deterministic path — only
`extract_calls` requires the CLI.

## The forward book

The demo is one snapshot. `collect_forward.py` keeps the measurement going: each day it scrapes
the channel, extracts only posts it has never seen, settles everything older than 24 hours
against CMC, and appends to `forward/` — `posts.jsonl`, `calls.jsonl`, `ledger.csv`,
`summary.txt`.

```bash
python3 collect_forward.py --dry      # fetch and report, write nothing, no model call
python3 collect_forward.py            # one daily cycle; silent unless something changed
```

It is seeded on 21 Nov 2026 with the 17 calls above, so the book starts with a real n instead of
an empty ledger, and it only grows from there. Each seeded row carries the post line it was read
from, so a reader can check a call against the channel rather than against our say-so; a call
whose post cannot be found in either scrape is dropped and counted instead of being booked with
blank evidence. Two rules keep it from flattering itself: a post
is never read twice (post ids are recorded), and a batch whose extraction fails is retried next
cycle rather than silently dropped. The daily message fires only when a call was added or a
position settled — a daily "nothing happened" trains you to ignore the day that matters.

As of the 21 Nov snapshot the book reads **11 scored · 4 open · 3 unpriceable** (18 calls);
`forward/summary.txt` is the authoritative figure and is rewritten each cycle, so read it rather
than this paragraph.

Evidence is attached per row and the tool says when it had to guess. A message timestamp is
only whole seconds, so two different posts can share one; the line that names the ticker wins,
with a marked ticker (`#IRYS`, `$IRYS`, `IRYSUSDT` — 17 of the book's 18 rows are tagged this
way) outranking an unmarked one, and a row whose line had to be inferred is marked
`evidence_ambiguous` in `forward/calls.jsonl` rather than passed off as exact provenance. One
limit is worth stating: a ticker that is also an ordinary English word is indistinguishable from
prose when it appears unmarked (`MAX gains today`), and no flag can rescue that case — the label
tells you the evidence was chosen by rule, not that the rule was right.

## Install

```bash
pip install -r requirements.txt
export CMC_API_KEY=...        # never in a file that is committed
python signal_autopsy.py score demo/calls.json --horizon 24
```

The tests run offline — no key, no network, no model — and CI runs exactly this on every push:

```bash
python3 test_math.py && python3 test_forward.py && python3 test_extract.py
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
