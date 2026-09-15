"""
kroger_new_items.py

Finds frozen-meal / frozen-breakfast products NOT already in
candidate_pool.json by searching Kroger's live public product catalog,
then enriches each one and appends it to the pool in the same schema
Dedup.py / build_meal_pool.py / check_active_urls.py already use.

Why Kroger for discovery: candidate_pool.json was built from a single
2022 Walmart CSV export and hasn't had new products added since (only
existing rows get checked/refreshed). Kroger's Product API is a free,
official, live catalog -- not a scrape -- so it's a reasonable stand-in
for "what frozen products exist right now" even though the item is
ultimately still looked up on Walmart. Kroger's API has no "date added"
field, so there's no way to literally filter to "listed after 2022" --
this script instead treats "in Kroger's catalog today AND not already in
the pool by normalized product name" as the practical definition of
"new". See kroger_new_items.yaml's header for more.

Pipeline per candidate product:
  1. Kroger Product API search (by term) -> brand, description,
     categories, size, UPC. No price/location lookup -- Kroger pricing
     isn't used anywhere here.
  2. SerpApi (site:walmart.com <UPC>) -> a real Walmart PRODUCT_URL, and
     a SKU extracted directly from that URL (never invented).
  3. DuckDuckGo Images (same technique as AddImageUrl.py) -> image_url.
  4. Gemini, no search (same model chain/behavior as
     gemini_meal_lookup.py) -> calories, price estimate, servings text.
     Gemini is also allowed a fallback "sku_guess" ONLY for items where
     step 2 found a real Walmart link but the SKU couldn't be parsed out
     of the URL -- that guess is stored as SKU but flagged with
     "_sku_is_estimate": true so it's never mistaken for a verified one.
     If Gemini doesn't recognize the product, or recognizes it but is
     missing calories, price, OR servings_per_container, the item is
     dropped -- no partially-filled nutrition/price data gets added.

An item is only appended to the pool if step 2 (a real Walmart URL),
step 3 (an image), AND step 4 (Gemini recognized it AND returned
calories, price, and servings_per_container -- all three, not just
some) all succeeded. Missing any one of those -> the candidate is
skipped entirely, not added half-filled.

New records get "active": True -- SerpApi already confirmed a live
walmart.com page exists for the UPC before a record is ever assembled
(that's step 2 below), and check_active_urls.py's Playwright-based
checks are unreliable here since Walmart bot-blocks it, so that
confirmation is trusted directly rather than leaving the item in limbo
waiting on a check that mostly can't complete. This script's own check
is still stored separately under "_serpapi_*" fields (as opposed to
check_active_urls.py's "_active_check_*" fields) so it's clear which
check actually set "active" for a given row -- and check_active_urls.py
is still free to flip a row to False later if Walmart genuinely delists
it and a check happens to get through.

Env vars required:
    KROGER_CLIENT_ID, KROGER_CLIENT_SECRET  -- api.kroger.com OAuth app
    SERPAPI_KEY                             -- serpapi.com
    GEMINI_KEY                              -- Gemini calorie/price/sku fill-in
If any are missing, the run is skipped entirely (exit 0), same pattern
as gemini_meal_lookup.py.

Install:
    pip install requests pyyaml ddgs

Usage:
    python kroger_new_items.py                     # up to run.max_new_items new items
    python kroger_new_items.py --max-new-items 5    # smoke test
    python kroger_new_items.py --dry-run            # discovery + dedup only, no network enrichment
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
except ImportError:  # pragma: no cover - fallback for the older, renamed package
    try:
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import (
            DuckDuckGoSearchException as DDGSException,
            RatelimitException,
            TimeoutException,
        )
    except ImportError:
        print("Missing dependency. Run: pip install ddgs", file=sys.stderr)
        raise

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _SCRIPT_DIR / "kroger_new_items.yaml"

KROGER_TOKEN_URL = "https://api.kroger.com/v1/connect/oauth2/token"
KROGER_PRODUCTS_URL = "https://api.kroger.com/v1/products"

# Same "grab the trailing numeric ID" pattern check_walmart_links.py uses.
PRODUCT_ID_RE = re.compile(r"/(\d+)(?:[/?#]|$)")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


class DailyQuotaExceeded(RuntimeError):
    """Every Gemini model in the fallback chain is rate-limited, over
    quota, or unavailable right now."""


class _ModelUnavailable(Exception):
    """One model in the fallback chain returned a 4xx -- move to the
    next model instead of retrying this one."""


class _RateLimiter:
    """Spaces out calls to at most one every `min_interval` seconds."""

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


# ---------------------------------------------------------------------------
# Config / pool I/O
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_pool(path):
    with open(path) as f:
        return json.load(f)


def save_pool(path, pool):
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)


def normalize_name(name):
    """Lowercase, collapse whitespace/punctuation -- used only to decide
    whether a Kroger result is "already in the pool", not stored anywhere."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def existing_name_and_upc_sets(pool):
    names = {normalize_name(item.get("PRODUCT_NAME")) for item in pool}
    names.discard("")
    upcs = {item.get("_kroger_upc") for item in pool if item.get("_kroger_upc")}
    return names, upcs


def next_index(pool):
    """Existing pool "index" values are numeric-looking strings inherited
    from the original Walmart CSV export; new items have no equivalent,
    so they get their own clearly-separate namespace instead of a
    colliding fake numeric one."""
    return max(
        (int(item["index"]) for item in pool
         if str(item.get("index", "")).isdigit()),
        default=0,
    )


# ---------------------------------------------------------------------------
# Step 1: Kroger product discovery
# ---------------------------------------------------------------------------

def get_kroger_token(client_id, client_secret, timeout):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        KROGER_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": "product.compact"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_kroger_term(token, term, cfg):
    """Yields raw Kroger product dicts for one search term, across
    cfg['kroger']['pages_per_term'] pages."""
    limit = cfg["kroger"]["results_per_term"]
    timeout = cfg["kroger"]["timeout_seconds"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    for page in range(cfg["kroger"]["pages_per_term"]):
        params = {
            "filter.term": term,
            "filter.limit": limit,
            "filter.start": page * limit + 1,  # Kroger's start is 1-based
        }
        resp = requests.get(KROGER_PRODUCTS_URL, headers=headers, params=params, timeout=timeout)
        if resp.status_code == 404 and page > 0:
            break  # ran past the end of available results
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data:
            break
        yield from data
        if len(data) < limit:
            break  # fewer than a full page -- no point requesting the next one


def kroger_product_to_candidate(product):
    """Normalizes one raw Kroger product dict into the loose fields this
    script cares about. Returns None if it's missing what we need
    (UPC + a description)."""
    upc = product.get("upc")
    description = (product.get("description") or "").strip()
    if not upc or not description:
        return None

    brand = (product.get("brand") or "").strip()
    categories = product.get("categories") or []
    items = product.get("items") or [{}]
    size = (items[0].get("size") or "").strip()

    return {
        "upc": str(upc),
        "brand": brand,
        "description": description,
        "categories": categories,
        "size": size,
    }


def discover_new_candidates(token, cfg, existing_names, existing_upcs, max_new_items):
    """Runs every configured search term against Kroger, dedupes against
    the existing pool AND across terms within this run, and returns up to
    max_new_items candidate dicts."""
    seen_upcs_this_run = set()
    candidates = []

    for term in cfg["kroger"]["search_terms"]:
        if len(candidates) >= max_new_items:
            break
        log(f"Kroger search: {term!r}")
        try:
            raw_products = list(search_kroger_term(token, term, cfg))
        except requests.RequestException as e:
            log(f"  Kroger search failed for {term!r}: {e}")
            continue

        found_this_term = 0
        for product in raw_products:
            candidate = kroger_product_to_candidate(product)
            if candidate is None:
                continue
            if candidate["upc"] in seen_upcs_this_run or candidate["upc"] in existing_upcs:
                continue
            if normalize_name(candidate["description"]) in existing_names:
                continue

            seen_upcs_this_run.add(candidate["upc"])
            candidates.append(candidate)
            found_this_term += 1
            if len(candidates) >= max_new_items:
                break

        log(f"  {len(raw_products)} result(s), {found_this_term} new candidate(s) "
            f"(running total: {len(candidates)}/{max_new_items})")

    return candidates


# ---------------------------------------------------------------------------
# Step 2: SerpApi Walmart link lookup
# ---------------------------------------------------------------------------

def find_walmart_listing(upc, api_key, cfg):
    """Searches 'site:walmart.com <upc>' via SerpApi. Returns a dict with
    product_url/sku (both None if not found) plus query/reason/top-result
    metadata, mirroring check_walmart_links.py's shape."""
    query = f"site:walmart.com {upc}"
    endpoint = cfg["serpapi"]["endpoint"]
    timeout = cfg["serpapi"]["timeout_seconds"]
    max_retries = cfg["serpapi"]["max_retries"]
    retry_delay = cfg["serpapi"]["retry_delay_seconds"]

    last_error = None
    data = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(
                endpoint,
                params={"engine": "google", "q": query, "num": 5, "api_key": api_key},
                timeout=timeout,
            )
            if resp.status_code in (401, 403):
                return {
                    "product_url": None, "sku": None, "query": query,
                    "reason": f"auth_error: check SERPAPI_KEY (status {resp.status_code})",
                    "top_result_url": None,
                }
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < max_retries:
                time.sleep(retry_delay)

    if data is None:
        return {
            "product_url": None, "sku": None, "query": query,
            "reason": f"request_failed: {last_error}", "top_result_url": None,
        }

    organic = data.get("organic_results") or []
    if not organic:
        return {
            "product_url": None, "sku": None, "query": query,
            "reason": "no_search_results", "top_result_url": None,
        }

    first_url = organic[0].get("link", "")
    netloc = urlparse(first_url).netloc.lower().split(":")[0]
    is_walmart_domain = netloc == "walmart.com" or netloc.endswith(".walmart.com")
    id_in_url = str(upc) in first_url
    match = PRODUCT_ID_RE.search(urlparse(first_url).path)
    sku_from_url = match.group(1) if match else None

    if not is_walmart_domain:
        reason = "top_result_not_walmart"
    elif not sku_from_url:
        reason = "walmart_domain_but_no_parseable_id"
    else:
        reason = "ok"

    return {
        "product_url": first_url if is_walmart_domain else None,
        "sku": sku_from_url if is_walmart_domain else None,
        "query": query,
        "reason": reason,
        "top_result_url": first_url,
    }


# ---------------------------------------------------------------------------
# Step 3: DuckDuckGo image lookup (same technique as AddImageUrl.py)
# ---------------------------------------------------------------------------

def build_image_query(brand, description):
    query = f"{brand} {description}".strip() if brand and brand.lower() not in description.lower() else description
    query = re.sub(r"\s+", " ", query).replace('"', "")
    return query[:200]


def search_image(ddgs, query, cfg):
    region = cfg["ddg_image"]["region"]
    safesearch = cfg["ddg_image"]["safesearch"]
    max_retries = cfg["ddg_image"]["max_retries"]
    backoff = 5.0

    for attempt in range(1, max_retries + 1):
        try:
            results = ddgs.images(query, region=region, safesearch=safesearch, max_results=5)
            for r in results:
                url = r.get("image")
                if url and url.startswith("http"):
                    return url, "ok"
            return None, "no_results"
        except RatelimitException:
            wait = min(backoff + random.uniform(0, backoff * 0.5), 120.0)
            log(f"  DDG ratelimit on {query!r} (attempt {attempt}/{max_retries}) -- backing off {wait:.1f}s")
            time.sleep(wait)
            backoff = min(backoff * 2, 90)
        except TimeoutException:
            wait = min(backoff + random.uniform(0, 2), 90.0)
            log(f"  DDG timeout on {query!r} (attempt {attempt}/{max_retries}) -- retrying in {wait:.1f}s")
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
        except (DDGSException, Exception) as e:  # noqa: BLE001
            if attempt < max_retries:
                wait = min(backoff + random.uniform(0, 2), 90.0)
                log(f"  DDG error on {query!r} (attempt {attempt}/{max_retries}): {e} -- retrying in {wait:.1f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            return None, f"error: {e}"

    return None, "retry_exhausted"


# ---------------------------------------------------------------------------
# Step 4: Gemini calories/price/servings (+ fallback sku_guess) fill-in
# ---------------------------------------------------------------------------

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def extract_json_array(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        raise ValueError(f"no JSON array found in response: {text[:200]!r}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, list):
        raise ValueError("parsed JSON was not an array")
    return parsed


def build_batch_prompt(cfg, candidates):
    item_line_tmpl = cfg["prompt"]["item_line"]
    lines = []
    for n, c in enumerate(candidates, start=1):
        lines.append(item_line_tmpl.format(
            n=n, product_name=c["description"], brand=c["brand"] or "unknown brand",
            size=c["size"] or "unknown size",
        ).rstrip("\n"))
    products_block = "\n".join(lines)
    return cfg["prompt"]["intro"].format(count=len(candidates), products_block=products_block)


def normalize_gemini_result(raw):
    if not isinstance(raw, dict):
        raise ValueError(f"batch result entry was not a JSON object: {raw!r}")

    index = raw.get("index")
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = None

    calories = raw.get("calories")
    if calories is not None:
        try:
            calories = int(float(calories))
        except (TypeError, ValueError):
            calories = None

    price = raw.get("price")
    if price is not None:
        try:
            price = round(float(str(price).replace("$", "").strip()), 2)
        except (TypeError, ValueError):
            price = None

    sku_guess = raw.get("sku_guess")
    if sku_guess is not None:
        digits = re.sub(r"\D", "", str(sku_guess))
        sku_guess = digits if digits else None

    return {
        "index": index,
        "recognized": bool(raw.get("recognized")),
        "calories": calories,
        "price": price,
        "servings_per_container": (str(raw.get("servings_per_container")).strip()
                                    if raw.get("servings_per_container") else None),
        "sku_guess": sku_guess,
        "notes": str(raw.get("notes", "")).strip(),
    }


def gemini_result_is_complete(result):
    """True only if Gemini recognized the product AND returned all three
    of calories/price/servings_per_container -- not just some of them.
    A candidate with no result, or an incomplete one, doesn't get added."""
    return (
        result is not None
        and result["recognized"]
        and result["calories"] is not None
        and result["price"] is not None
        and result["servings_per_container"] is not None
    )


def match_results_to_candidates(candidates, raw_results):
    normalized = []
    for raw in raw_results:
        try:
            normalized.append(normalize_gemini_result(raw))
        except ValueError as e:
            log(f"    skipping unparseable batch result entry: {e}")

    by_index = {r["index"]: r for r in normalized if r["index"] is not None}
    use_index_matching = len(by_index) >= max(1, len(candidates) // 2)

    matched = []
    for i in range(1, len(candidates) + 1):
        if use_index_matching and i in by_index:
            matched.append(by_index[i])
        elif not use_index_matching and i - 1 < len(normalized):
            matched.append(normalized[i - 1])
        else:
            matched.append(None)
    return matched


def call_gemini_batch_single_model(prompt, gemini_key, model, cfg, rate_limiter):
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    max_retries = cfg["gemini"]["max_retries"]
    retry_delay = cfg["gemini"]["retry_delay_seconds"]
    timeout = cfg["gemini"]["timeout_seconds"]
    last_err = None

    for attempt in range(max_retries):
        rate_limiter.wait()
        try:
            resp = requests.post(
                api_url,
                params={"key": gemini_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
                },
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            log(f"  {model}: request error -- retrying in {retry_delay:.0f}s (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(retry_delay)
            continue

        if 400 <= resp.status_code < 500:
            raise _ModelUnavailable(f"{resp.status_code} {resp.reason}: {resp.text[:200]}")

        if resp.status_code >= 500:
            last_err = requests.exceptions.HTTPError(f"{resp.status_code} {resp.reason} for url: {resp.url}", response=resp)
            if attempt == max_retries - 1:
                break
            log(f"  {model}: {resp.status_code} -- retrying in {retry_delay:.0f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_delay)
            continue

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise requests.exceptions.HTTPError(f"{resp.status_code} {resp.reason} for url: {resp.url}: {resp.text[:300]}") from e

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return extract_json_array(text)
        except Exception as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            log(f"  {model}: returned an unusable response -- retrying in {retry_delay:.0f}s (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(retry_delay)
            continue

    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Gemini call to {model} failed for unknown reason")


def call_gemini_batch(prompt, gemini_key, cfg, rate_limiter):
    last_unavailable = None
    for model in cfg["gemini"]["models"]:
        try:
            raw_results = call_gemini_batch_single_model(prompt, gemini_key, model, cfg, rate_limiter)
            return raw_results, model
        except _ModelUnavailable as e:
            last_unavailable = e
            log(f"  {model} unavailable ({e}) -- falling back to next model.")
            continue
    raise DailyQuotaExceeded(
        f"every model in the fallback chain ({', '.join(cfg['gemini']['models'])}) is "
        f"rate-limited, over quota, or unavailable right now (last error: {last_unavailable})"
    )


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

def build_pool_record(candidate, walmart, image_url, image_query, image_reason,
                       gemini_result, gemini_model, idx, run_date):
    sku = walmart["sku"]
    sku_is_estimate = False
    if not sku and gemini_result and gemini_result.get("sku_guess"):
        sku = gemini_result["sku_guess"]
        sku_is_estimate = True

    categories = candidate["categories"]
    price = gemini_result["price"] if gemini_result and gemini_result["recognized"] else None
    calories = gemini_result["calories"] if gemini_result and gemini_result["recognized"] else None
    servings = gemini_result["servings_per_container"] if gemini_result and gemini_result["recognized"] else None

    record = {
        "BRAND": candidate["brand"],
        "BREADCRUMBS": "Frozen/" + (categories[-1] if categories else "Frozen"),
        "CATEGORY": categories[-1] if categories else "Frozen",
        "DEPARTMENT": "Frozen",
        "PRICE_CURRENT": f"{price:.2f}" if price is not None else "",
        "PRICE_RETAIL": "",
        "PRODUCT_NAME": candidate["description"],
        "PRODUCT_SIZE": candidate["size"],
        "PRODUCT_URL": walmart["product_url"] or "",
        "PROMOTION": "",
        "RunDate": run_date,
        "SHIPPING_LOCATION": "",
        "SKU": sku or "",
        "SOURCE": "Kroger",  # every row from this script -- distinguishes it from
                              # the original 2022 Walmart-CSV rows, which have no
                              # SOURCE column at all (absent, not blank).
        "SUBCATEGORY": categories[0] if categories else "",
        "active": True,  # SerpApi already confirmed a live walmart.com page for
                          # this UPC before this record was ever assembled (see
                          # find_walmart_listing) -- check_active_urls.py's
                          # Playwright checks are unreliable here (Walmart bot-
                          # blocks it), so that confirmation is trusted directly
                          # instead of waiting on a check that mostly won't run.
        "calories": calories if calories is not None else "N/A",
        "image_url": image_url,
        "index": f"kroger-{idx}",
        "servings_per_container": servings or "N/A",
        "tid": "",
        # Provenance metadata -- "SOURCE" above is the human-readable
        # column; these _-prefixed fields are the detail behind it, kept
        # separate from check_active_urls.py's own "_active_check_*" writes.
        "_kroger_upc": candidate["upc"],
        "_kroger_discovered_at": run_date,
        "_serpapi_query": walmart["query"],
        "_serpapi_reason": walmart["reason"],
        "_serpapi_top_result_url": walmart["top_result_url"],
        "_image_search_query": image_query,
        "_image_search_reason": image_reason,
        "_sku_is_estimate": sku_is_estimate,
    }

    if gemini_result:
        record["_gemini_checked_at"] = run_date
        record["_gemini_model"] = gemini_model
        record["_gemini_notes"] = gemini_result["notes"]
        record["_gemini_recognized"] = gemini_result["recognized"]
        record["_gemini_price_is_estimate"] = True

    return record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--max-new-items", type=int, default=None,
                         help="Override run.max_new_items for this run")
    parser.add_argument("--dry-run", action="store_true",
                         help="Run Kroger discovery + dedup only; skip SerpApi/DDG/Gemini and don't write the pool")
    args = parser.parse_args()

    kroger_id = os.environ.get("KROGER_CLIENT_ID")
    kroger_secret = os.environ.get("KROGER_CLIENT_SECRET")
    serpapi_key = os.environ.get("SERPAPI_KEY")
    gemini_key = os.environ.get("GEMINI_KEY")

    missing = [name for name, val in [
        ("KROGER_CLIENT_ID", kroger_id), ("KROGER_CLIENT_SECRET", kroger_secret),
        ("SERPAPI_KEY", serpapi_key), ("GEMINI_KEY", gemini_key),
    ] if not val]
    if missing:
        log(f"Missing env var(s) {', '.join(missing)} -- skipping kroger_new_items run.")
        return

    cfg = load_config(args.config)
    max_new_items = args.max_new_items or cfg["run"]["max_new_items"]
    pool_path = _SCRIPT_DIR / cfg["pool"]["path"]

    pool = load_pool(pool_path)
    existing_names, existing_upcs = existing_name_and_upc_sets(pool)
    log(f"Loaded {len(pool)} existing pool item(s) ({len(existing_upcs)} with a known Kroger UPC).")

    log("Fetching Kroger OAuth token...")
    token = get_kroger_token(kroger_id, kroger_secret, cfg["kroger"]["timeout_seconds"])

    candidates = discover_new_candidates(token, cfg, existing_names, existing_upcs, max_new_items)
    log(f"Discovery done: {len(candidates)} new candidate(s) to enrich (cap {max_new_items}).")

    if not candidates:
        log("Nothing new found this run.")
        return

    if args.dry_run:
        for c in candidates:
            log(f"  [dry-run] would enrich: {c['brand']} {c['description']} (UPC {c['upc']})")
        return

    # --- Step 2 + 3: SerpApi link + DDG image for every candidate first,
    # so Gemini is only ever spent on candidates that already cleared the
    # "must have both a link and an image" bar. ---
    ddgs = DDGS()
    enriched = []
    for c in candidates:
        walmart = find_walmart_listing(c["upc"], serpapi_key, cfg)
        if not walmart["product_url"]:
            log(f"  SKIP (no Walmart link): {c['brand']} {c['description']} -- {walmart['reason']}")
            continue

        image_query = build_image_query(c["brand"], c["description"])
        image_url, image_reason = search_image(ddgs, image_query, cfg)
        if not image_url:
            log(f"  SKIP (no image): {c['brand']} {c['description']} -- {image_reason}")
            continue

        enriched.append((c, walmart, image_url, image_query, image_reason))
        log(f"  OK: {c['brand']} {c['description']} -> {walmart['product_url']}")
        time.sleep(random.uniform(cfg["ddg_image"]["min_delay_seconds"], cfg["ddg_image"]["max_delay_seconds"]))

    log(f"{len(enriched)}/{len(candidates)} candidate(s) got both a Walmart link and an image.")
    if not enriched:
        log("Nothing to add this run (all candidates failed link/image lookup).")
        return

    # --- Step 4: Gemini, batched, for calories/price/servings (+ sku_guess
    # only where step 2 found a link but couldn't parse a SKU out of it). ---
    items_per_call = cfg["batch"]["items_per_call"]
    rate_limiter = _RateLimiter(cfg["gemini"]["min_call_interval_seconds"])
    gemini_results_by_upc = {}
    gemini_model_used = None

    for i in range(0, len(enriched), items_per_call):
        batch = [e[0] for e in enriched[i:i + items_per_call]]
        prompt = build_batch_prompt(cfg, batch)
        try:
            raw_results, model_used = call_gemini_batch(prompt, gemini_key, cfg, rate_limiter)
            gemini_model_used = model_used
        except DailyQuotaExceeded as e:
            log(f"  Gemini fallback chain exhausted: {e} -- remaining items get no calorie/price estimate this run.")
            continue
        except Exception as e:  # noqa: BLE001 -- one bad call shouldn't kill the run
            log(f"  Gemini call failed: {e}")
            continue

        matched = match_results_to_candidates(batch, raw_results)
        for c, result in zip(batch, matched):
            if result is not None:
                gemini_results_by_upc[c["upc"]] = result

    # --- Assemble + append records. Only candidates with a COMPLETE
    # Gemini result (recognized, and calories + price + servings all
    # present) make it in -- everything else is dropped, not added with
    # gaps. ---
    idx = next_index(pool)
    run_date = datetime.now(timezone.utc).isoformat()
    added = 0
    skipped_incomplete = 0

    for c, walmart, image_url, image_query, image_reason in enriched:
        gemini_result = gemini_results_by_upc.get(c["upc"])
        if not gemini_result_is_complete(gemini_result):
            skipped_incomplete += 1
            reason = (gemini_result["notes"] if gemini_result and gemini_result["notes"]
                       else "not recognized or missing calories/price/servings")
            log(f"  SKIP (incomplete Gemini result): {c['brand']} {c['description']} -- {reason}")
            continue

        idx += 1
        record = build_pool_record(
            c, walmart, image_url, image_query, image_reason,
            gemini_result, gemini_model_used, idx, run_date,
        )
        pool.append(record)
        added += 1
        sku_note = " (estimated SKU)" if record["_sku_is_estimate"] else ""
        log(f"  ADDED: {record['PRODUCT_NAME']} -- SKU {record['SKU']}{sku_note}, "
            f"calories={record['calories']}, price={record['PRICE_CURRENT']}, "
            f"servings={record['servings_per_container']}")

    if skipped_incomplete:
        log(f"{skipped_incomplete} candidate(s) had a Walmart link + image but no complete "
            f"Gemini calories/price/servings -- not added.")

    save_pool(pool_path, pool)
    log(f"Done. Added {added} new item(s) to {pool_path.name} (pool size now {len(pool)}).")


if __name__ == "__main__":
    main()
