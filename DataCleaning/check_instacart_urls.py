#!/usr/bin/env python3
"""
Check whether a product actually has a real Instacart listing, by
searching "instacart <brand> <product name>" via Serper.dev and scanning
every organic result for the first one on instacart.com -- same
multi-result scanning kroger_new_items.py's find_walmart_listing()
already does for Walmart, rather than only trusting whatever the top
result happens to be.

Mirrors check_walmart_links.py's overall technique and conventions
(resumable, checkpointed writes, --debug/--limit), just pointed at
instacart.com instead of walmart.com, with that one difference in how
many results get checked.

Uses Serper.dev (https://serper.dev/) -- same SERPER_API_KEY as
check_walmart_links.py and kroger_new_items.py.
    export SERPER_API_KEY="your_key_here"

Sets two fields per record:
    INSTACART_URL     -- only overwritten when a real instacart.com match
                          is found; left as-is otherwise (so a previously
                          guessed search-results URL, e.g. from
                          update_instacart_urls.py, isn't erased just
                          because this run didn't confirm it)
    instacart_active  -- true if any result was on instacart.com, false
                          otherwise. index.html uses this (alongside the
                          existing "active" field for Walmart) to decide
                          whether to show the Instacart link at all.

Usage:
    python check_instacart_urls.py candidate_pool.json --debug   # sanity check first 10
    python check_instacart_urls.py candidate_pool.json           # full run, writes to
                                                                   # candidate_pool_instacart_checked.json
    python check_instacart_urls.py candidate_pool.json --out candidate_pool.json  # update in place
    python check_instacart_urls.py candidate_pool.json --limit 500  # cap Serper calls this run
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

import requests

SERPER_ENDPOINT = "https://google.serper.dev/search"
DEBUG_LIMIT = 10


def check_instacart_listing(brand: str, product_name: str, api_key: str, retries: int = 2) -> dict:
    """Searches 'instacart <brand> <product_name>' and checks every
    organic result Serper returns for the first one on instacart.com --
    same multi-result scanning kroger_new_items.py's find_walmart_listing()
    already does for Walmart, rather than only trusting the top hit.
    Serper returns the same 5ish results either way, so there's no extra
    cost to checking more than just the first one. Returns a dict with
    the query, whether a match was found, and the top result URL for
    visibility into why."""
    query = f"instacart {brand} {product_name}".strip()
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": 5}

    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=15)
            if resp.status_code in (401, 403):
                return {
                    "query": query, "active": False,
                    "reason": f"auth_error: check SERPER_API_KEY (status {resp.status_code})",
                    "matched_url": None, "top_result_url": None,
                }
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(1.5 * (attempt + 1))
    else:
        return {
            "query": query, "active": False,
            "reason": f"request_failed: {last_error}", "matched_url": None, "top_result_url": None,
        }

    organic = data.get("organic", [])
    if not organic:
        return {"query": query, "active": False, "reason": "no_search_results",
                 "matched_url": None, "top_result_url": None}

    top_result_url = organic[0].get("link", "")
    for result in organic:
        url = result.get("link", "")
        netloc = urlparse(url).netloc.lower().split(":")[0]
        if netloc == "instacart.com" or netloc.endswith(".instacart.com"):
            return {
                "query": query, "active": True, "reason": "ok",
                "matched_url": url, "top_result_url": top_result_url,
            }

    return {
        "query": query, "active": False, "reason": "no_instacart_result_in_top_results",
        "matched_url": None, "top_result_url": top_result_url,
    }


def process_candidate_pool(path, out_path, api_key, delay, resume, debug, limit):
    """Same resume/checkpoint shape as check_walmart_links.py: resumes by
    re-reading out_path (never the original input) if it exists, and a
    record only counts as "already done" if it has "_instacart_check_query"
    set -- a field only this script writes, so nothing else can be
    mistaken for a completed check."""
    if resume and os.path.exists(out_path):
        with open(out_path) as f:
            records = json.load(f)
        print(f"Resuming from existing {out_path} ...")
    else:
        with open(path) as f:
            records = json.load(f)
        if not resume:
            for record in records:
                record.pop("instacart_active", None)
                record.pop("_instacart_check_reason", None)
                record.pop("_instacart_check_query", None)
                record.pop("_instacart_check_url", None)
        print(f"Starting fresh from {path} -> {out_path}")

    if not isinstance(records, list):
        raise ValueError("Expected a JSON list of product records.")

    total = len(records)
    checked = 0
    for i, record in enumerate(records, 1):
        if resume and record.get("_instacart_check_query") is not None:
            continue  # already checked by this script in a previous run

        if debug and checked >= DEBUG_LIMIT:
            print(f"\n[debug mode] Stopping after {DEBUG_LIMIT} checks "
                  f"(remaining {total - i + 1} records left untouched).")
            break
        if limit is not None and checked >= limit:
            print(f"\n[--limit {limit}] Stopping after {limit} checks this run "
                  f"(remaining {total - i + 1} records left untouched -- re-run to continue).")
            break

        brand = record.get("BRAND", "")
        name = record.get("PRODUCT_NAME") or record.get("name") or ""
        if not name:
            record["instacart_active"] = False
            record["_instacart_check_reason"] = "no_product_name"
            record["_instacart_check_query"] = None
            record["_instacart_check_url"] = None
            print(f"[{i}/{total}] (no product name) -> SKIPPED")
            continue

        status = check_instacart_listing(brand, name, api_key)
        record["instacart_active"] = status["active"]
        record["_instacart_check_reason"] = status["reason"]
        record["_instacart_check_query"] = status["query"]
        record["_instacart_check_url"] = status["matched_url"] or status["top_result_url"]
        if status["active"]:
            # Only overwrite INSTACART_URL on a confirmed match -- a
            # previously guessed URL (e.g. from update_instacart_urls.py)
            # is left alone when this run couldn't confirm it, rather
            # than being erased.
            record["INSTACART_URL"] = status["matched_url"]
        checked += 1

        label = "ACTIVE" if status["active"] else "INACTIVE"
        print(f"[{i}/{total}] {name[:50]!r} -> {label} ({status['reason']})\n"
              f"    query:      \"{status['query']}\"\n"
              f"    matched:    {status['matched_url']}\n"
              f"    top result: {status['top_result_url']}")

        # Write progress after every item so a crash/interrupt doesn't lose work
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)

        if i < total:
            time.sleep(delay)

    active_count = sum(1 for r in records if r.get("instacart_active") is True)
    inactive_count = sum(1 for r in records if r.get("instacart_active") is False)
    print(f"\nDone. Checked {checked} this run. "
          f"Totals: {active_count} instacart_active / {inactive_count} not / {total} total. "
          f"Written to {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_file", help="Path to a candidate_pool.json-style file")
    parser.add_argument("--out", default=None,
                         help="Where to write results (default: candidate_pool_instacart_checked.json). "
                              "Pass the same path as input_file to update in place.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to sleep between requests")
    parser.add_argument("--no-resume", action="store_true",
                         help="Re-check every record, even ones already checked by a previous run")
    parser.add_argument("--debug", action="store_true",
                         help=f"Only check the first {DEBUG_LIMIT} items -- use this before running the full batch")
    parser.add_argument("--limit", type=int, default=None,
                         help="Cap how many NEW checks to run this call, to protect your Serper quota "
                              "(e.g. --limit 500). Re-run the same command to continue where it left off.")
    args = parser.parse_args()

    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        sys.exit("Set SERPER_API_KEY in your environment first: export SERPER_API_KEY=your_key")

    out_path = args.out or "candidate_pool_instacart_checked.json"
    process_candidate_pool(args.input_file, out_path, api_key, args.delay,
                            resume=not args.no_resume, debug=args.debug, limit=args.limit)


if __name__ == "__main__":
    main()
