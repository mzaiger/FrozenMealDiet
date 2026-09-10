"""
build_meal_pool.py

Pulls a fresh pool of frozen-meal candidates from Kroger's Products API,
splits them into a "breakfast" pool (name/category contains "breakfast")
and a "general" pool (everything else, used for lunch and dinner), then
looks up calories for each item from USDA FoodData Central using the
Kroger product name / description / brand as a text search.

Writes candidate_pool.json, which the static front-end (index.html) uses
to build a 7-day / 21-meal plan entirely in the browser.

Required environment variables:
  KROGER_CLIENT_ID
  KROGER_CLIENT_SECRET
  USDA_API_KEY

Optional environment variables:
  KROGER_ZIP
  REQUEST_PAUSE_SECONDS
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

KROGER_BASE = "https://api.kroger.com/v1"
USDA_BASE = "https://api.nal.usda.gov/fdc/v1"

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

# USDA can throttle api.data.gov traffic if requests are too fast.
# 0.45s is a conservative default for text-search lookups.
REQUEST_PAUSE_SECONDS = float(os.environ.get("REQUEST_PAUSE_SECONDS", "0.45"))

USDA_PAGE_SIZE = 8

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; frozen-meal-planner/1.0)",
    "Accept": "application/json",
}

_usda_stats = {
    "attempted": 0,
    "http_error": 0,
    "no_foods_returned": 0,
    "matched": 0,
}


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

            # Transient/throttling responses: retry with backoff.
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
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
                wait = backoff * (2 ** attempt)
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


def _clean_text(text):
    if not text:
        return ""

    text = str(text)
    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text):
    """
    Simple tokenization for matching Kroger product text against USDA text.
    """
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) > 1
    }


def _match_score(kroger_name, kroger_brand, usda_food):
    """
    Very lightweight relevance score.

    USDA text search can return close-but-not-exact items, so this prefers
    candidates whose description/brand shares more words with the Kroger
    product name.
    """
    kroger_text = f"{kroger_brand or ''} {kroger_name or ''}"
    usda_text = " ".join(
        [
            str(usda_food.get("brandName") or usda_food.get("brandOwner") or ""),
            str(usda_food.get("description") or ""),
        ]
    )

    kroger_tokens = _tokenize(kroger_text)
    usda_tokens = _tokenize(usda_text)

    if not kroger_tokens or not usda_tokens:
        return 0.0

    overlap = len(kroger_tokens & usda_tokens)
    return overlap / len(kroger_tokens)


def lookup_calories_by_name(name, brand=None):
    """
    Look up calories via USDA FoodData Central text search using the Kroger
    product name/description and optional brand.

    Returns int calories or None.
    """
    if not USDA_API_KEY or not name:
        return None

    name = _clean_text(name)
    brand = _clean_text(brand)

    if not name:
        return None

    # Keep queries reasonably short for USDA.
    name_query = name[:140]

    queries = []

    # If the brand is not already part of the product description, try
    # "Brand Product Name" first. This often improves USDA Branded Foods
    # search relevance.
    if brand and brand.lower() not in name.lower():
        queries.append(f"{brand} {name_query}".strip()[:150])

    queries.append(name_query)

    _usda_stats["attempted"] += 1

    best_calories = None
    best_score = -1.0

    for i, query in enumerate(queries):
        if i > 0:
            time.sleep(REQUEST_PAUSE_SECONDS)

        params = urllib.parse.urlencode(
            {
                "api_key": USDA_API_KEY,
                "query": query,
                "dataType": "Branded",
                "pageSize": USDA_PAGE_SIZE,
            }
        )

        url = f"{USDA_BASE}/foods/search?{params}"

        try:
            resp = http_json(url)
        except urllib.error.HTTPError as e:
            print(f"  USDA name lookup failed for query '{query}': {e}", file=sys.stderr)
            _usda_stats["http_error"] += 1
            continue
        except urllib.error.URLError as e:
            print(f"  USDA name lookup failed for query '{query}': {e}", file=sys.stderr)
            _usda_stats["http_error"] += 1
            continue

        foods = resp.get("foods", [])

        if not foods:
            continue

        for food in foods:
            label_nutrients = food.get("labelNutrients", {}) or {}
            calories_value = (label_nutrients.get("calories") or {}).get("value")

            calories_number = _as_number(calories_value)

            if calories_number is None or calories_number <= 0:
                continue

            score = _match_score(name, brand, food)

            if score > best_score:
                best_score = score
                best_calories = round(calories_number)

        # If we found a reasonably strong match, stop trying fallback queries.
        if best_calories is not None and best_score >= 0.45:
            break

    if best_calories is not None:
        _usda_stats["matched"] += 1
        return best_calories

    _usda_stats["no_foods_returned"] += 1
    return None


def build_pool(token, location_id, terms):
    seen_upcs = set()
    pool = []

    for term in terms:
        print(f"  searching: {term}")

        products = search_products(token, location_id, term)

        for product in products:
            upc = product.get("upc")

            if not upc or upc in seen_upcs:
                continue

            seen_upcs.add(upc)

            name = product.get("description")
            brand = product.get("brandName")

            # Some Kroger payloads may put useful text in items.
            if not name:
                items = product.get("items") or []
                if items:
                    name = items[0].get("description")

            if not name:
                continue

            calories = lookup_calories_by_name(name, brand)

            time.sleep(REQUEST_PAUSE_SECONDS)

            if calories is None or calories <= 0:
                continue

            items = product.get("items") or [{}]

            pool.append(
                {
                    "upc": upc,
                    "name": name,
                    "brand": brand,
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

    print(f"USDA name-search stats: {_usda_stats}")


if __name__ == "__main__":
    main()