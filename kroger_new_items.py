"""
kroger_new_items.py

Adds new frozen-meal / frozen-breakfast products to candidate_pool.json
by pulling Kroger's live public product catalog, keeping only products
that aren't already in the pool, and enriching each one in the same
schema Dedup.py / build_meal_pool.py / check_active_urls.py already use.

Why Kroger for discovery: candidate_pool.json was built from a single
2022 Walmart CSV export and hasn't had new products added since. Kroger's
Product API is a free, official, live catalog -- not a scrape -- and it
has no "date added" field, so "new" here means "in Kroger's catalog today
and not already in the pool (by fuzzy name match)". Meant to run 4 times a
day for up to 20 new products each run (see the workflow).

How one run works
-----------------
The run keeps pulling Kroger, one page at a time, until it has added
run.max_new_items (20) new products -- it may pull 100+ Kroger products to
find 20 new ones. For every Kroger product pulled:

  1. Not frozen (by Kroger's own category labels), no price at the
     configured store, or no UPC -> ignored. That's free to re-check, so
     it isn't recorded.
  2. Its Kroger UPC is already known -> ignored. A UPC is "known" when it
     is on a pool item ("_kroger_upc" / "_kroger_upc_aliases"), or in
     kroger_skipped_upcs.json (see step 8), or already seen this run.
  3. Otherwise the product name is cleaned and fuzzy-matched against the
     product names of the pool items that have the SAME BRAND, cleaned the
     same way (see clean_search_words(): cut at the first comma, sizes
     like "27 oz", other numbers, punctuation and the word "frozen"
     removed, lowercased). The score is rapidfuzz token_sort_ratio,
     0-100. A pool item with a different brand is never a match, however
     similar the names (see brands_match(): the pool's BRAND column is
     unreliable, so a brand also counts if it appears in the other
     side's product name).
  4. Best score ABOVE dedup.fuzzy_threshold (90) -> it already exists. The
     Kroger UPC is written onto the same-brand pool item with the HIGHEST
     score, so the product is skipped on every later pull, and the run
     moves on to the next Kroger product.
  5. Best score 90 or below -> it's new, and gets a new pool object:
       - Kroger UPC ("_kroger_upc"), PRODUCT_NAME (Kroger's description),
         and price (PRICE_CURRENT = promo price if Kroger lists one else
         regular; PRICE_RETAIL = regular) -- all straight from Kroger, at
         the store chosen by KROGER_ZIP / kroger.location_id;
       - Walmart page: DuckDuckGo text search for "site:walmart.com <brand>
         <name>" (ddgs.text() -- free, no API key). Each result is
         checked for the real /ip/<slug>/<id> product-page shape AND for
         being the SAME PRODUCT as the Kroger one: the product name in the
         URL must carry Kroger's brand and score at least
         ddg_walmart.min_name_match_score (75) against Kroger's name. DDG
         returns the closest Walmart page, which is often a different
         flavor or brand; the first result that passes wins, and if none
         does the product is skipped ("no_matching_walmart_page"). The URL
         (+ ?fulfillmentIntent=Pickup) goes in PRODUCT_URL and the numeric
         id in SKU. If that SKU is already on an ACTIVE pool item the
         product IS that item: its UPC is recorded there instead. If it's
         already on an item that is marked inactive (or was never
         checked), that row is refreshed in place instead of a duplicate
         being added: PRODUCT_URL is replaced with the looked-up URL,
         active becomes True, and everything else below is filled in
         as for a new product (the row keeps its "index" and any UPC it
         already had) -- it counts toward the run's target of new items;
       - image_url: DuckDuckGo Images, searching with the Walmart URL
         itself as the query and taking the top result, whoever hosts it
         (retry/backoff as in AddImageUrl.py);
       - calories + servings_per_container from USDA FoodData Central
         (servings_per_container the way build_meal_pool.py pulls it:
         householdServingFullText, else packageWeight, cleaned with
         clean_serving_text() -- the amount of ONE serving, never the
         count of servings in the container; "N/A" if USDA has no match);
       - calories: the LARGEST of USDA, Open Food Facts and Gemini (its
         own knowledge, no search) -- the same "largest wins" rule as
         DataCleaning/Max_Calories_Count.py -- rounded to the nearest 10.
         The source is in "_calorie_max_source" (and
         "_calorie_max_checked_at" is set so Max_Calories_Count.py leaves
         the row alone);
       - INSTACART_URL: BUILT from the cleaned product name, e.g.
         https://www.instacart.com/store/s?k=marie+callender%27s+pot+roast
         -- never searched for, and nothing verifies it.
  6. "active" is True on new records: the DuckDuckGo search already
     confirmed a live walmart.com page, and check_active_urls.py's
     Playwright checks are unreliable (Walmart bot-blocks them). Those
     details are kept in "_ddg_walmart_*" fields, separate from that
     script's "_active_check_*".
  7. A new product only gets added if it has ALL of: a Kroger price, a
     Walmart URL/SKU, an image, and at least one calorie number. Missing
     any one -> not added, never half-filled.
  8. A new product that can't be added for a reason that won't change
     (no Walmart page, no image results, no calorie number anywhere) has
     no pool object to carry its UPC, so the UPC goes in
     kroger_skipped_upcs.json (with the reason) and it isn't retried.
     Failures that might be temporary (network error, rate limit, Gemini
     quota) record nothing, so a later run tries again. Delete a line from
     that file to make the script try a product again.

Every UPC that gets pulled and evaluated therefore ends up recorded
somewhere -- on an existing pool item, on a new pool item, or in the
skipped file -- so the next run's pull starts past all of them.

Safety limits: run.max_enrichment_attempts caps how many new products get
run through the (rate-limited) DDG steps in one run, and
run.max_runtime_minutes stops the run early enough that the workflow
commits what it has instead of being killed.

Env vars required:
    KROGER_CLIENT_ID, KROGER_CLIENT_SECRET  -- api.kroger.com OAuth app
    GEMINI_KEY                              -- Gemini calorie estimate
    USDA_API_KEY                            -- USDA FoodData Central (same
                                                key build_meal_pool.py uses)
Also needed: a Kroger store to price against, from the first of these that
is set: the KROGER_LOCATION_ID env var, kroger.location_id in
kroger_new_items.yaml, or KROGER_ZIP -- a ZIP code (a GitHub secret or
variable in the workflow) that's turned into the NEAREST Kroger-family
store through Kroger's Locations API at the start of each run and logged.
Run `python kroger_new_items.py --find-location <ZIP>` to list nearby store
ids and pin one explicitly instead. If any key or the store is missing,
the run is skipped entirely (exit 0), same pattern as
gemini_meal_lookup.py. Open Food Facts needs no key.

Install:
    pip install requests pyyaml ddgs rapidfuzz

Usage:
    python kroger_new_items.py                     # up to run.max_new_items new items
    python kroger_new_items.py --max-new-items 5    # smoke test
    python kroger_new_items.py --dry-run            # pull + fuzzy-match only, write nothing
    python kroger_new_items.py --find-location 68508  # print Kroger store ids near a ZIP, then exit
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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import requests
import yaml

try:
    from rapidfuzz import fuzz, process
except ImportError:  # pragma: no cover
    print("Missing dependency. Run: pip install rapidfuzz", file=sys.stderr)
    raise

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
KROGER_LOCATIONS_URL = "https://api.kroger.com/v1/locations"

# (WALMART_IP_URL_RE, the pattern actually used for matching a Walmart
# product-page URL, is defined just above find_walmart_listing() below.)


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


# ---------------------------------------------------------------------------
# Name cleaning + fuzzy "is it already in the pool?" matching
# ---------------------------------------------------------------------------

# Size/count units that get stripped along with the number in front of
# them ("27 oz", "10.5oz", "10-oz", "6 ct", "1 1/2 lb", "9 7/8 ounce").
_SIZE_UNITS = (
    r"(?:fl\.?\s*oz|ounces?|oz|pounds?|lbs?|kg|grams?|g|ml|liters?|l|"
    r"count|ct|packs?|pk|pieces?|pcs?)"
)
_SIZE_RE = re.compile(
    r"\b(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)[\s-]*" + _SIZE_UNITS + r"\b"
)

# Words that carry no information about WHICH product it is here -- every
# item in this pool is frozen, so "frozen" only adds noise to a name match.
_NOISE_WORDS = {"frozen"}


def fix_mojibake(text):
    """Repairs text that was UTF-8 but got read as Windows-1252 somewhere
    upstream -- the pool has a few of these ("JosÃ© OlÃ©", "Julianâ€™s
    Recipe") -- so it cleans/compares the same as the correct spelling
    Kroger sends. Returns the text unchanged if it doesn't look broken
    or can't be repaired."""
    if not text or ("Ã" not in text and "â€" not in text):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def clean_search_words(text):
    """Cleans a product name down to the words that identify it, the same
    way an Instacart search query is built: everything after the first
    comma is dropped (that's where "27 oz, 6 count" style size/variant
    detail lives), sizes like "27 oz" / "6 ct" go, any other bare numbers
    go, punctuation and symbols go, accents are folded ("Jalapeno" not
    "Jalape o"), "&" becomes "and", and the result is lowercased.
    Apostrophes inside a word are kept (returned as e.g. "callender's") so
    build_instacart_search_url() can turn them into %27; compare_key()
    drops them. Returns a list of words.

    e.g. "Marie Callender's Pot Roast, Frozen Meal, 10 oz" ->
         ["marie", "callender's", "pot", "roast"]"""
    text = fix_mojibake(text or "").split(",", 1)[0].lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\u2019", "'").replace("&", " and ")
    text = _SIZE_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9'\s]", " ", text)
    words = []
    for word in text.split():
        word = word.strip("'")
        if not word or word.isdigit() or word in _NOISE_WORDS:
            continue
        words.append(word)
    return words


def compare_key(name):
    """The string two product names are fuzzy-compared as: the cleaned
    search words joined by spaces, apostrophes dropped ("callenders")."""
    return " ".join(clean_search_words(name)).replace("'", "")


def normalize_brand(brand):
    """Brand -> lowercase words with accents, apostrophes, symbols and
    punctuation dropped, "&" as "and": "Marie Callender's" ->
    "marie callenders", "Birds Eye®" -> "birds eye"."""
    text = fix_mojibake(brand or "").lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2019'`]", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def _phrase_in(phrase, text):
    """True if `phrase` appears in `text` as whole words."""
    return bool(phrase) and f" {phrase} " in f" {text} "


def brands_match(cand_brand, cand_name_key, pool_brand, pool_name_key, min_similarity):
    """Do a Kroger product and a pool item carry the same brand? Both
    brands are already normalize_brand()'d and both names are already
    compare_key()'d. The pool's BRAND column can't be trusted on its own
    (it says "Homestyle Bakes" on Banquet products, "Ezekiel 4:9" on a
    Stouffer's one), so a brand also counts as matching when it shows up
    as whole words in the OTHER side's product name. Any one of these is
    a match:
      - the two brands are the same, near-identical (ratio >= min_similarity,
        so "Birds Eye" ~ "Birdseye"), or one contains the other ("Amy's" /
        "Amy's Kitchen");
      - Kroger's brand appears in the pool item's product name;
      - the pool item's brand appears in Kroger's product name.
    Anything else -- including a Kroger product with no brand at all when
    the pool brand isn't in its name -- is NOT a match."""
    if cand_brand and pool_brand:
        if cand_brand == pool_brand or fuzz.ratio(cand_brand, pool_brand) >= min_similarity:
            return True
        if _phrase_in(cand_brand, pool_brand) or _phrase_in(pool_brand, cand_brand):
            return True
    if _phrase_in(cand_brand, pool_name_key):
        return True
    if _phrase_in(pool_brand, cand_name_key):
        return True
    return False


class PoolNameMatcher:
    """Fuzzy "does this product already exist in the pool?" check.

    Every PRODUCT_NAME in the pool is reduced with compare_key() once, up
    front. For a Kroger product, best_match() first throws out every pool
    item whose BRAND doesn't match the product's brand (brands_match() --
    different brands are never the same product, however alike the names
    look), then scores the Kroger name against the rest with rapidfuzz's
    token_sort_ratio (0-100; word order doesn't matter, but extra/missing
    words cost points) and returns the single HIGHEST-scoring pool item,
    so the caller knows exactly which object to tag with the Kroger UPC.
    Anything scoring ABOVE `threshold` counts as already existing. add()
    lets a record appended during this run join the comparison set, so a
    second size variant of a product just added isn't added again."""

    def __init__(self, pool, threshold, brand_min_similarity=88):
        self.threshold = threshold
        self.brand_min_similarity = brand_min_similarity
        self._keys = []
        self._brands = []
        self._items = []
        for item in pool:
            self.add(item)

    def add(self, item):
        key = compare_key(item.get("PRODUCT_NAME"))
        if key:
            self._keys.append(key)
            self._brands.append(normalize_brand(item.get("BRAND")))
            self._items.append(item)

    def best_match(self, name, brand):
        """Returns (closest_pool_item_or_None, score 0-100), considering
        only pool items with a matching brand."""
        key = compare_key(name)
        if not key or not self._keys:
            return None, 0.0
        cand_brand = normalize_brand(brand)
        eligible = [
            i for i in range(len(self._keys))
            if brands_match(cand_brand, key, self._brands[i], self._keys[i], self.brand_min_similarity)
        ]
        if not eligible:
            return None, 0.0
        hit = process.extractOne(key, [self._keys[i] for i in eligible], scorer=fuzz.token_sort_ratio)
        if hit is None:
            return None, 0.0
        _choice, score, pos = hit
        return self._items[eligible[pos]], float(score)

    def __len__(self):
        return len(self._keys)


def existing_upc_set(pool, skipped_upcs):
    """Every Kroger UPC this script already knows about, so a product with
    one of them is skipped for free on the next pull: UPCs recorded on
    pool items (primary "_kroger_upc" plus "_kroger_upc_aliases"), and
    UPCs of products that were new but couldn't be added (skipped_upcs,
    the sidecar file's keys)."""
    upcs = set(skipped_upcs)
    for item in pool:
        primary = item.get("_kroger_upc")
        if primary:
            upcs.add(primary)
        for alias in item.get("_kroger_upc_aliases") or []:
            if alias:
                upcs.add(alias)
    return upcs


def tag_pool_item_with_upc(item, upc):
    """Records a Kroger UPC on an existing pool item -- as "_kroger_upc"
    if it doesn't have one yet, else appended to "_kroger_upc_aliases"
    (a second size/variant of the same product gets its own UPC). Returns
    True if it changed anything. This is what makes the next Kroger pull
    skip that UPC for free instead of re-matching it."""
    primary = item.get("_kroger_upc")
    if not primary:
        item["_kroger_upc"] = upc
        return True
    if primary == upc:
        return False
    aliases = item.get("_kroger_upc_aliases") or []
    if upc in aliases:
        return False
    item["_kroger_upc_aliases"] = aliases + [upc]
    return True


def existing_active_skus(pool):
    """SKUs already in the pool on a record marked active=True. A newly
    resolved Walmart link whose SKU matches one of these is skipped --
    if it's already in the pool and confirmed active, there's no reason
    to add a second row for the same product."""
    return {
        str(item["SKU"]).strip()
        for item in pool
        if item.get("SKU") and item.get("active") is True
    }


def tag_existing_pool_item_with_upc(pool, sku, upc):
    """When a new candidate's Walmart SKU turns out to already be active
    in the pool (see main()), the candidate IS that existing product --
    just under a name the fuzzy match didn't catch. Record the Kroger UPC
    on that item so the same Kroger product is skipped for free on every
    later pull, instead of being re-resolved through DuckDuckGo (a rate-
    limited search) and thrown away again. Returns the pool item
    it tagged, or None."""
    for item in pool:
        if item.get("active") is True and str(item.get("SKU", "")).strip() == sku:
            tag_pool_item_with_upc(item, upc)
            return item
    return None


def revive_pool_item(item, record, upc, run_date):
    """Refreshes an existing pool row with a freshly built record. Used
    when the Walmart page DuckDuckGo just found has the same SKU as a row
    that was marked inactive (or never checked): the product IS that row,
    and the DDG search has just proved the page is live. Every field of `record`
    (Walmart URL, active=True, Kroger name/price, calories, servings,
    image, Instacart URL, ...) replaces what the row had, exactly as if it
    were being added new -- except the row keeps its original "index", and
    keeps any Kroger UPC it already had (the new UPC is added as an alias
    if it differs). Old "_active_check_*" and "_gemini_*" bookkeeping the
    record didn't regenerate is dropped, since it described the old,
    dead-link state."""
    old_index = item.get("index")
    old_primary = item.get("_kroger_upc")
    old_aliases = item.get("_kroger_upc_aliases")

    for key in list(item):
        if key.startswith(("_active_check", "_gemini_")) and key not in record:
            del item[key]
    item.update(record)

    if old_index is not None:
        item["index"] = old_index
    if old_primary:
        item["_kroger_upc"] = old_primary
    if old_aliases:
        item["_kroger_upc_aliases"] = old_aliases
    tag_pool_item_with_upc(item, upc)
    item["_reactivated_at"] = run_date
    return item


def load_skipped_upcs(path):
    """{upc: {"name", "reason", "at"}} for Kroger products that were new
    but couldn't be added (no Walmart page, no image, no calorie number).
    They have no pool object to carry their UPC, so this file does -- so
    they aren't re-tried (and don't re-spend DDG rate-limit budget) every
    run. Delete an entry to make the script try that product again."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_skipped_upcs(path, skipped):
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(skipped, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


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


def fetch_kroger_page(token, term, page, cfg, location_id):
    """One page (cfg['kroger']['results_per_term'] products) of Kroger
    results for one search term, scoped to a store with filter.locationId
    -- without it Kroger returns no price data at all. Returns a list
    (empty when the term has run out of results)."""
    limit = cfg["kroger"]["results_per_term"]
    resp = requests.get(
        KROGER_PRODUCTS_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={
            "filter.term": term,
            "filter.locationId": location_id,
            "filter.limit": limit,
            "filter.start": page * limit + 1,  # Kroger's start is 1-based
        },
        timeout=cfg["kroger"]["timeout_seconds"],
    )
    if resp.status_code == 404 and page > 0:
        return []  # ran past the end of available results
    resp.raise_for_status()
    return resp.json().get("data") or []


def iter_kroger_products(token, cfg, location_id):
    """Lazily yields raw Kroger products, one page at a time, and stops
    being asked for more the moment the caller has what it needs -- so
    the run keeps pulling Kroger only until it has enough NEW products,
    not a fixed amount. Pages are taken round-robin across the search
    terms (term A page 1, term B page 1, ..., then term A page 2, ...) so
    a generic early term like "bowl" can't crowd out later ones like
    "breakfast". A term drops out of the rotation when it returns a short
    page, an error, or hits kroger.max_pages_per_term."""
    limit = cfg["kroger"]["results_per_term"]
    max_pages = cfg["kroger"]["max_pages_per_term"]
    active_terms = list(cfg["kroger"]["search_terms"])

    for page in range(max_pages):
        for term in list(active_terms):
            log(f"Kroger search: {term!r} page {page + 1}")
            try:
                data = fetch_kroger_page(token, term, page, cfg, location_id)
            except requests.RequestException as e:
                log(f"  Kroger search failed for {term!r} page {page + 1}: {e} -- dropping this term.")
                active_terms.remove(term)
                continue
            if len(data) < limit:
                active_terms.remove(term)  # last page for this term
            yield from data
        if not active_terms:
            return


def _positive_price(value):
    """Kroger price value -> float, or None if missing / zero / junk."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def find_kroger_locations(token, zip_code, timeout, limit=10, radius_miles=100):
    """Lists Kroger-family stores near a ZIP code, nearest first. Used
    both by --find-location (to pick a locationId by hand) and to resolve
    the KROGER_ZIP secret into a store. Kroger's default search radius is
    only 10 miles, so it's widened (kroger.location_search_radius_miles)
    -- otherwise a ZIP with no store that close returns nothing. Returns
    a list of (locationId, one-line description)."""
    resp = requests.get(
        KROGER_LOCATIONS_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"filter.zipCode.near": zip_code, "filter.radiusInMiles": radius_miles,
                "filter.limit": limit},
        timeout=timeout,
    )
    resp.raise_for_status()
    found = []
    for loc in resp.json().get("data") or []:
        addr = loc.get("address") or {}
        desc = (f"{loc.get('name', '')} ({loc.get('chain', '')}) -- "
                f"{addr.get('addressLine1', '')}, {addr.get('city', '')}, "
                f"{addr.get('state', '')} {addr.get('zipCode', '')}")
        found.append((loc.get("locationId"), desc))
    return found


def kroger_product_to_candidate(product):
    """Normalizes one raw Kroger product dict into the loose fields this
    script cares about. Returns None if it's missing what we need
    (UPC + a description + a price at the configured store) OR if none of
    Kroger's own category labels for
    it actually mention "frozen" -- this is what makes broader, shorter
    search terms (like "bowl" or "meal" instead of "frozen bowl") safe to
    use: the term casts a wide net, but only genuinely frozen-aisle
    products make it through, based on Kroger's own categorization
    rather than on the search term matching anything."""
    upc = product.get("upc")
    description = (product.get("description") or "").strip()
    if not upc or not description:
        return None

    categories = product.get("categories") or []
    if not any("frozen" in cat.lower() for cat in categories):
        return None

    brand = (product.get("brand") or "").strip()
    items = product.get("items") or [{}]
    size = (items[0].get("size") or "").strip()

    # Kroger's price object: {"regular": 5.99, "promo": 4.99} -- "promo" is
    # 0 (or absent) when nothing's on sale. Only present when the search
    # was scoped to a store (filter.locationId). No price = no candidate,
    # since the price on the pool record comes from Kroger and nowhere else.
    price_info = items[0].get("price") or {}
    regular = _positive_price(price_info.get("regular"))
    promo = _positive_price(price_info.get("promo"))
    price_current = promo if promo is not None else regular
    if price_current is None:
        return None

    return {
        "upc": str(upc),
        "brand": brand,
        "description": description,
        "categories": categories,
        "size": size,
        "price_current": price_current,
        "price_regular": regular if regular is not None else price_current,
    }


def iter_new_candidates(token, cfg, location_id, matcher, known_upcs, stats, on_existing_match):
    """Lazily yields Kroger products that are NEW -- pulling more from
    Kroger only when the caller asks for the next one.

    For every product pulled:
      - not frozen / no price at the store / no UPC -> ignored (that's
        free to re-check every run, so it isn't recorded anywhere);
      - its UPC is already known (on a pool item, or in the skipped-UPCs
        file, or seen earlier this run) -> ignored, free;
      - otherwise its name is cleaned (clean_search_words) and fuzzy-
        matched against the cleaned name of every pool item WITH A MATCHING
        BRAND (a pool item with a different brand is never a candidate --
        see brands_match()), keeping the single HIGHEST score. Score
        above matcher.threshold (90) -> it already
        exists: on_existing_match(pool_item, upc) records the UPC on that
        best-matching item so the product is skipped next run, and the
        loop moves on to the next Kroger product. Score at or below 90 ->
        it's new, and it's yielded.

    `stats` is a dict this fills in (pulled / not_frozen_or_unpriced /
    known_upc / matched_existing) for the end-of-run log."""
    for product in iter_kroger_products(token, cfg, location_id):
        stats["pulled"] += 1
        candidate = kroger_product_to_candidate(product)
        if candidate is None:
            stats["not_frozen_or_unpriced"] += 1
            continue

        upc = candidate["upc"]
        if upc in known_upcs:
            stats["known_upc"] += 1
            continue

        item, score = matcher.best_match(candidate["description"], candidate["brand"])
        if item is not None and score > matcher.threshold:
            stats["matched_existing"] += 1
            log(f"    exists ({score:.0f}): {candidate['description']!r} ~ {item.get('PRODUCT_NAME')!r} "
                f"(UPC {upc})")
            on_existing_match(item, upc)
            continue

        known_upcs.add(upc)  # seen -- never yield the same UPC twice in one run
        yield candidate


# ---------------------------------------------------------------------------
# Step 2: DuckDuckGo Walmart link lookup
# ---------------------------------------------------------------------------

# Matches Walmart's actual product-page URL shape:
# https://www.walmart.com/ip/<slug>/<numeric-id>  -- requiring the "/ip/"
# segment (not just any trailing digits in the path) means a search-results
# page, category page, or something on a totally different domain can't
# accidentally get treated as a real product match. Captures the slug too
# (group 1), so the product's actual Walmart title can be read straight
# out of its own URL.
WALMART_IP_URL_RE = re.compile(r"/ip/([^/]+)/(\d+)(?:[/?#]|$)")


def slug_to_product_name(slug):
    """Turns a Walmart URL slug ('Banquet-Family-Size-Salisbury-Steaks-and-
    Brown-Gravy-Frozen-Meal-27-oz-Frozen') into a readable product name
    ('Banquet Family Size Salisbury Steaks and Brown Gravy Frozen Meal
    27 oz Frozen') -- decodes any %XX URL-encoding first, then swaps
    hyphens/underscores for spaces and collapses repeats."""
    from urllib.parse import unquote
    text = unquote(slug).replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def verification_key(text, from_slug=False):
    """Cleaned words of a product name, for checking that a Walmart page
    is the same product as a Kroger one. Same cleanup as compare_key(),
    plus: "and" is dropped (Walmart slugs lose the "&" that Kroger names
    keep), and in a URL slug a split possessive is rejoined ("Amy s" ->
    "Amys") so it lines up with Kroger's "Amy's"."""
    if from_slug:
        text = re.sub(r"(\w) s\b", r"\1s", text)
    words = [w for w in clean_search_words(text) if w != "and"]
    return " ".join(words).replace("'", "")


def walmart_page_matches(candidate, slug_name, cfg):
    """Is the Walmart page whose URL slug is `slug_name` the SAME PRODUCT
    as this Kroger candidate? DuckDuckGo's site:walmart.com search only
    returns the closest Walmart page(s) it found, which can be a
    different flavor or even a different brand -- and treating a wrong
    page as this product would tag its UPC on the wrong pool row or
    overwrite the wrong row.
    Returns (matches, score, reason). It matches when:
      1. the brands agree -- Kroger's brand appears in the URL's product
         name (same rule brands_match() uses against the pool; if Kroger
         gave no brand at all, this check is skipped), AND
      2. the cleaned names score at least ddg_walmart.min_name_match_score
         (0-100, rapidfuzz token_sort_ratio) against each other."""
    # brand_and_name() so that a Kroger description missing its brand still
    # lines up with a Walmart title that has it.
    cand_key = verification_key(brand_and_name(candidate))
    slug_key = verification_key(slug_name, from_slug=True)
    cand_brand = normalize_brand(candidate["brand"])
    if cand_brand and not brands_match(cand_brand, cand_key, "", slug_key,
                                        cfg["dedup"]["brand_min_similarity"]):
        return False, 0.0, "brand_mismatch"
    score = float(fuzz.token_sort_ratio(cand_key, slug_key))
    if score < cfg["ddg_walmart"]["min_name_match_score"]:
        return False, score, "name_mismatch"
    return True, score, "ok"


def brand_and_name(candidate):
    """"<brand> <description>", but without repeating the brand when
    Kroger's description already starts with / contains it (it usually
    does -- e.g. "Banquet Chicken Fried Steak" already has "Banquet")."""
    brand = candidate["brand"]
    description = candidate["description"]
    text = f"{brand} {description}" if brand and brand.lower() not in description.lower() else description
    return re.sub(r"\s+", " ", text).strip()


def find_walmart_listing(ddgs, candidate, cfg):
    """Searches 'site:walmart.com <brand> <product name>' via DuckDuckGo
    text search (ddgs.text()) -- free and keyless, unlike Serper.dev (paid
    credits) and the short-lived Groq openai/gpt-oss-20b + browser_search
    version this replaces (Groq's free tier caps at 8,000 tokens/minute
    and 200,000/day, and a browser_search call is token-hungry enough
    that real runs kept hitting that ceiling within a few products). Same
    retry/backoff technique as search_image() below (DDG image search)
    uses. Checks each result in turn for the first one that's on
    walmart.com, has the real /ip/<slug>/<id> product-page shape, AND is
    the same product as the Kroger candidate (walmart_page_matches():
    same brand, similar name -- judged from the product name inside the
    URL), rather than trusting whatever the top result happens to be.
    Returns a dict with product_url/sku (both None if nothing matched)
    plus query/reason/top-result metadata; "name_match_score" on success,
    and "closest_rejected" ((name, score) of the best product page that
    failed the same-product check) when that was the reason for
    failing."""
    query = f"site:walmart.com {brand_and_name(candidate)}"
    dcfg = cfg["ddg_walmart"]
    region = dcfg["region"]
    safesearch = dcfg["safesearch"]
    max_retries = dcfg["max_retries"]
    max_results = dcfg.get("max_results", 10)
    backoff = 5.0

    results = None
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            results = list(ddgs.text(query, region=region, safesearch=safesearch, max_results=max_results))
            break
        except RatelimitException:
            wait = min(backoff + random.uniform(0, backoff * 0.5), 120.0)
            log(f"  DDG ratelimit on {query!r} (attempt {attempt}/{max_retries}) -- backing off {wait:.1f}s")
            last_error = "ratelimited"
            time.sleep(wait)
            backoff = min(backoff * 2, 90)
        except TimeoutException:
            wait = min(backoff + random.uniform(0, 2), 90.0)
            log(f"  DDG timeout on {query!r} (attempt {attempt}/{max_retries}) -- retrying in {wait:.1f}s")
            last_error = "timeout"
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
        except (DDGSException, Exception) as e:  # noqa: BLE001
            # ddgs raises DDGSException("No results found.") instead of
            # returning [] when a search genuinely turns up nothing --
            # that's a real answer, not a transient failure, so it's
            # taken immediately instead of burning through retries/backoff
            # for something that won't be different on attempt 2.
            if "no results" in str(e).lower():
                results = []
                break
            last_error = str(e)
            if attempt < max_retries:
                wait = min(backoff + random.uniform(0, 2), 90.0)
                log(f"  DDG error on {query!r} (attempt {attempt}/{max_retries}): {e} -- retrying in {wait:.1f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue

    if results is None:
        return {
            "product_url": None, "sku": None, "product_name": None, "query": query,
            "reason": f"request_failed: {last_error}", "top_result_url": None,
        }

    if not results:
        return {
            "product_url": None, "sku": None, "product_name": None, "query": query,
            "reason": "no_search_results", "top_result_url": None,
        }

    top_result_url = results[0].get("href", "")
    saw_walmart_domain = False
    closest_rejected = None          # (name, score) of the best product page that wasn't the same product
    for result in results:
        url = result.get("href", "")
        netloc = urlparse(url).netloc.lower().split(":")[0]
        is_walmart_domain = netloc == "walmart.com" or netloc.endswith(".walmart.com")
        if not is_walmart_domain:
            continue
        saw_walmart_domain = True
        match = WALMART_IP_URL_RE.search(urlparse(url).path)
        if match:
            slug, sku = match.group(1), match.group(2)
            page_name = slug_to_product_name(slug)
            same_product, score, _why = walmart_page_matches(candidate, page_name, cfg)
            if same_product:
                return {
                    "product_url": f"https://www.walmart.com/ip/{slug}/{sku}", "sku": sku,
                    "product_name": page_name, "query": query,
                    "reason": "ok", "top_result_url": top_result_url,
                    "name_match_score": score,
                }
            if closest_rejected is None or score > closest_rejected[1]:
                closest_rejected = (page_name, score)

    if closest_rejected is not None:
        reason = "no_matching_walmart_page"
    elif saw_walmart_domain:
        reason = "walmart_domain_but_no_ip_pattern"
    else:
        reason = "no_walmart_result_in_top_results"
    return {
        "product_url": None, "sku": None, "product_name": None, "query": query,
        "reason": reason, "top_result_url": top_result_url,
        "closest_rejected": closest_rejected,
    }


# ---------------------------------------------------------------------------
# Step 4: DuckDuckGo image lookup (same retry technique as AddImageUrl.py)
# ---------------------------------------------------------------------------

def search_image(ddgs, query, cfg):
    """Returns (image_url_or_None, reason). DuckDuckGo Images, searched
    with the product's Walmart URL as the query (main() passes it in), and
    the TOP result that has an image URL is taken -- whoever hosts it.
    Retry/backoff behavior is the same as AddImageUrl.py's."""
    region = cfg["ddg_image"]["region"]
    safesearch = cfg["ddg_image"]["safesearch"]
    max_retries = cfg["ddg_image"]["max_retries"]
    max_results = cfg["ddg_image"].get("max_results", 8)
    backoff = 5.0

    for attempt in range(1, max_retries + 1):
        try:
            results = ddgs.images(query, region=region, safesearch=safesearch, max_results=max_results)
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
            # ddgs raises DDGSException("No results found.") instead of
            # returning [] when a search genuinely turns up nothing --
            # that's a real answer, not a transient failure, so it's
            # taken immediately instead of burning through retries/backoff
            # for something that won't be different on attempt 2.
            if "no results" in str(e).lower():
                return None, "no_results"
            if attempt < max_retries:
                wait = min(backoff + random.uniform(0, 2), 90.0)
                log(f"  DDG error on {query!r} (attempt {attempt}/{max_retries}): {e} -- retrying in {wait:.1f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            return None, f"error: {e}"

    return None, "retry_exhausted"


# ---------------------------------------------------------------------------
# Step 5: calories = max(USDA, Open Food Facts, Gemini); servings_per_container
# from USDA. Modeled on DataCleaning/Max_Calories_Count.py (calories) and
# DataCleaning/build_meal_pool.py (servings_per_container).
# ---------------------------------------------------------------------------

def clean_api_query_name(raw_name):
    """Same cleanup build_meal_pool.py's / Max_Calories_Count.py's
    clean_product_name() does: strips trailing size/count descriptors and
    stray punctuation so the USDA and Open Food Facts searches get a
    cleaner query."""
    if not raw_name:
        return ""
    cleaned = re.sub(r",?\s*Frozen Meals.*", "", raw_name, flags=re.IGNORECASE)
    cleaned = re.sub(r",?\s*\d+(\.\d+)?\s*(oz|ct|count|g|lb).*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+\s+\d+/\d+\s*(ounce|oz|lb)?\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+/\d+\s*(ounce|oz|lb)?\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[/,.'\"]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def clean_serving_text(raw):
    """Cleans a USDA-sourced serving-size string. Some branded-food
    labels write the serving size AND how-many-servings-are-in-the-
    container into the same field, e.g. "2.71 OZ SERVING, 36 Servings
    Per Container" -- that trailing ", N Servings Per Container" clause
    is a different fact (container count, not serving amount) and gets
    dropped, along with a bare "Per Serving"/"Per Container" boilerplate
    suffix some labels tack on. Returns "N/A" if what's left has no
    actual amount in it at all (e.g. bare "Amount")."""
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


def _usda_food_calories(food):
    """Calories per serving for one USDA branded-food record, or None.
    Same order Max_Calories_Count.py's usda_lookup() uses: the label's
    own calories value first; else the per-100g energy value scaled by
    the serving size in grams; else the raw per-100g value."""
    label_nutrients = food.get("labelNutrients") or {}
    if "calories" in label_nutrients and label_nutrients["calories"].get("value") is not None:
        return float(label_nutrients["calories"]["value"])

    serving_size = food.get("servingSize")
    serving_size_unit = (food.get("servingSizeUnit") or "").upper()
    for n in food.get("foodNutrients", []):
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


def fetch_usda_info(product_name, usda_api_key, cfg):
    """One USDA FoodData Central Branded Foods search, used for TWO
    fields: returns {"calories": float or None, "servings_per_container":
    str}. servings_per_container is pulled exactly the way
    build_meal_pool.py's fetch_usda_info() pulls it --
    householdServingFullText, else packageWeight, run through
    clean_serving_text() -- which is the amount of ONE serving ("1 cup",
    "0.25 pizza"), not how many servings are in the container. It's
    "N/A" if USDA has no match. Calories use the same logic as
    Max_Calories_Count.py's usda_lookup()."""
    empty = {"calories": None, "servings_per_container": "N/A"}
    query = clean_api_query_name(product_name)
    if not query:
        return empty

    timeout = cfg["usda"]["timeout_seconds"]
    max_retries = cfg["usda"]["max_retries"]
    retry_delay = cfg["usda"]["retry_delay_seconds"]

    foods = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(
                "https://api.nal.usda.gov/fdc/v1/foods/search",
                params={"api_key": usda_api_key, "query": query, "dataType": "Branded", "pageSize": 5},
                timeout=timeout,
            )
            resp.raise_for_status()
            foods = resp.json().get("foods") or []
            break
        except (requests.RequestException, ValueError):
            if attempt < max_retries:
                time.sleep(retry_delay)

    if not foods:
        return empty

    best = foods[0]
    serving_raw = best.get("householdServingFullText") or best.get("packageWeight")
    serving = clean_serving_text(str(serving_raw).strip()) if serving_raw else "N/A"
    return {"calories": _usda_food_calories(best), "servings_per_container": serving}


def fetch_off_calories(product_name, cfg):
    """Open Food Facts search (no API key, just a descriptive User-Agent).
    Returns (calories_per_serving or None, is_100g_fallback). Same logic
    as Max_Calories_Count.py's off_lookup(): prefers the label's own
    per-serving kcal, then per-100g kcal scaled by serving_quantity
    (grams), and as a last resort the raw per-100g value with
    is_100g_fallback=True -- less directly comparable, so it's flagged
    ("off_100g_fallback") in _calorie_max_source. That last resort rarely
    OVERSTATES a serving (most frozen entree servings are >100g), which
    keeps it safe for a "take the max" comparison."""
    query = clean_api_query_name(product_name)
    if not query:
        return None, False

    off_cfg = cfg["open_food_facts"]
    max_retries = off_cfg["max_retries"]
    retry_delay = off_cfg["retry_delay_seconds"]

    data = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(
                "https://world.openfoodfacts.org/cgi/search.pl",
                params={
                    "search_terms": query, "search_simple": 1, "action": "process",
                    "json": 1, "page_size": 5,
                    "fields": "product_name,nutriments,serving_quantity,serving_size",
                },
                headers={"User-Agent": off_cfg["user_agent"]},
                timeout=off_cfg["timeout_seconds"],
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError):
            if attempt < max_retries:
                time.sleep(retry_delay)

    products = (data or {}).get("products") or []
    if not products:
        return None, False
    nutriments = products[0].get("nutriments") or {}

    per_serving = nutriments.get("energy-kcal_serving")
    if per_serving is not None:
        try:
            return float(per_serving), False
        except (TypeError, ValueError):
            pass

    per_100g = nutriments.get("energy-kcal_100g")
    serving_qty = products[0].get("serving_quantity")
    if per_100g is not None and serving_qty:
        try:
            return round(float(per_100g) * float(serving_qty) / 100.0, 1), False
        except (TypeError, ValueError):
            pass

    if per_100g is not None:
        try:
            return float(per_100g), True
        except (TypeError, ValueError):
            pass
    return None, False


def round_to_nearest_ten(value):
    """260 -> 260, 263.4 -> 260, 265 -> 270 (halves round up, not to the
    nearest even number the way Python's round() would)."""
    return int(value / 10.0 + 0.5) * 10


def pick_max_calories(usda_cal, off_cal, off_is_100g, gemini_cal):
    """The "largest wins" rule from Max_Calories_Count.py, applied to the
    three sources a new item has: USDA, Open Food Facts, Gemini. Any that
    came back empty (or zero -- no real entree is 0 calories) are ignored.
    Returns (calories, source_label, {source: value, ...}) -- calories and
    label are None if nothing usable came back from any of them. Ties go
    to the earlier of USDA, Open Food Facts, Gemini. The winning value is
    returned as-is (unrounded); main() rounds it to the nearest ten when
    it builds the record, so "_calorie_sources" keeps the raw numbers."""
    values = {}
    if usda_cal is not None and usda_cal > 0:
        values["usda"] = usda_cal
    if off_cal is not None and off_cal > 0:
        values["off_100g_fallback" if off_is_100g else "open_food_facts"] = off_cal
    if gemini_cal is not None and gemini_cal > 0:
        values["gemini"] = gemini_cal
    if not values:
        return None, None, {}
    source = max(values, key=lambda k: values[k])
    return values[source], source, values


# ---------------------------------------------------------------------------
# Step 5 (cont.): Gemini calorie estimate -- the third input to the max()
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


def build_batch_prompt(cfg, enriched_batch):
    """enriched_batch is a list of (candidate, walmart, image_url,
    image_query, image_reason) tuples. Each product is described to
    Gemini by Kroger's own product description, brand and size -- the
    same name that ends up in PRODUCT_NAME."""
    item_line_tmpl = cfg["prompt"]["item_line"]
    lines = []
    for n, (c, walmart, *_rest) in enumerate(enriched_batch, start=1):
        lines.append(item_line_tmpl.format(
            n=n, product_name=c["description"], brand=c["brand"] or "unknown brand",
            size=c["size"] or "unknown size",
        ).rstrip("\n"))
    products_block = "\n".join(lines)
    return cfg["prompt"]["intro"].format(count=len(enriched_batch), products_block=products_block)


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

    return {
        "index": index,
        "recognized": bool(raw.get("recognized")),
        "calories": calories,
        "notes": str(raw.get("notes", "")).strip(),
    }


def gemini_calories(result):
    """Gemini's calorie estimate if it's usable as an input to the
    max(): the product was recognized AND a number came back. A guess it
    flagged as unrecognized is ignored. None otherwise. (This is one of
    three calorie sources now -- USDA and Open Food Facts are the others
    -- so a missing/unrecognized Gemini answer no longer blocks an item
    the way it used to when Gemini was the only source.)"""
    if result is None or not result["recognized"]:
        return None
    return result["calories"]


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

def add_pickup_param(url):
    """Appends the Walmart in-store-pickup fulfillment query param onto a
    product URL, respecting whatever's already there (Walmart /ip/ URLs
    don't normally have a query string, but this stays safe either way)."""
    if not url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}fulfillmentIntent=Pickup"


def build_instacart_search_url(name):
    """Builds an Instacart search-results URL from a product name, using
    the exact same cleaned words the fuzzy "already in the pool?" check
    compares (clean_search_words(): cut at the first comma, sizes and
    numbers removed, punctuation removed, lowercased). Apostrophes stay
    as a literal "%27" in place (so "Callender's" becomes "callender%27s",
    not "callender s" or "callenders"), and the words are joined with
    "+" -- e.g. "Marie Callender's Pot Roast, Frozen Meal, 10 oz" ->
    https://www.instacart.com/store/s?k=marie+callender%27s+pot+roast

    This URL is BUILT, not looked up: nothing checks that the search
    actually returns anything on Instacart."""
    words = clean_search_words(name)
    if not words:
        return ""
    return "https://www.instacart.com/store/s?k=" + "+".join(words).replace("'", "%27")


def _money(value):
    return f"{value:.2f}" if value is not None else ""


def build_pool_record(candidate, walmart, image_url, image_query, image_reason,
                       calories, calorie_source, calorie_values, servings_per_container,
                       gemini_result, gemini_model, location_id, idx, run_date):
    # PRODUCT_NAME and both prices come straight from Kroger. SKU is the
    # numeric id parsed out of the Walmart URL (find_walmart_listing only
    # returns a URL when it found one, so there's always a SKU with it).
    product_name = candidate["description"]

    record = {
        "BRAND": candidate["brand"],
        "BREADCRUMBS": "Frozen/" + (candidate["categories"][-1] if candidate["categories"] else "Frozen"),
        "CATEGORY": candidate["categories"][-1] if candidate["categories"] else "Frozen",
        "DEPARTMENT": "Frozen",
        "PRICE_CURRENT": _money(candidate["price_current"]),
        "PRICE_RETAIL": _money(candidate["price_regular"]),
        "PRODUCT_NAME": product_name,
        "PRODUCT_SIZE": candidate["size"],
        "PRODUCT_URL": add_pickup_param(walmart["product_url"]),
        "PROMOTION": "",
        "RunDate": run_date,
        "SHIPPING_LOCATION": "",
        "SKU": walmart["sku"],
        "SOURCE": "Kroger",  # every row from this script -- distinguishes it from
                              # the original 2022 Walmart-CSV rows, which have no
                              # SOURCE column at all (absent, not blank).
        "SUBCATEGORY": candidate["categories"][0] if candidate["categories"] else "",
        "active": True,  # DuckDuckGo's search already confirmed a live walmart.com
                          # page before this record was ever assembled (see
                          # find_walmart_listing) -- check_active_urls.py's Playwright
                          # checks are unreliable here (Walmart bot-blocks it), so that
                          # confirmation is trusted directly instead of waiting on a
                          # check that mostly won't run.
        "calories": calories,
        "image_url": image_url,
        "INSTACART_URL": build_instacart_search_url(product_name),  # built, never searched
        "index": f"kroger-{idx}",
        "servings_per_container": servings_per_container or "N/A",
        "tid": "",
        # Provenance metadata -- "SOURCE" above is the human-readable
        # column; these _-prefixed fields are the detail behind it, kept
        # separate from check_active_urls.py's own "_active_check_*" writes.
        "_kroger_upc": candidate["upc"],
        "_kroger_discovered_at": run_date,
        "_kroger_location_id": location_id,
        "_price_source": "Kroger",
        "_ddg_walmart_query": walmart["query"],
        "_ddg_walmart_reason": walmart["reason"],
        "_ddg_walmart_top_result_url": walmart["top_result_url"],
        "_walmart_name_match_score": walmart.get("name_match_score"),  # how closely the Walmart
                                                                        # page's name matched Kroger's
        "_walmart_url_slug_name": walmart.get("product_name"),  # debug/reference only -- not
                                                                  # PRODUCT_NAME (Kroger's is).
        "_image_search_query": image_query,
        "_image_search_reason": image_reason,
        # Same two bookkeeping fields Max_Calories_Count.py writes, so a
        # later run of that script treats this row as already checked.
        "_calorie_max_checked_at": run_date,
        "_calorie_max_source": calorie_source,
        "_calorie_sources": calorie_values,  # every source that returned a number
    }

    if gemini_result:
        record["_gemini_checked_at"] = run_date
        record["_gemini_model"] = gemini_model
        record["_gemini_notes"] = gemini_result["notes"]
        record["_gemini_recognized"] = gemini_result["recognized"]

    return record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class AbortRun(Exception):
    """Something that would fail every remaining candidate the same way
    (e.g. a Kroger token failure) -- stop the run instead of burning
    through the list."""


# Walmart-search "reason"s that are about THIS product and won't change
# tomorrow (as opposed to a network error or a rate limit, which are worth
# retrying on a later run). Only these get the product's UPC written to
# the skipped-UPCs file.
_PERMANENT_WALMART_REASONS = {
    "no_search_results", "no_walmart_result_in_top_results", "walmart_domain_but_no_ip_pattern",
    "no_matching_walmart_page",
}


def ask_gemini_for_calories(c, walmart, image_url, image_query, image_reason, gemini_key, cfg, rate_limiter):
    """One Gemini call for one product. Returns (result_or_None, model,
    call_failed) -- call_failed is True if the call itself blew up (quota,
    network), as opposed to Gemini answering "I don't recognize this"."""
    batch = [(c, walmart, image_url, image_query, image_reason)]
    try:
        raw_results, model = call_gemini_batch(build_batch_prompt(cfg, batch), gemini_key, cfg, rate_limiter)
    except DailyQuotaExceeded as e:
        log(f"  Gemini fallback chain exhausted: {e}")
        return None, None, True
    except Exception as e:  # noqa: BLE001 -- one bad call shouldn't kill the run
        log(f"  Gemini call failed: {e}")
        return None, None, True
    matched = match_results_to_candidates(batch, raw_results)
    return matched[0], model, False


def enrich_candidate(c, ctx):
    """Runs one NEW Kroger product through the rest of the pipeline and
    returns (outcome, detail):
      ("added", record)         -- built and ready to append to the pool
      ("tagged", pool_item)     -- its Walmart SKU is already ACTIVE in the pool, so it
                                   IS that existing product; the UPC was recorded on it
      ("revived", pool_item)    -- its Walmart SKU is already in the pool on a row that is
                                   inactive (or never checked): that row was refreshed in
                                   place with the new Walmart URL, active=True, and
                                   everything else filled in as for a new product
      ("skip", reason)          -- can't be added, and won't be able to later either;
                                   the caller records its UPC in the skipped-UPCs file
      ("retry_later", reason)   -- failed for a transient reason (network, rate limit);
                                   nothing is recorded, so a later run tries it again
    Cheapest-to-fail steps come first, so Open Food Facts and Gemini calls
    aren't spent on products with no Walmart page (Kroger's own store
    brands, mostly)."""
    cfg = ctx.cfg

    # Walmart page via DuckDuckGo (site:walmart.com search) -> PRODUCT_URL + SKU (the id in the URL).
    walmart = find_walmart_listing(ctx.ddgs, c, cfg)
    if not walmart["product_url"]:
        reason = walmart["reason"]
        detail = f"no Walmart link ({reason})"
        if walmart.get("closest_rejected"):
            name, score = walmart["closest_rejected"]
            detail += (f" -- closest Walmart page was a different product: {name!r} "
                       f"(name match {score:.0f}, needs {cfg['ddg_walmart']['min_name_match_score']})")
        return ("skip" if reason in _PERMANENT_WALMART_REASONS else "retry_later"), detail

    sku = walmart["sku"]
    if sku in ctx.active_skus:
        item = tag_existing_pool_item_with_upc(ctx.pool, sku, c["upc"])
        return "tagged", item
    # A row with this SKU that ISN'T active (dead link, or never checked)
    # gets revived once the rest of the pipeline succeeds -- see the end.
    revive_item = next((it for it in ctx.pool if str(it.get("SKU", "")).strip() == sku), None)

    # Image via DuckDuckGo: the Walmart URL itself is the search query and
    # the top result wins, whoever hosts it.
    image_query = walmart["product_url"]
    image_url, image_reason = search_image(ctx.ddgs, image_query, cfg)
    if not image_url:
        return ("skip" if image_reason == "no_results" else "retry_later"), f"no image ({image_reason})"
    time.sleep(random.uniform(cfg["ddg_image"]["min_delay_seconds"], cfg["ddg_image"]["max_delay_seconds"]))

    # Calories + servings_per_container from USDA; calories also from Open
    # Food Facts and Gemini; the highest calorie number wins.
    usda = fetch_usda_info(c["description"], ctx.usda_key, cfg)
    time.sleep(cfg["usda"]["min_call_interval_seconds"])
    off_cal, off_is_100g = fetch_off_calories(c["description"], cfg)
    time.sleep(cfg["open_food_facts"]["min_call_interval_seconds"])
    gemini_result, gemini_model, gemini_failed = ask_gemini_for_calories(
        c, walmart, image_url, image_query, image_reason, ctx.gemini_key, cfg, ctx.rate_limiter)

    calories_raw, calorie_source, calorie_values = pick_max_calories(
        usda["calories"], off_cal, off_is_100g, gemini_calories(gemini_result))
    calories = round_to_nearest_ten(calories_raw) if calories_raw else 0
    if calories <= 0:
        note = gemini_result["notes"] if gemini_result and gemini_result["notes"] else "no USDA/OFF match"
        return ("retry_later" if gemini_failed else "skip"), f"no calorie number from USDA, Open Food Facts, or Gemini ({note})"

    record = build_pool_record(
        c, walmart, image_url, image_query, image_reason,
        calories, calorie_source, calorie_values, usda["servings_per_container"],
        gemini_result, gemini_model, ctx.location_id, ctx.idx + 1, ctx.run_date,
    )
    if revive_item is not None:
        return "revived", revive_pool_item(revive_item, record, c["upc"], ctx.run_date)
    ctx.idx += 1
    return "added", record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--max-new-items", type=int, default=None,
                         help="Override run.max_new_items for this run")
    parser.add_argument("--dry-run", action="store_true",
                         help="Pull Kroger and fuzzy-match only: list the new products it would enrich, "
                              "write nothing (no UPC tagging, no pool changes), skip DDG/USDA/OFF/Gemini")
    parser.add_argument("--find-location", metavar="ZIP", default=None,
                         help="Print Kroger store ids near this ZIP code (for kroger.location_id), then exit")
    args = parser.parse_args()

    cfg = load_config(args.config)

    kroger_id = os.environ.get("KROGER_CLIENT_ID")
    kroger_secret = os.environ.get("KROGER_CLIENT_SECRET")
    gemini_key = os.environ.get("GEMINI_KEY")
    usda_key = os.environ.get("USDA_API_KEY")
    # The env var wins over the yaml, so a one-off run can point at a
    # different store without editing the config.
    location_id = (os.environ.get("KROGER_LOCATION_ID") or str(cfg["kroger"].get("location_id") or "")).strip()
    # Used only when no store id is set above: the nearest Kroger-family
    # store to this ZIP code becomes the store prices are taken from.
    kroger_zip = (os.environ.get("KROGER_ZIP") or "").strip()
    radius_miles = cfg["kroger"].get("location_search_radius_miles", 100)

    if args.find_location:
        if not (kroger_id and kroger_secret):
            log("--find-location needs KROGER_CLIENT_ID and KROGER_CLIENT_SECRET set.")
            return
        token = get_kroger_token(kroger_id, kroger_secret, cfg["kroger"]["timeout_seconds"])
        stores = find_kroger_locations(token, args.find_location, cfg["kroger"]["timeout_seconds"],
                                       radius_miles=radius_miles)
        if not stores:
            log(f"No Kroger-family stores found near {args.find_location}.")
        for loc_id, desc in stores:
            print(f"{loc_id}  {desc}")
        return

    required = [("KROGER_CLIENT_ID", kroger_id), ("KROGER_CLIENT_SECRET", kroger_secret)]
    if not args.dry_run:
        required += [("GEMINI_KEY", gemini_key), ("USDA_API_KEY", usda_key)]
    missing = [name for name, val in required if not val]
    if not location_id and not kroger_zip:
        missing.append("KROGER_ZIP (or KROGER_LOCATION_ID / kroger.location_id in kroger_new_items.yaml)")
    if missing:
        log(f"Missing {', '.join(missing)} -- skipping kroger_new_items run.")
        return

    max_new_items = args.max_new_items or cfg["run"]["max_new_items"]
    max_attempts = cfg["run"]["max_enrichment_attempts"]
    deadline = time.monotonic() + cfg["run"]["max_runtime_minutes"] * 60
    pool_path = _SCRIPT_DIR / cfg["pool"]["path"]
    skipped_path = _SCRIPT_DIR / cfg["pool"]["skipped_upcs_path"]

    pool = load_pool(pool_path)
    skipped_upcs = load_skipped_upcs(skipped_path)
    matcher = PoolNameMatcher(pool, cfg["dedup"]["fuzzy_threshold"], cfg["dedup"]["brand_min_similarity"])
    known_upcs = existing_upc_set(pool, skipped_upcs)
    active_skus = existing_active_skus(pool)
    log(f"Loaded {len(pool)} existing pool item(s) and {len(skipped_upcs)} skipped UPC(s); "
        f"{len(known_upcs)} Kroger UPC(s) already known, {len(active_skus)} active pool SKU(s). "
        f"Fuzzy-dedup threshold: score > {matcher.threshold}, same brand only. Target: {max_new_items} new item(s).")

    log("Fetching Kroger OAuth token...")
    token = get_kroger_token(kroger_id, kroger_secret, cfg["kroger"]["timeout_seconds"])

    if not location_id:
        try:
            stores = find_kroger_locations(token, kroger_zip, cfg["kroger"]["timeout_seconds"],
                                           limit=1, radius_miles=radius_miles)
        except requests.RequestException as e:
            log(f"Couldn't look up a Kroger store for KROGER_ZIP {kroger_zip!r}: {e} -- skipping this run.")
            return
        if not stores:
            log(f"No Kroger-family store within {radius_miles} miles of KROGER_ZIP {kroger_zip!r} -- "
                f"skipping this run. Set kroger.location_id (or KROGER_LOCATION_ID) to pick a store directly.")
            return
        location_id, store_desc = stores[0]
        log(f"KROGER_ZIP {kroger_zip}: using the nearest store, {location_id} ({store_desc}) for prices.")

    stats = {"pulled": 0, "not_frozen_or_unpriced": 0, "known_upc": 0, "matched_existing": 0}
    unsaved_tags = 0

    def on_existing_match(item, upc):
        """A Kroger product fuzzy-matched a pool item: record its UPC on
        that (highest-scoring) item so the next pull skips it."""
        nonlocal unsaved_tags
        if args.dry_run:
            return
        tag_pool_item_with_upc(item, upc)
        known_upcs.add(upc)
        unsaved_tags += 1

    new_candidates = iter_new_candidates(
        token, cfg, location_id, matcher, known_upcs, stats, on_existing_match)

    if args.dry_run:
        for n, c in enumerate(new_candidates, start=1):
            log(f"  [dry-run] new: {c['brand']} {c['description']} (UPC {c['upc']}, "
                f"Kroger price {c['price_current']:.2f}, instacart {build_instacart_search_url(c['description'])})")
            if n >= max_new_items:
                break
        log(f"[dry-run] pulled {stats['pulled']} Kroger product(s); nothing written.")
        return

    ctx = SimpleNamespace(
        cfg=cfg, pool=pool, active_skus=active_skus, location_id=location_id,
        gemini_key=gemini_key, usda_key=usda_key,
        ddgs=DDGS(), rate_limiter=_RateLimiter(cfg["gemini"]["min_call_interval_seconds"]),
        idx=next_index(pool), run_date=datetime.now(timezone.utc).isoformat(),
    )

    added = attempts = tagged_by_sku = revived = skipped = retry_later = 0
    stop_reason = "Kroger had nothing more new to pull"

    while True:
        if added >= max_new_items:
            stop_reason = f"reached the target of {max_new_items} new item(s)"
            break
        if attempts >= max_attempts:
            stop_reason = f"hit run.max_enrichment_attempts ({max_attempts})"
            break
        if time.monotonic() >= deadline:
            stop_reason = f"hit run.max_runtime_minutes ({cfg['run']['max_runtime_minutes']})"
            break

        c = next(new_candidates, None)   # pulls more from Kroger only when it has to
        if c is None:
            break
        attempts += 1
        label = brand_and_name(c)

        try:
            outcome, detail = enrich_candidate(c, ctx)
        except AbortRun as e:
            stop_reason = f"aborted: {e}"
            break

        if outcome == "added":
            pool.append(detail)
            matcher.add(detail)
            ctx.active_skus.add(detail["SKU"])
            added += 1
            log(f"  ADDED [{added}/{max_new_items}]: {detail['PRODUCT_NAME']} -- SKU {detail['SKU']}, "
                f"calories={detail['calories']} (max of {detail['_calorie_sources']}, from {detail['_calorie_max_source']}, "
                f"rounded to nearest 10), price={detail['PRICE_CURRENT']} (Kroger), "
                f"servings_per_container={detail['servings_per_container']}")
            save_pool(pool_path, pool)
            unsaved_tags = 0
        elif outcome == "revived":
            # Counts toward the target: it went through the full pipeline
            # and the row is now live with fresh data.
            matcher.add(detail)   # its name changed -- let the new name match too
            ctx.active_skus.add(detail["SKU"])
            added += 1
            revived += 1
            log(f"  REVIVED [{added}/{max_new_items}]: SKU {detail['SKU']} was inactive/unchecked -> "
                f"{detail['PRODUCT_NAME']} -- new Walmart URL, active=True, "
                f"calories={detail['calories']} (from {detail['_calorie_max_source']}), "
                f"price={detail['PRICE_CURRENT']} (Kroger), servings_per_container={detail['servings_per_container']}")
            save_pool(pool_path, pool)
            unsaved_tags = 0
        elif outcome == "tagged":
            tagged_by_sku += 1
            unsaved_tags += 1
            log(f"  EXISTS (its Walmart SKU is already active in the pool): {label} -> UPC {c['upc']} "
                f"recorded on {(detail or {}).get('PRODUCT_NAME')!r}")
        elif outcome == "skip":
            skipped += 1
            skipped_upcs[c["upc"]] = {"name": c["description"], "reason": detail, "at": ctx.run_date}
            save_skipped_upcs(skipped_path, skipped_upcs)
            log(f"  SKIP: {label} -- {detail} (UPC {c['upc']} recorded so it isn't retried)")
        else:
            retry_later += 1
            log(f"  SKIP for now: {label} -- {detail} (nothing recorded; a later run will retry it)")

        if unsaved_tags >= 25:
            save_pool(pool_path, pool)
            unsaved_tags = 0

    # Final save -- also persists UPCs recorded on existing pool items even
    # when nothing new was added this run.
    save_pool(pool_path, pool)
    log(f"Stopped: {stop_reason}.")
    log(f"Kroger pulled {stats['pulled']} product(s): {stats['not_frozen_or_unpriced']} not frozen/unpriced (ignored), "
        f"{stats['known_upc']} UPC already known, {stats['matched_existing']} fuzzy-matched an existing item "
        f"(UPC recorded on it), {tagged_by_sku} matched an already-active row by Walmart SKU (UPC recorded), "
        f"{attempts} treated as new.")
    log(f"Done. Added {added} item(s) ({revived} of them refreshed inactive/unchecked rows that already had "
        f"the same Walmart SKU); {skipped} new product(s) couldn't be added and were remembered, "
        f"{retry_later} skipped for now. Pool size now {len(pool)}.")


if __name__ == "__main__":
    main()
