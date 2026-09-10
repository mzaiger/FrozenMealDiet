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


def usda_search(query, page_size=10):
    """Search USDA FoodData Central Branded foods by text."""
    if not USDA_API_KEY:
        return {}
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


def normalize_text(value):
    """Normalize product text for approximate matching."""
    value = value or ""
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def nutrition_calories(food):
    """Get calories from the USDA search result's label nutrients."""
    label = food.get("labelNutrients") or {}
    value = (label.get("calories") or {}).get("value")
    if value is not None:
        try:
            return round(float(value))
        except (TypeError, ValueError):
            pass

    # Some USDA records expose nutrients as a list instead.
    for nutrient in food.get("foodNutrients") or []:
        name = normalize_text(nutrient.get("nutrientName"))
        if name == "energy" or "calorie" in name:
            value = nutrient.get("value")
            unit = normalize_text(nutrient.get("unitName"))
            if value is not None and (not unit or unit in {"kcal", "cal"}):
                try:
                    return round(float(value))
                except (TypeError, ValueError):
                    pass
    return None


def best_calories_from_foods(foods, product_name=None, brand=None):
    """
    Pick the USDA Branded result most likely to be the Kroger product.

    USDA's search endpoint is fuzzy, so do not blindly take the first result.
    Score candidates using brand/name token overlap and prefer a close match.
    """
    if not foods:
        return None

    target_name = normalize_text(product_name)
    target_brand = normalize_text(brand)

    target_tokens = set(target_name.split())
    # Remove generic words that do not help identify the product.
    generic = {
        "frozen", "breakfast", "meal", "entree", "sandwich", "bowl",
        "burrito", "food", "foods", "the", "and", "with", "cheese",
    }
    target_tokens -= generic

    scored = []
    for food in foods:
        description = normalize_text(food.get("description"))
        food_brand = normalize_text(food.get("brandOwner") or food.get("brandName"))

        if not description:
            continue

        desc_tokens = set(description.split())
        name_overlap = len(target_tokens & desc_tokens)
        name_ratio = name_overlap / max(1, len(target_tokens))

        brand_match = bool(target_brand and (
            target_brand in food_brand or food_brand in target_brand
        ))

        cal = nutrition_calories(food)
        if cal is None:
            continue

        score = name_ratio * 100
        if brand_match:
            score += 35

        # Exact/near-exact UPC is handled separately; this is a text fallback.
        scored.append((score, name_overlap, brand_match, food, cal))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return scored[0][4]


def find_usda_barcode_match(upc, debug=False):
    """
    USDA barcode lookup using text search, followed by explicit gtinUpc
    comparison. FoodData Central does not provide a reliable standalone
    exact-GTIN endpoint, so the search endpoint is used defensively.
    """
    for candidate in upc_variants(upc):
        try:
            resp = usda_search(candidate, page_size=10)
        except urllib.error.HTTPError as e:
            if debug:
                print(
                    f"    [USDA] barcode='{candidate}' -> HTTPError {e.code}",
                    file=sys.stderr,
                )
            continue

        foods = resp.get("foods", [])
        if debug:
            sample = [
                (f.get("description"), f.get("gtinUpc"))
                for f in foods[:3]
            ]
            print(
                f"    [USDA] barcode='{candidate}' -> {len(foods)} foods, "
                f"sample: {sample}",
                file=sys.stderr,
            )

        wanted = candidate.lstrip("0")
        for food in foods:
            gtin = str(food.get("gtinUpc") or "").strip()
            if gtin and gtin.lstrip("0") == wanted:
                cal = nutrition_calories(food)
                if cal is not None:
                    if debug:
                        print(
                            f"    [USDA] exact gtinUpc match -> {cal} cal",
                            file=sys.stderr,
                        )
                    return cal, "exact"

    return None, None


def lookup_calories_off(upc, debug=False):
    """Fallback to Open Food Facts with one normalized barcode first."""
    # A UPC-A is the most useful form for OFF. Avoid making four requests
    # for every product; try variants only when necessary.
    candidates = upc_variants(upc)

    for candidate in candidates:
        url = f"https://world.openfoodfacts.org/api/v2/product/{candidate}.json"
        try:
            resp = http_json(
                url,
                headers={
                    "User-Agent": (
                        "freezer-week-planner/1.1 "
                        "(github.com/mzaiger/SportsDashboard)"
                    )
                },
            )
        except urllib.error.HTTPError as e:
            if debug:
                print(
                    f"    [OFF] barcode='{candidate}' -> HTTPError {e.code}",
                    file=sys.stderr,
                )
            if e.code == 429:
                # Give OFF a chance to recover before trying another product.
                time.sleep(2.0)
            else:
                time.sleep(0.2)
            continue
        except urllib.error.URLError as e:
            if debug:
                print(
                    f"    [OFF] barcode='{candidate}' -> URLError: {e.reason}",
                    file=sys.stderr,
                )
            continue

        if debug:
            print(
                f"    [OFF] barcode='{candidate}' -> status={resp.get('status')}, "
                f"product_name={resp.get('product', {}).get('product_name')!r}",
                file=sys.stderr,
            )

        if resp.get("status") != 1:
            continue

        product = resp.get("product") or {}
        nutriments = product.get("nutriments") or {}

        cal = (
            nutriments.get("energy-kcal_serving")
            or nutriments.get("energy-kcal_value")
        )
        if cal is not None:
            try:
                return round(float(cal))
            except (TypeError, ValueError):
                pass

    return None


_usda_cache = {}
_off_cache = {}


def lookup_calories(upc, name=None, brand=None, force_debug=False):
    """
    Look up calories using:
      1. USDA Branded exact GTIN comparison
      2. USDA brand + product-name text match
      3. Open Food Facts exact barcode

    Results are cached for the duration of the build.
    Returns (calories, source).
    """
    global _debug_used

    debug = force_debug
    if not debug and _debug_used < DEBUG_BUDGET:
        _debug_used += 1
        debug = True

    if not USDA_API_KEY or not upc:
        return None, None

    cache_key = str(upc).strip()
    if cache_key in _usda_cache:
        return _usda_cache[cache_key]

    if debug:
        print(
            f"  [debug] ---- {name!r} (upc={upc}) ----",
            file=sys.stderr,
        )

    # Tier 1: USDA exact GTIN.
    result = find_usda_barcode_match(upc, debug=debug)
    if result[0] is not None:
        _usda_cache[cache_key] = result
        return result

    # Tier 2: USDA text search. This is intentionally before OFF because
    # branded packaged foods are much more likely to be represented in USDA.
    query = " ".join(
        p for p in [brand, name] if p and str(p).strip()
    ).strip()

    if query:
        try:
            resp = usda_search(query, page_size=10)
        except urllib.error.HTTPError as e:
            if debug:
                print(
                    f"    [USDA name-search] query={query!r} "
                    f"-> HTTPError {e.code}",
                    file=sys.stderr,
                )
            resp = {}

        foods = resp.get("foods", [])
        cal = best_calories_from_foods(
            foods,
            product_name=name,
            brand=brand,
        )

        if debug:
            best_names = [
                f.get("description")
                for f in foods[:3]
                if f.get("description")
            ]
            print(
                f"    [USDA name-search] query={query!r} -> "
                f"{len(foods)} foods, candidates={best_names}, calories={cal}",
                file=sys.stderr,
            )

        if cal is not None:
            result = (cal, "approximate")
            _usda_cache[cache_key] = result
            return result

    # Tier 3: Open Food Facts.
    if cache_key in _off_cache:
        off_cal = _off_cache[cache_key]
    else:
        off_cal = lookup_calories_off(upc, debug=debug)
        _off_cache[cache_key] = off_cal

    if off_cal is not None:
        if debug:
            print(f"    [OFF] matched -> {off_cal} cal", file=sys.stderr)
        result = (off_cal, "exact")
        _usda_cache[cache_key] = result
        return result

    if debug:
        print("    -> no calorie match from any source", file=sys.stderr)

    result = (None, None)
    _usda_cache[cache_key] = result
    return result


FORCE_DEBUG_TERMS = {"lean cuisine", "stouffer's", "healthy choice frozen"}


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
            upc = product.get("upc")
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
                    "calorie_source": calorie_source,
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
