"""
add_image_urls.py

Looks up a product image on DuckDuckGo Images for every *active* product in
candidate_pool.json and writes the result back as "image_url".

Why it's built this way (rate-limit avoidance)
------------------------------------------------
DuckDuckGo's image endpoint (duckduckgo.com/i.js) isn't a public API - it's
the same endpoint the website itself calls, and it will hand back a 202
"Ratelimit" response if you hit it too fast. Nothing here fully defeats that,
but the combination below is what people report actually works in practice:

  1. Reuse a single DDGS() session instead of creating a new one per request
     (fewer TLS handshakes / vqd token fetches -> fewer requests overall).
  2. Add a randomized delay (jitter) between every request instead of a fixed
     sleep - fixed intervals are easy for anti-bot heuristics to fingerprint.
  3. Exponential backoff with jitter for rate limits, timeouts, and transient HTTP errors such as
     happen, instead of failing immediately or hammering it again right away.
  4. A "cooldown" that kicks in after repeated consecutive failures, so a bad
     patch doesn't just keep escalating retries forever.
  5. Cache results per search query - many rows share the same PRODUCT_NAME
     (different sizes/SKUs of the same item), so we only search once per
     unique name instead of once per row.
  6. Checkpoint the JSON to disk periodically (and skip rows that already
     have an image_url on a re-run), so the script is safe to Ctrl-C and
     resume, and a rate-limit wall partway through doesn't lose progress.
  7. Optional --proxy support (including Tor's socks5://127.0.0.1:9150 via
     the ddgs library) since a single IP is what actually gets rate-limited.

Even with all that, DuckDuckGo can still throttle a busy IP for a while -
that's expected. Re-run the script later (it will skip everything already
done) rather than cranking the delays down to fight through it.

Install:
    pip install ddgs

Usage:
    python add_image_urls.py                          # process up to 900 active products
    python add_image_urls.py --limit 50 --dry-run      # smoke-test without hitting the network
    python add_image_urls.py --force                   # re-check items that already have image_url
    python add_image_urls.py --proxy socks5://127.0.0.1:9150
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

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
DEFAULT_LIMIT = 900          # "only do for ~900 active products"
DEFAULT_MIN_DELAY = 4.0      # seconds, base delay between requests
DEFAULT_MAX_DELAY = 9.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_CHECKPOINT_EVERY = 15
DEFAULT_COOLDOWN_AFTER = 4     # consecutive failures before a long cooldown
DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_ABORT_AFTER_COOLDOWNS = 4  # give up gracefully after this many cooldowns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("add_image_urls")


def build_query(product: dict[str, Any]) -> str:
    """Build a search query from the product's brand + name."""
    brand = (product.get("BRAND") or "").strip()
    name = (product.get("PRODUCT_NAME") or "").strip()
    query = f"{brand} {name}".strip() if brand and brand.lower() not in name.lower() else name
    # Collapse whitespace and strip characters that tend to confuse the endpoint.
    query = re.sub(r"\s+", " ", query)
    query = query.replace('"', "")
    return query[:200]  # keep queries reasonably short


def normalize_query(query: str) -> str:
    return query.strip().lower()


def load_pool(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_pool(path: Path, data: list[dict[str, Any]]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)  # atomic-ish swap so a crash mid-write can't corrupt the real file


def search_image(
    ddgs: "DDGS",
    query: str,
    *,
    region: str,
    safesearch: str,
    max_retries: int,
) -> tuple[Optional[str], str]:
    """Return (image_url_or_None, reason).

    Retry transient network errors, including the newer ddgs/httpx
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

        except Exception as e:
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
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max number of active products to process")
    parser.add_argument("--min-delay", type=float, default=DEFAULT_MIN_DELAY)
    parser.add_argument("--max-delay", type=float, default=DEFAULT_MAX_DELAY)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY)
    parser.add_argument("--region", default="us-en")
    parser.add_argument("--safesearch", default="moderate", choices=["on", "moderate", "off"])
    parser.add_argument("--proxy", default=None, help="e.g. socks5://127.0.0.1:9150 for Tor, or an http(s) proxy URL")
    parser.add_argument("--timeout", type=int, default=20,
                        help="Per-request timeout in seconds")
    parser.add_argument("--recreate-after", type=int, default=3,
                        help="Recreate DDGS session after this many consecutive failures")
    parser.add_argument("--force", action="store_true", help="Re-search items that already have an image_url")
    parser.add_argument("--dry-run", action="store_true", help="Skip network calls; useful for testing the pipeline")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    pool = load_pool(in_path)

    active = [p for p in pool if p.get("active")]
    log.info("Loaded %d products total, %d marked active.", len(pool), len(active))

    targets = active if args.force else [p for p in active if not p.get("image_url")]
    if len(targets) < len(active):
        log.info("%d already have image_url - skipping (use --force to redo).", len(active) - len(targets))

    targets = targets[: args.limit]
    log.info("Processing %d active products (limit=%d).", len(targets), args.limit)

    if not targets:
        log.info("Nothing to do.")
        return

    ddgs = None if args.dry_run else make_ddgs(args.proxy, args.timeout)
    query_cache: dict[str, tuple[Optional[str], str]] = {}

    consecutive_failures = 0
    cooldowns_used = 0
    processed = 0

    try:
        for product in targets:
            query = build_query(product)
            cache_key = normalize_query(query)

            if cache_key in query_cache:
                url, reason = query_cache[cache_key]
                log.info("[cache] %s -> %s", query, reason if not url else url)
            elif args.dry_run:
                url, reason = None, "dry_run"
                log.info("[dry-run] would search: %s", query)
            else:
                url, reason = search_image(
                    ddgs,
                    query,
                    region=args.region,
                    safesearch=args.safesearch,
                    max_retries=args.max_retries,
                )
                query_cache[cache_key] = (url, reason)

            product["image_url"] = url or ""
            product["_image_search_query"] = query
            product["_image_search_reason"] = reason
            processed += 1

            if url:
                consecutive_failures = 0
                log.info("(%d/%d) %s -> %s", processed, len(targets), query, url)
            else:
                consecutive_failures += 1
                log.info("(%d/%d) %s -> [%s]", processed, len(targets), query, reason)

            if (not args.dry_run
                    and consecutive_failures >= args.recreate_after
                    and consecutive_failures < DEFAULT_COOLDOWN_AFTER):
                log.warning(
                    "%d consecutive failures - recreating DuckDuckGo session.",
                    consecutive_failures,
                )
                try:
                    ddgs = make_ddgs(args.proxy, args.timeout)
                except Exception as e:
                    log.warning("Could not recreate DDGS session: %s", e)

            if not args.dry_run and consecutive_failures >= DEFAULT_COOLDOWN_AFTER:
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

            if not args.dry_run:
                time.sleep(random.uniform(args.min_delay, args.max_delay))

    finally:
        save_pool(out_path, pool)
        log.info("Final save complete -> %s", out_path)

    found = sum(1 for p in targets if p.get("image_url"))
    log.info("Done. %d/%d products got an image_url.", found, len(targets))


if __name__ == "__main__":
    main()