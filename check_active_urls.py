"""
Checks every PRODUCT_URL in candidate_pool.json against walmart.com and
tags each item with an "active" field:

    active: true   -> page loaded and looks like a real product page
    active: false  -> confirmed dead (404, redirected off /ip/, or "no
                       longer available" text on the page)
    active: null   -> couldn't tell (Walmart's bot protection blocked the
                       request) - left unresolved rather than guessed at

Run it locally (not from this sandbox - walmart.com isn't reachable from
here). Walmart's bot blocking means this WILL mark some genuinely-live
pages as null rather than true; rerun on just the null items later
(--only-unknown) to whittle those down.

Usage:
    python3 check_active_urls.py                # check every item
    python3 check_active_urls.py --only-unknown  # re-check only active=null
    python3 check_active_urls.py --limit 20      # quick test run
"""

import argparse
import concurrent.futures
import json
import random
import time
import urllib.error
import urllib.request

INPUT_JSON = "candidate_pool.json"
OUTPUT_JSON = "candidate_pool.json"  # overwritten in place; back it up first if you want to diff

MAX_WORKERS = 3
REQUEST_TIMEOUT = 12
DELAY_RANGE = (2.0, 5.0)  # randomized pause between requests per worker

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

BLOCKED_MARKERS = [
    "robot or human",
    "px-captcha",
    "access to this page has been denied",
    "are you a human",
    "unusual traffic",
    "verify you are a human",
]

DEAD_MARKERS = [
    "we can't find that page",
    "we cannot find that page",
    "page not found",
    "this item is no longer available",
    "sorry, that item is currently unavailable",
    "this product is no longer available",
]


def check_url(url):
    """Returns (active, reason) where active is True/False/None."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
            final_url = resp.geturl()
            body = resp.read(300000).decode("utf-8", errors="ignore").lower()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "404"
        if e.code in (403, 412, 429):
            return None, f"blocked ({e.code})"
        return None, f"http error {e.code}"
    except Exception as e:
        return None, f"error: {e}"

    if any(marker in body for marker in BLOCKED_MARKERS):
        return None, "blocked (captcha/bot check)"
    if any(marker in body for marker in DEAD_MARKERS):
        return False, "dead page text"
    if "/ip/" not in final_url:
        return False, "redirected off product page"
    if status == 200:
        return True, "200 ok"
    return None, f"unclear status {status}"


def process_item(item):
    url = item.get("PRODUCT_URL")
    if not url:
        item["active"] = None
        item["_active_check_reason"] = "no PRODUCT_URL"
        return item

    active, reason = check_url(url)
    item["active"] = active
    item["_active_check_reason"] = reason
    time.sleep(random.uniform(*DELAY_RANGE))
    return item


def main():
    parser = argparse.ArgumentParser(description="Tag candidate_pool.json items as active/dead/unknown.")
    parser.add_argument("--only-unknown", action="store_true",
                         help="Only re-check items whose active field is currently null/missing.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process the first N matching items (useful for a quick test run).")
    args = parser.parse_args()

    with open(INPUT_JSON, encoding="utf-8") as f:
        items = json.load(f)

    total_items = len(items)
    resolved_before = sum(1 for i in items if i.get("active") in (True, False))

    if args.only_unknown:
        to_check = [i for i in items if i.get("active") is not True and i.get("active") is not False]
    else:
        to_check = items

    if args.limit:
        to_check = to_check[: args.limit]

    if not to_check:
        print(f"Nothing left to check — {resolved_before}/{total_items} items already resolved.")
        return

    print(f"Checking {len(to_check)} of {total_items} items "
          f"(~{len(to_check) * sum(DELAY_RANGE) / 2 / MAX_WORKERS / 60:.1f} min at current settings)...")

    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for _ in executor.map(process_item, to_check):
            checked += 1
            if checked % 25 == 0:
                print(f"  {checked}/{len(to_check)} checked...")

    # This-run breakdown: of the items just checked, how many resolved vs.
    # got stopped by bot detection vs. failed for some other reason.
    resolved_this_run = sum(1 for i in to_check if i.get("active") in (True, False))
    blocked_this_run = sum(
        1 for i in to_check
        if i.get("active") is None and "blocked" in (i.get("_active_check_reason") or "")
    )
    other_unresolved_this_run = len(to_check) - resolved_this_run - blocked_this_run

    active_count = sum(1 for i in items if i.get("active") is True)
    dead_count = sum(1 for i in items if i.get("active") is False)
    resolved_after = active_count + dead_count
    unresolved_after = total_items - resolved_after

    print("\n=== This run ===")
    print(f"Checked this run:            {len(to_check)}")
    print(f"  Marked (active/dead):      {resolved_this_run}")
    print(f"  Blocked by bot detection:  {blocked_this_run}")
    if other_unresolved_this_run:
        print(f"  Unresolved, other reason:  {other_unresolved_this_run}")

    print("\n=== Overall progress ===")
    print(f"Total items:                 {total_items}")
    print(f"Marked so far (active+dead): {resolved_after}  (active={active_count} dead={dead_count})")
    print(f"Still left to mark:          {unresolved_after}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {OUTPUT_JSON}.")


if __name__ == "__main__":
    main()
