"""DynamoDB access for card prices and the watchlist.

Two tables:

  CardPrices
    PK card_id  (string)   e.g. 'swsh4-25'
    SK timestamp (number)  Unix epoch seconds
    Attributes: price (Decimal), variant (string), name (string)

  CardWatchlist
    PK card_id (string)
    Attributes: name (string), added_ts (number)

The schedule Lambda scans CardWatchlist and writes a CardPrices snapshot for
each watched card. The /price API also lazy-adds new cards to the watchlist
the first time a user looks them up.
"""

import os
import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from .log import get_logger

log = get_logger(__name__)

PRICES_TABLE = os.environ.get("PRICES_TABLE", "CardPrices")
WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "CardWatchlist")

_resource = None


def _ddb():
    """Lazy-init the DynamoDB resource so unit tests can patch boto3."""
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb")
    return _resource


def _to_decimal(value: float | int | str) -> Decimal:
    # DynamoDB rejects float; round-trip through str to avoid binary repr noise.
    return Decimal(str(value))


def _decode_item(item: dict) -> dict:
    """Convert Decimal fields to native Python types for JSON serialization."""
    out = dict(item)
    for k, v in list(out.items()):
        if isinstance(v, Decimal):
            # Timestamps are ints, prices are floats — both fit in float here.
            out[k] = float(v) if "." in str(v) else int(v)
    return out


def put_price(
    card_id: str,
    price: float,
    *,
    variant: str | None = None,
    name: str | None = None,
    timestamp: int | None = None,
) -> int:
    """Write a price snapshot. Returns the timestamp used."""
    ts = timestamp or int(time.time())
    item: dict[str, Any] = {
        "card_id": card_id,
        "timestamp": ts,
        "price": _to_decimal(price),
    }
    if variant:
        item["variant"] = variant
    if name:
        item["name"] = name

    log.info(
        "put_price card_id=%s price=%s variant=%s ts=%s",
        card_id, price, variant, ts,
    )
    try:
        _ddb().Table(PRICES_TABLE).put_item(Item=item)
    except (BotoCoreError, ClientError):
        log.exception("DynamoDB put_item failed for card_id=%s", card_id)
        raise
    return ts


def get_history(
    card_id: str,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> list[dict]:
    """Query price history for a card, optionally bounded by timestamps."""
    log.info(
        "get_history card_id=%s since=%s until=%s",
        card_id, since_ts, until_ts,
    )
    cond = Key("card_id").eq(card_id)
    if since_ts is not None and until_ts is not None:
        cond = cond & Key("timestamp").between(since_ts, until_ts)
    elif since_ts is not None:
        cond = cond & Key("timestamp").gte(since_ts)
    elif until_ts is not None:
        cond = cond & Key("timestamp").lte(until_ts)

    try:
        resp = _ddb().Table(PRICES_TABLE).query(
            KeyConditionExpression=cond,
            ScanIndexForward=True,  # ascending by timestamp
        )
    except (BotoCoreError, ClientError):
        log.exception("DynamoDB query failed for card_id=%s", card_id)
        raise

    items = [_decode_item(i) for i in resp.get("Items", [])]
    log.info("get_history card_id=%s returned %d snapshots", card_id, len(items))
    return items


def latest_price(card_id: str) -> dict | None:
    """Return the single most recent snapshot for a card, or None."""
    log.info("latest_price card_id=%s", card_id)
    try:
        resp = _ddb().Table(PRICES_TABLE).query(
            KeyConditionExpression=Key("card_id").eq(card_id),
            ScanIndexForward=False,
            Limit=1,
        )
    except (BotoCoreError, ClientError):
        log.exception("DynamoDB query failed for card_id=%s", card_id)
        raise
    items = resp.get("Items", [])
    return _decode_item(items[0]) if items else None


def add_to_watchlist(card_id: str, name: str | None = None) -> None:
    """Idempotently add a card to the ingest watchlist."""
    log.info("add_to_watchlist card_id=%s name=%r", card_id, name)
    item = {"card_id": card_id, "added_ts": int(time.time())}
    if name:
        item["name"] = name
    try:
        _ddb().Table(WATCHLIST_TABLE).put_item(Item=item)
    except (BotoCoreError, ClientError):
        log.exception("Watchlist put_item failed for card_id=%s", card_id)
        raise


def list_watchlist() -> list[dict]:
    """Scan all watched cards. Watchlist is small (~hundreds) so scan is fine."""
    log.info("list_watchlist scan starting")
    items: list[dict] = []
    table = _ddb().Table(WATCHLIST_TABLE)
    try:
        resp = table.scan()
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
    except (BotoCoreError, ClientError):
        log.exception("Watchlist scan failed")
        raise
    log.info("list_watchlist returned %d entries", len(items))
    return [_decode_item(i) for i in items]


