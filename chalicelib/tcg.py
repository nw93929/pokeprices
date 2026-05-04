"""Thin client for the Pokemon TCG API (https://pokemontcg.io).

The Pokemon TCG API exposes TCGplayer market prices on every card payload
under `card.tcgplayer.prices.<variant>.market`. Free tier with an API key
allows 20k requests/day, which is plenty for ingesting a few hundred cards
on a 6-hour cadence.
"""

import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .log import get_logger

log = get_logger(__name__)

API_BASE = "https://api.pokemontcg.io/v2"
DEFAULT_TIMEOUT = 10  # seconds

# Variants ordered by how typical they are for valuable cards. We pick the
# first variant that has a market price; this avoids returning $0 for a card
# whose `normal` variant is unpriced but whose `holofoil` is.
PRICE_VARIANT_PREFERENCE = (
    "holofoil",
    "reverseHolofoil",
    "1stEditionHolofoil",
    "unlimitedHolofoil",
    "1stEditionNormal",
    "unlimited",
    "normal",
)


class CardNotFoundError(Exception):
    """Raised when no card matches the given identifier."""


class TCGFetchError(Exception):
    """Raised when the upstream API call fails (network, 5xx, etc.)."""


def _headers() -> dict:
    api_key = os.environ.get("POKEMONTCG_API_KEY", "")
    if not api_key:
        log.warning(
            "POKEMONTCG_API_KEY not set; falling back to anonymous rate limits"
        )
        return {}
    return {"X-Api-Key": api_key}


def _looks_like_card_id(identifier: str) -> bool:
    """Heuristic: pokemontcg.io IDs look like 'swsh4-25', 'sv1-220'."""
    return "-" in identifier and " " not in identifier and len(identifier) <= 32


def get_card(card_id: str) -> dict | None:
    """Fetch a single card by exact pokemontcg.io ID."""
    log.info("Fetching card by id=%s", card_id)
    try:
        resp = requests.get(
            f"{API_BASE}/cards/{urllib.parse.quote(card_id, safe='')}",
            headers=_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.exception("Network error fetching card_id=%s", card_id)
        raise TCGFetchError(f"network error: {exc}") from exc

    if resp.status_code == 404:
        log.info("Card not found upstream: %s", card_id)
        raise CardNotFoundError(card_id)
    if resp.status_code >= 500:
        log.error(
            "Upstream 5xx for card_id=%s status=%s body=%s",
            card_id, resp.status_code, resp.text[:200],
        )
        raise TCGFetchError(f"upstream {resp.status_code}")
    if not resp.ok:
        log.error(
            "Unexpected status fetching card_id=%s status=%s body=%s",
            card_id, resp.status_code, resp.text[:200],
        )
        raise TCGFetchError(f"unexpected status {resp.status_code}")

    try:
        return resp.json().get("data")
    except ValueError as exc:
        log.exception("Invalid JSON from upstream for card_id=%s", card_id)
        raise TCGFetchError("invalid JSON from upstream") from exc


def search_card(name_query: str, page_size: int = 20) -> list[dict]:
    """Search cards by name fragment. Returns a (possibly empty) list."""
    # Escape quotes inside the user query so it can't break out of the q string.
    safe_q = name_query.replace('"', '\\"')
    log.info("Searching cards by name=%r", name_query)
    try:
        resp = requests.get(
            f"{API_BASE}/cards",
            params={"q": f'name:"{safe_q}"', "pageSize": page_size},
            headers=_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.exception("Search failed for query=%r", name_query)
        raise TCGFetchError(f"search failed: {exc}") from exc

    try:
        data = resp.json().get("data", []) or []
    except ValueError as exc:
        log.exception("Invalid JSON in search response for query=%r", name_query)
        raise TCGFetchError("invalid JSON from upstream") from exc

    log.info("Search for %r returned %d candidates", name_query, len(data))
    return data


def extract_market_price(card: dict) -> tuple[float | None, str | None]:
    """Pull the best-available TCGplayer market price out of a card payload.

    Returns (price, variant_name). If no variant has a market price, returns
    (None, None) — the caller decides whether that's an error or just "skip".
    """
    if not card:
        return None, None
    prices = ((card.get("tcgplayer") or {}).get("prices")) or {}

    for variant in PRICE_VARIANT_PREFERENCE:
        v = prices.get(variant) or {}
        if v.get("market") is not None:
            return float(v["market"]), variant

    # Fallback: any variant we didn't enumerate above.
    for variant, payload in prices.items():
        if isinstance(payload, dict) and payload.get("market") is not None:
            return float(payload["market"]), variant

    return None, None


def _query_variant_candidates(variant: str, threshold: float) -> list[dict]:
    """Fetch cards above `threshold` for one price variant. Returns [] on any error."""
    field = f"tcgplayer.prices.{variant}.market"
    log.info("top query variant=%s threshold=%s", variant, threshold)
    try:
        resp = requests.get(
            f"{API_BASE}/cards",
            params={"q": f"{field}:[{threshold:g} TO *]", "pageSize": 250},
            headers=_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", []) or []
        log.info("top query variant=%s -> %d candidates", variant, len(data))
        return data
    except requests.RequestException:
        log.exception("top query HTTP error for variant=%s", variant)
        return []
    except ValueError:
        log.exception("top query invalid JSON for variant=%s", variant)
        return []


def fetch_all_priced_cards(threshold: float = 5.0) -> list[dict]:
    """Bulk-fetch every card above `threshold`, with prices already extracted.

    Used by the scheduled ingest to track every meaningfully-priced card
    in the catalog without anyone having to call /price first. Runs the
    7 variant queries in parallel and dedupes by card id; pokemontcg.io's
    250-per-page cap × 7 variants gives us roughly 1k-1.5k unique cards
    per call after dedup, which comfortably covers everything above $5.

    Returns: [{"card": payload, "price": float, "variant": str}, ...]
    (no sorting; the caller writes them all to the store).
    """
    seen: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(PRICE_VARIANT_PREFERENCE)) as pool:
        futures = {
            pool.submit(_query_variant_candidates, v, threshold): v
            for v in PRICE_VARIANT_PREFERENCE
        }
        for future in as_completed(futures):
            for card in future.result():
                cid = card.get("id")
                if cid and cid not in seen:
                    seen[cid] = card

    out: list[dict] = []
    for card in seen.values():
        price, variant = extract_market_price(card)
        if price is not None:
            out.append({"card": card, "price": price, "variant": variant})

    log.info(
        "fetch_all_priced_cards: %d unique cards above $%s",
        len(out), threshold,
    )
    return out


def top_cards_by_price(
    n: int = 10,
    threshold: float = 100.0,
) -> list[dict]:
    """Find the most expensive cards across the pokemontcg.io catalog.

    Runs all variant queries in parallel so total wall time is one round
    trip instead of len(PRICE_VARIANT_PREFERENCE) round trips.

    Returns a list of dicts: {"card": full_card_payload, "price": float,
    "variant": str}, sorted descending by price, up to n items.
    """
    seen: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(PRICE_VARIANT_PREFERENCE)) as pool:
        futures = {
            pool.submit(_query_variant_candidates, v, threshold): v
            for v in PRICE_VARIANT_PREFERENCE
        }
        for future in as_completed(futures):
            for card in future.result():
                cid = card.get("id")
                if cid and cid not in seen:
                    seen[cid] = card

    scored: list[dict] = []
    for card in seen.values():
        price, variant = extract_market_price(card)
        if price is not None:
            scored.append({"card": card, "price": price, "variant": variant})
    scored.sort(key=lambda x: x["price"], reverse=True)
    log.info(
        "top_cards_by_price: %d unique candidates; returning top %d",
        len(scored), n,
    )
    return scored[:n]


def resolve_card(identifier: str) -> dict:
    """Resolve a user-supplied identifier (id or name) to a single card payload.

    - If `identifier` looks like a pokemontcg.io ID, try direct fetch first.
    - Otherwise (or on miss) fall back to a name search and pick the match
      with the highest market price (so 'Charizard' resolves to a chase
      version, not a $0.05 reprint).
    """
    if not identifier or not identifier.strip():
        raise CardNotFoundError("empty identifier")

    identifier = identifier.strip()

    if _looks_like_card_id(identifier):
        try:
            card = get_card(identifier)
            if card:
                log.info("Resolved %r via direct id lookup", identifier)
                return card
        except CardNotFoundError:
            log.info("ID lookup miss for %r; falling back to name search", identifier)

    candidates = search_card(identifier)
    if not candidates:
        log.info("No name-search candidates for %r", identifier)
        raise CardNotFoundError(identifier)

    best, best_price = None, -1.0
    for cand in candidates:
        price, _ = extract_market_price(cand)
        if price is not None and price > best_price:
            best, best_price = cand, price

    chosen = best or candidates[0]
    log.info(
        "Resolved %r to card_id=%s name=%r best_price=%s",
        identifier, chosen.get("id"), chosen.get("name"), best_price,
    )
    return chosen
