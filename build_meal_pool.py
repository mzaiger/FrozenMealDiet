#!/usr/bin/env python3
"""
build_meal_pool.py

Pulls a fresh pool of frozen-meal candidates from Kroger's Products API,
splits them into a "breakfast" pool and a "general" pool, then looks up
calories for each item.

Calorie lookup order:
  1. USDA FoodData Central by barcode/UPC variants
  2. Open Food Facts by barcode
  3. USDA FoodData Central by brand/name text search (approximate)

Writes candidate_pool.json, which the static front-end uses to build a
7-day / 21-meal plan in the browser.

Required environment variables:
  KROGER_CLIENT_ID
  KROGER_CLIENT_SECRET
  USDA_API_KEY

Optional:
  KROGER_ZIP
  PRODUCTS_PER_TERM
  REQUEST_PAUSE_SECONDS
  OFF_REQUEST_PAUSE_SECONDS
  USE_OPEN_FOOD_FACTS
  USER_AGENT
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

KROGER_ZIP = os.environ.get("KROGER_ZIP", "68508").strip() or "68508"

KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID", "").strip()
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET", "").strip()
USDA_API_KEY = os.environ.get("USDA_API_KEY", "").strip()

KROGER_BASE = "https://api.kroger.com/v1"
USDA_BASE = "https://api.nal.usda.gov/fdc/v1"

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "freezer-week-planner/1.0 (contact: github-actions@users.noreply.github.com)",
).strip()

USE_OPEN_FOOD_FACTS = (
    os.environ.get("USE_OPEN_FOOD_FACTS", "1").strip().lower()
    not in ("0", "false", "no", "off")
)


def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


PRODUCTS_PER_TERM = env_int("PRODUCTS_PER_TERM", 20)
REQUEST_PAUSE_SECONDS = env_float("REQUEST_PAUSE_SECONDS", 0.6)
OFF_REQUEST_PAUSE_SECONDS = env_float("OFF_REQUEST_PAUSE_SECONDS", 1.0)

DEBUG_BUDGET = 8

FORCE_DEBUG_TERMS = {
    "lean cuisine",
    "stouffer's",
    "healthy choice frozen",
}

BREAKFAST_TERMS = [
    "frozen breakfast",
    "frozen breakfast sandwich",
    "frozen breakfast bowl",
    "frozen pancakes breakfast",
    "frozen breakfast burrito",
]

GENERAL_TERMS = [
    "frozen meal",
    "frozen dinner",
    "frozen entree",
    "lean cuisine",
    "stouffer's",
    "healthy choice frozen",
    "frozen bowl meal",
]

# Optional manual override for barcodes that free APIs do not have.
# Format:
#   "0000000000000": (calories, "manual")
MANUAL_CALORIES = {}


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def http_json(url, data=None, headers=None, method=None):
    """
    Minimal JSON HTTP helper using only stdlib.
    """
    headers = headers or {}

    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = urllib.parse.urlencode(data).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean_upc(value):
    """
    Keep only digits.
    """
    return re.sub(r"\D", "", str(value or ""))


def clean_text(value):
    """
    Normalize text for search queries.
    """
    if value is None:
        return ""

    value = str(value)
    value = re.sub(r"[®™©]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def to_float(value):
    """
    Safe float conversion.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(seq):
    """
    De-dupe a list while preserving order.
    """
    out = []
    for item in seq:
        if item and item not in out:
            out.append(item)
    return out


# ----------------------------------------------------------------------
# Kroger API
# ----------------------------------------------------------------------

def get_kroger_token():
    if not KROGER_CLIENT_ID or not KROGER_CLIENT_SECRET:
        sys.exit(
            "Missing KROGER_CLIENT_ID / KROGER_CLIENT_SECRET environment variables."
        )

    creds = base64.b64encode(
        f"{KROGER_CLIENT_ID}:{KROGER_CLIENT_SECRET}".encode("utf-8")
    ).decode("utf-8")

    resp = http_json(
        f"{KROGER_BASE}/connect/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "scope": "product.compact",
        },
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    token = resp.get("access_token")
    if not token:
        sys.exit(f"Kroger auth did not return access_token: {resp}")

    return token


def find_location_id(token, zip_code):
    url = (
        f"{KROGER_BASE}/locations"
        f"?filter.zipCode.near={zip_code}&filter.limit=1"
    )

    resp = http_json(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )

    locations = resp.get("data", [])
    if not locations:
        sys.exit(f"No Kroger store found near zip {zip_code}.")

    location_id = locations[0].get("locationId")
    if not location_id:
        sys.exit(f"Kroger location had no locationId: {locations[0]}")

    return location_id


def search_products(token, location_id, term, limit=PRODUCTS_PER_TERM):
    term = clean_text(term)

    params = urllib.parse.urlencode(
        {
            "filter.term": term,
            "filter.locationId": location_id,
            "filter.fulfillment": "csp",
            "filter.limit": limit,
        }
    )

    url = f"{KROGER_BASE}/products?{params}"

    try:
        resp = http_json(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
    except urllib.error.HTTPError as e:
        print(
            f"  Kroger search failed for '{term}': HTTP {e.code} {e.reason}",
            file=sys.stderr,
        )
        return []
    except urllib.error.URLError as e:
        print(
            f"  Kroger search failed for '{term}': {e.reason}",
            file=sys.stderr,
        )
        return []
    except Exception as e:
        print(
            f"  Kroger search failed for '{term}': {e!r}",
            file=sys.stderr,
        )
        return []

    return resp.get("data", [])


def extract_price(product):
    items = product.get("items") or []
    if not items:
        return None

    price = items[0].get("price") or {}

    raw = price.get("promo")
    if raw is None:
        raw = price.get("regular")

    return to_float(raw)


def extract_image(product):
    images = product.get("images") or []

    for img in images:
        if img.get("size") == "medium" and img.get("url"):
            return img["url"]

    for img in images:
        if img.get("url"):
            return img["url"]

    return None


# ----------------------------------------------------------------------
# Barcode / UPC normalization
# ----------------------------------------------------------------------

def upc_variants(upc):
    """
    Generate plausible barcode variants.

    Kroger UPCs and USDA/Open Food Facts barcodes may differ by leading
    zero padding or UPC-A vs EAN-13 representation.
    """
    upc = clean_upc(upc)
    if not upc:
        return []

    variants = [upc]

    # Strip leading zero padding one digit at a time until we reach
    # a normal 12-digit UPC-A length.
    v = upc
    while len(v) > 12 and v.startswith("0"):
        v = v[1:]
        variants.append(v)

    # Cross-convert common 12/13/14 forms.
    for v in list(variants):
        if len(v) == 12:
            variants.append("0" + v)

        if len(v) == 13 and v.startswith("0"):
            variants.append(v[1:])

        if len(v) == 13:
            variants.append("0" + v)

        if len(v) <= 14:
            variants.append(v.zfill(14))

    return _dedupe(variants)


def usda_barcode_candidates(upc):
    """
    Barcode candidates for USDA UPC search.

    Limited to two requests per product to avoid spending the whole run
    on padded variants that usually return zero.
    """
    variants = upc_variants(upc)

    preferred = (
        [v for v in variants if len(v) == 13]
        + [v for v in variants if len(v) == 12]
        + [v for v in variants if len(v) == 8]
        + [v for v in variants if len(v) == 14]
    )

    return _dedupe(preferred)[:2]


def off_candidates(upc):
    """
    Barcode candidates for Open Food Facts.

    OFF works best with 13-digit and 12-digit codes.
    """
    variants = upc_variants(upc)

    preferred = (
        [v for v in variants if len(v) == 13]
        + [v for v in variants if len(v) == 12]
        + [v for v in variants if len(v) == 8]
    )

    return _dedupe(preferred)[:2]


# ----------------------------------------------------------------------
# USDA helpers
# ----------------------------------------------------------------------

def usda_search(query, page_size=5):
    query = clean_text(query)

    if not query or not USDA_API_KEY:
        return {"foods": []}

    params = urllib.parse.urlencode(
        {
            "api_key": USDA_API_KEY,
            "query": query,
            "dataType": "Branded",
            "pageSize": page_size,
        }
    )

    url = f"{USDA_BASE}/foods/search?{params}"
    return http_json(url)


def usda_calories_from_food(food):
    """
    Extract calories from a USDA branded-food result.

    Prefers labelNutrients.calories.value, then falls back to
    foodNutrients entries where nutrient id 1008 is calories.
    """
    label = food.get("labelNutrients") or {}
    cal_raw = (label.get("calories") or {}).get("value")
    cal = to_float(cal_raw)

    if cal and cal > 0:
        return cal

    for nutrient in food.get("foodNutrients") or []:
        nutrient_info = nutrient.get("nutrient") or {}

        nutrient_ids = [
            nutrient_info.get("id"),
            nutrient.get("nutrientId"),
            nutrient.get("nutrientNumber"),
        ]

        if 1008 in nutrient_ids:
            cal = to_float(nutrient.get("value"))
            if cal and cal > 0:
                return cal

    return None


def best_calories_from_foods(foods, brand=None, name=None):
    """
    Pick the best calorie value from USDA search results.

    This is used for approximate matching, so it tries to prefer results
    whose description contains the brand/name words.
    """
    if not foods:
        return None

    brand_clean = clean_text(brand).lower()
    name_clean = clean_text(name).lower()
    name_words = set(re.findall(r"[a-z0-9]+", name_clean)) if name_clean else set()

    best_cal = None
    best_score = -1

    for food in foods:
        cal = usda_calories_from_food(food)
        if not cal:
            continue

        desc = clean_text(food.get("description")).lower()
        desc_words = set(re.findall(r"[a-z0-9]+", desc))

        score = 0

        if brand_clean and brand_clean in desc:
            score += 3

        if name_words:
            score += len(name_words & desc_words) / len(name_words)

        if score > best_score:
            best_score = score
            best_cal = round(cal)

    return best_cal


def usda_name_queries(brand, name):
    """
    Build a few reasonable USDA text-search queries from Kroger brand/name.
    """
    brand = clean_text(brand)
    name = clean_text(name)

    queries = []

    def add(query):
        query = clean_text(query)
        if query and query not in queries:
            queries.append(query)

    if brand and name:
        add(f"{brand} {name}")

    if name:
        add(name)

    # Try a shorter query without generic frozen-meal words.
    if name:
        strip_words = [
            "frozen",
            "meal",
            "meals",
            "dinner",
            "dinners",
            "entree",
            "entrees",
            "bowl",
            "bowls",
            "product",
            "net",
            "wt",
            "oz",
        ]

        pattern = r"\b(?:" + "|".join(strip_words) + r")\b"
        short_name = re.sub(pattern, " ", name, flags=re.IGNORECASE)
        short_name = clean_text(short_name)

        if short_name and short_name != name:
            if brand:
                add(f"{brand} {short_name}")
            else:
                add(short_name)

    return queries[:3]


# ----------------------------------------------------------------------
# Open Food Facts helper
# ----------------------------------------------------------------------

def lookup_calories_off(upc, debug=False):
    """
    Fallback nutrition source: Open Food Facts.

    This does direct barcode lookup, but OFF rate-limits aggressive clients.
    We limit candidate count, sleep between requests, and back off on 429.
    """
    if not USE_OPEN_FOOD_FACTS:
        if debug:
            print("    [OFF] disabled via USE_OPEN_FOOD_FACTS", file=sys.stderr)
        return None

    candidates = off_candidates(upc)
    backoff = 2
    rate_limited_count = 0

    for candidate in candidates:
        url = (
            "https://world.openfoodfacts.org/api/v2/product/"
            f"{candidate}.json?fields=code,product_name,nutriments"
        )

        try:
            resp = http_json(
                url,
                headers={"User-Agent": USER_AGENT},
            )

        except urllib.error.HTTPError as e:
            if e.code == 429:
                rate_limited_count += 1

                retry_after = None
                if getattr(e, "headers", None):
                    retry_after = e.headers.get("Retry-After")

                try:
                    sleep_for = int(retry_after)
                except (TypeError, ValueError):
                    sleep_for = backoff

                sleep_for = min(max(sleep_for, backoff), 30)

                if debug:
                    print(
                        f"    [OFF] barcode='{candidate}' -> 429 rate limited; "
                        f"sleeping {sleep_for}s",
                        file=sys.stderr,
                    )

                if rate_limited_count >= 2:
                    if debug:
                        print(
                            "    [OFF] too many rate limits; skipping OFF",
                            file=sys.stderr,
                        )
                    return None

                time.sleep(sleep_for)
                backoff = min(backoff * 2, 30)
                continue

            if e.code == 404:
                if debug:
                    print(
                        f"    [OFF] barcode='{candidate}' -> 404 Not Found",
                        file=sys.stderr,
                    )
                time.sleep(OFF_REQUEST_PAUSE_SECONDS)
                continue

            if debug:
                print(
                    f"    [OFF] barcode='{candidate}' -> "
                    f"HTTPError {e.code}: {e.reason}",
                    file=sys.stderr,
                )

            time.sleep(OFF_REQUEST_PAUSE_SECONDS)
            continue

        except urllib.error.URLError as e:
            if debug:
                print(
                    f"    [OFF] barcode='{candidate}' -> URLError: {e.reason}",
                    file=sys.stderr,
                )
            continue

        except Exception as e:
            if debug:
                print(
                    f"    [OFF] barcode='{candidate}' -> unexpected error: {e!r}",
                    file=sys.stderr,
                )
            continue

        time.sleep(OFF_REQUEST_PAUSE_SECONDS)

        if resp.get("status") != 1:
            continue

        product = resp.get("product", {})
        nutriments = product.get("nutriments", {})

        cal_raw = (
            nutriments.get("energy-kcal_serving")
            or nutriments.get("energy-kcal_value")
        )

        cal = to_float(cal_raw)

        if cal and cal > 0:
            return round(cal)

    return None


# ----------------------------------------------------------------------
# Main calorie lookup
# ----------------------------------------------------------------------

_usda_error_count = 0
_debug_used = 0


def lookup_calories(upc, name=None, brand=None, force_debug=False):
    """
    Look up calories for a UPC.

    Returns:
      (calories, source)

      source may be:
        - "exact"
        - "approximate"
        - "manual"

      Returns (None, None) if nothing was found.
    """
    global _usda_error_count
    global _debug_used

    debug = force_debug

    if not debug and _debug_used < DEBUG_BUDGET:
        _debug_used += 1
        debug = True

    upc = clean_upc(upc)

    if upc in MANUAL_CALORIES:
        manual = MANUAL_CALORIES[upc]

        if isinstance(manual, tuple):
            return manual

        return manual, "manual"

    if not upc:
        return None, None

    if debug:
        print(f"  [debug] ---- {name!r} (upc={upc}) ----", file=sys.stderr)

    # If there is no USDA key, we can still try Open Food Facts.
    if not USDA_API_KEY:
        if debug:
            print("    [USDA] USDA_API_KEY missing; skipping USDA", file=sys.stderr)

        off_cal = lookup_calories_off(upc, debug=debug)
        if off_cal:
            return off_cal, "exact"

        return None, None

    usda_fuzzy_cal = None

    # ------------------------------------------------------------------
    # Tier 1: USDA barcode search.
    # Only call it exact when the returned food's gtinUpc matches.
    # ------------------------------------------------------------------
    for candidate in usda_barcode_candidates(upc):
        params = urllib.parse.urlencode(
            {
                "api_key": USDA_API_KEY,
                "query": candidate,
                "dataType": "Branded",
                "pageSize": 5,
            }
        )

        url = f"{USDA_BASE}/foods/search?{params}"

        try:
            resp = http_json(url)

        except urllib.error.HTTPError as e:
            _usda_error_count += 1

            if e.code == 429:
                time.sleep(2)

            if _usda_error_count <= 3 or debug:
                print(
                    f"    [USDA] query='{candidate}' -> HTTPError: {e}",
                    file=sys.stderr,
                )

                if e.code in (401, 403):
                    print(
                        "    -> looks like an invalid/unauthorized USDA_API_KEY, "
                        "not a bad UPC. Check the secret value.",
                        file=sys.stderr,
                    )

            continue

        except urllib.error.URLError as e:
            if debug:
                print(
                    f"    [USDA] query='{candidate}' -> URLError: {e.reason}",
                    file=sys.stderr,
                )
            continue

        except Exception as e:
            if debug:
                print(
                    f"    [USDA] query='{candidate}' -> unexpected error: {e!r}",
                    file=sys.stderr,
                )
            continue

        time.sleep(REQUEST_PAUSE_SECONDS)

        foods = resp.get("foods", [])

        if debug:
            sample = [
                (f.get("description"), f.get("gtinUpc"))
                for f in foods[:3]
            ]
            print(
                f"    [USDA] query='{candidate}' -> {len(foods)} foods, "
                f"sample: {sample}",
                file=sys.stderr,
            )

        # Exact gtinUpc match.
        for food in foods:
            cal = usda_calories_from_food(food)
            if not cal:
                continue

            food_upc = clean_upc(food.get("gtinUpc"))

            if (
                food_upc
                and candidate.lstrip("0")
                and food_upc.lstrip("0") == candidate.lstrip("0")
            ):
                if debug:
                    print(
                        f"    [USDA] matched by gtinUpc -> {round(cal)} cal",
                        file=sys.stderr,
                    )
                return round(cal), "exact"

        # Save the best fuzzy/barcode-search result as an approximate fallback.
        if usda_fuzzy_cal is None:
            usda_fuzzy_cal = best_calories_from_foods(
                foods,
                brand=brand,
                name=name,
            )

    # ------------------------------------------------------------------
    # Tier 2: Open Food Facts exact barcode lookup.
    # ------------------------------------------------------------------
    off_cal = lookup_calories_off(upc, debug=debug)

    if off_cal:
        if debug:
            print(f"    [OFF] matched -> {off_cal} cal", file=sys.stderr)
        return off_cal, "exact"

    # ------------------------------------------------------------------
    # Tier 2.5: USDA barcode search produced a fuzzy result.
    # It is not an exact gtin match, so mark it approximate.
    # ------------------------------------------------------------------
    if usda_fuzzy_cal:
        if debug:
            print(
                f"    [USDA] fuzzy barcode result -> {usda_fuzzy_cal} cal",
                file=sys.stderr,
            )
        return usda_fuzzy_cal, "approximate"

    # ------------------------------------------------------------------
    # Tier 3: USDA brand/name text search.
    # This is approximate but prevents the pool from becoming empty.
    # ------------------------------------------------------------------
    for query in usda_name_queries(brand, name):
        try:
            resp = usda_search(query, page_size=5)

        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2)

            if debug:
                print(
                    f"    [USDA name-search] query={query!r} -> HTTPError: {e}",
                    file=sys.stderr,
                )

            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        except urllib.error.URLError as e:
            if debug:
                print(
                    f"    [USDA name-search] query={query!r} -> URLError: {e.reason}",
                    file=sys.stderr,
                )
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        except Exception as e:
            if debug:
                print(
                    f"    [USDA name-search] query={query!r} -> "
                    f"unexpected error: {e!r}",
                    file=sys.stderr,
                )
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        time.sleep(REQUEST_PAUSE_SECONDS)

        foods = resp.get("foods", [])
        cal = best_calories_from_foods(foods, brand=brand, name=name)

        if debug:
            print(
                f"    [USDA name-search] query={query!r} -> {len(foods)} foods, "
                f"calories={cal}",
                file=sys.stderr,
            )

        if cal:
            return cal, "approximate"

    if debug:
        print("    -> no calorie match from any source", file=sys.stderr)

    return None, None


# ----------------------------------------------------------------------
# Pool builder
# ----------------------------------------------------------------------

def build_pool(token, location_id, terms):
    seen_upcs = set()
    pool = []

    for term in terms:
        products = search_products(token, location_id, term)

        found = 0
        matched = 0
        skipped_dupe = 0
        no_upc = 0
        forced_this_term = False

        for product in products:
            upc = clean_upc(product.get("upc"))

            if not upc:
                no_upc += 1
                continue

            if upc in seen_upcs:
                skipped_dupe += 1
                continue

            seen_upcs.add(upc)
            found += 1

            force_debug = term in FORCE_DEBUG_TERMS and not forced_this_term
            if force_debug:
                forced_this_term = True

            calories, calorie_source = lookup_calories(
                upc,
                name=product.get("description"),
                brand=product.get("brandName"),
                force_debug=force_debug,
            )

            time.sleep(REQUEST_PAUSE_SECONDS)

            if calories is None:
                continue

            matched += 1

            items = product.get("items") or [{}]
            first_item = items[0] if items else {}

            pool.append(
                {
                    "upc": upc,
                    "name": product.get("description"),
                    "brand": product.get("brandName"),
                    "size": first_item.get("size"),
                    "price": extract_price(product),
                    "image": extract_image(product),
                    "calories": calories,
                    "calorie_source": calorie_source,
                }
            )

        print(
            f"  '{term}': {len(products)} from Kroger, {found} new/unique, "
            f"{matched} with a calorie match, {no_upc} missing upc, "
            f"{skipped_dupe} dupes"
        )

    return pool


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    if not USDA_API_KEY:
        print(
            "WARNING: USDA_API_KEY is not set. USDA lookups will be skipped. "
            "Open Food Facts may still provide some results, but the pool may "
            "be very small.",
            file=sys.stderr,
        )

    if not KROGER_CLIENT_ID or not KROGER_CLIENT_SECRET:
        sys.exit(
            "Missing KROGER_CLIENT_ID / KROGER_CLIENT_SECRET environment variables."
        )

    print("Authenticating with Kroger...")
    token = get_kroger_token()

    print(f"Finding store near {KROGER_ZIP}...")
    location_id = find_location_id(token, KROGER_ZIP)
    print(f"  using locationId {location_id}")

    print("Building breakfast pool...")
    breakfast_pool = build_pool(token, location_id, BREAKFAST_TERMS)

    print("Building general lunch/dinner pool...")
    general_pool = build_pool(token, location_id, GENERAL_TERMS)

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "location_id": location_id,
        "zip": KROGER_ZIP,
        "breakfast": breakfast_pool,
        "general": general_pool,
    }

    with open("candidate_pool.json", "w") as f:
        json.dump(output, f, indent=2)

    print(
        f"Wrote candidate_pool.json: {len(breakfast_pool)} breakfast items, "
        f"{len(general_pool)} general items."
    )


if __name__ == "__main__":
    main()
