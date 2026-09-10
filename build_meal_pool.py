"""
build_meal_pool.py

Pulls a fresh pool of frozen-meal candidates from Kroger's Products API,
splits them into a "breakfast" pool (name/category contains "breakfast")
and a "general" pool (everything else, used for lunch and dinner), then
looks up calories for each item from USDA FoodData Central by UPC.

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


def _upc_variants(upc):
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
    candidates = [digits, bare, bare.zfill(12), bare.zfill(13), bare.zfill(14)]
    seen = set()
    variants = []
    for v in candidates:
        if v not in seen:
            seen.add(v)
            variants.append(v)
    return variants


def lookup_calories(upc):
    """Look up calories for a UPC via USDA FoodData Central. Returns int or None."""
    if not USDA_API_KEY or not upc:
        return None

    variants = _upc_variants(upc)
    if not variants:
        return None
    # Compare on the zero-stripped form so "011110001234" and its GTIN-14
    # padded equivalent "00011110001234" are recognized as the same code.
    target_bare_forms = {v.lstrip("0") or "0" for v in variants}

    for variant in variants:
        params = urllib.parse.urlencode(
            {"api_key": USDA_API_KEY, "query": variant, "dataType": "Branded", "pageSize": 5}
        )
        url = f"{USDA_BASE}/foods/search?{params}"
        try:
            resp = http_json(url)
        except urllib.error.HTTPError as e:
            print(f"  USDA lookup failed for UPC {variant}: {e}", file=sys.stderr)
            continue

        for food in resp.get("foods", []):
            food_gtin = str(food.get("gtinUpc") or "")
            food_bare = food_gtin.lstrip("0") or "0"
            if food_bare not in target_bare_forms:
                # Query matched on text relevance, not actually this barcode.
                continue
            label = food.get("labelNutrients", {})
            cal = label.get("calories", {}).get("value")
            if cal:
                return round(cal)

    return None


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
            if calories is None:
                continue  # skip items we can't get real calorie data for

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
    return pool


def main():
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
