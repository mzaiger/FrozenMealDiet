"""
build_meal_pool.py

Local-CSV version (no Kroger API needed).

Strategy:
1. Load frozen items from a local Walmart grocery export (WMT_Grocery_*.csv,
   or the .zip it ships in) instead of hitting the Kroger API.
   - "Frozen Breakfast" category -> breakfast pool
   - "Frozen Meals & Snacks" / "Frozen Pizza, Pasta, & Breads" categories,
     filtered down to actual entrees (excludes sides/snacks/desserts) -> general pool
2. For each candidate item, look up calories on Open Food Facts (OFF):
   - Text search (Walmart SKUs aren't UPCs, so barcode lookup is skipped).
   - Strict Category Filtering prevents the text search from matching
     "yogurt" to "pancakes" or "raw sausage" to "sandwiches".
3. Writes candidate_pool.json, same shape as before, plus a product_url field
   on every item (used by index.html to link back to the Walmart listing).

Usage:
    python build_meal_pool.py /path/to/archive.zip
    python build_meal_pool.py /path/to/frozen_food.csv
    python build_meal_pool.py                      # looks for frozen_food.csv
                                                     # or archive.zip in cwd
"""

import csv
import io
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
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


# --- CSV source config -------------------------------------------------
DEFAULT_CSV_CANDIDATES = ["frozen_food.csv", "archive.zip"]
DEPARTMENT_COLUMN = "DEPARTMENT"
FROZEN_DEPARTMENT = "Frozen"

BREAKFAST_CATEGORY = "Frozen Breakfast"
GENERAL_CATEGORIES = {"Frozen Meals & Snacks", "Frozen Pizza, Pasta, & Breads"}

# Keeps the general pool to actual entrees, not sides/snacks/desserts.
GENERAL_INCLUDE_KEYWORDS = {
    "meal", "dinner", "entree", "bowl", "pizza", "lasagna", "pot pie",
    "casserole", "skillet", "stir fry", "alfredo", "teriyaki",
    "shepherd's pie", "meatloaf", "meat loaf", "salisbury", "burrito",
    "enchilada", "taco", "chimichanga", "mac & cheese", "mac and cheese",
    "fettuccine", "fettucini", "pasta", "spaghetti", "ravioli", "gnocchi",
    "risotto", "curry", "fried rice", "noodle", "calzone", "stromboli",
    "pot roast", "salisbury steak",
}
GENERAL_EXCLUDE_KEYWORDS = {
    "breadstick", "garlic bread", "texas toast", "toast", "dessert",
    "ice cream", "cookie", "cake", "sundae", "chip", "cracker", "pretzel",
    "popcorn", "roll", "stick", "ring", "slider", "tot", "fries",
    "hash brown", "waffle", "pancake",
}

# How many candidates (max) to pull calories for, per pool. OFF text search
# is rate-limited (OFF_PAUSE_SECONDS between calls), so this keeps runtime
# reasonable. Raise via env var if you want a bigger pool and don't mind
# the wait.
BREAKFAST_SAMPLE_SIZE = int(os.environ.get("BREAKFAST_SAMPLE_SIZE", "60"))
GENERAL_SAMPLE_SIZE = int(os.environ.get("GENERAL_SAMPLE_SIZE", "60"))
RANDOM_SEED = os.environ.get("RANDOM_SEED")

# --- Open Food Facts config (unchanged from the Kroger version) --------
OFF_SEARCH_BASE = os.environ.get("OFF_SEARCH_BASE", "https://search.openfoodfacts.org")
OFF_PRODUCT_BASE = "https://world.openfoodfacts.org"

OFF_PAUSE_SECONDS = float(os.environ.get("OFF_PAUSE_SECONDS", "1.5"))
HTTP_TIMEOUT_SECONDS = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(os.environ.get("HTTP_MAX_RETRIES", "5"))
INITIAL_BACKOFF_SECONDS = float(os.environ.get("HTTP_INITIAL_BACKOFF_SECONDS", "1.0"))
BACKOFF_FACTOR = float(os.environ.get("HTTP_BACKOFF_FACTOR", "2.0"))
MAX_BACKOFF_SECONDS = float(os.environ.get("HTTP_MAX_BACKOFF_SECONDS", "60.0"))

OFF_PAGE_SIZE = int(os.environ.get("OFF_PAGE_SIZE", "25"))
OFF_MAX_QUERIES_PER_ITEM = int(os.environ.get("OFF_MAX_QUERIES_PER_ITEM", "3"))

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "walmart-csv-meal-pool/1.0 (local CSV source + OFF calories)",
)

# --- Strict Category Filtering Rules (unchanged) ------------------------
MAINS = {"pancake", "waffle", "sandwich", "burrito", "bowl", "biscuit", "croissant", "wrap", "bagel", "muffin", "toast", "scramble", "hash", "pizza", "pasta", "meal", "entree", "dinner", "taco", "chimichanga"}
INGREDIENTS = {"sausage", "bacon", "egg", "cheese", "chicken", "beef", "pork", "turkey", "potato", "gravy", "steak"}
BAD_WORDS = {"yogurt", "shake", "milk", "cereal", "bread", "loaf", "sauce", "dip", "spread", "soup", "juice", "coffee", "tea", "water", "soda", "cookie", "pie", "donut", "bar"}

def get_categories(name):
    name_lower = (name or "").lower()
    mains = {m for m in MAINS if m in name_lower}
    ings = {i for i in INGREDIENTS if i in name_lower}
    return mains, ings

def is_valid_fuzzy_match(source_name, off_name):
    k_mains, k_ings = get_categories(source_name)
    o_mains, o_ings = get_categories(off_name)

    if k_mains and not o_mains: return False
    if k_mains and o_mains and not (k_mains & o_mains): return False

    for bw in BAD_WORDS:
        if bw in (off_name or "").lower() and bw not in (source_name or "").lower():
            return False

    return True
# --------------------------------------------

def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    log_file = os.environ.get("LOG_FILE")
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers, force=True,
    )

def safe_float(value):
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None

def parse_retry_after(value):
    if not value: return None
    try: return max(0.0, float(value))
    except ValueError: pass
    try:
        retry_dt = parsedate_to_datetime(value)
        if retry_dt.tzinfo is None: retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_dt - datetime.now(timezone.utc)).total_seconds())
    except Exception: return None

def should_retry_http_error(status):
    return status in {408, 429, 503} or 500 <= status <= 599

def http_json(url, data=None, headers=None, method=None, timeout=HTTP_TIMEOUT_SECONDS, label="HTTP"):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    body_data = None
    if data is not None:
        if isinstance(data, (bytes, bytearray)): body_data = data
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
                status = getattr(resp, "status", None) or (resp.getcode() if hasattr(resp, "getcode") else 200)
                raw = resp.read().decode("utf-8", "replace")
                logging.info("%s response status=%s bytes=%s url=%s", label, status, len(raw), url)
                try: return json.loads(raw)
                except json.JSONDecodeError:
                    logging.error("%s response was not valid JSON. First 200 chars: %r", label, raw[:200])
                    raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            try: raw = exc.read().decode("utf-8", "replace")
            except Exception: raw = ""
            logging.warning("%s HTTP error status=%s url=%s reason=%s body_preview=%r", label, status, url, exc.reason, raw[:200])
            if should_retry_http_error(status) and attempt <= MAX_RETRIES:
                retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                base_delay = retry_after if retry_after is not None else backoff
                delay = min(max(base_delay, 0.0), MAX_BACKOFF_SECONDS) + random.uniform(0.0, max(0.1, 0.15 * backoff))
                logging.info("%s retrying after %.2f seconds due to HTTP %s", label, delay, status)
                time.sleep(delay)
                attempt += 1
                backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF_SECONDS)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            logging.warning("%s network error attempt %s/%s url=%s error=%r", label, attempt, MAX_RETRIES + 1, url, exc)
            if attempt <= MAX_RETRIES:
                delay = min(backoff, MAX_BACKOFF_SECONDS) + random.uniform(0.0, max(0.1, 0.15 * backoff))
                logging.info("%s retrying after %.2f seconds due to network error", label, delay)
                time.sleep(delay)
                attempt += 1
                backoff = min(backoff * BACKOFF_FACTOR, MAX_BACKOFF_SECONDS)
                continue
            raise

MASS_UNIT_TO_GRAMS = {
    "g": 1.0, "gram": 1.0, "grams": 1.0, "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495, "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
}
MASS_RE = re.compile(r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>oz|ounce|ounces|lb|lbs|pound|pounds|kg|kilogram|kilograms|g|gram|grams)\b", re.IGNORECASE)

def parse_mass_grams(size_text):
    if not size_text: return None
    matches = list(MASS_RE.finditer(str(size_text)))
    if not matches: return None
    match = matches[0]
    amount = safe_float(match.group("amount"))
    unit = match.group("unit").lower()
    if amount is None or unit not in MASS_UNIT_TO_GRAMS: return None
    return amount * MASS_UNIT_TO_GRAMS[unit]

def build_off_queries(name, brand):
    name_clean = re.sub(r'[®™&]', ' ', name or '')
    brand_clean = re.sub(r'[®™&]', ' ', brand or '')
    fillers = r'\b(frozen|breakfast|sandwich|sandwiches|bowl|bowls|burrito|burritos|meal|meals|entree|entrees|dinner|dinners|lunch|snack|size|delights|signature|ct|oz|lbs|lb|g|kg|ml|l|and|on|a|the|with|for|of|in)\b'
    name_clean = re.sub(fillers, ' ', name_clean, flags=re.IGNORECASE)
    name_clean = re.sub(r'\s+', ' ', name_clean).strip()
    if not brand_clean:
        words = name_clean.split()
        brand_clean = f"{words[0]} {words[1]}" if len(words) >= 2 else (words[0] if words else "")
    if brand_clean and name_clean.lower().startswith(brand_clean.lower()):
        name_clean = name_clean[len(brand_clean):].strip()
    name_words = name_clean.split()[:6]
    name_short = ' '.join(name_words)
    queries = []
    if brand_clean and name_short: queries.append(f"{brand_clean} {name_short}")
    if name_short: queries.append(name_short)
    if brand_clean: queries.append(brand_clean)
    unique, seen = [], set()
    for q in queries:
        q = q.strip()
        if len(q) >= 3 and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique[: max(1, OFF_MAX_QUERIES_PER_ITEM)]

def get_off_kcal_100g(nutriments):
    kcal = safe_float(nutriments.get("energy-kcal_100g"))
    if kcal and kcal > 0: return kcal
    kj = safe_float(nutriments.get("energy-kj_100g"))
    if kj and kj > 0: return kj / 4.184
    energy = safe_float(nutriments.get("energy_100g"))
    if energy and energy > 0:
        if "kj" in str(nutriments.get("energy_unit") or "").lower() or energy > 900: return energy / 4.184
        return energy
    return None

def extract_off_calories(off_product, source_mass_g=None):
    nutriments = off_product.get("nutriments") or {}
    kcal_serving = safe_float(nutriments.get("energy-kcal_serving"))
    if kcal_serving and kcal_serving > 0: return round(kcal_serving), "off_serving"
    kj_serving = safe_float(nutriments.get("energy-kj_serving"))
    if kj_serving and kj_serving > 0: return round(kj_serving / 4.184), "off_serving_kj"
    energy_serving = safe_float(nutriments.get("energy_serving"))
    if energy_serving and energy_serving > 0:
        if "kj" in str(nutriments.get("energy_unit") or "").lower() or energy_serving > 900: return round(energy_serving / 4.184), "off_serving_kj"
        return round(energy_serving), "off_serving"
    kcal_100g = get_off_kcal_100g(nutriments)
    if not kcal_100g or kcal_100g <= 0: return None, None
    serving_g = safe_float(off_product.get("serving_quantity"))
    if serving_g and serving_g > 0: return round(kcal_100g * serving_g / 100.0), "off_serving_estimated_from_100g"
    if source_mass_g and source_mass_g > 0: return round(kcal_100g * source_mass_g / 100.0), "source_size_estimated"
    product_g = safe_float(off_product.get("product_quantity"))
    if product_g and product_g > 0: return round(kcal_100g * product_g / 100.0), "off_package_estimated_from_100g"
    return round(kcal_100g), "per_100g"

def token_set(value): return set(re.findall(r"[a-z0-9]+", (value or "").lower()))

def score_off_candidate(candidate, query, brand):
    score = 0.0
    name = candidate.get("product_name") or candidate.get("generic_name") or ""
    brands = candidate.get("brands") or ""
    if isinstance(brands, list): brands = " ".join(brands)
    q_tokens = token_set(query)
    n_tokens = token_set(name)
    if q_tokens: score += 100.0 * len(q_tokens & n_tokens) / len(q_tokens)
    b_tokens = token_set(brand)
    if b_tokens:
        if b_tokens & token_set(brands): score += 30.0
        if b_tokens & n_tokens: score += 10.0
    if safe_float(candidate.get("serving_quantity")): score += 10.0
    if safe_float(candidate.get("product_quantity")): score += 5.0
    return score

def lookup_open_food_facts(name, brand, size_text):
    """Walmart SKUs aren't real barcodes, so this goes straight to the
    (strict) text search rather than trying a barcode lookup first."""
    source_mass_g = parse_mass_grams(size_text)
    logging.info("OFF lookup start: name=%r brand=%r size=%r parsed_mass_g=%s", name, brand, size_text, source_mass_g)

    queries = build_off_queries(name, brand)
    if not queries:
        logging.info("OFF lookup skipped: no usable query could be built.")
        return None

    for query in queries:
        params = {"q": query, "page_size": OFF_PAGE_SIZE, "fields": "code,product_name,brands,nutriments,serving_quantity,product_quantity"}
        url = f"{OFF_SEARCH_BASE}/search?{urllib.parse.urlencode(params)}"
        time.sleep(OFF_PAUSE_SECONDS)

        try:
            resp = http_json(url, headers={"Accept": "application/json"}, label="OFF text search")
        except Exception as exc:
            logging.error("OFF text search failed query=%r error=%r", query, exc)
            continue

        products = resp.get("hits") or []
        api_count = resp.get("count", len(products))
        logging.info("OFF text search result: query=%r api_count=%s returned=%s", query, api_count, len(products))

        candidates = []
        for idx, candidate in enumerate(products):
            off_code = str(candidate.get("code") or candidate.get("_id") or "")
            candidate_name = candidate.get("product_name") or ""

            if not is_valid_fuzzy_match(name, candidate_name):
                logging.info("OFF candidate %s skipped CATEGORY MISMATCH: source=%r off=%r", idx, name, candidate_name)
                continue

            candidate_brand = candidate.get("brands") or ""
            if isinstance(candidate_brand, list): candidate_brand = ", ".join(candidate_brand)
            calories, basis = extract_off_calories(candidate, source_mass_g)
            if calories is None:
                logging.info("OFF candidate %s skipped no calories: code=%s name=%r", idx, off_code, candidate_name)
                continue

            score = score_off_candidate(candidate, query, brand)
            candidates.append((score, idx, calories, basis, candidate))

        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            best_score, _, best_calories, best_basis, best = candidates[0]
            best_code = str(best.get("code") or best.get("_id") or "")
            best_name = best.get("product_name") or ""
            best_brand = best.get("brands") or ""
            if isinstance(best_brand, list): best_brand = ", ".join(best_brand)

            logging.info("OFF selected: query=%r code=%s name=%r calories=%s", query, best_code, best_name, best_calories)

            return {
                "calories": best_calories, "calories_basis": best_basis,
                "off_upc": best_code or None,
                "off_url": f"{OFF_PRODUCT_BASE}/product/{best_code}" if best_code else None,
                "off_name": best_name, "off_brand": best_brand, "off_query": query,
                "match_type": "fuzzy_text",
            }

        logging.warning("OFF query returned no candidates with usable calories: query=%r", query)

    logging.warning("OFF lookup failed to find calories: name=%r brand=%r", name, brand)
    return None

# --- Local CSV sourcing (replaces the Kroger API calls) -----------------

def resolve_csv_path(arg_path):
    if arg_path:
        return Path(arg_path)
    for candidate in DEFAULT_CSV_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    sys.exit(
        "No input file found. Pass a path to your Walmart export "
        "(archive.zip or frozen_food.csv), e.g.:\n"
        "  python build_meal_pool.py frozen_food.csv"
    )

def open_source(path: Path):
    """Text-mode handle for the CSV, whether path is a .zip or a raw .csv."""
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            sys.exit(f"No CSV file found inside {path}")
        if len(csv_names) > 1:
            logging.info("Multiple CSVs found in zip, using first: %s", csv_names[0])
        inner = zf.open(csv_names[0], "r")
        return io.TextIOWrapper(inner, encoding="utf-8", newline="")
    return open(path, "r", encoding="utf-8", newline="")

def is_general_entree(name):
    name_lower = (name or "").lower()
    if any(bad in name_lower for bad in GENERAL_EXCLUDE_KEYWORDS):
        return False
    return any(good in name_lower for good in GENERAL_INCLUDE_KEYWORDS)

def load_frozen_rows(csv_path):
    """Reads the Frozen-department CSV and splits it into deduped
    breakfast / general candidate lists (list of dicts)."""
    breakfast, general = [], []
    seen_skus = set()

    with open_source(csv_path) as f:
        reader = csv.DictReader(f)
        required = {DEPARTMENT_COLUMN, "CATEGORY", "PRODUCT_NAME", "SKU", "PRODUCT_URL"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"Input CSV is missing expected columns: {sorted(missing)}")

        for row in reader:
            if row.get(DEPARTMENT_COLUMN, "").strip() != FROZEN_DEPARTMENT:
                continue
            sku = row.get("SKU", "").strip()
            if not sku or sku in seen_skus:
                continue
            seen_skus.add(sku)

            category = row.get("CATEGORY", "").strip()
            name = row.get("PRODUCT_NAME", "").strip()

            if category == BREAKFAST_CATEGORY:
                breakfast.append(row)
            elif category in GENERAL_CATEGORIES and is_general_entree(name):
                general.append(row)

    logging.info("Loaded %s breakfast candidates, %s general candidates from %s", len(breakfast), len(general), csv_path)
    return breakfast, general

def build_pool(rows, sample_size):
    if RANDOM_SEED is not None:
        random.seed(RANDOM_SEED)
    if sample_size and len(rows) > sample_size:
        rows = random.sample(rows, sample_size)

    pool = []
    for row in rows:
        sku = row.get("SKU", "").strip()
        name = row.get("PRODUCT_NAME", "").strip()
        brand = row.get("BRAND", "").strip().strip('"') or None
        size = row.get("PRODUCT_SIZE", "").strip() or None
        price = safe_float(row.get("PRICE_CURRENT"))
        product_url = row.get("PRODUCT_URL", "").strip() or None
        category = row.get("CATEGORY", "").strip() or None

        logging.info("Processing sku=%s name=%r brand=%r", sku, name, brand)
        off_result = lookup_open_food_facts(name, brand, size)

        if off_result is None:
            logging.info("Skipping sku=%s (no OFF data)", sku)
            continue

        item = {
            "sku": sku,
            "name": name,
            "brand": brand,
            "size": size,
            "price": price,
            "category": category,
            "product_url": product_url,
            "calories": off_result["calories"], "calories_basis": off_result.get("calories_basis"),
            "calories_source": "open_food_facts",
            "off_upc": off_result.get("off_upc"),
            "open_food_facts_url": off_result.get("off_url"),
            "off_name": off_result.get("off_name"), "off_brand": off_result.get("off_brand"),
            "off_query": off_result.get("off_query"),
            "match_type": off_result.get("match_type"),
        }
        pool.append(item)
        logging.info("Added to pool sku=%s calories=%s", sku, item["calories"])

    return pool

def main():
    setup_logging()
    logging.info("Starting meal pool build from local CSV + Open Food Facts calories.")

    arg_path = sys.argv[1] if len(sys.argv) > 1 else None
    csv_path = resolve_csv_path(arg_path)

    breakfast_rows, general_rows = load_frozen_rows(csv_path)
    if not breakfast_rows:
        logging.warning("No breakfast candidates found (category=%r).", BREAKFAST_CATEGORY)
    if not general_rows:
        logging.warning("No general candidates found (categories=%s).", GENERAL_CATEGORIES)

    logging.info("Building breakfast pool (sampling up to %s)...", BREAKFAST_SAMPLE_SIZE)
    breakfast_pool = build_pool(breakfast_rows, BREAKFAST_SAMPLE_SIZE)
    logging.info("Building general lunch/dinner pool (sampling up to %s)...", GENERAL_SAMPLE_SIZE)
    general_pool = build_pool(general_rows, GENERAL_SAMPLE_SIZE)

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": str(csv_path),
        "calories_source": "open_food_facts",
        "breakfast": breakfast_pool, "general": general_pool,
    }
    with open("candidate_pool.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    logging.info("Wrote candidate_pool.json: %s breakfast, %s general.", len(breakfast_pool), len(general_pool))

if __name__ == "__main__":
    main()
