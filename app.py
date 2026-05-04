"""Chalice app — the API + the scheduled ingest Lambda live in one project.

Resources (per the API contract in INSTRUCTIONS.md):

  GET /                              -> {about, resources}
  GET /price                         -> usage hint
  GET /price/{card}                  -> latest market price (lazy-tracks new cards)
  GET /price?card=<id|name>          -> same, query-string form
  GET /top                           -> top 10 most expensive cards globally
  GET /plot                          -> usage hint
  GET /plot/{card}                   -> S3 URL of price-history PNG (default 30d)
  GET /plot/{card}/{window}          -> ...with a custom window
  GET /plot?card=<id|name>&window=W  -> same, query-string form
  GET /change                        -> usage hint
  GET /change/{card}                 -> total %% + avg $/month (default 30d)
  GET /change/{card}/{window}        -> ...with a custom window
  GET /change?card=...&window=W      -> same, query-string form

Why both path and query forms? The Discord bot URL-encodes spaces between
typed args and tacks them onto the path, so `/project pokeprices price mew`
becomes GET /price%20mew (a 404). Path params with slashes survive URL
encoding intact, so `/project pokeprices price/mew` becomes GET /price/mew
which matches /price/{card}. The query-string forms are kept for curl/browser
testing.

Window format: '7d', '30d', '1m', '3m', '1y' (see chalicelib.analytics).

The scheduled ingest fires every hour and writes a snapshot per watched
card into DynamoDB. The watchlist is populated lazily by /price calls and
also gets seeded each time /top is called.
"""

import os
import urllib.parse
from datetime import datetime, timedelta, timezone

from chalice import BadRequestError, Chalice, NotFoundError, Rate

from chalicelib import analytics, plotting, store, tcg
from chalicelib.log import get_logger

app = Chalice(app_name="card-prices")
app.log.setLevel("INFO")

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qs() -> dict:
    return app.current_request.query_params or {}


def _looks_like_window(s: str) -> bool:
    """Heuristic: '7d', '30d', '1m', '1y' look like windows."""
    return bool(s) and s[-1:].lower() in ("d", "m", "y") and s[:-1].isdigit()


def _extract_card(qs: dict) -> str | None:
    """Pull the card identifier from query params, both ?card=X and bare ?X."""
    if not qs:
        return None
    if qs.get("card"):
        return qs["card"]
    for k, v in qs.items():
        if not v and k != "window" and not _looks_like_window(k):
            return k
    return None


def _extract_window(qs: dict) -> str | None:
    """Pull the window from query params. Returns None when unspecified
    (signals 'auto-fit to all available data')."""
    if not qs:
        return None
    if qs.get("window"):
        return qs["window"]
    for k, v in qs.items():
        if not v and _looks_like_window(k):
            return k
    return None


def _humanize_span(seconds: float) -> str:
    """Compact label for a duration: '23h', '5d', '2mo', '1y'."""
    if seconds < 86400 * 1.5:
        return f"{max(1, int(round(seconds / 3600)))}h"
    if seconds < 86400 * 60:
        return f"{int(round(seconds / 86400))}d"
    if seconds < 86400 * 730:
        return f"{int(round(seconds / (86400 * 30)))}mo"
    return f"{int(round(seconds / (86400 * 365)))}y"


def _auto_window(history: list[dict]) -> tuple[timedelta, str, float]:
    """Derive a (timedelta, label, months) covering all available data.

    Used when the caller didn't specify a window. With a single sample we
    fall back to a 24h frame so the chart axis is still meaningful.
    """
    if len(history) >= 2:
        first_ts = int(history[0]["timestamp"])
        last_ts = int(history[-1]["timestamp"])
        span_s = max(last_ts - first_ts, 3600)
        # 5% padding so the first/last marker isn't on the axis edge
        span_s *= 1.05
    else:
        span_s = 86400.0
    months = span_s / (86400 * 30)
    return timedelta(seconds=span_s), _humanize_span(span_s), months


def _resolve_or_404(card_q: str) -> dict:
    """Resolve a user query to a card payload, mapping errors to HTTP errors."""
    try:
        return tcg.resolve_card(card_q)
    except tcg.CardNotFoundError:
        log.info("No card matched user query=%r", card_q)
        raise NotFoundError(f"no card found for {card_q!r}")
    except tcg.TCGFetchError as exc:
        # Don't 500 — the bot prints `response` directly, so a friendly
        # string is more useful than a stack trace.
        log.error("Upstream TCG error resolving %r: %s", card_q, exc)
        raise BadRequestError(f"upstream error fetching {card_q!r}: {exc}")


def _decode(s: str) -> str:
    """URL-decode a path segment (Chalice gives us the raw value)."""
    return urllib.parse.unquote(s) if s else s


def _ensure_tracked(card: dict) -> tuple[float | None, str | None]:
    """Persist a fresh snapshot + ensure the card is on the watchlist.

    Called by /price, /plot, and /change so that any card the user asks
    about starts collecting history immediately. All store errors are
    logged and swallowed — a transient DynamoDB failure shouldn't break
    the user-facing reply.
    """
    market, variant = tcg.extract_market_price(card)
    if market is None:
        log.info("No market price available for card_id=%s", card.get("id"))
        return None, None

    try:
        store.put_price(
            card["id"], market, variant=variant, name=card.get("name", "")
        )
    except Exception:
        log.exception("Failed to persist snapshot for %s", card.get("id"))

    try:
        store.add_to_watchlist(card["id"], name=card.get("name", ""))
    except Exception:
        log.exception("Failed to add %s to watchlist", card.get("id"))

    return market, variant


# ---------------------------------------------------------------------------
# Zone apex
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return {
        "about": (
            "Tracks Pokemon TCG card market prices over time using the "
            "Pokemon TCG API (TCGplayer pricing). Cards become tracked the "
            "first time anyone queries /price for them."
        ),
        "resources": ["price", "top", "plot", "change"],
    }


# ---------------------------------------------------------------------------
# /price — current price for a specific card (lazy-adds to watchlist)
# ---------------------------------------------------------------------------

def _price_impl(card_q: str | None) -> dict:
    if not card_q:
        log.warning("/price missing card argument")
        return {
            "response": (
                "usage: try `/project pokeprices price/<card-name>` "
                "(slashes between args, no spaces)"
            )
        }

    log.info("/price card=%r", card_q)
    card = _resolve_or_404(card_q)
    market, variant = _ensure_tracked(card)
    if market is None:
        return {
            "response": (
                f"{card.get('name')} ({card.get('id')}) - "
                f"no TCGplayer market price available"
            )
        }
    return {
        "response": (
            f"{card.get('name')} ({card.get('id')}) - "
            f"${market:.2f} market [{variant}]"
        )
    }


@app.route("/price")
def price():
    return _price_impl(_extract_card(_qs()))


@app.route("/price/{card}")
def price_by_card(card):
    return _price_impl(_decode(card))


# ---------------------------------------------------------------------------
# /top — top 10 most expensive Pokemon cards globally
# ---------------------------------------------------------------------------

@app.route("/top")
def top():
    """Top 10 most expensive Pokemon cards in the pokemontcg.io catalog."""
    log.info("/top called")
    try:
        top_cards = tcg.top_cards_by_price(n=10)
    except Exception:
        log.exception("/top: upstream top-cards query failed")
        return {"response": "error fetching top cards from upstream"}

    if not top_cards:
        return {
            "response": (
                "couldn't retrieve top cards from upstream - "
                "try again in a minute"
            )
        }

    # Persist + watchlist-add so /plot and /change work for these cards too.
    for entry in top_cards:
        card = entry["card"]
        try:
            store.put_price(
                card["id"],
                entry["price"],
                variant=entry["variant"],
                name=card.get("name", ""),
            )
            store.add_to_watchlist(card["id"], name=card.get("name", ""))
        except Exception:
            log.exception(
                "Failed to persist top card %s (continuing)", card.get("id")
            )

    parts = [
        f"{i + 1}. {entry['card'].get('name')} ({entry['card']['id']}) "
        f"${entry['price']:.2f}"
        for i, entry in enumerate(top_cards)
    ]
    return {"response": " | ".join(parts)}


# ---------------------------------------------------------------------------
# /plot — price-history chart in S3
# ---------------------------------------------------------------------------

def _plot_impl(card_q: str | None, window: str | None) -> dict:
    if not card_q:
        return {
            "response": (
                "usage: try `/project pokeprices plot/<card-name>` "
                "(or plot/<card>/<window> for 7d, 30d, 1m, 1y - "
                "defaults to all available data)"
            )
        }

    log.info("/plot card=%r window=%r", card_q, window)

    # Resolve window if user gave one; otherwise we'll auto-fit after
    # loading history.
    if window is not None:
        try:
            delta, _ = analytics.parse_window(window)
        except analytics.WindowParseError as exc:
            return {"response": str(exc)}
        since = int((datetime.now(timezone.utc) - delta).timestamp())
    else:
        delta = None
        since = None

    card = _resolve_or_404(card_q)
    card_id = card["id"]
    _ensure_tracked(card)

    try:
        history = store.get_history(card_id, since_ts=since)
    except Exception:
        log.exception("/plot: history query failed for %s", card_id)
        return {"response": "internal error retrieving history"}

    if not history:
        return {
            "response": (
                f"no TCGplayer market price available for "
                f"{card.get('name')} ({card_id})"
            )
        }

    if delta is None:
        delta, window_label, _ = _auto_window(history)
    else:
        window_label = window

    try:
        url = plotting.render_history_plot(
            card_id, card.get("name", card_id), history, window_label,
            window_delta=delta,
        )
    except plotting.PlotError as exc:
        log.error("/plot: render failed: %s", exc)
        return {"response": f"plot rendering failed: {exc}"}

    return {"response": url}


@app.route("/plot")
def plot():
    qs = _qs()
    return _plot_impl(_extract_card(qs), _extract_window(qs))


@app.route("/plot/{card}")
def plot_by_card(card):
    return _plot_impl(_decode(card), None)


@app.route("/plot/{card}/{window}")
def plot_by_card_window(card, window):
    return _plot_impl(_decode(card), _decode(window))


# ---------------------------------------------------------------------------
# /change — total % over window + avg $/month
# ---------------------------------------------------------------------------

def _change_impl(card_q: str | None, window: str | None) -> dict:
    if not card_q:
        return {
            "response": (
                "usage: try `/project pokeprices change/<card-name>` "
                "(or change/<card>/<window> for 7d, 30d, 1m, 1y - "
                "defaults to all available data)"
            )
        }

    log.info("/change card=%r window=%r", card_q, window)

    if window is not None:
        try:
            delta, months = analytics.parse_window(window)
        except analytics.WindowParseError as exc:
            return {"response": str(exc)}
        since = int((datetime.now(timezone.utc) - delta).timestamp())
    else:
        delta = None
        months = None
        since = None

    card = _resolve_or_404(card_q)
    card_id = card["id"]
    _ensure_tracked(card)

    try:
        history = store.get_history(card_id, since_ts=since)
    except Exception:
        log.exception("/change: history query failed for %s", card_id)
        return {"response": "internal error retrieving history"}

    if months is None:
        _, window_label, months = _auto_window(history)
    else:
        window_label = window

    stats = analytics.compute_change(history, months)
    if not stats:
        return {
            "response": (
                f"not enough price history for {card.get('name')} "
                f"({card_id}) over the last {window_label} yet - need at "
                "least 2 snapshots; check back after the next hourly ingest"
            )
        }

    return {
        "response": (
            f"{card.get('name')} ({card_id}) over last {window_label}: "
            f"${stats['start_price']:.2f} -> ${stats['end_price']:.2f} "
            f"({stats['pct_change_window']:+.1f}% total over window, "
            f"${stats['dollar_per_month']:+.2f}/month avg, "
            f"{stats['samples']} samples)"
        )
    }


@app.route("/change")
def change():
    qs = _qs()
    return _change_impl(_extract_card(qs), _extract_window(qs))


@app.route("/change/{card}")
def change_by_card(card):
    return _change_impl(_decode(card), None)


@app.route("/change/{card}/{window}")
def change_by_card_window(card, window):
    return _change_impl(_decode(card), _decode(window))


# ---------------------------------------------------------------------------
# Scheduled ingest — fires every hour, writes a snapshot per watched card
# ---------------------------------------------------------------------------

INGEST_THRESHOLD = float(os.environ.get("INGEST_THRESHOLD", "5.0"))


@app.schedule(Rate(1, unit=Rate.HOURS))
def ingest(event):
    """Hourly bulk ingest.

    Phase 1 (bulk): pull every card above $INGEST_THRESHOLD from pokemontcg.io
    in a few parallel queries and snapshot them all. This is what makes the
    system work for any card the user asks about without any prior /price
    call — everything above the threshold gets tracked from deploy onwards.

    Phase 2 (stragglers): for any card on the watchlist that *wasn't* in the
    bulk pull (cheap cards a user added via /price etc.), do a per-card
    fetch so they don't fall out of history.
    """
    log.info("Scheduled ingest starting (event_id=%s)", getattr(event, "event_id", "?"))

    bulk_count, bulk_errors = 0, 0
    bulk_ids: set[str] = set()
    try:
        bulk = tcg.fetch_all_priced_cards(threshold=INGEST_THRESHOLD)
    except Exception:
        log.exception("Bulk fetch failed; falling back to watchlist-only this cycle")
        bulk = []

    for entry in bulk:
        card = entry["card"]
        cid = card["id"]
        try:
            store.put_price(
                cid, entry["price"], variant=entry["variant"],
                name=card.get("name", ""),
            )
            store.add_to_watchlist(cid, name=card.get("name", ""))
            bulk_ids.add(cid)
            bulk_count += 1
        except Exception:
            log.exception("Failed to persist bulk card %s", cid)
            bulk_errors += 1

    # Phase 2: catch stragglers — watched cards not covered by the bulk pull.
    try:
        watched = store.list_watchlist()
    except Exception:
        log.exception("Failed to load watchlist; skipping stragglers this cycle")
        return {
            "ok": True, "bulk": bulk_count, "bulk_errors": bulk_errors,
            "stragglers": 0, "straggler_errors": 0,
        }

    stragglers = [w for w in watched if w["card_id"] not in bulk_ids]
    log.info(
        "Bulk ingested %d cards; %d stragglers to fetch individually",
        bulk_count, len(stragglers),
    )

    extra_count, extra_errors, no_price, missing = 0, 0, 0, 0
    for entry in stragglers:
        cid = entry["card_id"]
        try:
            card = tcg.get_card(cid)
        except tcg.CardNotFoundError:
            log.warning("Watched card %s no longer in upstream", cid)
            missing += 1
            continue
        except tcg.TCGFetchError:
            log.exception("Upstream fetch failed for %s; retry next cycle", cid)
            extra_errors += 1
            continue
        except Exception:
            log.exception("Unexpected error fetching %s", cid)
            extra_errors += 1
            continue

        price, variant = tcg.extract_market_price(card or {})
        if price is None:
            no_price += 1
            continue

        try:
            store.put_price(
                cid, price, variant=variant, name=card.get("name", "")
            )
            extra_count += 1
        except Exception:
            log.exception("Failed to write snapshot for straggler %s", cid)
            extra_errors += 1

    log.info(
        "Ingest done: %d bulk + %d stragglers (%d bulk-err, %d straggler-err, "
        "%d no-price, %d missing)",
        bulk_count, extra_count, bulk_errors, extra_errors, no_price, missing,
    )
    return {
        "ok": True,
        "bulk": bulk_count,
        "stragglers": extra_count,
        "bulk_errors": bulk_errors,
        "straggler_errors": extra_errors,
        "no_price": no_price,
        "missing": missing,
        "watchlist_size": len(watched),
    }
