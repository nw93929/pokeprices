# card-prices

A serverless pipeline + API that tracks Pokémon TCG card market prices
over time and exposes them through a Chalice / API Gateway service. Built
for DS5220 Data Project 3 (`INSTRUCTIONS.md`).

**API URL:** `https://ybq9genx9i.execute-api.us-east-1.amazonaws.com/api`

---

## What it tracks and why

Pokémon TCG card prices are a constantly-moving market — singles can swing
double digits in a week off a tournament result, a reprint announcement, or
a YouTube video. One snapshot is uninteresting; a *series* of snapshots
reveals trends, spikes, and value-over-time, which is exactly the shape of
data the assignment asks for.

Prices come from the [Pokémon TCG API](https://pokemontcg.io), which
republishes TCGplayer market prices on every card payload under
`card.tcgplayer.prices.<variant>.market`. This is the same TCGplayer pricing
data behind every major price-tracker, available without going through
TCGplayer's commercial API partnership.

The watchlist is **lazy**: a card starts being tracked the first time
*anyone* calls `/price` for it, or when it appears in the global top-10
served by `/top`. So the dataset grows organically as the API gets used.

## Architecture

```
EventBridge (rate(1 hour))
        │
        ▼
   Lambda: ingest    ── reads ──▶  CardWatchlist (DynamoDB)
        │                                   ▲
        │ for each watched card,            │
        │ fetch latest price                │ /price lazy-adds
        ▼                                   │
   pokemontcg.io                            │
        │                                   │
        ▼                                   │
   CardPrices (DynamoDB)  ◀─── reads ───  Lambda: api ◀── API Gateway ◀── Discord bot / curl
                                                │
                                                ▼ /plot writes
                                           S3 (public-read on plots/*)
```

Both Lambdas live in one Chalice app (`app.py`). The scheduled ingest is a
`@app.schedule(Rate(1, unit=Rate.HOURS))` handler in the same code base.

## Sampling cadence and storage schema

- **Cadence:** the ingest Lambda fires every **1 hour** (EventBridge
  scheduled rule). At ~50 watched cards × 24 fires/day that's ~1,200
  upstream calls/day — well under the pokemontcg.io free-tier limit (20k).

- **DynamoDB tables** (both `PAY_PER_REQUEST`):

  | Table | PK | SK | Other attributes |
  |-------|----|----|------------------|
  | `CardPrices` | `card_id` (S) | `timestamp` (N, Unix epoch) | `price` (N), `variant` (S), `name` (S) |
  | `CardWatchlist` | `card_id` (S) | — | `name` (S), `added_ts` (N) |

  Time-series queries (`/plot`, `/change`) are bounded `Query` operations
  on `CardPrices` using the timestamp sort key — no scans on the hot path.

- **S3 plot bucket:** `p3-pokemon-cardprices`, with a public-read bucket
  policy on the `plots/*` prefix only. Plots are rendered on demand by the
  `/plot` Lambda using matplotlib (provided by a custom Lambda layer
  published from S3 — see `scripts/build_matplotlib_layer.sh`).

## API resources

The zone apex (`GET /`) returns:

```json
{
  "about": "Tracks Pokemon TCG card market prices over time using the Pokemon TCG API (TCGplayer pricing). Cards become tracked the first time anyone queries /price for them.",
  "resources": ["price", "top", "plot", "change"]
}
```

All other resources return a single `{ "response": ... }` per the project
contract.

| Resource | Path | Args | Returns |
|---|---|---|---|
| **price** | `GET /price` | `card` (id or name) | Latest TCGplayer market price for one card. Lazy-adds to the watchlist. |
| **top** | `GET /top` | — | Top 10 most expensive Pokémon cards in the pokemontcg.io catalog. Queries 7 price variants in parallel and merges client-side. |
| **plot** | `GET /plot` | `card`, `window` (default `30d`) | URL of a price-history PNG in S3. Renders on demand. |
| **change** | `GET /change` | `card`, `window` (default `30d`) | Total %% change over the window + average $/month. |

`window` accepts `7d`, `30d`, `90d`, `1m`, `3m`, `1y`, etc.

The handlers accept three argument styles, so they work from both curl and
the Discord bot without compromise:

1. **Path parameters:** `/price/charizard`, `/plot/mew/7d`,
   `/change/charizard/1m` — what the Discord bot ends up calling
   (slashes between args survive URL encoding).
2. **Standard query strings:** `/price?card=charizard&window=7d` — for
   curl and browser testing.
3. **Bare-key query strings:** `/price?charizard&7d` — also accepted, parsed
   order-independently (anything matching `\d+[dmy]` is the window).

### Direct examples (curl)

```bash
URL="https://ybq9genx9i.execute-api.us-east-1.amazonaws.com/api"

curl -s "$URL/"
curl -s "$URL/price?card=charizard"
curl -s "$URL/top"
curl -s "$URL/plot?card=mew&window=7d"
curl -s "$URL/change?card=mew&window=1m"
```

## Using it from Discord

The course's `cloudbot` exposes the API through `/project`. **Important:**
the bot URL-encodes spaces between typed arguments, so multi-arg calls have
to use **slashes** as separators — slashes survive URL encoding intact and
hit our path-parameter routes.

After registering the project (`/register pokeprices <username> <api-url>`),
anyone can call:

```
/project pokeprices
```
> Lists `about` + the 4 resources.

```
/project pokeprices price/charizard
```
> "Blaine's Charizard (gym2-2) - $894.61 market [1stEditionHolofoil]"

```
/project pokeprices price/mew
```
> "Mew (ecard1-19) - $411.66 market [holofoil]"

```
/project pokeprices top
```
> "1. Shining Tyranitar (neo4-113) $4249.99 | 2. Shining Charizard (neo4-107) $3998.99 | 3. Espeon ★ (pop5-16) $1900.00 | …"

```
/project pokeprices plot/charizard
```
> Returns an S3 URL like
> `https://p3-pokemon-cardprices.s3.amazonaws.com/plots/gym2-2/30d-1777777646.png`
> which Discord renders inline as a chart.

```
/project pokeprices plot/mew/7d
```
> Same as above but for a 7-day window.

```
/project pokeprices change/charizard/1m
```
> "Charizard (...) over last 1m: $850.00 -> $894.61 (+5.2% total over window, +$44.61/month avg, 720 samples)"

For card names that contain spaces, replace each space with a hyphen or
just type a single fragment — pokemontcg.io's name search is fuzzy enough
to match (e.g. `price/charizard-ex` or just `price/charizard`).

Calling `price`, `plot`, or `change` with no card returns a usage hint
rather than an error, so a bare `/project pokeprices price` is harmless.

## Logging and exception handling

Every external call (pokemontcg.io HTTP, DynamoDB read/write, S3 upload,
matplotlib render) is wrapped in a `try/except` that:

1. Catches the most specific exception type the call raises
   (`requests.RequestException`, `botocore.exceptions.ClientError`,
   `BotoCoreError`, `ValueError` for JSON parsing).
2. Logs the failure with `log.exception(...)` (includes the traceback)
   and the relevant context (`card_id`, query, status code).
3. Either re-raises a domain exception (`CardNotFoundError`,
   `TCGFetchError`, `PlotError`) or, in the route handlers, converts to a
   user-friendly `{"response": "..."}` so the Discord bot never prints a
   stack trace into the channel.

`log.info(...)` calls bracket every meaningful step (fetches, writes,
ingest start/end with counters for success/missing/no-price/errors), and
the log level is configurable via the `LOG_LEVEL` env var. All logs land
in CloudWatch under the two Lambda log groups
(`/aws/lambda/card-prices-dev` and `/aws/lambda/card-prices-dev-ingest`).

## Project layout

```
.
├── app.py                          Chalice app: 4 routes + scheduled ingest
├── chalicelib/
│   ├── log.py                      shared logger setup
│   ├── tcg.py                      pokemontcg.io client + top-cards fetcher
│   ├── store.py                    DynamoDB access (CardPrices, CardWatchlist)
│   ├── analytics.py                window parsing, top_n, compute_change
│   └── plotting.py                 matplotlib + S3 upload
├── scripts/
│   ├── setup_aws.py                creates DynamoDB tables + S3 bucket
│   └── build_matplotlib_layer.sh   builds the matplotlib Lambda layer
├── requirements.txt                chalice, boto3, requests (matplotlib via layer)
├── .chalice/
│   ├── config.json                 env vars + IAM policy + layer ARN
│   └── policy-dev.json             DynamoDB + S3 perms
├── INSTRUCTIONS.md                 the assignment brief
└── README.md                       this file
```

## Stretch goals shipped

- **More than 3 resources.** Four were required minimum, all four are
  parameterized where it makes sense.
- **Parameterized window** on `/plot` and `/change` (any `\d+[dmy]`).
- **Lazy watchlist.** No hand-curated list of cards to track — the dataset
  expands as the API gets used.
- **Global top from upstream**, not just from the local watchlist. The
  `/top` resource issues parallel range-filter queries against
  pokemontcg.io to get the genuine top 10 across the whole catalog,
  rather than only what users have already asked about.
- **Custom matplotlib Lambda layer** published from S3 (no Klayers
  dependency), with stripped tests / sample data / debug symbols to fit
  Lambda's size limits.

## Setup (for graders / re-deploy)

```bash
# 1. Python 3.12 venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Get a free pokemontcg.io API key from https://dev.pokemontcg.io
#    Paste it into .chalice/config.json under POKEMONTCG_API_KEY.

# 3. Create AWS resources (idempotent)
python scripts/setup_aws.py --bucket <your-unique-bucket> --region us-east-1

# 4. Build + publish the matplotlib layer, paste the ARN into config.json
./scripts/build_matplotlib_layer.sh

# 5. Deploy
chalice deploy
```
