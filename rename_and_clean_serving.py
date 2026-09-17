"""
rename_and_clean_serving.py

One-time migration for candidate_pool.json:
  1. Renames the "servings_per_container" key to "serving" on every row.
  2. Cleans the value with the same clean_serving_text() logic now used
     in build_meal_pool.py and kroger_new_items.py -- some USDA-sourced
     values bundle the serving amount AND how-many-servings-are-in-the-
     container into one string (e.g. "2.71 OZ SERVING, 36 Servings Per
     Container"), which this strips down to just the amount.

Safe to re-run: rows that already only have "serving" (no old key) are
left alone, and clean_serving_text() is idempotent (re-cleaning an
already-clean value doesn't change it).

Usage:
    python rename_and_clean_serving.py candidate_pool.json
    python rename_and_clean_serving.py candidate_pool.json --dry-run
"""

import argparse
import json
import re
import sys
from pathlib import Path


def clean_serving_text(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() == "N/A":
        return text or "N/A"
    text = re.split(r",\s*(?:about\s+)?[\d.]+\s*servings?\s+per\s+container", text, flags=re.I)[0]
    text = re.sub(r"\s*per\s+serving\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*per\s+container\s*$", "", text, flags=re.I)
    text = text.strip().rstrip(",").strip()
    if text and not re.search(r"\d", text):
        return "N/A"
    return text or "N/A"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="Path to candidate_pool.json")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    path = Path(args.path)
    pool = json.loads(path.read_text(encoding="utf-8"))

    renamed = 0
    cleaned = 0
    for item in pool:
        old_val = item.pop("servings_per_container", None)
        if old_val is not None:
            renamed += 1
            if "serving" not in item:
                item["serving"] = old_val

        if "serving" in item:
            new_val = clean_serving_text(item["serving"])
            if new_val != item["serving"]:
                cleaned += 1
                item["serving"] = new_val

    print(f"{'Would rename' if args.dry_run else 'Renamed'} {renamed} row(s) "
          f"servings_per_container -> serving.")
    print(f"{'Would clean' if args.dry_run else 'Cleaned'} {cleaned} messy/compound serving value(s).")

    if args.dry_run:
        print("Dry run -- nothing written.")
        return

    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)
        f.write("\n")
    Path(tmp_path).replace(path)
    print(f"Saved -> {path}")


if __name__ == "__main__":
    main()
