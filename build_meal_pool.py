"""
build_meal_pool.py

Pulls a fresh pool of frozen-meal candidates from Kroger's Products API,
splits them into a "breakfast" pool (name/category contains "breakfast")
and a "general" pool (everything else, used for lunch and dinner), then
looks up calories for each item -- first from USDA FoodData Central by
UPC, falling back to Open Food Facts (direct barcode lookup) for items
USDA's branded database doesn't have, which in practice is mostly
store-brand/private-label items.

Writes candidate_pool.json, which the static front-end (index.html) uses
to build a 7-day / 21-meal plan entirely in the browser. Meant to run on
a schedule via GitHub Actions (see .github/workflows/update-meal-pool.yml),
the same pattern as the other trackers.

Required environment variables (set as GitHub Actions secrets):
    KROGER_CLIENT_ID
    KROGER_CLIENT_SECRET
    USDA_API_KEY        -- free, instant signup at https://api.data.gov/signup/

Optional:
    KROGER_ZIP           -- zip code used to find a nearby store (default below)
"""

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

KROGER_ZIP = os.environ.get("KROGER_ZIP", "68508")  # Lincoln, NE default
KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID")
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET")
USDA_API_KEY = os.environ.get("USDA_API_KEY")

KROGER_BASE = "https://api.kroger.com/v1"
USDA_BASE = "https://api.nal.usda.gov/fdc/v1"

# Search terms used to build each pool. Kroger's product search is a plain
# term search, not a strict category filter, so we run several queries and
# de-dupe by UPC to get a decent-sized, varied pool.
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

PRODUCTS_PER_TERM = 20
REQUEST_PAUSE_SECONDS = 0.3


def http_json(url, data=None, headers=None, method=None):
    headers = headers or {}
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_kroger_token():
    if not KROGER_CLIENT_ID or not KROGER_CLIENT_SECRET:
        sys.exit("Missing KROGER_CLIENT_ID / KROGER_CLIENT_SECRET environment variables.")
    creds = base64.b64encode(
        f"{KROGER_CLIENT_ID}:{KROGER_CLIENT_SECRET}".encode("utf-8")
    ).decode("utf-8")
    resp = http_json(
        f"{KROGER_BASE}/connect/oauth2/token",
        data={"grant_type": "client_credentials", "scope": "product.compact"},
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


def upc_variants(upc):
    """Kroger UPCs and USDA's stored gtinUpc values don't always agree on
    digit count/padding (13-digit padded vs 12-digit UPC-A vs 14-digit GTIN).
    Try the plausible variants rather than a single exact string."""
    upc = upc.strip()
    variants = [upc]

    stripped = upc.lstrip("0")
    if stripped and stripped not in variants:
        variants.append(stripped)

    if len(upc) == 13 and upc.startswith("0"):
        no_lead = upc[1:]
        if no_lead not in variants:
            variants.append(no_lead)

    if len(upc) <= 13:
        padded14 = upc.zfill(14)
        if padded14 not in variants:
            variants.append(padded14)

    if len(upc) == 12:
        padded13 = upc.zfill(13)
        if padded13 not in variants:
            variants.append(padded13)

    return variants


def lookup_calories_off(upc):
    """Fallback nutrition source: Open Food Facts. Free, no API key, and does
    a direct exact-barcode lookup rather than a fuzzy text search -- better
    coverage for store-brand/private-label items USDA's branded database
    tends to miss."""
    global _off_debug_count
    for candidate in upc_variants(upc):
        url = f"https://world.openfoodfacts.org/api/v2/product/{candidate}.json"
        try:
            resp = http_json(url, headers={"User-Agent": "freezer-week-planner/1.0"})
        except urllib.error.HTTPError:
            continue
        except urllib.error.URLError:
            continue

        if _off_debug_count < USDA_DEBUG_SAMPLES:
            _off_debug_count += 1
            print(
                f"  [debug] OFF barcode='{candidate}' -> status={resp.get('status')}, "
                f"product_name={resp.get('product', {}).get('product_name')!r}",
                file=sys.stderr,
            )

        if resp.get("status") != 1:
            continue

        product = resp.get("product", {})
        nutriments = product.get("nutriments", {})
        cal = nutriments.get("energy-kcal_serving") or nutriments.get("energy-kcal_value")
        if cal:
            return round(cal)
    return None


_usda_error_count = 0
_usda_debug_count = 0
_off_debug_count = 0
USDA_DEBUG_SAMPLES = 3


def lookup_calories(upc):
    """Look up calories for a UPC via USDA FoodData Central. Returns int or None.
    Tries a few digit-padding variants since Kroger's UPC format and USDA's
    stored gtinUpc format don't always match exactly."""
    global _usda_error_count
    global _usda_debug_count
    if not USDA_API_KEY or not upc:
        return None

    for candidate in upc_variants(upc):
        params = urllib.parse.urlencode(
            {"api_key": USDA_API_KEY, "query": candidate, "dataType": "Branded", "pageSize": 5}
        )
        url = f"{USDA_BASE}/foods/search?{params}"
        try:
            resp = http_json(url)
        except urllib.error.HTTPError as e:
            _usda_error_count += 1
            if _usda_error_count <= 3:
                print(f"  USDA lookup failed for UPC {candidate}: {e}", file=sys.stderr)
                if e.code in (401, 403):
                    print(
                        "  -> looks like an invalid/unauthorized USDA_API_KEY, "
                        "not a bad UPC. Check the secret value.",
                        file=sys.stderr,
                    )
            continue

        if _usda_debug_count < USDA_DEBUG_SAMPLES:
            _usda_debug_count += 1
            foods = resp.get("foods", [])
            sample_gtins = [f.get("gtinUpc") for f in foods[:3]]
            print(
                f"  [debug] USDA query='{candidate}' -> {len(foods)} foods, "
                f"sample gtinUpc values: {sample_gtins}",
                file=sys.stderr,
            )

        for food in resp.get("foods", []):
            # Prefer a food whose own gtinUpc actually matches this candidate
            # (search is fuzzy text match, not an exact UPC filter) before
            # falling back to "first result with a calorie value".
            label = food.get("labelNutrients", {})
            cal = label.get("calories", {}).get("value")
            if cal and food.get("gtinUpc", "").lstrip("0") == candidate.lstrip("0"):
                return round(cal)

        for food in resp.get("foods", []):
            label = food.get("labelNutrients", {})
            cal = label.get("calories", {}).get("value")
            if cal:
                return round(cal)

    off_cal = lookup_calories_off(upc)
    if off_cal:
        return off_cal

    return None


def build_pool(token, location_id, terms):
    seen_upcs = set()
    pool = []
    for term in terms:
        products = search_products(token, location_id, term)
        found = 0
        matched = 0
        skipped_dupe = 0
        no_upc = 0
        for product in products:
            upc = product.get("upc")
            if not upc:
                no_upc += 1
                continue
            if upc in seen_upcs:
                skipped_dupe += 1
                continue
            seen_upcs.add(upc)
            found += 1

            calories = lookup_calories(upc)
            time.sleep(REQUEST_PAUSE_SECONDS)
            if calories is None:
                continue  # skip items we can't get real calorie data for
            matched += 1

            pool.append(
                {
                    "upc": upc,
                    "name": product.get("description"),
                    "brand": product.get("brandName"),
                    "size": product.get("items", [{}])[0].get("size"),
                    "price": extract_price(product),
                    "image": extract_image(product),
                    "calories": calories,
                }
            )
        print(
            f"  '{term}': {len(products)} from Kroger, {found} new/unique, "
            f"{matched} with a calorie match, {no_upc} missing upc, "
            f"{skipped_dupe} dupes"
        )
    return pool


def main():
    if not USDA_API_KEY:
        print(
            "WARNING: USDA_API_KEY is not set — every product will fail its "
            "calorie lookup and the pool will come back empty. Add it as a "
            "GitHub Actions secret (see README).",
            file=sys.stderr,
        )

    print(f"Authenticating with Kroger...")
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


if __name__ == "__main__":
    main()
