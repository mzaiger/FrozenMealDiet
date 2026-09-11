import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

INPUT_CSV = "frozen_food_deduped.csv"  # Using deduplicated file
OUTPUT_JSON = "candidate_pool.json"
API_KEY = os.environ.get("USDA_API_KEY")
MAX_WORKERS = 5
PAGE_REQUEST_TIMEOUT = 10
ITEMS_PER_MINUTE = 15
DEBUG_TIME_LIMIT_SECONDS = 180  # 3 minutes


def clean_product_name(raw_name):
    """
    Cleans raw Walmart product names for better USDA FDC search hits.
    Handles fractions like '9 7/8 Ounce', quotes, slashes, and commas.
    """
    if not raw_name:
        return ""

    # 1. Strip trailing descriptors like ', Frozen Meals, 12 oz.' or ', 24.8 oz, 50 ct'
    cleaned = re.sub(r',?\s*Frozen Meals.*', '', raw_name, flags=re.IGNORECASE)
    cleaned = re.sub(r',?\s*\d+(\.\d+)?\s*(oz|ct|count|g|lb).*', '', cleaned, flags=re.IGNORECASE)

    # 2. Strip fractional weights/sizes like '9 7/8 Ounce' or '1/2 lb'
    cleaned = re.sub(r'\b\d+\s+\d+/\d+\s*(ounce|oz|lb)?\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\d+/\d+\s*(ounce|oz|lb)?\b', '', cleaned, flags=re.IGNORECASE)

    # 3. Remove forward slashes, apostrophes, periods, and commas
    cleaned = re.sub(r'[/,.\'"]', ' ', cleaned)

    # 4. Collapse multiple spaces into a single space
    cleaned = re.sub(r'\s+', ' ', cleaned)

    return cleaned.strip()


def fetch_usda_info(product_name, max_retries=3):
    """Queries the USDA FDC API for product calorie and servings per container information with retry logic."""
    cleaned_query = clean_product_name(product_name)
    if not cleaned_query:
        return "N/A", "N/A"

    search_url = "https://api.nal.usda.gov/fdc/v1/foods/search?" + urllib.parse.urlencode({
        "api_key": API_KEY,
        "query": cleaned_query,
        "dataType": "Branded",
        "pageSize": 5,
    })

    # Retry loop for handling transient 503/500 network errors
    for attempt in range(1, max_retries + 1):
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

        except urllib.error.HTTPError as e:
            # If server returns 503/500 or temporary error, pause and retry
            if e.code in (500, 502, 503, 504) and attempt < max_retries:
                wait_time = attempt * 2  # Waits 2s, then 4s
                print(f"HTTP {e.code} for '{cleaned_query}'. Retrying in {wait_time}s (Attempt {attempt}/{max_retries})...")
                time.sleep(wait_time)
            else:
                print(f"Error fetching USDA info for '{cleaned_query}': {e}")
                break
        except Exception as e:
            print(f"Error fetching USDA info for '{cleaned_query}': {e}")
            break

    return "N/A", "N/A"

def process_row(row):
    """Processes each row using exact header fields from frozen_food.csv."""
    product_name = row.get("PRODUCT_NAME", "")

    calories, servings_per_container = fetch_usda_info(product_name)

    updated_row = dict(row)
    updated_row["calories"] = calories
    updated_row["servings_per_container"] = servings_per_container

    return updated_row


def load_existing_results():
    """Loads existing processed rows from candidate_pool.json if present."""
    if os.path.exists(OUTPUT_JSON):
        try:
            with open(OUTPUT_JSON, mode="r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"Warning: Could not parse existing '{OUTPUT_JSON}': {e}")
    return []


def save_results(processed_rows):
    """Writes the current list of processed rows to candidate_pool.json."""
    with open(OUTPUT_JSON, mode="w", encoding="utf-8") as f:
        json.dump(processed_rows, f, indent=2, ensure_ascii=False)


def get_row_key(row):
    """Generates a unique key for a row based on SKU or PRODUCT_NAME + PRODUCT_URL."""
    if row.get("SKU"):
        return str(row["SKU"]).strip()
    return f"{row.get('PRODUCT_NAME', '')}|{row.get('PRODUCT_URL', '')}"


def main():
    parser = argparse.ArgumentParser(description="Process frozen food CSV with USDA info.")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode for 3 minutes (~45 items).")
    args = parser.parse_args()

    # 1. Load previously saved results
    processed_rows = load_existing_results()
    processed_keys = {get_row_key(r) for r in processed_rows}
    print(f"Found {len(processed_rows)} existing items in '{OUTPUT_JSON}'.")

    # 2. Load input CSV and skip already processed rows
    rows_to_process = []
    already_done_count = 0

    with open(INPUT_CSV, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_key = get_row_key(row)
            if row_key in processed_keys:
                already_done_count += 1
                continue

            rows_to_process.append(row)

    total_remaining = len(rows_to_process)
    print(f"Skipping {already_done_count} items already completed in '{OUTPUT_JSON}'.")
    print(f"Remaining unique items to process: {total_remaining}")

    if total_remaining == 0:
        print("All items have already been processed!")
        return

    if args.debug:
        print(f"Debug mode ENABLED: Will run for up to {DEBUG_TIME_LIMIT_SECONDS // 60} minutes (~{ITEMS_PER_MINUTE * (DEBUG_TIME_LIMIT_SECONDS // 60)} items).")

    start_time = time.time()
    row_index = 0

    while row_index < total_remaining:
        elapsed_total = time.time() - start_time
        if args.debug and elapsed_total >= DEBUG_TIME_LIMIT_SECONDS:
            print(f"\nDebug time limit reached ({DEBUG_TIME_LIMIT_SECONDS} seconds). Stopping early.")
            break

        batch_start_time = time.time()
        batch_rows = rows_to_process[row_index : row_index + ITEMS_PER_MINUTE]
        print(f"\n[Minute {(int(elapsed_total) // 60) + 1}] Processing batch of {len(batch_rows)} rows ({row_index + 1} to {row_index + len(batch_rows)} of {total_remaining})...")

        batch_results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_row, r) for r in batch_rows]
            for future in as_completed(futures):
                batch_results.append(future.result())

        # Append newly completed batch and export immediately
        processed_rows.extend(batch_results)
        save_results(processed_rows)
        print(f"Saved {len(processed_rows)} total records to '{OUTPUT_JSON}'.")

        row_index += len(batch_rows)

        # Pause to enforce 15/min rate limit if more items remain
        if row_index < total_remaining:
            batch_duration = time.time() - batch_start_time
            sleep_time = 60.0 - batch_duration

            if args.debug and (time.time() - start_time + sleep_time) >= DEBUG_TIME_LIMIT_SECONDS:
                print("Debug mode total time threshold reached. Stopping.")
                break

            if sleep_time > 0:
                print(f"Batch completed in {batch_duration:.1f}s. Pausing {sleep_time:.1f}s to satisfy rate limit...")
                time.sleep(sleep_time)

    total_elapsed = time.time() - start_time
    print(f"\nDone! Processed batch in {total_elapsed / 60:.2f} minutes.")
    print(f"Total dataset size in '{OUTPUT_JSON}': {len(processed_rows)} items.")


if __name__ == "__main__":
    main()
