# card-prices

DS5220 Data Project 3. Tracks Pokémon TCG card market prices over time and
exposes them through a Chalice / API Gateway service.

**API URL:** `https://ybq9genx9i.execute-api.us-east-1.amazonaws.com/api`
**Discord project ID:** `pokeprices`

## Data source

Prices come from the [Pokémon TCG API](https://pokemontcg.io), which
republishes TCGplayer market prices on every card under
`card.tcgplayer.prices.<variant>.market`. A good amount of people have tried investing in cards as a viable passive income strategy and this bot helps collectors and investors keep track of both long term movement and short term. Card prices can swing based on key events like tournament results, and reprint announcements, and so on, which cannot be captured with a single lookup price but can be with a time series plot over time.

## Sampling cadence and storage

- **Cadence:** the ingest Lambda fires every **1 hour** via an EventBridge
  scheduled rule.
- **DynamoDB tables** (both `PAY_PER_REQUEST`):

  | Table | PK | SK | Other attributes |
  |-------|----|----|------------------|
  | `CardPrices` | `card_id` (S) | `timestamp` (N, Unix epoch) | `price` (N), `variant` (S), `name` (S) |
  | `CardWatchlist` | `card_id` (S) | — | `name` (S), `added_ts` (N) |

- **S3 plot bucket:** `p3-pokemon-cardprices`, with public-read on
  `plots/*` only.

## API resources

The zone apex (`GET /`) returns `{about, resources}`. Every other resource
returns `{ "response": ... }`.

NOTE: You can find any card's ID by searching at https://pokemontcg.io.

| Resource | Args | Returns |
|---|---|---|
| `price` | `card` (id or name) | Latest TCGplayer market price for one card. Lazy-adds the card to the watchlist. |
| `top` | — | Top 10 most expensive Pokémon cards in the pokemontcg.io catalog. |
| `plot` | `card`, `window` (default `30d`) | URL of a price-history PNG in S3. |
| `change` | `card`, `window` (default `30d`) | Total % change over the window + average $/month. |

`window` accepts `7d`, `30d`, `1m`, `1y`, etc.

## Stretch goals

- **Four resources** instead of the required three.
- **Parameterized window** on `/plot` and `/change`.
- **Lazy watchlist** — cards are added the first time anyone queries
  them, so the dataset grows organically.
- **Global top from upstream** — `/top` queries pokemontcg.io directly
  rather than ranking only cards we already track.
- **Custom matplotlib Lambda layer** published from S3 to fit Lambda's
  size limits without depending on Klayers.
