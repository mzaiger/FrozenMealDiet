"""
kroger_add_skipped_items.py

Works through kroger_skipped_upcs.json -- the Kroger products that
kroger_new_items.py couldn't match to a Walmart page on its own -- for the
entries where YOU have filled in a "walmart_url", and puts them in
candidate_pool.json. It is kroger_new_items.py's pipeline with the Serper
Walmart lookup replaced by the URL you supplied.

For every entry that has a walmart_url (entries without one are ignored):

  1. The URL is checked to be a walmart.com /ip/<name>/<id> product page.
     The numeric id is the SKU; the URL (query string removed, then
     ?fulfillmentIntent=Pickup added) is the PRODUCT_URL.
  2. If that SKU is already in candidate_pool.json -- on any row, active
     or not -- the product is that row: the Kroger UPC is recorded on it
     ("_kroger_upc", or "_kroger_upc_aliases" if it already has one) and
     nothing is added. (Inactive rows are NOT revived by this script.)
  3. Otherwise the product is looked up in Kroger by its UPC (falling
     back to a name search) at your store, for brand, name, size and
     PRICE -- PRODUCT_NAME, BRAND, PRICE_CURRENT and PRICE_RETAIL all come
     from Kroger, same as kroger_new_items.py.
  4. Image: DuckDuckGo Images, searched with the Walmart URL itself as the
     query, taking the FIRST image URL in the results.
  5. Calories: the LARGEST of USDA FoodData Central, Open Food Facts and
     Gemini (its own knowledge, no search), rounded to the nearest 10.
     servings_per_container: the USDA serving description (same way
     build_meal_pool.py pulls it).
  6. INSTACART_URL is built from the product name (cut at the first
     comma, sizes/numbers/punctuation removed), never searched for.
  7. The new row is appended to candidate_pool.json with active=True
     (you supplied the URL) and SOURCE "Kroger".

A product is only added if it has a Kroger price, an image and at least
one calorie number (--allow-missing-image lets it in without an image,
leaving image_url blank so UpdateImageUrlsFromWalmartUrl.py can fill it
later).

Progress is written back onto each entry in kroger_skipped_upcs.json as
"status" / "detail" / "processed_at". Entries that ended "added" or
"tagged_existing_sku" are finished and skipped on later runs; anything
else (no Kroger price at your store, no image, a network hiccup, ...) is
retried on the next run. The file's keys (the UPCs) are untouched, so
kroger_new_items.py keeps excluding all of them.

This script imports its helpers from kroger_new_items.py, so the two files
must sit in the same folder. Settings (USDA/Open Food Facts/Gemini/DuckDuckGo
timing, the Gemini prompt, Kroger paging and store) come from
kroger_new_items.yaml. Serper is not used, so SERPER_API_KEY isn't needed.

Env vars required (unless --dry-run):
    KROGER_CLIENT_ID, KROGER_CLIENT_SECRET, GEMINI_KEY, USDA_API_KEY
    plus a Kroger store for prices, from the first of: KROGER_LOCATION_ID,
    kroger.location_id in kroger_new_items.yaml, or KROGER_ZIP (nearest store).

Install:
    pip install requests pyyaml ddgs rapidfuzz

Usage:
    python kroger_add_skipped_items.py --dry-run          # offline: how many would be tagged vs added
    python kroger_add_skipped_items.py --limit 5          # small real run
    python kroger_add_skipped_items.py                    # everything with a walmart_url
    python kroger_add_skipped_items.py --allow-missing-image --max-minutes 100   # what the workflow runs
    python kroger_add_skipped_items.py --skipped path/to/kroger_skipped_upcs.json --pool path/to/candidate_pool.json
"""

from __future__ import annotations

import argparse
import os
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import requests

import kroger_new_items as kni

DONE_STATUSES = {"added", "tagged_existing_sku"}


# ---------------------------------------------------------------------------
# Walmart URL + SKU handling
# ---------------------------------------------------------------------------

def parse_walmart_url(raw):
    """Returns (clean_url, sku, name_from_url, None) for a usable
    walmart.com product-page link, or (None, None, None, why_not).
    clean_url has the query string / fragment removed."""
    parts = urlsplit((raw or "").strip())
    host = parts.netloc.lower().split(":")[0]
    if parts.scheme not in ("http", "https") or not (host == "walmart.com" or host.endswith(".walmart.com")):
        return None, None, None, "walmart_url is not a walmart.com link"
    match = kni.WALMART_IP_URL_RE.search(parts.path)
    if not match:
        return None, None, None, "walmart_url is not a walmart.com /ip/<name>/<id> product page"
    slug, sku = match.group(1), match.group(2)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return clean, sku, kni.slug_to_product_name(slug), None


def build_sku_index(pool):
    index = defaultdict(list)
    for item in pool:
        index[str(item.get("SKU", "")).strip()].append(item)
    return index


def pick_row(rows):
    """Which of several pool rows sharing a SKU gets the UPC: an active one
    if there is one, else the first."""
    return next((r for r in rows if r.get("active") is True), rows[0])


# ---------------------------------------------------------------------------
# Kroger lookup by UPC
# ---------------------------------------------------------------------------

def fetch_kroger_product(token, upc, saved_name, cfg, location_id):
    """The raw Kroger product for this UPC at the configured store, or None.
    Tries Kroger's productId filter first (a Kroger productId is the
    13-digit UPC); if that finds nothing, searches by the name saved in the
    skipped file and picks the result with this UPC. Raises
    requests.RequestException on a network/API error."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    timeout = cfg["kroger"]["timeout_seconds"]

    def get(params):
        resp = requests.get(kni.KROGER_PRODUCTS_URL, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("data") or []

    data = get({"filter.productId": upc, "filter.locationId": location_id, "filter.limit": 1})
    if data:
        return data[0]

    words = kni.clean_search_words(saved_name, cut_at_comma=False)[:8]
    if words:
        for product in get({"filter.term": " ".join(words), "filter.locationId": location_id, "filter.limit": 50}):
            if str(product.get("upc") or product.get("productId")) == upc:
                return product
    return None


def why_unusable(product):
    """Why kroger_product_to_candidate() rejected a Kroger product."""
    if not (product.get("categories") and any("frozen" in c.lower() for c in product["categories"])):
        return "not in a frozen category at Kroger"
    price = ((product.get("items") or [{}])[0].get("price")) or {}
    if kni._positive_price(price.get("regular")) is None and kni._positive_price(price.get("promo")) is None:
        return "Kroger has no price for it at your store"
    return "Kroger data is missing its UPC or description"


def resolve_location_id(cfg, token):
    """Same store selection as kroger_new_items.py."""
    location_id = (os.environ.get("KROGER_LOCATION_ID") or str(cfg["kroger"].get("location_id") or "")).strip()
    if location_id:
        return location_id
    kroger_zip = (os.environ.get("KROGER_ZIP") or "").strip()
    radius = cfg["kroger"].get("location_search_radius_miles", 100)
    stores = kni.find_kroger_locations(token, kroger_zip, cfg["kroger"]["timeout_seconds"], limit=1, radius_miles=radius)
    if not stores:
        return None
    kni.log(f"KROGER_ZIP {kroger_zip}: using the nearest store, {stores[0][0]} ({stores[0][1]}) for prices.")
    return stores[0][0]


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------

def process_entry(upc, entry, ctx):
    """Returns (status, detail). status is one of: added, tagged_existing_sku
    (both finished) or bad_walmart_url, kroger_error, kroger_not_found,
    kroger_unusable, no_image, no_calories (retried next run)."""
    cfg = ctx.cfg
    clean_url, sku, url_name, bad = parse_walmart_url(entry.get("walmart_url"))
    if bad:
        return "bad_walmart_url", bad

    # SKU already in the pool -> this product is that row.
    rows = ctx.sku_index.get(sku)
    if rows:
        row = pick_row(rows)
        kni.tag_pool_item_with_upc(row, upc)
        ctx.pool_dirty = True
        return "tagged_existing_sku", (f"SKU {sku} already in the pool (index {row.get('index')}, "
                                       f"active={row.get('active')}): {row.get('PRODUCT_NAME')!r}; UPC recorded on it")

    if ctx.dry_run:
        return "would_add", f"SKU {sku} is new -> would be looked up in Kroger and added"

    # Kroger: brand, name, size, price.
    try:
        product = fetch_kroger_product(ctx.token, upc, entry.get("name", ""), cfg, ctx.location_id)
    except requests.RequestException as e:
        return "kroger_error", f"Kroger lookup failed: {e}"
    if product is None:
        return "kroger_not_found", "Kroger returned nothing for this UPC at your store"
    candidate = kni.kroger_product_to_candidate(product)
    if candidate is None:
        return "kroger_unusable", why_unusable(product)

    walmart = {
        "product_url": clean_url, "sku": sku, "product_name": url_name,
        "query": "manual: walmart_url from kroger_skipped_upcs.json",
        "reason": "manual_url", "top_result_url": clean_url, "name_match_score": None,
    }

    # Image: DuckDuckGo, the Walmart URL as the query, first image in the list.
    image_query = clean_url
    image_url, image_reason = kni.search_image(ctx.ddgs, image_query, cfg)
    time.sleep(random.uniform(cfg["ddg_image"]["min_delay_seconds"], cfg["ddg_image"]["max_delay_seconds"]))
    if not image_url and not ctx.allow_missing_image:
        return "no_image", f"no image from DuckDuckGo for {clean_url} ({image_reason})"
    image_url = image_url or ""

    # Calories (largest of USDA / Open Food Facts / Gemini) + USDA serving.
    usda = kni.fetch_usda_info(candidate["description"], ctx.usda_key, cfg)
    time.sleep(cfg["usda"]["min_call_interval_seconds"])
    off_cal, off_is_100g = kni.fetch_off_calories(candidate["description"], cfg)
    time.sleep(cfg["open_food_facts"]["min_call_interval_seconds"])
    gemini_result, gemini_model, gemini_failed = kni.ask_gemini_for_calories(
        candidate, walmart, image_url, image_query, image_reason, ctx.gemini_key, cfg, ctx.rate_limiter)

    calories_raw, calorie_source, calorie_values = kni.pick_max_calories(
        usda["calories"], off_cal, off_is_100g, kni.gemini_calories(gemini_result))
    calories = kni.round_to_nearest_ten(calories_raw) if calories_raw else 0
    if calories <= 0:
        return "no_calories", ("no calorie number from USDA, Open Food Facts, or Gemini"
                               + (" (Gemini call failed)" if gemini_failed else ""))

    ctx.idx += 1
    record = kni.build_pool_record(
        candidate, walmart, image_url, image_query, image_reason,
        calories, calorie_source, calorie_values, usda["servings_per_container"],
        gemini_result, gemini_model, ctx.location_id, ctx.idx, ctx.run_date)
    record["_walmart_url_source"] = "manual (walmart_url in kroger_skipped_upcs.json)"
    ctx.pool.append(record)
    ctx.sku_index[sku].append(record)
    ctx.pool_dirty = True
    return "added", (f"{record['PRODUCT_NAME']} -- SKU {sku}, calories={calories} "
                     f"(max of {calorie_values}, from {calorie_source}), price={record['PRICE_CURRENT']} (Kroger), "
                     f"servings_per_container={record['servings_per_container']}, "
                     + ("image from DuckDuckGo" if image_url else "NO image found -- image_url left blank"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(kni.DEFAULT_CONFIG_PATH))
    parser.add_argument("--skipped", default=None, help="Path to kroger_skipped_upcs.json (default: from the yaml)")
    parser.add_argument("--pool", default=None, help="Path to candidate_pool.json (default: from the yaml)")
    parser.add_argument("--limit", type=int, default=0, help="Process at most this many entries this run (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Offline: report which entries would be tagged onto an existing SKU vs added; write nothing")
    parser.add_argument("--allow-missing-image", action="store_true",
                        help="Add the product even if DuckDuckGo finds no image (image_url left blank)")
    parser.add_argument("--max-minutes", type=float, default=0,
                        help="Stop starting new entries after this many minutes (0 = no limit); "
                             "progress so far is already saved, and the rest is picked up next run")
    args = parser.parse_args()

    cfg = kni.load_config(args.config)
    pool_path = args.pool or str(kni._SCRIPT_DIR / cfg["pool"]["path"])
    skipped_path = args.skipped or str(kni._SCRIPT_DIR / cfg["pool"]["skipped_upcs_path"])

    pool = kni.load_pool(pool_path)
    skipped = kni.load_skipped_upcs(skipped_path)
    todo = [
        (upc, entry) for upc, entry in skipped.items()
        if str(entry.get("walmart_url") or "").strip() and entry.get("status") not in DONE_STATUSES
    ]
    kni.log(f"Loaded {len(pool)} pool item(s) and {len(skipped)} skipped UPC(s): "
            f"{sum(1 for e in skipped.values() if str(e.get('walmart_url') or '').strip())} have a walmart_url, "
            f"{sum(1 for e in skipped.values() if e.get('status') in DONE_STATUSES)} already done, "
            f"{len(todo)} to process.")
    if args.limit > 0:
        todo = todo[: args.limit]
    if not todo:
        kni.log("Nothing to do.")
        return

    ctx = SimpleNamespace(
        cfg=cfg, pool=pool, sku_index=build_sku_index(pool), pool_dirty=False, dry_run=args.dry_run,
        allow_missing_image=args.allow_missing_image, idx=kni.next_index(pool),
        run_date=datetime.now(timezone.utc).isoformat(),
        token=None, location_id=None, ddgs=None, rate_limiter=None,
        gemini_key=os.environ.get("GEMINI_KEY"), usda_key=os.environ.get("USDA_API_KEY"),
    )

    if not args.dry_run:
        kroger_id, kroger_secret = os.environ.get("KROGER_CLIENT_ID"), os.environ.get("KROGER_CLIENT_SECRET")
        missing = [n for n, v in (("KROGER_CLIENT_ID", kroger_id), ("KROGER_CLIENT_SECRET", kroger_secret),
                                  ("GEMINI_KEY", ctx.gemini_key), ("USDA_API_KEY", ctx.usda_key)) if not v]
        has_store = os.environ.get("KROGER_LOCATION_ID") or cfg["kroger"].get("location_id") or os.environ.get("KROGER_ZIP")
        if not has_store:
            missing.append("KROGER_ZIP (or KROGER_LOCATION_ID / kroger.location_id in kroger_new_items.yaml)")
        if missing:
            kni.log(f"Missing {', '.join(missing)} -- nothing done.")
            return
        ctx.token = kni.get_kroger_token(kroger_id, kroger_secret, cfg["kroger"]["timeout_seconds"])
        ctx.location_id = resolve_location_id(cfg, ctx.token)
        if not ctx.location_id:
            kni.log("No Kroger store found for KROGER_ZIP -- nothing done.")
            return
        ctx.ddgs = kni.DDGS()
        ctx.rate_limiter = kni._RateLimiter(cfg["gemini"]["min_call_interval_seconds"])

    outcomes = Counter()
    problems = []
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else None
    for n, (upc, entry) in enumerate(todo, start=1):
        if deadline is not None and time.monotonic() >= deadline:
            kni.log(f"Stopping: reached --max-minutes ({args.max_minutes:g}); "
                    f"{len(todo) - n + 1} entr(ies) left for the next run.")
            break
        label = f"[{n}/{len(todo)}] {upc} {entry.get('name', '')}"
        try:
            status, detail = process_entry(upc, entry, ctx)
        except Exception as e:  # noqa: BLE001 -- one bad entry shouldn't kill the run
            status, detail = "error", f"{type(e).__name__}: {e}"
        outcomes[status] += 1
        kni.log(f"  {label} -> {status}: {detail}")
        if status not in DONE_STATUSES and status != "would_add":
            problems.append((upc, entry.get("name", ""), status, detail))

        if args.dry_run:
            continue
        entry["status"], entry["detail"], entry["processed_at"] = status, detail, ctx.run_date
        kni.save_skipped_upcs(skipped_path, skipped)
        if ctx.pool_dirty and status in DONE_STATUSES:
            kni.save_pool(pool_path, ctx.pool)
            ctx.pool_dirty = False

    if not args.dry_run and ctx.pool_dirty:
        kni.save_pool(pool_path, ctx.pool)

    kni.log("Done. " + ", ".join(f"{v} {k}" for k, v in sorted(outcomes.items())) + f". Pool size now {len(ctx.pool)}.")
    if problems:
        kni.log("Not finished (they will be retried on the next run unless you fix the walmart_url):")
        for upc, name, status, detail in problems:
            kni.log(f"    {upc} {name[:50]} -- {status}: {detail}")


if __name__ == "__main__":
    main()
