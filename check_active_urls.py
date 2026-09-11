"""
Checks each PRODUCT_URL using a real headless Chromium browser (Playwright)
instead of a plain HTTP request.

Why: the plain-requests version (check_active_urls.py) got the identical
"blocked (captcha/bot check)" result on 20/20 different product URLs in a
row - a strong sign Walmart's edge bot-management is intercepting every
request before it reaches real content, regardless of which item was
requested. A real browser executes JS, holds cookies, and has a normal
fingerprint, which a plain HTTP client can never have - this gives a
meaningfully better shot at getting through.

It is still NOT guaranteed. This still runs from whatever IP the caller is
on (a GitHub Actions runner = datacenter IP), and IP reputation alone can
still trigger a block independent of how good the browser fingerprint is.
Same active: true/false/null semantics and reason-breakdown reporting as
the plain-requests version.

Install (locally or in CI):
    pip install playwright playwright-stealth
    python -m playwright install --with-deps chromium

Usage: same flags as check_active_urls.py
    python3 check_active_urls_playwright.py                # check every item
    python3 check_active_urls_playwright.py --only-unknown  # re-check only active=null
    python3 check_active_urls_playwright.py --limit 20      # quick test run
"""

import argparse
import json
import random
import time
from collections import Counter

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

INPUT_JSON = "candidate_pool.json"
OUTPUT_JSON = "candidate_pool.json"  # overwritten in place; back it up first if you want to diff

PAGE_TIMEOUT_MS = 20000
POST_LOAD_WAIT_MS = 1500  # let any late-firing challenge script render
DELAY_RANGE = (3.0, 6.0)  # randomized pause between page loads

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BLOCKED_MARKERS = [
    "robot or human",
    "px-captcha",
    "access to this page has been denied",
    "are you a human",
    "unusual traffic",
    "verify you are a human",
    "press and hold",
    "press & hold",
]

DEAD_MARKERS = [
    "we can't find that page",
    "we cannot find that page",
    "page not found",
    "this item is no longer available",
    "sorry, that item is currently unavailable",
    "this product is no longer available",
]


def check_url(page, url):
    """Returns (active, reason) where active is True/False/None."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(POST_LOAD_WAIT_MS)
        status = response.status if response else None
        final_url = page.url
        body = page.content().lower()
    except PlaywrightTimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"error: {e}"

    if status == 404:
        return False, "404"
    if any(marker in body for marker in BLOCKED_MARKERS):
        return None, f"blocked (captcha/bot check, status {status})"
    if any(marker in body for marker in DEAD_MARKERS):
        return False, "dead page text"
    if "/ip/" not in final_url:
        return False, "redirected off product page"
    if status and status >= 400:
        return None, f"http error {status}"
    if status == 200:
        return True, "200 ok"
    return None, f"unclear status {status}"


def process_item(context, item):
    url = item.get("PRODUCT_URL")
    if not url:
        item["active"] = None
        item["_active_check_reason"] = "no PRODUCT_URL"
        return

    page = context.new_page()
    if HAS_STEALTH:
        stealth_sync(page)
    try:
        active, reason = check_url(page, url)
    finally:
        page.close()

    item["active"] = active
    item["_active_check_reason"] = reason
    time.sleep(random.uniform(*DELAY_RANGE))


def main():
    parser = argparse.ArgumentParser(description="Tag candidate_pool.json items using a real headless browser.")
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

    est_minutes = len(to_check) * (sum(DELAY_RANGE) / 2 + 3) / 60
    print(f"Checking {len(to_check)} of {total_items} items with a real browser (~{est_minutes:.1f} min estimated)...")
    if not HAS_STEALTH:
        print("Note: playwright-stealth isn't installed, running without extra fingerprint patches. "
              "pip install playwright-stealth for a better shot at passing headless-detection checks.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/Chicago",
        )

        checked = 0
        for item in to_check:
            process_item(context, item)
            checked += 1
            if checked % 5 == 0:
                print(f"  {checked}/{len(to_check)} checked...")

        context.close()
        browser.close()

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

    reason_counts = Counter(i.get("_active_check_reason") for i in to_check)
    print("  Reason breakdown:")
    for reason, count in reason_counts.most_common():
        print(f"    {count:>3}  {reason}")

    print("\n=== Overall progress ===")
    print(f"Total items:                 {total_items}")
    print(f"Marked so far (active+dead): {resolved_after}  (active={active_count} dead={dead_count})")
    print(f"Still left to mark:          {unresolved_after}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {OUTPUT_JSON}.")


if __name__ == "__main__":
    main()
