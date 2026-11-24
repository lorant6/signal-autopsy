# API evidence — a real call, its real response

Every number in this project comes from the following call. This page exists because a screenshot of
a chart is not evidence and a description of an API is not a submission: this is the code, the exact
request, and the response the API actually returned.

Captured **2026-11-24 18:33 UTC** against `https://pro-api.coinmarketcap.com`.

## The code that makes the call

`cmc.py` — used by both the demo and the forward ledger:

```python
BASE = "https://pro-api.coinmarketcap.com"

def get(path: str, params: dict | None = None, cache_seconds: int = 0) -> dict:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "X-CMC_PRO_API_KEY": _key(),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# the instant a call was posted, priced at that instant:
data = get("/v1/cryptocurrency/quotes/historical", {
    "id": 1,                                    # resolved once via /v1/cryptocurrency/map
    "time_start": "2026-11-24T15:00:00Z",
    "time_end":   "2026-11-24T18:00:00Z",
    "interval": "1h",
    "count": 100,
})
```

## The request

```
GET /v1/cryptocurrency/quotes/historical
    ?id=1
    &time_start=2026-11-24T15:00:00Z
    &time_end=2026-11-24T18:00:00Z
    &interval=1h
    &count=100
X-CMC_PRO_API_KEY: <from ~/.hermes/cmc.env, never committed>
```

## The response (real, truncated)

```json
{
  "status": { "error_code": 0, "error_message": null, "credit_count": 1,
              "elapsed": 35, "timestamp": "2026-11-24T18:33:54.162Z" },
  "data": { "id": 1, "name": "Bitcoin", "symbol": "BTC", "quotes": [
    { "timestamp": "2026-11-24T16:00:00.000Z",
      "quote": { "USD": { "price": 59654.20151064547, "percent_change_1h": -1.22247465313,
                          "volume_24h": 35653715173.2, "market_cap": 1200051640949.3 } } },
    { "timestamp": "2026-11-24T17:00:00.000Z", "quote": { "USD": { "price": 59765.1097 } } },
    { "timestamp": "2026-11-24T18:00:00.000Z",
      "quote": { "USD": { "price": 60042.31472278671, "percent_change_1h": 1.16279534636 } } }
  ] }
}
```

Three hourly quotes, one credit consumed, `error_code: 0`.

## The endpoints used, named explicitly

| endpoint | what it is used for |
|---|---|
| `/v1/cryptocurrency/map` | resolve a ticker (`BTC`) to a CMC id once, so every later call is unambiguous |
| `/v1/cryptocurrency/quotes/historical` | price a call at **the minute it was posted**, not at "now" |

That second point is the whole project. A signal channel can only be scored honestly if the entry is
the price at the moment of the post; pricing it with a later quote is how a losing call becomes a
winner in a screenshot.

## What the API made possible

- **Settlement without hindsight.** `quotes/historical` gives an hourly series for any past window, so
  entry and exit are both taken from CMC at fixed timestamps. The ledger cannot drift.
- **One id, many symbols.** `/cryptocurrency/map` turns a ticker into a stable id, so a token
  smart-contract migration or a symbol collision does not silently price the wrong asset.
- **A cross-check the venue does not offer.** Matching CMC's print against the exchange feed is what
  lets us report `pre_move` — how much of the move had already happened before the call was posted.

## Where it got in the way

**1. A symbol-based historical request fails silently.** The obvious call is by ticker:

```
GET /v1/cryptocurrency/quotes/historical?symbol=BTC&interval=1h&...
```

This returns **HTTP 200**, `error_code: 0`, and:

```json
{"data": {"id": 1, "name": "Bitcoin", "symbol": "BTC", "is_active": 1, "quotes": []},
 "status": {"error_code": 0, "error_message": null, "credit_count": 0}}
```

No error, no warning, no quotes. Found while capturing the evidence above, not from a historical
incident: this collector has always resolved ids through `/cryptocurrency/map` first, which is why
its own ledger was never affected. The hazard is real anyway — an implementation that passed the
ticker straight through would get a success response with an empty array, and recording that as a
price would put a zero into the ledger without a single error in the log. A `400` saying "use id for
this endpoint" would make the correct call the obvious one.

**2. Cost is known only after spending it.** `/v1/key/info` is genuinely useful and the collector
polls it — it returns `current_minute.requests_left`, `current_day.credits_used` and
`current_month.credits_left` (563 used, 449,437 left on this account at capture time). What it
cannot answer is the question a batch job asks *before* a loop: "what will these 200 calls cost?"
Cost per endpoint lives in the documentation, while `credit_count` in the response tells you what
you already spent. A per-request cost estimate, or a batch of calls that reports its total cost
before executing, would turn quota planning from a guess into arithmetic.

## Reproducing this

```bash
export CMC_API_KEY=<your key>       # never committed; ~/.hermes/cmc.env, mode 600
python cmc.py --probe BTC           # one live call, prints the status block
python collect_forward.py --dry     # fetch and report without writing the ledger
```
