import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

INPUT_CSV = "frozen_food.csv"
OUTPUT_JSON = "candidate_pool.json"
API_KEY = os.environ.get("USDA_API_KEY")
MAX_WORKERS = 5
PAGE_REQUEST_TIMEOUT = 10
ITEMS_PER_MINUTE = 15
DEBUG_TIME_LIMIT_SECONDS = 600  # 3 minutes

# Categories to skip during processing
EXCLUDED_CATEGORIES = {
    "Frozen Desserts",
    "Frozen Meat & Seafood",
    "Frozen Produce",
    "Frozen Potatoes",
}


def clean_product_name(raw_name):
    """
    Cleans raw Walmart product names for better USDA FDC search hits.
    Example: 'Marie Callender's Aged Cheddar Cheesy Chicken & Rice Bowl, Frozen Meals, 12 oz.'
          -> 'Marie Callender's Aged Cheddar Cheesy Chicken & Rice Bowl'
    """
    if not raw_name:
        return ""
    cleaned = re.sub(r',?\s*Frozen Meals.*', '', raw_name, flags=re.IGNORECASE)
    cleaned = re.sub(r',?\s*\d+(\.\d+)?\s*(oz|ct|count|g|lb).*', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def fetch_usda_info(product_name):
    """Queries the USDA FDC API for product calorie and servings per container information."""
    cleaned_query = clean_product_name(product_name)
    if not cleaned_query:
        return "N/A", "N/A"

    search_url = "https://api.nal.usda.gov/fdc/v1/foods/search?" + urllib.parse.urlencode({
        "api_key": API_KEY,
        "query": cleaned_query,
        "dataType": "Branded",
        "pageSize": 5,
    })

    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=PAGE_REQUEST_TIMEOUT) as resp:
            search_data = json.load(resp)

        foods = search_data.get("foods") or []
        if not foods:
            return "N/A", "N/A"

        best = foods[0]

        # Check top-level fields & fallbacks for servings
        servings = (
            best.get("servingsPerContainer") 
            or best.get("householdServingFullText")
            or best.get("packageWeight")
        )

        if not servings:
            label_nutrients = best.get("labelNutrients") or {}
            servings = label_nutrients.get("servingsPerContainer", {}).get("value")

        formatted_servings = str(servings).strip() if servings is not None else "N/A"

        # Check calories
        serving_size = best.get("servingSize")
        serving_size_unit = best.get("servingSizeUnit")
        calories = "N/A"

        label_nutrients = best.get("labelNutrients") or {}
        if "calories" in label_nutrients and label_nutrients["calories"].get("value") is not None:
            calories = label_nutrients["calories"]["value"]
        else:
            for n in best.get("foodNutrients", []):
                n_id = n.get("nutrientId") or n.get("nutrient", {}).get("id")
                n_name = n.get("nutrientName") or n.get("nutrient", {}).get("name", "")
                unit = n.get("unitName") or n.get("nutrient", {}).get("unitName", "")

                if n_id in (1008, 2047) or (n_name.lower() == "energy" and unit.lower() == "kcal"):
                    per_100g = n.get("value") if "value" in n else n.get("amount")
                    if per_100g is not None:
                        if serving_size and serving_size_unit and serving_size_unit.upper() in ("GRM", "G"):
                            calories = round(per_100g * serving_size / 100.0)
                        else:
                            calories = per_100g
                        break

        return calories, formatted_servings

    except Exception as e:
        print(f"Error fetching USDA info for '{cleaned_query}': {e}")

    return "N/A", "N/A"


def process_row(row):
    """Processes each row using exact header fields from frozen_food.csv."""
    product_name = row.get("PRODUCT_NAME", "")

    calories, servings_per_container = fetch_usda_info(product_name)

    updated_row = dict(row)
    updated_row["calories"] = calories
    updated_row["servings_per_container"] = servings_per_container

    return updated_row


def main():
    parser = argparse.ArgumentParser(description="Process frozen food CSV with USDA info.")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode for 3 minutes (~45 items).")
    args = parser.parse_args()

    rows = []
    skipped_count = 0

    with open(INPUT_CSV, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            category = row.get("CATEGORY", "").strip()
            if category in EXCLUDED_CATEGORIES:
                skipped_count += 1
                continue
            rows.append(row)

    total_rows = len(rows)
    print(f"Loaded {total_rows} remaining rows (filtered out {skipped_count} items from excluded categories).")

    if args.debug:
        print(f"Debug mode ENABLED: Will run for up to {DEBUG_TIME_LIMIT_SECONDS // 60} minutes (~{ITEMS_PER_MINUTE * (DEBUG_TIME_LIMIT_SECONDS // 60)} items).")

    processed_rows = []
    start_time = time.time()
    row_index = 0

    while row_index < total_rows:
        # Check overall debug timer
        elapsed_total = time.time() - start_time
        if args.debug and elapsed_total >= DEBUG_TIME_LIMIT_SECONDS:
            print(f"\nDebug time limit reached ({DEBUG_TIME_LIMIT_SECONDS} seconds). Stopping early.")
            break

        # Grab next batch of up to 15 items
        batch_start_time = time.time()
        batch_rows = rows[row_index : row_index + ITEMS_PER_MINUTE]
        print(f"\n[Minute {(int(elapsed_total) // 60) + 1}] Processing batch of {len(batch_rows)} rows ({row_index + 1} to {row_index + len(batch_rows)} of {total_rows})...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_row, r) for r in batch_rows]
            for future in as_completed(futures):
                processed_rows.append(future.result())

        row_index += len(batch_rows)

        # Check if more rows remain and if we need to throttle to maintain 15/min
        if row_index < total_rows:
            batch_duration = time.time() - batch_start_time
            sleep_time = 60.0 - batch_duration

            # If debug mode will hit limit before next batch finishes, break early
            if args.debug and (time.time() - start_time + sleep_time) >= DEBUG_TIME_LIMIT_SECONDS:
                print(f"Debug mode total time threshold reached. Stopping.")
                break

            if sleep_time > 0:
                print(f"Batch completed in {batch_duration:.1f}s. Pausing {sleep_time:.1f}s to satisfy 15/min rate limit...")
                time.sleep(sleep_time)

    # Export results to candidate_pool.json
    with open(OUTPUT_JSON, mode="w", encoding="utf-8") as f:
        json.dump(processed_rows, f, indent=2, ensure_ascii=False)

    total_elapsed = time.time() - start_time
    print(f"\nDone! Processed {len(processed_rows)} rows in {total_elapsed / 60:.2f} minutes.")
    print(f"Exported dataset to '{OUTPUT_JSON}'.")


if __name__ == "__main__":
    main()
