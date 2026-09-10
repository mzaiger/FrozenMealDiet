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
  KROGER_ZIP              -- zip code used to find a nearby store
  USDA_API_KEY            -- only needed if USDA fallback is enabled
  ENABLE_USDA_FALLBACK    -- set to 0/false/no/off to disable USDA fallback
"""

import base64
import json
import os
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
REQUEST_PAUSE_SECONDS = 0.3

DEFAULT_HEADERS = {
    # Federal API gateways and Open Food Facts are less likely to throttle
    # a normal-looking client UA. Open Food Facts also appreciates a
    # descriptive UA.
    "User-Agent": "frozen-meal-planner/1.0 (GitHub Actions meal-pool builder)",
    "Accept": "application/json",
}

OFF_HEADERS = {
    **DEFAULT_HEADERS,
    "User-Agent": "frozen-meal-planner/1.0 (meal planner barcode lookup)",
}

_off_stats = {
    "attempted": 0,
    "http_error": 0,
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


def http_json(url, data=None, headers=None, method=None, retries=3, backoff=1.5):
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

            # 429/502/503/504 are transient/throttling — worth retrying.
            # 404 and auth errors usually will not fix themselves.
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                print(f"    got HTTP {e.code}, retrying in {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            raise

        except urllib.error.URLError as e:
            last_error = e

            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                print(f"    URL error, retrying in {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            raise

    if last_error is not None:
        raise last_error

    raise RuntimeError("HTTP request failed for an unknown reason.")


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
    """
    Convert JSON values that may be numbers or stringified numbers into float.
    Returns None if not parseable.
    """
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
    """
    Build barcode candidates for Open Food Facts.

    Kroger often gives UPC-A style 12-digit codes. Open Food Facts frequently
    stores EAN-13/GTIN-13 codes, so a 12-digit UPC usually needs one leading
    zero added.
    """
    digits = "".join(ch for ch in str(upc).strip() if ch.isdigit())

    if not digits:
        return []

    candidates = []
    stripped = digits.lstrip("0") or "0"

    if len(digits) == 12:
        # UPC-A -> EAN-13 style.
        candidates.append("0" + digits)
        candidates.append(digits)

    elif len(digits) == 13:
        candidates.append(digits)
        candidates.append(stripped)

    elif len(digits) == 14:
        candidates.append(digits)
        candidates.append(stripped)

        if len(stripped) == 12:
            candidates.append("0" + stripped)

        if len(stripped) <= 13:
            candidates.append(stripped.zfill(13))

    else:
        candidates.append(digits)
        candidates.append(digits.zfill(13))

    if len(digits) < 13:
        candidates.append(digits.zfill(13))

    seen = set()
    variants = []

    for variant in candidates:
        if variant and variant not in seen:
            seen.add(variant)
            variants.append(variant)

    return variants


def _parse_serving_grams(product):
    """
    Try to determine serving size in grams from Open Food Facts data.

    Preference:
      1. nutriments.serving_quantity
      2. parse grams/ml from serving_size text
    """
    nutriments = product.get("nutriments") or {}

    serving_quantity = _as_number(nutriments.get("serving_quantity"))
    if serving_quantity is not None and serving_quantity > 0:
        return serving_quantity

    serving_size_text = str(product.get("serving_size") or "").lower()
    if not serving_size_text:
        return None

    # Examples:
    #   "1 sandwich (129 g)"
    #   "1 package (269g)"
    #   "1/2 meal 260 grams"
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:g|grams?|gram)\b", serving_size_text)
    if match:
        parsed = _as_number(match.group(1))
        if parsed is not None and parsed > 0:
            return parsed

    # Treat ml roughly as g for calorie-per-serving estimation.
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
    """
    Try to determine total package weight in grams from Open Food Facts data.
    """
    quantity = _as_number(product.get("product_quantity"))
    unit = str(product.get("product_quantity_unit") or "").strip().lower()

    if quantity is None or quantity <= 0:
        return None

    # If no unit is present, OFF often still means grams for many foods.
    # Accept common metric units; avoid oz/lb because conversion is more
    # likely to be wrong for food shape/density.
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
    """
    Extract calories from an Open Food Facts product dict.

    Preference:
      1. kcal per serving, if available
      2. kJ per serving converted to kcal
      3. kcal per 100g scaled to serving size
      4. kcal per 100g scaled to package weight
      5. kcal per 100g as-is, if nothing better exists
      6. kJ per 100g converted/scaled
    """
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

    # 1. Direct kcal per serving.
    kcal_serving = num(
        "energy-kcal_serving",
        "energy-kcal_serving_value",
    )
    if kcal_serving is not None and kcal_serving > 0:
        return round(kcal_serving)

    # 2. kJ per serving.
    kj_serving = num(
        "energy_serving",
        "energy_serving_value",
    )
    if kj_serving is not None and kj_serving > 0:
        return round(kj_serving / 4.184)

    # 3/4/5. kcal per 100g.
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

        # Less ideal: return per-100g calories if no package/serving weight
        # is available. This keeps more items usable, but may understate
        # calories for multi-serving packages.
        return round(kcal_100)

    # 6. kJ per 100g.
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
    if not upc:
        return None

    variants = _off_barcode_variants(upc)

    if not variants:
        return None

    _off_stats["attempted"] += 1

    found_product = False

    for i, barcode in enumerate(variants):
        if i > 0:
            time.sleep(REQUEST_PAUSE_SECONDS)

        url = f"{OFF_BASE}/api/v2/product/{barcode}.json"

        try:
            resp = http_json(url, headers=OFF_HEADERS)

        except urllib.error.HTTPError as e:
            # Open Food Facts returns 404 for missing products in some cases.
            if e.code == 404:
                continue

            print(f"  Open Food Facts lookup failed for barcode {barcode}: {e}", file=sys.stderr)
            _off_stats["http_error"] += 1
            continue

        except urllib.error.URLError as e:
            print(f"  Open Food Facts lookup failed for barcode {barcode}: {e}", file=sys.stderr)
            _off_stats["http_error"] += 1
            continue

        if not isinstance(resp, dict):
            continue

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
    """
    USDA FoodData Central stores barcodes as GTIN-14 (often zero-padded),
    while Kroger returns plain UPC-A (12-digit, sometimes without leading
    zeros). Build the set of formats a given code might appear as so we
    can try them all: bare digits, and zero-padded to 12/13/14 digits.
    """
    digits = "".join(ch for ch in str(upc).strip() if ch.isdigit())

    if not digits:
        return []

    bare = digits.lstrip("0") or "0"

    # Most Branded Foods entries store gtinUpc as a 14-digit, zero-padded
    # GTIN, so try that first — it's the most likely hit — then fall back
    # to the as-received UPC and the bare/13-digit forms.
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
    """
    Optional fallback: look up calories via USDA FoodData Central.
    Returns int calories or None.
    """
    if not USDA_API_KEY or not upc:
        return None

    variants = _upc_variants_usda(upc)

    if not variants:
        return None

    # Compare on the zero-stripped form so "011110001234" and its GTIN-14
    # padded equivalent "00011110001234" are recognized as the same code.
    target_bare_forms = {v.lstrip("0") or "0" for v in variants}

    _usda_stats["attempted"] += 1

    for i, variant in enumerate(variants):
        if i > 0:
            time.sleep(REQUEST_PAUSE_SECONDS)

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
                # Query matched on text relevance, not actually this barcode.
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
    """
    Primary: Open Food Facts.
    Fallback: USDA FoodData Central, if enabled and configured.
    """
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

        for product in search_products(token, location_id, term):
            upc = product.get("upc")

            if not upc or upc in seen_upcs:
                continue

            seen_upcs.add(upc)

            calories = lookup_calories(upc)
            time.sleep(REQUEST_PAUSE_SECONDS)

            if calories is None or calories <= 0:
                continue

            pool.append(
                {
                    "upc": upc,
                    "name": product.get("description"),
                    "brand": product.get("brandName"),
                    "size": (product.get("items") or [{}])[0].get("size"),
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