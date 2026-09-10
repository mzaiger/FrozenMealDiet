"""
build_meal_pool.py

Pulls a fresh pool of frozen-meal candidates from Kroger's Products API,
splits them into a "breakfast" pool (name/category contains "breakfast")
and a "general" pool (everything else, used for lunch and dinner), then
looks up calories for each item.

Primary calorie lookup:
  Open Food Facts barcode endpoint:
    https://world.openfoodfacts.org/api/v2/product/{barcode}.json

Optional fallback calorie lookup:
  USDA FoodData Central, used only if Open Food Facts does not return
  usable calorie data and ENABLE_USDA_FALLBACK is enabled.

Writes candidate_pool.json, which the static front-end (index.html) uses
to build a 7-day / 21-meal plan entirely in the browser.

Required environment variables:
  KROGER_CLIENT_ID
  KROGER_CLIENT_SECRET

Optional environment variables:
  KROGER_ZIP
  USDA_API_KEY
  ENABLE_USDA_FALLBACK
  OFF_REQUEST_PAUSE_SECONDS
  KROGER_REQUEST_PAUSE_SECONDS
  USDA_REQUEST_PAUSE_SECONDS
  OFF_USER_AGENT
"""

import base64
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error


KROGER_ZIP = os.environ.get("KROGER_ZIP", "68508")  # Lincoln, NE default
KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID")
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET")
USDA_API_KEY = os.environ.get("USDA_API_KEY")

ENABLE_USDA_FALLBACK = os.environ.get("ENABLE_USDA_FALLBACK", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# Rate limiting / pacing.
# Open Food Facts limits unauthenticated users to ~100 req/min. 2.5s keeps us safe.
OFF_REQUEST_PAUSE_SECONDS = float(os.environ.get("OFF_REQUEST_PAUSE_SECONDS", "2.5"))
KROGER_REQUEST_PAUSE_SECONDS = float(os.environ.get("KROGER_REQUEST_PAUSE_SECONDS", "1.0"))
USDA_REQUEST_PAUSE_SECONDS = float(os.environ.get("USDA_REQUEST_PAUSE_SECONDS", "0.75"))
BETWEEN_PRODUCTS_PAUSE_SECONDS = float(os.environ.get("BETWEEN_PRODUCTS_PAUSE_SECONDS", "0.25"))

KROGER_BASE = "https://api.kroger.com/v1"
USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
OFF_BASE = "https://world.openfoodfacts.org"

BREAKFAST_TERMS = [
    "frozen breakfast ",
    "frozen breakfast sandwich ",
    "frozen breakfast bowl ",
    "frozen pancakes breakfast ",
    "frozen breakfast burrito ",
]

GENERAL_TERMS = [
    "frozen meal ",
    "frozen dinner ",
    "frozen entree ",
    "lean cuisine ",
    "stouffer's ",
    "healthy choice frozen ",
    "frozen bowl meal ",
]

PRODUCTS_PER_TERM = 20

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; frozen-meal-planner/1.0)",
    "Accept": "application/json",
}

# Open Food Facts appreciates a descriptive UA and actively blocks generic/fake emails.
# Set OFF_USER_AGENT in your environment to something like:
# "my-app/1.0 (https://example.com; me@example.com)"
OFF_USER_AGENT = os.environ.get(
    "OFF_USER_AGENT",
    "frozen-meal-planner/1.0 (barcode nutrition lookup; contact: contact@example.com)",
)

OFF_HEADERS = {
    **DEFAULT_HEADERS,
    "User-Agent": OFF_USER_AGENT,
}

OFF_FIELDS = "nutriments,serving_size,product_quantity,product_quantity_unit"

_off_stats = {
    "attempted": 0,
    "http_error": 0,
    "rate_limited": 0,
    "not_found": 0,
    "found_no_calories": 0,
    "matched": 0,
}

_usda_stats = {
    "attempted": 0,
    "http_error": 0,
    "no_match": 0,
    "matched": 0,
}

_usda_missing_key_warned = False

_last_off_request = 0.0
_off_slowdown_factor = 1.0


def _retry_after_seconds(http_error, default_wait):
    """
    Parse a Retry-After header if present. If it cannot be parsed, fall back
    to the provided default wait. Adds a little jitter.
    """
    wait = default_wait

    try:
        retry_after = http_error.headers.get("Retry-After")
        if retry_after:
            retry_after = str(retry_after).strip()

            if retry_after.replace(".", "", 1).isdigit():
                wait = max(wait, float(retry_after))
    except Exception:
        pass

    return wait + random.uniform(0.0, 0.5)


def http_json(url, data=None, headers=None, method=None, retries=4, backoff=2.0):
    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}

    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = urllib.parse.urlencode(data).encode("utf-8")

    last_error = None

    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=merged_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            last_error = e

            # Transient/throttling responses: retry with backoff.
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                default_wait = backoff * (2 ** attempt)
                wait = _retry_after_seconds(e, default_wait)

                print(
                    f"    got HTTP {e.code} for {url}, retrying in {wait:.1f}s...",
                    file=sys.stderr,
                )

                time.sleep(wait)
                continue

            raise

        except urllib.error.URLError as e:
            last_error = e

            if attempt < retries - 1:
                wait = backoff * (2 ** attempt) + random.uniform(0.0, 0.5)

                print(
                    f"    URL error for {url}, retrying in {wait:.1f}s...",
                    file=sys.stderr,
                )

                time.sleep(wait)
                continue

            raise

    if last_error is not None:
        raise last_error

    raise RuntimeError("HTTP request failed for an unknown reason.")


def _off_throttle():
    """
    Enforce a minimum interval between Open Food Facts requests.
    """
    global _last_off_request

    interval = OFF_REQUEST_PAUSE_SECONDS * _off_slowdown_factor
    now = time.monotonic()
    wait = interval - (now - _last_off_request)

    if wait > 0:
        time.sleep(wait)

    _last_off_request = time.monotonic()


def _off_mark_rate_limited():
    global _off_slowdown_factor
    _off_slowdown_factor = min(_off_slowdown_factor * 1.5, 8.0)


def _off_mark_success():
    global _off_slowdown_factor
    _off_slowdown_factor = max(1.0, _off_slowdown_factor / 1.1)


def get_kroger_token():
    if not KROGER_CLIENT_ID or not KROGER_CLIENT_SECRET:
        sys.exit("Missing KROGER_CLIENT_ID / KROGER_CLIENT_SECRET environment variables.")

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

    return resp["access_token"]


def find_location_id(token, zip_code):
    url = f"{KROGER_BASE}/locations?filter.zipCode.near={zip_code}&filter.limit=1"
    resp = http_json(url, headers={"Authorization": f"Bearer {token}"})
    locations = resp.get("data", [])

    if not locations:
        sys.exit(f"No Kroger store found near zip {zip_code}.")

    return locations[0]["locationId"]


def search_products(token, location_id, term, limit=PRODUCTS_PER_TERM):
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
        resp = http_json(url, headers={"Authorization": f"Bearer {token}"})
    except urllib.error.HTTPError as e:
        print(f"  Kroger search failed for '{term}': {e}", file=sys.stderr)
        return []
    except urllib.error.URLError as e:
        print(f"  Kroger search failed for '{term}': {e}", file=sys.stderr)
        return []

    return resp.get("data", [])


def extract_price(product):
    items = product.get("items") or []

    if not items:
        return None

    price = items[0].get("price") or {}
    return price.get("promo") or price.get("regular")


def extract_image(product):
    for img in product.get("images", []):
        if img.get("size") == "medium":
            return img.get("url")

    images = product.get("images", [])
    return images[0]["url"] if images else None


def _as_number(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", ".")

    if not text:
        return None

    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _off_barcode_variants(upc):
    digits = "".join(ch for ch in str(upc).strip() if ch.isdigit())

    if not digits:
        return []

    candidates = []

    if len(digits) == 12:
        candidates.append("0" + digits)
        candidates.append(digits)

    elif len(digits) == 13:
        candidates.append(digits)

    elif len(digits) == 14:
        stripped = digits.lstrip("0") or "0"

        candidates.append(digits)
        candidates.append(stripped)

        if len(stripped) == 12:
            candidates.append("0" + stripped)

    elif len(digits) < 13:
        candidates.append(digits.zfill(13))
        candidates.append(digits)

    else:
        candidates.append(digits)

    seen = set()
    variants = []

    for variant in candidates:
        if variant and variant not in seen:
            seen.add(variant)
            variants.append(variant)

    return variants[:3]


def _parse_serving_grams(product):
    nutriments = product.get("nutriments") or {}

    serving_quantity = _as_number(nutriments.get("serving_quantity"))
    if serving_quantity is not None and serving_quantity > 0:
        return serving_quantity

    serving_size_text = str(product.get("serving_size") or "").lower()
    if not serving_size_text:
        return None

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:g|grams?|gram)\b", serving_size_text)
    if match:
        parsed = _as_number(match.group(1))
        if parsed is not None and parsed > 0:
            return parsed

    match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:ml|milliliters?|millilitres?)\b",
        serving_size_text,
    )
    if match:
        parsed = _as_number(match.group(1))
        if parsed is not None and parsed > 0:
            return parsed

    return None


def _parse_product_grams(product):
    quantity = _as_number(product.get("product_quantity"))
    unit = str(product.get("product_quantity_unit") or "").strip().lower()

    if quantity is None or quantity <= 0:
        return None

    if not unit or unit in {
        "g",
        "gram",
        "grams",
        "ml",
        "milliliter",
        "milliliters",
        "millilitre",
        "millilitres",
    }:
        return quantity

    return None


def _extract_off_calories(product):
    if not isinstance(product, dict):
        return None

    nutriments = product.get("nutriments") or {}

    def num(*keys):
        for key in keys:
            value = _as_number(nutriments.get(key))
            if value is not None:
                return value
        return None

    serving_qty = _parse_serving_grams(product)
    product_qty = _parse_product_grams(product)

    kcal_serving = num(
        "energy-kcal_serving",
        "energy-kcal_serving_value",
    )
    if kcal_serving is not None and kcal_serving > 0:
        return round(kcal_serving)

    kj_serving = num(
        "energy_serving",
        "energy_serving_value",
    )
    if kj_serving is not None and kj_serving > 0:
        return round(kj_serving / 4.184)

    kcal_100 = num(
        "energy-kcal_100g",
        "energy-kcal_value",
        "energy-kcal",
    )

    if kcal_100 is not None and kcal_100 > 0:
        if serving_qty is not None and serving_qty > 0:
            return round(kcal_100 * serving_qty / 100.0)

        if product_qty is not None and product_qty > 0:
            return round(kcal_100 * product_qty / 100.0)

        return round(kcal_100)

    kj_100 = num(
        "energy_100g",
        "energy_value",
        "energy",
    )

    if kj_100 is not None and kj_100 > 0:
        kcal_100_from_kj = kj_100 / 4.184

        if serving_qty is not None and serving_qty > 0:
            return round(kcal_100_from_kj * serving_qty / 100.0)

        if product_qty is not None and product_qty > 0:
            return round(kcal_100_from_kj * product_qty / 100.0)

        return round(kcal_100_from_kj)

    return None


def lookup_calories_off(upc):
    """
    Look up calories via Open Food Facts barcode endpoint.
    Returns int calories or None.
    """
    global _off_slowdown_factor

    if not upc:
        return None

    variants = _off_barcode_variants(upc)

    if not variants:
        return None

    _off_stats["attempted"] += 1

    found_product = False
    fields_params = urllib.parse.urlencode({"fields": OFF_FIELDS})

    for barcode in variants:
        _off_throttle()

        url = f"{OFF_BASE}/api/v2/product/{barcode}.json?{fields_params}"

        resp = None
        try:
            resp = http_json(
                url,
                headers=OFF_HEADERS,
                retries=3,
                backoff=5.0,
            )

        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue

            if e.code == 429:
                _off_stats["rate_limited"] += 1
                print(
                    "  !! Open Food Facts rate limit (429) hit. Sleeping 60s to let limit reset...",
                    file=sys.stderr,
                )
                time.sleep(60)
                
                # Try one more time after the long sleep
                try:
                    _off_throttle()
                    resp = http_json(url, headers=OFF_HEADERS, retries=2, backoff=5.0)
                except Exception as retry_e:
                    print(f"  Still failed after 60s sleep: {retry_e}", file=sys.stderr)
                    _off_stats["http_error"] += 1
                    return None  # Skip this UPC to avoid infinite loops
                    
            else:
                print(
                    f"  Open Food Facts lookup failed for barcode {barcode}: {e}",
                    file=sys.stderr,
                )
                _off_stats["http_error"] += 1
                continue

        except urllib.error.URLError as e:
            print(
                f"  Open Food Facts lookup failed for barcode {barcode}: {e}",
                file=sys.stderr,
            )
            _off_stats["http_error"] += 1
            continue

        if not isinstance(resp, dict):
            continue

        if resp.get("status") == 1:
            _off_mark_success()

        if resp.get("status") != 1:
            continue

        product = resp.get("product")

        if not isinstance(product, dict) or not product:
            continue

        found_product = True

        calories = _extract_off_calories(product)

        if calories is not None and calories > 0:
            _off_stats["matched"] += 1
            return calories

    if found_product:
        _off_stats["found_no_calories"] += 1
    else:
        _off_stats["not_found"] += 1

    return None


def _upc_variants_usda(upc):
    digits = "".join(ch for ch in str(upc).strip() if ch.isdigit())

    if not digits:
        return []

    bare = digits.lstrip("0") or "0"

    candidates = [
        bare.zfill(14),
        digits,
        bare,
        bare.zfill(13),
    ]

    seen = set()
    variants = []

    for variant in candidates:
        if variant not in seen:
            seen.add(variant)
            variants.append(variant)

    return variants


def lookup_calories_usda(upc):
    if not USDA_API_KEY or not upc:
        return None

    variants = _upc_variants_usda(upc)

    if not variants:
        return None

    target_bare_forms = {v.lstrip("0") or "0" for v in variants}

    _usda_stats["attempted"] += 1

    for i, variant in enumerate(variants):
        if i > 0:
            time.sleep(USDA_REQUEST_PAUSE_SECONDS)

        params = urllib.parse.urlencode(
            {
                "api_key": USDA_API_KEY,
                "query": variant,
                "dataType": "Branded",
                "pageSize": 5,
            }
        )

        url = f"{USDA_BASE}/foods/search?{params}"

        try:
            resp = http_json(url)

        except urllib.error.HTTPError as e:
            print(f"  USDA lookup failed for UPC {variant}: {e}", file=sys.stderr)
            _usda_stats["http_error"] += 1
            continue

        except urllib.error.URLError as e:
            print(f"  USDA lookup failed for UPC {variant}: {e}", file=sys.stderr)
            _usda_stats["http_error"] += 1
            continue

        foods = resp.get("foods", [])

        if not foods:
            continue

        for food in foods:
            food_gtin = str(food.get("gtinUpc") or "")
            food_bare = food_gtin.lstrip("0") or "0"

            if food_bare not in target_bare_forms:
                continue

            label_nutrients = food.get("labelNutrients", {}) or {}
            calories_value = (label_nutrients.get("calories") or {}).get("value")

            calories_number = _as_number(calories_value)

            if calories_number is not None and calories_number > 0:
                _usda_stats["matched"] += 1
                return round(calories_number)

    _usda_stats["no_match"] += 1
    return None


def lookup_calories(upc):
    global _usda_missing_key_warned

    calories = lookup_calories_off(upc)

    if calories is not None and calories > 0:
        return calories

    if not ENABLE_USDA_FALLBACK:
        return None

    if not USDA_API_KEY:
        if not _usda_missing_key_warned:
            print(
                "  USDA fallback is enabled but USDA_API_KEY is not set; skipping USDA fallback.",
                file=sys.stderr,
            )
            _usda_missing_key_warned = True
        return None

    return lookup_calories_usda(upc)


def build_pool(token, location_id, terms):
    seen_upcs = set()
    pool = []

    for term in terms:
        print(f"  searching: {term}")

        products = search_products(token, location_id, term)

        time.sleep(KROGER_REQUEST_PAUSE_SECONDS)

        for product in products:
            upc = product.get("upc")

            if not upc or upc in seen_upcs:
                continue

            seen_upcs.add(upc)

            calories = lookup_calories(upc)

            time.sleep(BETWEEN_PRODUCTS_PAUSE_SECONDS)

            if calories is None or calories <= 0:
                continue

            items = product.get("items") or [{}]

            pool.append(
                {
                    "upc": upc,
                    "name": product.get("description"),
                    "brand": product.get("brandName"),
                    "size": items[0].get("size"),
                    "price": extract_price(product),
                    "image": extract_image(product),
                    "calories": calories,
                }
            )

    return pool


def main():
    print("Authenticating with Kroger...")
    token = get_kroger_token()

    print(f"Finding store near {KROGER_ZIP}...")
    location_id = find_location_id(token, KROGER_ZIP)
    print(f"  using locationId {location_id}")

    print("Building breakfast pool...")
    breakfast_pool = build_pool(token, location_id, BREAKFAST_TERMS)

    print("Building general (lunch/dinner) pool...")
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

    print(f"Open Food Facts stats: {_off_stats}")

    if ENABLE_USDA_FALLBACK:
        print(f"USDA fallback stats: {_usda_stats}")
    else:
        print("USDA fallback disabled.")


if __name__ == "__main__":
    main()