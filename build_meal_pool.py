from pathlib import Path
import re

src = Path("/mnt/data/build_meal_pool.py")
text = src.read_text()

# Replace the entire nutrition lookup section with a more robust implementation.
start = text.index("def usda_search(")
end = text.index("\n\nFORCE_DEBUG_TERMS =", start)

new_section = r'''def usda_search(query, page_size=10):
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
'''

text = text[:start] + new_section + text[end:]

# Fix the tuple handling in build_pool.
old = '''            calories = lookup_calories(upc, name=product.get("description"), force_debug=force_debug)
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
            )'''

new = '''            calories, calorie_source = lookup_calories(
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
            )'''

if old not in text:
    raise RuntimeError("Could not find build_pool lookup block to replace.")

text = text.replace(old, new)

out = Path("/mnt/data/build_meal_pool_fixed.py")
out.write_text(text)

print(f"Created: {out}")
print(f"Lines: {len(text.splitlines())}")
