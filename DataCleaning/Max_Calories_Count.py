"""
Cross-checks each candidate_pool.json item's "calories" value against the
USDA FoodData Central API and the Open Food Facts API, and keeps whichever
of {current value, USDA, Open Food Facts} is LARGEST.

Why "largest wins" instead of "most recent source wins": gemini_meal_lookup.py
has been overwriting "calories" with Gemini's from-memory estimate for a
rotating subset of the pool, and a number of those estimates are clearly
wrong-low (e.g. a full entree showing 5-10 calories) rather than
wrong-high. A full entree that's actually ~5-10 calories essentially never
happens, but "the label rounds oddly" or "USDA matched a smaller serving"
can occasionally make a correct value look smaller than another correct
value. Given that asymmetry, taking the max of the three candidates is a
simple, defensible heuristic: it can never make an already-too-low value
worse, and the risk of it overshooting is low. It's a heuristic, not
ground truth -- see NOTES at the bottom of this file's --help output.

Only overwrites "calories" when a fetched value is STRICTLY GREATER than
the current stored value. Never lowers a value, even if you suspect the
current one is inflated -- run this, then spot check the diff log this
script prints/writes before trusting it blindly.

Env var required: USDA_API_KEY (same one build_meal_pool.py uses).
Open Food Facts needs no key, just a descriptive User-Agent (set below).

Modeled on this repo's other pool-maintenance scripts (build_meal_pool.py,
gemini_meal_lookup.py): incremental save, resumable via a per-item
timestamp field, rate-limited, safe to Ctrl-C and rerun.

Usage:
    export USDA_API_KEY=...
    python calorie_max_check.py                  # process the whole pool
    python calorie_max_check.py --limit 200       # just the next 200 unchecked
    python calorie_max_check.py --recheck-all     # ignore checked-at, redo everyone
    python calorie_max_check.py --dry-run         # log what WOULD change, write nothing

Output: overwrites candidate_pool.json in place (same file, same shape --
adds two new bookkeeping fields per item: "_calorie_max_checked_at" and
"_calorie_max_source"). Also writes calorie_max_check_diff.csv next to it
listing every item that changed, so you can eyeball the diffs before
trusting them.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

INPUT_JSON = "candidate_pool.json"
OUTPUT_JSON = "candidate_pool.json"
DIFF_CSV = "calorie_max_check_diff.csv"

USDA_API_KEY = os.environ.get("USDA_API_KEY")
OFF_USER_AGENT = "FrozenMealDiet/1.0 (github.com/mzaiger/FrozenMealDiet)"

USDA_MIN_INTERVAL = 0.4   # ~150/min, well under the 1000/hr free-key limit spread over a run
OFF_MIN_INTERVAL = 1.1    # Open Food Facts asks for <=100 req/min from anonymous callers; stay well under
REQUEST_TIMEOUT = 10
SAVE_EVERY = 25           # write candidate_pool.json to disk every N items processed


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


class RateLimiter:
    def __init__(self, min_interval):
        self._min_interval = min_interval
        self._next_allowed = 0.0

    def wait(self):
        now = time.monotonic()
        start_at = max(now, self._next_allowed)
        self._next_allowed = start_at + self._min_interval
        sleep_for = start_at - now
        if sleep_for > 0:
            time.sleep(sleep_for)


usda_limiter = RateLimiter(USDA_MIN_INTERVAL)
off_limiter = RateLimiter(OFF_MIN_INTERVAL)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def clean_product_name(raw_name):
    """Same cleaning build_meal_pool.py uses, so both APIs see the same
    normalized query and results stay comparable."""
    if not raw_name:
        return ""
    cleaned = re.sub(r',?\s*Frozen Meals.*', '', raw_name, flags=re.IGNORECASE)
    cleaned = re.sub(r',?\s*\d+(\.\d+)?\s*(oz|ct|count|g|lb).*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\d+\s+\d+/\d+\s*(ounce|oz|lb)?\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\d+/\d+\s*(ounce|oz|lb)?\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'[/,.\'"]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def parse_current_calories(value):
    """The existing 'calories' field is sometimes an int, sometimes a
    numeric string, sometimes 'N/A'. Returns a float or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = re.search(r'-?\d+(\.\d+)?', text)
    return float(match.group()) if match else None


# ---------------------------------------------------------------------------
# USDA FoodData Central
# ---------------------------------------------------------------------------

def usda_lookup(product_name, max_retries=3):
    """Returns calories-per-serving (float) or None. Mirrors
    build_meal_pool.py's fetch_usda_info but only returns the calorie
    half -- we already have serving text from the original pass."""
    if not USDA_API_KEY:
        return None
    cleaned_query = clean_product_name(product_name)
    if not cleaned_query:
        return None

    url = "https://api.nal.usda.gov/fdc/v1/foods/search?" + urllib.parse.urlencode({
        "api_key": USDA_API_KEY,
        "query": cleaned_query,
        "dataType": "Branded",
        "pageSize": 5,
    })

    for attempt in range(1, max_retries + 1):
        usda_limiter.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.load(resp)

            foods = data.get("foods") or []
            if not foods:
                return None
            best = foods[0]

            label_nutrients = best.get("labelNutrients") or {}
            if "calories" in label_nutrients and label_nutrients["calories"].get("value") is not None:
                return float(label_nutrients["calories"]["value"])

            serving_size = best.get("servingSize")
            serving_size_unit = (best.get("servingSizeUnit") or "").upper()
            for n in best.get("foodNutrients", []):
                n_id = n.get("nutrientId") or n.get("nutrient", {}).get("id")
                n_name = n.get("nutrientName") or n.get("nutrient", {}).get("name", "")
                unit = n.get("unitName") or n.get("nutrient", {}).get("unitName", "")
                if n_id in (1008, 2047) or (n_name.lower() == "energy" and unit.lower() == "kcal"):
                    per_100g = n.get("value") if "value" in n else n.get("amount")
                    if per_100g is None:
                        continue
                    if serving_size and serving_size_unit in ("GRM", "G"):
                        return round(per_100g * serving_size / 100.0, 1)
                    return float(per_100g)  # per-100g fallback, better than nothing
            return None

        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(attempt * 2)
                continue
            log(f"USDA error for '{cleaned_query}': {e}")
            return None
        except Exception as e:
            log(f"USDA error for '{cleaned_query}': {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Open Food Facts
# ---------------------------------------------------------------------------

def off_lookup(product_name, max_retries=3):
    """Returns (calories_per_serving, is_100g_fallback) or (None, False),
    using Open Food Facts' search endpoint. Prefers the label's own
    per-serving kcal; falls back to scaling per-100g kcal by
    serving_quantity (grams), or as a last resort returns the raw
    per-100g value with is_100g_fallback=True so callers can flag it as
    less directly comparable."""
    cleaned_query = clean_product_name(product_name)
    if not cleaned_query:
        return None, False

    url = "https://world.openfoodfacts.org/cgi/search.pl?" + urllib.parse.urlencode({
        "search_terms": cleaned_query,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": 5,
        "fields": "product_name,nutriments,serving_quantity,serving_size",
    })

    for attempt in range(1, max_retries + 1):
        off_limiter.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": OFF_USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.load(resp)

            products = data.get("products") or []
            if not products:
                return None, False
            best = products[0]
            nutriments = best.get("nutriments") or {}

            # Prefer an explicit per-serving kcal figure.
            per_serving = nutriments.get("energy-kcal_serving")
            if per_serving is not None:
                return float(per_serving), False

            # Fall back: per-100g kcal, scaled by serving_quantity (grams).
            per_100g = nutriments.get("energy-kcal_100g")
            serving_qty = best.get("serving_quantity")
            if per_100g is not None and serving_qty:
                try:
                    return round(float(per_100g) * float(serving_qty) / 100.0, 1), False
                except (TypeError, ValueError):
                    pass

            # Last resort: raw per-100g value. Not directly comparable to
            # a per-serving figure, but still useful as a floor -- most
            # frozen entree servings are >100g, so per-100g rarely
            # OVERSTATES the true per-serving calories, keeping this safe
            # for a "take the max" comparison. Flagged so callers can
            # treat it with extra suspicion.
            if per_100g is not None:
                return float(per_100g), True

            return None, False

        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(attempt * 2)
                continue
            log(f"OFF error for '{cleaned_query}': {e}")
            return None, False
        except Exception as e:
            log(f"OFF error for '{cleaned_query}': {e}")
            return None, False
    return None, False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cross-check calories against USDA + Open Food Facts, keep the max.",
        epilog=(
            "NOTES: this is a heuristic, not ground truth. 'Largest wins' assumes "
            "wrong-low values (Gemini estimation errors) are the main problem, "
            "which matches what's currently in this pool -- entrees showing "
            "0-10 calories. Always check calorie_max_check_diff.csv after a run "
            "before trusting the results blindly, especially any item where the "
            "chosen source was Open Food Facts' per-100g fallback (marked "
            "'off_100g_fallback' in the source column), since that's the least "
            "directly comparable of the value types this script considers."
        ),
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Max items to process this run (default: all).")
    parser.add_argument("--recheck-all", action="store_true",
                         help="Ignore _calorie_max_checked_at and reprocess every item.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Log what would change but don't write candidate_pool.json.")
    args = parser.parse_args()

    if not USDA_API_KEY:
        log("WARNING: USDA_API_KEY not set -- USDA lookups will be skipped, only Open Food Facts will run.")

    with open(INPUT_JSON, encoding="utf-8") as f:
        items = json.load(f)

    to_process = items if args.recheck_all else [
        it for it in items if not it.get("_calorie_max_checked_at")
    ]
    if args.limit:
        to_process = to_process[: args.limit]

    log(f"Loaded {len(items)} items. Processing {len(to_process)} this run.")

    diff_rows = []
    changed = 0
    checked = 0

    for idx, item in enumerate(to_process, start=1):
        name = item.get("PRODUCT_NAME", "")
        current = parse_current_calories(item.get("calories"))

        usda_cal = usda_lookup(name)
        off_cal, off_is_100g_fallback = off_lookup(name)
        off_source_label = "off_100g_fallback" if off_is_100g_fallback else "open_food_facts"

        candidates = []
        if current is not None:
            candidates.append(("current", current))
        if usda_cal is not None:
            candidates.append(("usda", usda_cal))
        if off_cal is not None:
            candidates.append((off_source_label, off_cal))

        item["_calorie_max_checked_at"] = datetime.now(timezone.utc).isoformat()

        if candidates:
            source, best_value = max(candidates, key=lambda pair: pair[1])
            item["_calorie_max_source"] = source
            if current is None or best_value > current:
                old = item.get("calories")
                item["calories"] = best_value
                changed += 1
                diff_rows.append({
                    "PRODUCT_NAME": name,
                    "old_calories": old,
                    "new_calories": best_value,
                    "source": source,
                    "usda_value": usda_cal,
                    "off_value": off_cal,
                })
                log(f"  ^ {name[:60]}: {old} -> {best_value} (source: {source})")
        else:
            item["_calorie_max_source"] = "none_found"

        checked += 1
        if checked % SAVE_EVERY == 0 and not args.dry_run:
            with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2, ensure_ascii=False)
            log(f"Progress saved: {checked}/{len(to_process)} processed, {changed} changed so far.")

    if not args.dry_run:
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)

        if diff_rows:
            with open(DIFF_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(diff_rows[0].keys()))
                writer.writeheader()
                writer.writerows(diff_rows)
            log(f"Wrote {len(diff_rows)} changed rows to {DIFF_CSV}")

    log(f"Done. Checked {checked} items, changed {changed}.")
    if args.dry_run:
        log("(dry run -- candidate_pool.json was NOT written)")


if __name__ == "__main__":
    main()