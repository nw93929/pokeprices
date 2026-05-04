"""Render a card's price history to a PNG and upload it to S3.

The S3 bucket needs a public-read policy on the `plots/` prefix (or we set
the object ACL to public-read). The `/plot` resource returns the public URL.
"""

import io
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
import matplotlib

# Lambda has no display; force the non-interactive Agg backend before pyplot.
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402  (must come after backend set)
import matplotlib.pyplot as plt  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from .log import get_logger

log = get_logger(__name__)

S3_BUCKET = os.environ.get("PLOT_BUCKET", "")
PLOT_PREFIX = os.environ.get("PLOT_PREFIX", "plots")
# If the bucket has BlockPublicAccess turned on, ACLs are rejected. Use a
# bucket policy instead and set this env var to "false" to skip the ACL.
USE_PUBLIC_ACL = os.environ.get("USE_PUBLIC_ACL", "true").lower() == "true"


class PlotError(Exception):
    """Raised when rendering or upload fails."""


def render_history_plot(
    card_id: str,
    card_name: str,
    history: list[dict],
    window_label: str,
    window_delta: timedelta | None = None,
) -> str:
    """Render a price-history plot, upload to S3, and return the public URL."""
    if not history:
        raise PlotError("no history to plot")
    if not S3_BUCKET:
        raise PlotError("PLOT_BUCKET env var is not set")

    log.info(
        "Rendering plot card_id=%s window=%s samples=%d",
        card_id, window_label, len(history),
    )

    try:
        times = [
            datetime.fromtimestamp(int(h["timestamp"]), tz=timezone.utc)
            for h in history
        ]
        prices = [float(h["price"]) for h in history]
    except (KeyError, TypeError, ValueError) as exc:
        log.exception("History rows are malformed for card_id=%s", card_id)
        raise PlotError(f"malformed history rows: {exc}") from exc

    fig, ax = plt.subplots(figsize=(8, 4))
    try:
        ax.plot(times, prices, marker="o", markersize=4, linewidth=1.5)
        ax.set_title(f"{card_name} ({card_id}) — last {window_label}")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("TCGplayer market price ($)")
        ax.grid(True, alpha=0.3)

        # Zoom y-axis tight to the actual price range so small fluctuations
        # are visible. Without this, matplotlib chooses round-number ticks
        # and a $5 swing on a $4,250 card looks completely flat. If the
        # range is essentially zero, force a small visible band so the line
        # doesn't sit on top of an axis tick.
        pmin, pmax = min(prices), max(prices)
        prange = pmax - pmin
        if prange < 0.005 * pmax:
            prange = max(0.02 * pmax, 0.10)
        pad = prange * 0.20
        ax.set_ylim(pmin - pad, pmax + pad)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.2f}"))

        # Annotate min and max so the actual magnitude is unambiguous.
        imin = prices.index(pmin)
        imax = prices.index(pmax)
        ax.annotate(
            f"min ${pmin:,.2f}",
            xy=(times[imin], pmin),
            xytext=(5, -12), textcoords="offset points",
            fontsize=8, color="#666",
        )
        if imax != imin:
            ax.annotate(
                f"max ${pmax:,.2f}",
                xy=(times[imax], pmax),
                xytext=(5, 5), textcoords="offset points",
                fontsize=8, color="#666",
            )

        # Pin the x-axis to the requested window so a 1-point plot doesn't
        # auto-scale to a multi-year default. End at "now", start at
        # now - window. If we don't know the window for some reason, fall
        # back to the data range.
        if window_delta is not None:
            now = datetime.now(timezone.utc)
            ax.set_xlim(now - window_delta, now)

        # ConciseDateFormatter picks an appropriate granularity for the
        # window: hours for a single day, dates for a week+, months for a
        # year. Way better than a fixed YYYY-MM-DD format.
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        fig.autofmt_xdate()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    except Exception as exc:
        log.exception("matplotlib render failed for card_id=%s", card_id)
        raise PlotError(f"render failed: {exc}") from exc
    finally:
        plt.close(fig)

    buf.seek(0)
    safe_card = card_id.replace("/", "_")
    key = f"{PLOT_PREFIX}/{safe_card}/{window_label}-{int(time.time())}.png"
    log.info("Uploading plot to s3://%s/%s", S3_BUCKET, key)

    put_kwargs = {
        "Bucket": S3_BUCKET,
        "Key": key,
        "Body": buf.getvalue(),
        "ContentType": "image/png",
        "CacheControl": "public, max-age=300",
    }
    if USE_PUBLIC_ACL:
        put_kwargs["ACL"] = "public-read"

    try:
        boto3.client("s3").put_object(**put_kwargs)
    except (BotoCoreError, ClientError) as exc:
        log.exception("S3 upload failed for key=%s", key)
        raise PlotError(f"S3 upload failed: {exc}") from exc

    return f"https://{S3_BUCKET}.s3.amazonaws.com/{key}"
