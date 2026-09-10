"""
build_meal_pool.py

Pulls a fresh pool of frozen-meal candidates from Kroger's Products API,
splits them into a "breakfast" pool and a "general" pool, then looks up
calories from Open Food Facts by product name.

USDA FoodData Central is no longer used.

Writes candidate_pool.json, which the static front-end can use to build a
7-day / 21-meal plan entirely in the browser.

Required environment variables:
    KROGER_CLIENT_ID
    KROGER_CLIENT_SECRET

Optional environment variables:
    KROGER_ZIP
    OFF_BASE
    OFF_PAUSE_SECONDS
    OFF_PAGE_SIZE
    OFF_MAX_QUERIES_PER_ITEM
    HTTP_MAX_RETRIES
    HTTP_INITIAL_BACKOFF_SECONDS
    HTTP_BACKOFF_FACTOR
    HTTP_MAX_BACKOFF_SECONDS
    HTTP_TIMEOUT_SECONDS
    LOG_LEVEL
    LOG_FILE
    USER_AGENT

Open Food Facts rate limiting notes:
    - Open Food Facts is a shared public API.
    - There is not always a strict published rate limit, but the polite
      default is to stay around 1 request per second or slower.
    - If Open Food Facts returns HTTP 429, this script respects Retry-After
      when present and backs off exponentially with jitter.
    - Adjust OFF_PAUSE_SECONDS if you need to be more conservative.
"""

import base64
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


KROGER_ZIP = os.environ.get("KROGER_ZIP", "68508")  # Lincoln, NE default
KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID")
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET")

KROGER_BASE = "https://api.kroger.com/v1"
OFF_BASE = os.environ.get("OFF_BASE", "https://world.openfoodfacts.org")

PRODUCTS_PER_TERM = int(os.environ.get("PRODUCTS_PER_TERM", "20"))

# Pause between Kroger API calls.
KROGER_PAUSE_SECONDS = float(os.environ.get("KROGER_PAUSE_SECONDS", "0.3"))

# Pause before every Open Food Facts request.
# Open Food Facts is public/shared; 1 second is a conservative default.
OFF_PAUSE_SECONDS = float(os.environ.get("OFF_PAUSE_SECONDS", "1.0"))

HTTP_TIMEOUT_SECONDS = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(os.environ.get("HTTP_MAX_RETRIES", "5"))
INITIAL_BACKOFF_SECONDS = float(os.environ.get("HTTP_INITIAL_BACKOFF_SECONDS", "1.0"))
BACKOFF_FACTOR = float(os.environ.get("HTTP_BACKOFF_FACTOR", "2.0"))
MAX_BACKOFF_SECONDS = float(os.environ.get("HTTP_MAX_BACKOFF_SECONDS", "60.0"))

OFF_PAGE_SIZE = int(os.environ.get("OFF_PAGE_SIZE", "25"))

# How many Open Food Facts text queries to try per Kroger item.
# Example:
#   1. brand + cleaned product name
#   2. cleaned product name
OFF_MAX_QUERIES_PER_ITEM = int(os.environ.get("OFF_MAX_QUERIES_PER_ITEM", "2"))

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "kroger-meal-pool/2.0 (Open Food Facts text search; set USER_AGENT env var with contact info)",
)


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


def setup_logging():
    """
    Logs to stdout always, and optionally to a file if LOG_FILE is set.
    """
    handlers = [logging.StreamHandler(sys.stdout)]

    log_file = os.environ.get("LOG_FILE")
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_retry_after(value):
    """
    Parse Retry-After header. Supports seconds and HTTP-date.
    Returns seconds to wait, or None if not parseable.
    """
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_dt = parsedate_to_datetime(value)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def should_retry_http_error(status):
    return status in {408, 429} or 500 <= status <= 599


def http_json(url, data=None, headers=None, method=None, timeout=HTTP_TIMEOUT_SECONDS, label="HTTP"):
    """
    Fetch JSON with retries and rate-limit handling.

    Retries on:
        - HTTP 408
        - HTTP 429
        - HTTP 5xx
        - Network errors / timeouts

    Respects Retry-After when present.
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)

    body_data = None
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body_data = data
        else:
            body_data = urllib.parse.urlencode(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    attempt = 1
    backoff = INITIAL_BACKOFF_SECONDS

    while True:
        logging.info("%s request attempt %s/%s: %s", label, attempt, MAX_RETRIES + 1, url)

        try:
            req = urllib.request.Request(url, data=body_data, headers=headers, method=method)

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode() if hasattr(resp, "getcode") else 200

                raw = resp.read().decode("utf-8", "replace")
                logging.info("%s response status=%s bytes=%s url=%s", label, status, len(raw), url)

                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    logging.error(
                        "%s response was not valid JSON. First 200 chars: %r",
                        label,
                        raw[:200],
                    )
                    raise

        except urllib.error.HTTPError as exc:
            status = exc.code

            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                raw = ""

            logging.warning(
                "%s HTTP error status=%s url=%s reason=%s body_preview=%r",
                label,
                status,
                url,
                exc.reason,
                raw[:200],
            )

            if should_retry_http_error(status) and attempt <= MAX_RETRIES:
                retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                base_delay = retry_after if retry_after is not None else backoff
                delay = min(max(base_delay, 0.0), MAX_BACKOFF_SECONDS)
                delay += random.uniform(0.0, max(0.1, 0.15 * delay))

                logging.info(
                    "%s retrying after %.2f seconds due to HTTP %s",
                    label,
                    delay,
                    status,
                )
                time.sleep(delay)

                attempt += 1
                backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF_SECONDS)
                continue

            raise

        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            logging.warning(
                "%s network error attempt %s/%s url=%s error=%r",
                label,
                attempt,
                MAX_RETRIES + 1,
                url,
                exc,
            )

            if attempt <= MAX_RETRIES:
                delay = min(backoff, MAX_BACKOFF_SECONDS)
                delay += random.uniform(0.0, max(0.1, 0.15 * delay))

                logging.info(
                    "%s retrying after %.2f seconds due to network error",
                    label,
                    delay,
                )
                time.sleep(delay)

                attempt += 1
                backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF_SECONDS)
                continue

            raise


def get_kroger_token():
    if not KROGER_CLIENT_ID or not KROGER_CLIENT_SECRET:
        sys.exit("Missing KROGER_CLIENT_ID / KROGER_CLIENT_SECRET environment variables.")

    creds = base64.b64encode(
        f"{KROGER_CLIENT_ID}:{KROGER_CLIENT_SECRET}".encode("utf-8")
    ).decode("utf-8")

    logging.info("Authenticating with Kroger...")

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
        label="Kroger token",
    )

    token = resp.get("access_token")
    if not token:
        sys.exit("Kroger token response did not include access_token.")

    logging.info("Kroger token acquired.")
    return token


def find_location_id(token, zip_code):
    url = f"{KROGER_BASE}/locations?filter.zipCode.near={zip_code}&filter.limit=1"

    logging.info("Finding Kroger store near zip=%s", zip_code)

    resp = http_json(
        url,
        headers={"Authorization": f"Bearer {token}"},
        label="Kroger locations",
    )

    locations = resp.get("data", [])
    if not locations:
        sys.exit(f"No Kroger store found near zip {zip_code}.")

    location_id = locations[0]["locationId"]
    logging.info("Found Kroger locationId=%s", location_id)
    return location_id


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
        resp = http_json(
            url,
            headers={"Authorization": f"Bearer {token}"},
            label=f"Kroger search term={term}",
        )
    except urllib.error.HTTPError as exc:
        logging.error(
            "Kroger search failed term=%r status=%s reason=%s",
            term,
            exc.code,
            exc.reason,
        )
        return []
    except Exception as exc:
        logging.error("Kroger search failed term=%r error=%r", term, exc)
        return []

    products = resp.get("data", [])
    logging.info("Kroger search term=%r returned %s products", term, len(products))

    time.sleep(KROGER_PAUSE_SECONDS)
    return products


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


def extract_size(product):
    items = product.get("items") or []
    if not items:
        return None

    return items[0].get("size")


MASS_UNIT_TO_GRAMS = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "oz": 28.3495,
    "ounce": 28.3495,
    "ounces": 28.3495,
    "lb": 453.592,
    "lbs": 453.592,
    "pound": 453.592,
    "pounds": 453.592,
}

MASS_RE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>oz|ounce|ounces|lb|lbs|pound|pounds|kg|kilogram|kilograms|g|gram|grams)\b",
    re.IGNORECASE,
)

SIZE_TOKEN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*"
    r"(?:fl\.?\s*oz|fluid\s+ounce|fluid\s+ounces|oz|ounce|ounces|lb|lbs|pound|pounds|"
    r"kg|kilogram|kilograms|g|gr|gram|grams|ml|milliliter|milliliters|l|liter|liters|"
    r"ct|count|pack|packs|package|packages|tray|trays|bag|bags|box|boxes)\b",
    re.IGNORECASE,
)


def parse_mass_grams(size_text):
    """
    Attempts to parse a Kroger size like '9.5 oz', '12 OZ', '1.2 LB' into grams.
    Used to estimate total calories from Open Food Facts 100g calories when
    serving calories are not available.
    """
    if not size_text:
        return None

    matches = list(MASS_RE.finditer(str(size_text)))
    if not matches:
        return None

    match = matches[0]
    amount = safe_float(match.group("amount"))
    unit = match.group("unit").lower()

    if amount is None or unit not in MASS_UNIT_TO_GRAMS:
        return None

    return amount * MASS_UNIT_TO_GRAMS[unit]


def normalize_text(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9&'/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def base_name_from_description(description):
    """
    Kroger product descriptions often include package size at the end.
    Example:
        Healthy Choice Cafe Steamers General Tso's Chicken Frozen Meal, 9.5 oz

    This reduces it to something more searchable:
        healthy choice cafe steamers general tso's chicken frozen meal
    """
    if not description:
        return ""

    text = str(description)

    # Many Kroger names put the size after a comma.
    if "," in text:
        text = text.split(",", 1)[0]

    # Remove obvious size/count/package tokens.
    text = SIZE_TOKEN_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)

    return normalize_text(text)


def build_off_queries(name, brand):
    """
    Build Open Food Facts text-search queries from Kroger product name/brand.
    """
    base = base_name_from_description(name)
    full_name = normalize_text(name)
    brand_norm = normalize_text(brand)

    variants = []

    if brand_norm and base:
        variants.append(f"{brand_norm} {base}")

    if base:
        variants.append(base)

    if brand_norm and full_name and full_name != base:
        variants.append(f"{brand_norm} {full_name}")

    if full_name and full_name != base:
        variants.append(full_name)

    unique = []
    seen = set()

    for variant in variants:
        variant = re.sub(r"\s+", " ", variant).strip()
        if len(variant) >= 3 and variant not in seen:
            seen.add(variant)
            unique.append(variant)

    return unique[: max(0, OFF_MAX_QUERIES_PER_ITEM)]


def get_off_kcal_100g(nutriments):
    """
    Extract kcal per 100g from Open Food Facts nutriments.
    """
    kcal = safe_float(nutriments.get("energy-kcal_100g"))
    if kcal and kcal > 0:
        return kcal

    kj = safe_float(nutriments.get("energy-kj_100g"))
    if kj and kj > 0:
        return kj / 4.184

    energy = safe_float(nutriments.get("energy_100g"))
    if energy and energy > 0:
        energy_unit = str(nutriments.get("energy_unit") or "").lower()

        # If explicitly kJ, convert. If value looks too large to be kcal,
        # assume kJ. This is imperfect but helps for many OFF entries.
        if "kj" in energy_unit or energy > 900:
            return energy / 4.184

        return energy

    return None


def extract_off_calories(off_product, kroger_mass_g=None):
    """
    Extract calories from an Open Food Facts product.

    Preference order:
        1. energy-kcal_serving
        2. energy-kj_serving
        3. energy_serving, with unit guessing
        4. 100g calories multiplied by OFF serving_quantity
        5. 100g calories multiplied by parsed Kroger package weight
        6. 100g calories multiplied by OFF product_quantity
        7. 100g calories alone

    Returns:
        (calories, basis)
    """
    nutriments = off_product.get("nutriments") or {}

    kcal_serving = safe_float(nutriments.get("energy-kcal_serving"))
    if kcal_serving and kcal_serving > 0:
        return round(kcal_serving), "off_serving"

    kj_serving = safe_float(nutriments.get("energy-kj_serving"))
    if kj_serving and kj_serving > 0:
        return round(kj_serving / 4.184), "off_serving_kj"

    energy_serving = safe_float(nutriments.get("energy_serving"))
    if energy_serving and energy_serving > 0:
        energy_unit = str(nutriments.get("energy_unit") or "").lower()

        if "kj" in energy_unit or energy_serving > 900:
            return round(energy_serving / 4.184), "off_serving_kj"

        return round(energy_serving), "off_serving"

    kcal_100g = get_off_kcal_100g(nutriments)
    if not kcal_100g or kcal_100g <= 0:
        return None, None

    serving_g = safe_float(off_product.get("serving_quantity"))
    if serving_g and serving_g > 0:
        return round(kcal_100g * serving_g / 100.0), "off_serving_estimated_from_100g"

    if kroger_mass_g and kroger_mass_g > 0:
        return round(kcal_100g * kroger_mass_g / 100.0), "kroger_size_estimated"

    product_g = safe_float(off_product.get("product_quantity"))
    if product_g and product_g > 0:
        # product_quantity is often grams, but sometimes ml. Treat ml as g.
        return round(kcal_100g * product_g / 100.0), "off_package_estimated_from_100g"

    return round(kcal_100g), "per_100g"


def token_set(value):
    return set(re.findall(r"[a-z0-9]+", (value or "").lower()))


def score_off_candidate(candidate, query, brand):
    """
    Very lightweight relevance score for Open Food Facts candidates.
    """
    score = 0.0

    name = candidate.get("product_name") or candidate.get("generic_name") or ""
    brands = candidate.get("brands") or ""

    q_tokens = token_set(query)
    n_tokens = token_set(name)

    if q_tokens:
        score += 100.0 * len(q_tokens & n_tokens) / len(q_tokens)

    b_tokens = token_set(brand)
    if b_tokens:
        if b_tokens & token_set(brands):
            score += 30.0
        if b_tokens & n_tokens:
            score += 10.0

    if safe_float(candidate.get("serving_quantity")):
        score += 10.0

    if safe_float(candidate.get("product_quantity")):
        score += 5.0

    return score


def lookup_open_food_facts(name, brand, size_text):
    """
    Look up calories from Open Food Facts by product name.

    Returns dict with:
        calories
        calories_basis
        off_upc
        off_url
        off_name
        off_brand
        off_query

    or None if no usable calories are found.
    """
    queries = build_off_queries(name, brand)
    kroger_mass_g = parse_mass_grams(size_text)

    logging.info(
        "Open Food Facts lookup start: name=%r brand=%r size=%r parsed_mass_g=%s query_count=%s",
        name,
        brand,
        size_text,
        kroger_mass_g,
        len(queries),
    )

    if not queries:
        logging.info(
            "Open Food Facts lookup skipped: no usable query could be built from name=%r brand=%r",
            name,
            brand,
        )
        return None

    for query in queries:
        params = {
            "search_terms": query,
            "search_simple": 1,
            "action": "process",
            "json": 1,
            "page_size": OFF_PAGE_SIZE,
            "fields": "code,product_name,brands,nutriments,serving_quantity,product_quantity",
        }

        url = f"{OFF_BASE}/cgi/search.pl?{urllib.parse.urlencode(params)}"

        # Simple rate-limit delay before every Open Food Facts request.
        time.sleep(OFF_PAUSE_SECONDS)

        try:
            resp = http_json(
                url,
                headers={"Accept": "application/json"},
                label="Open Food Facts search",
            )
        except Exception as exc:
            logging.error("Open Food Facts search failed query=%r error=%r", query, exc)
            continue

        products = resp.get("products") or []
        api_count = resp.get("count", len(products))

        logging.info(
            "Open Food Facts search result: query=%r api_count=%s returned=%s",
            query,
            api_count,
            len(products),
        )

        candidates = []

        for idx, candidate in enumerate(products):
            off_code = str(candidate.get("code") or candidate.get("_id") or "")
            candidate_name = candidate.get("product_name") or ""
            candidate_brand = candidate.get("brands") or ""

            calories, basis = extract_off_calories(candidate, kroger_mass_g)

            if calories is None:
                logging.info(
                    "Open Food Facts candidate %s skipped no calories: code=%s name=%r brand=%r",
                    idx,
                    off_code,
                    candidate_name,
                    candidate_brand,
                )
                continue

            score = score_off_candidate(candidate, query, brand)

            logging.info(
                "Open Food Facts candidate %s usable: code=%s name=%r brand=%r calories=%s basis=%s score=%.1f",
                idx,
                off_code,
                candidate_name,
                candidate_brand,
                calories,
                basis,
                score,
            )

            candidates.append(
                (
                    score,
                    idx,
                    calories,
                    basis,
                    candidate,
                )
            )

        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))

            best_score, _, best_calories, best_basis, best = candidates[0]

            best_code = str(best.get("code") or best.get("_id") or "")
            best_name = best.get("product_name") or ""
            best_brand = best.get("brands") or ""

            logging.info(
                "Open Food Facts selected: query=%r code=%s name=%r brand=%r calories=%s basis=%s score=%.1f candidates_with_calories=%s",
                query,
                best_code,
                best_name,
                best_brand,
                best_calories,
                best_basis,
                best_score,
                len(candidates),
            )

            return {
                "calories": best_calories,
                "calories_basis": best_basis,
                "off_upc": best_code or None,
                "off_url": f"{OFF_BASE}/product/{best_code}" if best_code else None,
                "off_name": best_name,
                "off_brand": best_brand,
                "off_query": query,
            }

        logging.warning(
            "Open Food Facts query returned no candidates with usable calories: query=%r",
            query,
        )

    logging.warning(
        "Open Food Facts lookup failed to find calories: name=%r brand=%r",
        name,
        brand,
    )
    return None


def build_pool(token, location_id, terms):
    seen_upcs = set()
    pool = []

    for term in terms:
        logging.info("Searching Kroger term=%r", term)

        products = search_products(token, location_id, term)

        for product in products:
            upc = product.get("upc")
            name = product.get("description")
            brand = product.get("brandName")

            if not upc:
                logging.warning("Kroger product skipped: missing UPC name=%r", name)
                continue

            if upc in seen_upcs:
                logging.info(
                    "Kroger product skipped duplicate UPC=%s name=%r",
                    upc,
                    name,
                )
                continue

            seen_upcs.add(upc)

            logging.info(
                "Processing Kroger product upc=%s name=%r brand=%r",
                upc,
                name,
                brand,
            )

            size = extract_size(product)

            off_result = lookup_open_food_facts(name, brand, size)

            if off_result is None:
                logging.info(
                    "Skipping Kroger product upc=%s because Open Food Facts did not return usable calories",
                    upc,
                )
                time.sleep(KROGER_PAUSE_SECONDS)
                continue

            item = {
                # Existing field, kept for compatibility.
                "upc": upc,
                # Explicit Kroger UPC.
                "kroger_upc": upc,
                # Explicit Open Food Facts code / UPC.
                "open_food_facts_upc": off_result.get("off_upc"),
                "off_upc": off_result.get("off_upc"),
                "open_food_facts_url": off_result.get("off_url"),
                "name": name,
                "brand": brand,
                "size": size,
                "price": extract_price(product),
                "image": extract_image(product),
                "calories": off_result["calories"],
                "calories_basis": off_result.get("calories_basis"),
                "calories_source": "open_food_facts",
                "off_name": off_result.get("off_name"),
                "off_brand": off_result.get("off_brand"),
                "off_query": off_result.get("off_query"),
            }

            pool.append(item)

            logging.info(
                "Added product to pool upc=%s off_upc=%s calories=%s basis=%s",
                upc,
                item["open_food_facts_upc"],
                item["calories"],
                item["calories_basis"],
            )

            time.sleep(KROGER_PAUSE_SECONDS)

        time.sleep(KROGER_PAUSE_SECONDS)

    return pool


def main():
    setup_logging()

    logging.info(
        "Starting meal pool build. Calories source: Open Food Facts text search by name."
    )

    token = get_kroger_token()
    location_id = find_location_id(token, KROGER_ZIP)

    logging.info("Building breakfast pool...")
    breakfast_pool = build_pool(token, location_id, BREAKFAST_TERMS)

    logging.info("Building general lunch/dinner pool...")
    general_pool = build_pool(token, location_id, GENERAL_TERMS)

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "location_id": location_id,
        "zip": KROGER_ZIP,
        "calories_source": "open_food_facts",
        "breakfast": breakfast_pool,
        "general": general_pool,
    }

    with open("candidate_pool.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    logging.info(
        "Wrote candidate_pool.json: %s breakfast items, %s general items.",
        len(breakfast_pool),
        len(general_pool),
    )


if __name__ == "__main__":
    main()