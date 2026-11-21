# Submission kit — Build with CMC: API Hackathon, Round 2

Everything below is copy-paste ready. Two fields are blocked on your accounts: the **repo URL**
(needs GitHub access) and the **X post** (needs your account). Everything else is done.

Deadline: **9 Dec 2026**. Judging to 25 Dec.

---

## BUIDL fields

**Project name**

```
Signal Autopsy
```

**Tagline** (one line, ~140 chars)

```
Score any crypto signal source against CoinMarketCap data — with fees, slippage, and the ledger nobody publishes.
```

**Track**

```
AI Agents & Automation
```

**Description** (paste as markdown)

```markdown
## The problem

A signal channel posts `#IRYS Bullish` over a chart. No entry price, no stop, no target, and no
record of results. That is not a signal, it is an unfalsifiable claim: with no entry, no horizon
and no published settlement, nothing it says can ever be shown to be wrong.

## What it does

Signal Autopsy takes a list of calls — coin, direction, timestamp — and makes them falsifiable.

1. Resolves each ticker to a CMC ID (`/v1/cryptocurrency/map`)
2. Prices the coin at the moment the call was posted (`/v1/cryptocurrency/quotes/historical`)
3. Prices it again at `timestamp + horizon`
4. Signs the move by the claimed direction, charges fees and slippage, and writes a per-call ledger
5. Reports mean, median, hit rate, t-statistic — and the pre-call drift

Pre-call drift is the field most tools omit. It separates *"predicted"* from *"described"*: how
much of the move had already happened before the call was posted.

## First live run — 24 real calls from a live channel

```
10 scored · 11 still open (horizon not elapsed) · 3 unscoreable
mean +6.62% net · median +2.56% · sd 15.52 · t = 1.35 · 8/10 profitable
pre-call drift: +5.51% (7 of 10 had already moved)
VERDICT: positive but indistinguishable from luck at n=10
```

Four things the output refuses to hide:

- **The mean is carried by micro-caps.** One IRYS call pays +23.9% net; the median call +2.56%.
- **t = 1.35 is not an edge.** At n=10 the tool says so instead of declaring a winner.
- **The calls arrive after the move.** +5.51% had already happened in the hour before posting.
- **Three calls cannot be priced at all.** CROSS is delisted on CMC — reported, not dropped from
  the sample to flatter the average.

## MCP

The engine is also a Model Context Protocol server, so an agent can score a source mid-conversation:

```json
{"mcpServers": {"signal-autopsy": {"command": "python", "args": ["mcp_server.py"]}}}
```

Tools: `resolve_coin`, `quote_at`, `score_calls`.

## Honesty as a feature

A scoring tool is trivially easy to make look good: drop the losing calls, drop the unpriceable
ones, shorten the horizon, stop charging fees. This one reports every exclusion as its own line —
scored, pending, unscoreable — because a verdict you cannot audit is the thing it exists to replace.

## Built with

Python standard library only (no runtime dependencies), CoinMarketCap Pro API, MCP. 7 arithmetic
and behaviour tests, including the case that a zero-mean result is reported as "indistinguishable
from luck" rather than "loses money".
```

**API usage / credits line** (if the form asks which endpoints)

```
/v1/cryptocurrency/map · /v1/cryptocurrency/quotes/historical (interval=1h)
```

**Links**

```
Repo:      <blocked — needs GitHub push, see below>
Demo run:  demo/RUN.txt in the repo (verbatim terminal output, real key, real calls)
```

---

## The X post (246 chars — fits)

```
Signal channels post calls nobody can check. I built Signal Autopsy on @CoinMarketCap data: it prices every call at the moment it was posted, charges fees and slippage, and publishes the ledger — including the calls it cannot price.
#BuildwithCMC
```

Post it, then paste the post URL into the submission form.

---

## What is blocked on you

**1. Publish the repo (needs GitHub access — I have no credentials on this box).**

Fastest path, no token pasted in chat: add this machine's SSH key to your GitHub account.

    https://github.com/settings/keys  →  New SSH key  →  paste:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFdnWDqOgnARqQ4kXT1UcjvdFWq6L+DIrL+P5KcsNnlE max@max-hp-elitedesk-705-g1-dm
```

fingerprint `SHA256:QPFXLyAWEGY5sN0ZLT56lbcg4q73VLjd8UPtepDzr0c`

Then tell me the repo name and I push it, public, in one step. Alternative: fine-grained PAT
with `repo` scope into `~/.hermes/github.env` as `GITHUB_TOKEN=...` — not in chat.

**2. X post** — only your account can do it. Text above.

**3. Paste the two URLs into DoraHacks** — repo + post.

---

## Pre-flight before you submit

- [x] Live key, live CMC data, real calls (`demo/RUN.txt`, `demo/ledger_live.csv`)
- [x] 7/7 tests pass
- [x] MCP handshake + `score_calls` verified against the real API
- [x] No key in the repo or in git history (grep-verified)
- [x] `README.md` matches measured numbers, not estimates
- [ ] Repo public on GitHub — blocked on your key
- [ ] X post with `#BuildwithCMC` — blocked on your account
- [ ] BUIDL submitted on DoraHacks — your account
