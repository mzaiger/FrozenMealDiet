"""
UpdateImageUrlsFromWalmartUrl.py

Re-does "image_url" for every product in candidate_pool.json: searches
DuckDuckGo Images with the product's WALMART URL as the search query and
takes the TOP result that has an image URL -- whoever hosts it. (Unlike
AddImageUrl.py, which searches on the brand + product name, and only for
active items that don't have an image yet.)

What it does to each product
----------------------------
  - Query = the product's PRODUCT_URL with the "?fulfillmentIntent=Pickup"
    (or any other) query string and #fragment removed, e.g.
        https://www.walmart.com/ip/Banquet-Chicken-Pot-Pie-7-oz/10450841
  - Every product that has a PRODUCT_URL is processed, active or not
    (--only-active limits it to active ones).
  - The top image result overwrites "image_url". If the search finds
    nothing, or fails, the product's existing image_url is left alone --
    this script never blanks an image.
  - Writes "_image_search_query" (the URL searched), "_image_search_reason"
    ("ok" / "no_results"), and "_image_walmart_url_checked_at" (UTC
    timestamp). That last one is what makes it resumable: a re-run skips
    every product that already has it (use --force to redo them all).
    It's only set when the search gave a definite answer -- a rate limit
    or network error leaves the product unmarked, so the next run retries it.

Rate-limit avoidance (same approach as AddImageUrl.py)
------------------------------------------------------
DuckDuckGo's image endpoint isn't a public API, and it hands back a
"Ratelimit" response if you hit it too fast. Nothing fully defeats that,
but this is what works in practice: one reused DDGS() session, a
randomized delay between requests, exponential backoff with jitter on
rate limits/timeouts/transient errors, a long cooldown after repeated
consecutive failures (and a graceful stop if it keeps happening),
per-URL result caching, periodic checkpointing of the JSON so it's safe to
Ctrl-C and resume, and optional --proxy support. DuckDuckGo can still
throttle a busy IP for a while -- re-run later (it skips everything already
done) rather than shortening the delays. ~1,800 products at the default
4-9s delay is roughly 3 hours.

Updating only certain products
------------------------------
  --skus-file skus.txt   only the SKUs listed in the file (one per line)
  --sku 123 456 789      only these SKUs, typed on the command line
  --source Kroger        only rows added by the Kroger scripts (SOURCE column)
  --list-skus            print the SKUs that selection matches, then exit
                         (add --list-output skus.txt to save them as a file)
Products picked this way are always redone, even if already marked done.
See UpdateImageUrlsFromWalmartUrl.md for a step-by-step.

Install:
    pip install ddgs

Usage:
    python UpdateImageUrlsFromWalmartUrl.py                      # every product with a PRODUCT_URL
    python UpdateImageUrlsFromWalmartUrl.py --limit 25            # small trial run
    python UpdateImageUrlsFromWalmartUrl.py --dry-run --limit 5   # show the queries, no network
    python UpdateImageUrlsFromWalmartUrl.py --force               # redo products already done
    python UpdateImageUrlsFromWalmartUrl.py --only-active
    python UpdateImageUrlsFromWalmartUrl.py --skus-file skus.txt --dry-run   # only the SKUs in skus.txt
    python UpdateImageUrlsFromWalmartUrl.py --list-skus --source Kroger --list-output skus.txt
    python UpdateImageUrlsFromWalmartUrl.py --proxy socks5://127.0.0.1:9150
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
except ImportError:  # pragma: no cover - fallback for the older, renamed package
    try:
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import (
            DuckDuckGoSearchException as DDGSException,
            RatelimitException,
            TimeoutException,
        )
    except ImportError:
        print("Missing dependency. Run: pip install ddgs", file=sys.stderr)
        raise

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------
DEFAULT_INPUT = "candidate_pool.json"
DEFAULT_OUTPUT = "candidate_pool.json"
DEFAULT_MIN_DELAY = 4.0      # seconds, base delay between requests
DEFAULT_MAX_DELAY = 9.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_CHECKPOINT_EVERY = 15
DEFAULT_COOLDOWN_AFTER = 4         # consecutive failures before a long cooldown
DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_ABORT_AFTER_COOLDOWNS = 4  # give up gracefully after this many cooldowns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("update_image_urls_from_walmart_url")


def walmart_query(product: dict[str, Any]) -> str:
    """The product's Walmart URL, minus any query string / fragment. Empty
    string if the product has no usable URL."""
    url = (product.get("PRODUCT_URL") or "").strip()
    if not url.lower().startswith("http"):
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def read_skus_file(path: str) -> list[str]:
    """SKUs from a text/CSV file: one per line (for a CSV, the first column).
    Blank lines, lines starting with #, and a header line like "sku" are
    ignored."""
    skus: list[str] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            first = line.split(",")[0].strip().strip("\"'")
            if first.isdigit():
                skus.append(first)
    return skus


def load_pool(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_pool(path: Path, data: list[dict[str, Any]]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)  # atomic swap so a crash mid-write can't corrupt the real file


def search_image(
    ddgs: "DDGS",
    query: str,
    *,
    region: str,
    safesearch: str,
    max_retries: int,
) -> tuple[Optional[str], str]:
    """Return (image_url_or_None, reason) -- the top result that has an
    image URL, from any site. Reason is "ok", "no_results" (a definite
    answer), or "error: ..." / "retry_exhausted" (transient -- worth
    trying again on a later run).

    Retries transient network errors, including the newer ddgs/httpx
    'malformed headers' failure that may not be reported as a rate limit.
    """
    backoff = 5.0

    for attempt in range(1, max_retries + 1):
        try:
            results = ddgs.images(
                query,
                region=region,
                safesearch=safesearch,
                max_results=5,
            )

            for r in results:
                url = r.get("image")
                if url and url.startswith("http"):
                    return url, "ok"

            return None, "no_results"

        except RatelimitException:
            wait = min(backoff + random.uniform(0, backoff * 0.5), 120.0)
            log.warning(
                "Ratelimit on %r (attempt %d/%d) - backing off %.1fs",
                query, attempt, max_retries, wait,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, 90)

        except TimeoutException:
            wait = min(backoff + random.uniform(0, 2), 90.0)
            log.warning(
                "Timeout on %r (attempt %d/%d) - retrying in %.1fs",
                query, attempt, max_retries, wait,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, 60)

        except DDGSException as e:
            msg = str(e)
            retryable = any(marker in msg.lower() for marker in (
                "malformed headers",
                "error sending request",
                "connection",
                "connecterror",
                "readerror",
                "remoteprotocolerror",
                "temporarily unavailable",
                "502", "503", "504",
            ))

            if retryable and attempt < max_retries:
                wait = min(backoff + random.uniform(0, backoff * 0.5), 120.0)
                log.warning(
                    "Retryable DDGS error on %r (attempt %d/%d): %s - "
                    "retrying in %.1fs",
                    query, attempt, max_retries, e, wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 90)
                continue

            log.warning(
                "DDGS error on %r (attempt %d/%d): %s",
                query, attempt, max_retries, e,
            )
            return None, f"error: {e}"

        except Exception as e:  # noqa: BLE001
            msg = str(e)
            retryable = any(marker in msg.lower() for marker in (
                "malformed headers",
                "error sending request",
                "connection",
                "connecterror",
                "readerror",
                "remoteprotocolerror",
                "timeout",
            ))

            if retryable and attempt < max_retries:
                wait = min(backoff + random.uniform(0, 2), 90.0)
                log.warning(
                    "Retryable network error on %r (attempt %d/%d): %s - "
                    "retrying in %.1fs",
                    query, attempt, max_retries, e, wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue

            log.warning(
                "Unexpected error on %r (attempt %d/%d): %s",
                query, attempt, max_retries, e,
            )
            return None, f"error: {e}"

    return None, "retry_exhausted"


def make_ddgs(proxy: Optional[str], timeout: int) -> "DDGS":
    """Create a fresh DDGS client/session."""
    return DDGS(proxy=proxy, timeout=timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Path to candidate_pool.json (input)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Path to write results (default: same as input)")
    parser.add_argument("--limit", type=int, default=0, help="Max number of products to process this run (0 = no limit)")
    parser.add_argument("--only-active", action="store_true", help="Only products marked active (default: every product with a PRODUCT_URL)")
    parser.add_argument("--skus-file", default=None,
                        help="Only update these SKUs: a text/CSV file with one SKU per line")
    parser.add_argument("--sku", nargs="+", default=None, metavar="SKU",
                        help="Only update these SKUs, given right on the command line (space-separated)")
    parser.add_argument("--source", default=None,
                        help='Only update products whose SOURCE column equals this, e.g. "Kroger" '
                             "(the products kroger_new_items.py / kroger_add_skipped_items.py added)")
    parser.add_argument("--list-skus", action="store_true",
                        help="Just print the SKUs the --source / --skus-file / --sku selection matches, "
                             "one per line, then exit (no network, nothing changed)")
    parser.add_argument("--list-output", default=None,
                        help="With --list-skus: also write the list to this file (usable as --skus-file)")
    parser.add_argument("--min-delay", type=float, default=DEFAULT_MIN_DELAY)
    parser.add_argument("--max-delay", type=float, default=DEFAULT_MAX_DELAY)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY)
    parser.add_argument("--region", default="us-en")
    parser.add_argument("--safesearch", default="moderate", choices=["on", "moderate", "off"])
    parser.add_argument("--proxy", default=None, help="e.g. socks5://127.0.0.1:9150 for Tor, or an http(s) proxy URL")
    parser.add_argument("--timeout", type=int, default=20, help="Per-request timeout in seconds")
    parser.add_argument("--recreate-after", type=int, default=3,
                        help="Recreate DDGS session after this many consecutive failures")
    parser.add_argument("--force", action="store_true", help="Redo products already done by this script")
    parser.add_argument("--dry-run", action="store_true", help="Skip network calls and don't change the file")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    pool = load_pool(in_path)

    # Optional narrowing to specific products. When any of --sku / --skus-file /
    # --source is given, the products they pick are ALWAYS processed, even if
    # an earlier run already marked them done (no --force needed).
    wanted: set[str] = set()
    if args.skus_file:
        wanted |= set(read_skus_file(args.skus_file))
    if args.sku:
        wanted |= {str(x).strip() for x in args.sku}
    targeted = bool(wanted) or bool(args.source)

    selected = pool
    if wanted:
        selected = [p for p in selected if str(p.get("SKU", "")).strip() in wanted]
        in_pool = {str(p.get("SKU", "")).strip() for p in pool}
        missing = sorted(wanted - in_pool)
        if missing:
            log.warning("%d requested SKU(s) are not in the pool at all: %s", len(missing), ", ".join(missing))
    if args.source:
        selected = [p for p in selected if str(p.get("SOURCE") or "").strip().lower() == args.source.strip().lower()]

    if args.list_skus:
        listed = list(dict.fromkeys(str(p.get("SKU", "")).strip() for p in selected))
        for sku in listed:
            print(sku)
        if args.list_output:
            with open(args.list_output, "w", encoding="utf-8") as f:
                f.write("\n".join(listed) + "\n")
            log.info("Wrote %d SKU(s) to %s", len(listed), args.list_output)
        log.info("%d SKU(s) matched.", len(listed))
        return

    candidates = [p for p in selected if walmart_query(p)]
    log.info("Loaded %d products; %d selected, %d of those have a PRODUCT_URL.", len(pool), len(selected), len(candidates))
    if targeted and len(candidates) < len(selected):
        log.warning("%d selected product(s) have no PRODUCT_URL and can't be searched.", len(selected) - len(candidates))
    if args.only_active:
        candidates = [p for p in candidates if p.get("active")]
        log.info("%d of those are active (--only-active).", len(candidates))

    if args.force or targeted:
        targets = candidates
    else:
        targets = [p for p in candidates if not p.get("_image_walmart_url_checked_at")]
        if len(targets) < len(candidates):
            log.info("%d already done - skipping (use --force to redo).", len(candidates) - len(targets))

    if args.limit > 0:
        targets = targets[: args.limit]
    log.info("Processing %d product(s).", len(targets))

    if not targets:
        log.info("Nothing to do.")
        return

    ddgs = None if args.dry_run else make_ddgs(args.proxy, args.timeout)
    query_cache: dict[str, tuple[Optional[str], str]] = {}

    consecutive_failures = 0
    cooldowns_used = 0
    processed = 0
    updated = 0
    unchanged = 0
    kept_old = 0   # search found nothing / failed, so the existing image_url was left alone

    try:
        for product in targets:
            query = walmart_query(product)

            if query in query_cache:
                url, reason = query_cache[query]
                log.info("[cache] %s -> %s", query, reason if not url else url)
            elif args.dry_run:
                log.info("[dry-run] would search: %s", query)
                processed += 1
                continue
            else:
                url, reason = search_image(
                    ddgs,
                    query,
                    region=args.region,
                    safesearch=args.safesearch,
                    max_retries=args.max_retries,
                )
                query_cache[query] = (url, reason)

            processed += 1
            transient = not url and reason != "no_results"

            if url:
                consecutive_failures = 0
                if product.get("image_url") == url:
                    unchanged += 1
                else:
                    updated += 1
                product["image_url"] = url
                log.info("(%d/%d) %s -> %s", processed, len(targets), query, url)
            else:
                consecutive_failures += 1
                if product.get("image_url"):
                    kept_old += 1
                log.info("(%d/%d) %s -> [%s]%s", processed, len(targets), query, reason,
                         " (kept existing image_url)" if product.get("image_url") else "")

            # A definite answer (found, or DuckDuckGo says no results) marks
            # the product done; a transient failure leaves it unmarked so
            # the next run tries it again.
            product["_image_search_query"] = query
            product["_image_search_reason"] = reason
            if not transient:
                product["_image_walmart_url_checked_at"] = datetime.now(timezone.utc).isoformat()

            if (consecutive_failures >= args.recreate_after
                    and consecutive_failures < DEFAULT_COOLDOWN_AFTER):
                log.warning(
                    "%d consecutive failures - recreating DuckDuckGo session.",
                    consecutive_failures,
                )
                try:
                    ddgs = make_ddgs(args.proxy, args.timeout)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not recreate DDGS session: %s", e)

            if consecutive_failures >= DEFAULT_COOLDOWN_AFTER:
                cooldowns_used += 1
                if cooldowns_used > DEFAULT_ABORT_AFTER_COOLDOWNS:
                    log.error(
                        "Hit %d cooldowns - DuckDuckGo looks fully blocked from this IP right now. "
                        "Saving progress and stopping early. Re-run the script later to resume.",
                        cooldowns_used,
                    )
                    break
                log.warning(
                    "%d consecutive misses - cooling down for %ds before continuing.",
                    consecutive_failures, DEFAULT_COOLDOWN_SECONDS,
                )
                time.sleep(DEFAULT_COOLDOWN_SECONDS)
                consecutive_failures = 0

            if processed % args.checkpoint_every == 0:
                save_pool(out_path, pool)
                log.info("Checkpoint saved (%d/%d).", processed, len(targets))

            time.sleep(random.uniform(args.min_delay, args.max_delay))

    finally:
        if not args.dry_run:
            save_pool(out_path, pool)
            log.info("Final save complete -> %s", out_path)

    log.info(
        "Done. Processed %d: %d image_url(s) updated, %d already the same, "
        "%d had no usable result (existing image_url kept).",
        processed, updated, unchanged, kept_old,
    )


if __name__ == "__main__":
    main()
