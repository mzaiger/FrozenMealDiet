#!/usr/bin/env python3
"""
Check whether Walmart product URLs are still "live" by searching Google
for the product ID and seeing if walmart.com is the top organic result.

Why this works: dead/delisted Walmart product pages get de-indexed from
Google fairly quickly, so "site:walmart.com <product_id>" stops returning
a matching result at position 1. This mirrors what you were doing by hand.

Uses Serper.dev (https://serper.dev/) — 2,500 free searches on signup,
no credit card required. Set your key as an environment variable:
    export SERPER_API_KEY="your_key_here"

Usage:
    python check_walmart_links.py urls.txt              # one URL per line
    python check_walmart_links.py pool.json --json       # candidate_pool-style JSON
    python check_walmart_links.py pool.json --json --debug   # only checks first 10
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests

SERPER_ENDPOINT = "https://google.serper.dev/search"
DEBUG_LIMIT = 10

# Regex: grabs the last run of digits before an optional query string,
# e.g. .../10794571?fulfillmentIntent=Pickup -> "10794571"
PRODUCT_ID_RE = re.compile(r"/(\d+)(?:[/?#]|$)")


def extract_product_id(url: str) -> str | None:
    """Pull the numeric Walmart item ID out of a product URL."""
    match = PRODUCT_ID_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def check_link_active(product_id: str, api_key: str, retries: int = 2) -> dict:
    """
    Search Google (via Serper.dev) for "site:walmart.com <product_id>" and check
    whether the top result is on walmart.com AND contains the product ID.

    Returns a dict with status details — including the exact query used
    and the top result URL — so you can see *why* something was marked
    inactive (no results at all vs. wrong domain vs. ID missing from URL).
    """
    query = f"site:walmart.com {product_id}"
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": 5}

    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=15)
            if resp.status_code == 401 or resp.status_code == 403:
                return {
                    "product_id": product_id,
                    "query": query,
                    "active": False,
                    "reason": f"auth_error: check SERPER_API_KEY (status {resp.status_code})",
                    "first_result_url": None,
                }
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(1.5 * (attempt + 1))
    else:
        return {
            "product_id": product_id,
            "query": query,
            "active": False,
            "reason": f"request_failed: {last_error}",
            "first_result_url": None,
        }

    organic = data.get("organic", [])
    if not organic:
        return {
            "product_id": product_id,
            "query": query,
            "active": False,
            "reason": "no_search_results",
            "first_result_url": None,
        }

    first_url = organic[0].get("link", "")
    netloc = urlparse(first_url).netloc.lower().split(":")[0]  # strip port if present
    # Exact domain or real subdomain only — NOT a substring match, so
    # lookalikes like "myshopwalmart.com" or "dealswalmart.com" can't
    # falsely count as walmart.com the way "walmart.com" in netloc would.
    is_walmart_domain = netloc == "walmart.com" or netloc.endswith(".walmart.com")
    id_in_url = product_id in first_url

    if not is_walmart_domain:
        reason = "top_result_not_walmart"
    elif not id_in_url:
        reason = "walmart_domain_but_id_not_in_url"
    else:
        reason = "ok"
    is_active = is_walmart_domain and id_in_url

    return {
        "product_id": product_id,
        "query": query,
        "active": is_active,
        "reason": reason,
        "first_result_url": first_url,
    }


def load_urls_from_txt(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def process_candidate_pool(path: str, out_path: str, api_key: str, delay: float,
                            resume: bool, debug: bool):
    """
    Native mode for files shaped like candidate_pool.json: a flat list of
    product records with a PRODUCT_URL field. Writes the full list back
    out to out_path (never overwrites the original input file) with
    'active' set fresh by this script.

    Any pre-existing 'active' / '_active_check_reason' values from an
    earlier attempt are stripped on a fresh start — they're not trusted.

    Resume works by re-reading out_path itself (if it already exists)
    rather than the original input, since that's the only file this
    script ever writes progress to. A record counts as "already done"
    only if it has '_active_check_query' set, a field only this script
    writes — so old data can never be mistaken for a completed check.

    In --debug mode, only the first DEBUG_LIMIT unchecked records are
    processed (the rest are left untouched) so you can sanity-check
    behavior before burning your free quota on all 1801 items.
    """
    if resume and os.path.exists(out_path):
        with open(out_path) as f:
            records = json.load(f)
        print(f"Resuming from existing {out_path} ...")
    else:
        with open(path) as f:
            records = json.load(f)
        for record in records:
            record.pop("active", None)
            record.pop("_active_check_reason", None)
        print(f"Starting fresh from {path} "
              f"(cleared any old active/_active_check_reason values) -> {out_path}")

    if not isinstance(records, list):
        raise ValueError("Expected a JSON list of product records.")

    total = len(records)
    checked = 0
    for i, record in enumerate(records, 1):
        if resume and record.get("_active_check_query") is not None:
            continue  # already checked by this script in a previous run

        if debug and checked >= DEBUG_LIMIT:
            print(f"\n[debug mode] Stopping after {DEBUG_LIMIT} checks "
                  f"(remaining {total - i + 1} records left untouched).")
            break

        url = record.get("PRODUCT_URL", "")
        sku = record.get("SKU", "")
        product_id = extract_product_id(url) or sku

        if not product_id:
            record["active"] = False
            record["_active_check_reason"] = "could_not_parse_id"
            record["_active_check_query"] = None
            record["_active_check_url"] = None
            print(f"[{i}/{total}] {url or '(no url)'} -> COULD NOT PARSE ID")
            continue

        status = check_link_active(str(product_id), api_key)
        record["active"] = status["active"]
        record["_active_check_reason"] = status["reason"]
        record["_active_check_query"] = status["query"]     # exact search query used
        record["_active_check_url"] = status["first_result_url"]  # what the top result actually was
        checked += 1

        label = "ACTIVE" if status["active"] else "INACTIVE"
        print(f"[{i}/{total}] {product_id} -> {label} ({status['reason']})\n"
              f"    checked: {url}\n"
              f"    query:   \"{status['query']}\"\n"
              f"    top hit: {status['first_result_url']}")

        # Write progress after every item so a crash/interrupt doesn't lose work
        with open(out_path, "w") as f:
            json.dump(records, f, indent=2)

        if i < total:
            time.sleep(delay)

    active_count = sum(1 for r in records if r.get("active") is True)
    inactive_count = sum(1 for r in records if r.get("active") is False)
    print(f"\nDone. Checked {checked} this run. "
          f"Totals: {active_count} active / {inactive_count} inactive / {total} total. "
          f"Written to {out_path}")


def process_txt(path: str, out_path: str, api_key: str, delay: float, debug: bool):
    urls = load_urls_from_txt(path)
    if debug:
        urls = urls[:DEBUG_LIMIT]
        print(f"[debug mode] Only checking the first {len(urls)} URLs.")

    results = []
    for i, url in enumerate(urls, 1):
        product_id = extract_product_id(url)
        if not product_id:
            results.append({
                "url": url,
                "product_id": None,
                "query": None,
                "active": False,
                "reason": "could_not_parse_id",
                "first_result_url": None,
            })
            print(f"[{i}/{len(urls)}] {url} -> COULD NOT PARSE ID")
            continue

        status = check_link_active(product_id, api_key)
        status["url"] = url
        results.append(status)

        label = "ACTIVE" if status["active"] else "INACTIVE"
        print(f"[{i}/{len(urls)}] {product_id} -> {label} ({status['reason']})\n"
              f"    checked: {url}\n"
              f"    query:   \"{status['query']}\"\n"
              f"    top hit: {status['first_result_url']}")

        if i < len(urls):
            time.sleep(delay)

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    active_count = sum(1 for r in results if r["active"])
    print(f"\nDone: {active_count}/{len(results)} active. Results written to {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", help="Path to a .txt (one URL per line) or a candidate_pool-style .json file")
    parser.add_argument("--json", action="store_true",
                         help="Treat input as a candidate_pool-style JSON list (records with PRODUCT_URL/SKU/active)")
    parser.add_argument("--out", default=None,
                         help="Where to write results (default: link_status.json, or candidate_pool_checked.json for --json)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to sleep between requests")
    parser.add_argument("--no-resume", action="store_true",
                         help="Re-check every record, even ones that already have an active status (--json mode only)")
    parser.add_argument("--debug", action="store_true",
                         help=f"Only check the first {DEBUG_LIMIT} items — use this before running the full batch")
    args = parser.parse_args()

    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        sys.exit("Set SERPER_API_KEY in your environment first: export SERPER_API_KEY=your_key")

    if args.json:
        out_path = args.out or "candidate_pool_checked.json"
        process_candidate_pool(args.input_file, out_path, api_key, args.delay,
                                resume=not args.no_resume, debug=args.debug)
    else:
        out_path = args.out or "link_status.json"
        process_txt(args.input_file, out_path, api_key, args.delay, debug=args.debug)


if __name__ == "__main__":
    main()