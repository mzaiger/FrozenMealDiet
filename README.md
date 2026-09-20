# Poor Man's Frozen Meal Diet

A 7-day frozen meal planner: `candidate_pool.json` holds the pool of
frozen-food items (name, price, calories, serving, image, Walmart and
Instacart links, link-liveness status), and `index.html` builds a
randomized week of meals from that pool, kept within a calorie target and a
per-meal price cap, all client-side. `products.html` is a browsable view of
the same pool.

The pool started as a Walmart product CSV export (~1,800 items, 2022)
enriched with USDA calorie data, and keeps growing with new products found
through Kroger's product catalog (`kroger_new_items.py`, 4× a day).

## Repository layout

| Path | What it is |
|---|---|
| `index.html` | The meal planner. Reads `candidate_pool.json` in the browser. |
| `products.html` | Product browser over `candidate_pool.json`. |
| `candidate_pool.json` | The pool. Every script below reads and/or writes it. |
| `.github/workflows/` | The three scheduled GitHub Actions workflows — see [Workflows](#workflows). |
| `DataCleaning/` | Scripts, config and source data for building and maintaining the pool — see below. |

## DataCleaning folder

Everything in `DataCleaning/`, roughly in the order you'd use it to build the
pool from scratch and then maintain it.

| File | What it does | Automated? |
|---|---|---|
| `frozen_food.csv` | The raw Walmart product export that `Dedup.py` reads (columns: `index, SHIPPING_LOCATION, DEPARTMENT, CATEGORY, SUBCATEGORY, BREADCRUMBS, SKU, PRODUCT_URL, PRODUCT_NAME, BRAND, PRICE_RETAIL, PRICE_CURRENT, PRODUCT_SIZE, PROMOTION, RunDate, tid`). In this copy of the repo it's an empty placeholder — drop a fresh export in before running `Dedup.py`. | — (data) |
| `Dedup.py` | Reads `frozen_food.csv`, drops a few unwanted categories (Frozen Desserts, Frozen Meat & Seafood, Frozen Produce, Frozen Potatoes), de-dupes by SKU, writes `frozen_food_deduped.csv`. | No — run manually when you refresh the source CSV. |
| `frozen_food_deduped.csv` | Output of `Dedup.py` (~1,800 unique items) and the input of `build_meal_pool.py`. | — (data) |
| `build_meal_pool.py` | Reads `frozen_food_deduped.csv`, cleans each product name, looks up calories and the serving size from the USDA FoodData Central API (serving = `householdServingFullText`, else `packageWeight`, cleaned), and writes `candidate_pool.json`. Resumable (skips SKUs already in the pool), rate-limited to 15 items/minute, `--debug` for a 3-minute trial run. Needs `USDA_API_KEY`. | No — run manually to (re)build the pool from scratch. |
| `rename_and_clean_serving.py` | One-time migration: renames the `servings_per_container` key to `serving` on every row and cleans messy values (e.g. `"2.71 OZ SERVING, 36 Servings Per Container"` → `"2.71 OZ SERVING"`). Idempotent; `--dry-run` to preview. **Note:** `index.html` and `kroger_new_items.py` use the `servings_per_container` key, so don't run this on the live pool unless they're changed to match. | No — one-time. |
| `AddImageUrl.py` | For every `active` item in `candidate_pool.json`, searches DuckDuckGo Images and writes the result to `image_url`. Rate-limit-conscious: one reused session, jittered delays, exponential backoff, a cooldown after repeated failures, per-query caching, and checkpointing so it's safe to Ctrl-C and re-run. `--limit`, `--force`, `--dry-run`, `--proxy`. No API key. | No — run manually. |
| `UpdateImageUrlsFromWalmartUrl.py` | Re-does `image_url` for **every** product that has a `PRODUCT_URL` (active or not; `--only-active` to limit): searches DuckDuckGo Images with the product's Walmart URL (query string removed) as the query and takes the **top result, whoever hosts it**. A product whose search finds nothing (or errors) keeps its existing `image_url` — it never blanks one. Same rate-limit hygiene as `AddImageUrl.py` (one session, jittered delays, backoff, cooldowns, checkpointing, `--proxy`); resumable via `_image_walmart_url_checked_at` (`--force` redoes all). `--limit`, `--dry-run`. About 3 hours for the full pool at default delays. No API key. | No — run manually. |
| `check_active_urls.py` | Uses Playwright (with stealth) to visit each item's `PRODUCT_URL` on walmart.com and tag it `active: true/false/null` (null = couldn't tell, e.g. bot-blocked), with reason/query/checked-URL metadata in `_active_check_*` fields. `--only-unknown`, `--limit`. | **Yes** — `check-active-urls.yml`, hourly. |
| `check_walmart_links.py` | Alternate liveness check: searches Google via Serper.dev for `site:walmart.com <product_id>` and checks whether walmart.com is the top result. Takes a URL list or a pool JSON (`--json`), resumable, `--debug` checks the first 10. Needs `SERPER_API_KEY`. | No — not wired into any workflow; `check_active_urls.py` is the one that runs. |
| `check_instacart_urls.py` | Searches `instacart <brand> <product name>` via Serper.dev and scans every result for one on instacart.com. Sets `INSTACART_URL` (only overwritten when a real match is found) and `instacart_active`. Resumable, `--limit`, `--debug`, `--out`. Needs `SERPER_API_KEY`. | No — run manually. |
| `kroger_new_items.py` / `kroger_new_items.yaml` | Finds and adds new products from Kroger's catalog — see [Adding new products](#adding-new-products-kroger_new_itemspy). |
| `kroger_skipped_upcs.json` | Written by `kroger_new_items.py` (created on first use): Kroger UPCs of new products that couldn't be added, so they aren't retried. |
| `update_instacart_urls.py` | Sets/refreshes `INSTACART_URL` on **every** row in `candidate_pool.json` from each row's own `PRODUCT_NAME` (text cut at the first comma, apostrophes kept as `%27`, everything else non-alphanumeric turned into `+`). Pure local transform — no network, no API key. `--only-missing` fills blanks only; `--dry-run` previews. (`kroger_new_items.py` builds its Instacart URLs with a slightly stricter cleanup that also drops sizes and numbers, so URLs on new Kroger rows can differ in form from ones this script writes.) | No — run manually. |
| `Max_Calories_Count.py` | Cross-checks each item's `calories` against USDA and Open Food Facts and keeps the **largest** of {current, USDA, Open Food Facts} — only ever raises a value, never lowers it. Adds `_calorie_max_checked_at` / `_calorie_max_source` to each item (that's what makes it resumable) and writes `calorie_max_check_diff.csv` listing every change, for spot-checking. `--limit`, `--recheck-all`, `--dry-run`. Needs `USDA_API_KEY`; Open Food Facts needs none. | No — run manually. |
| `gemini_meal_lookup.py` | Asks Gemini for an estimated calories-per-serving and price for a rotating batch of pool items (oldest/never-checked first), from Gemini's own knowledge — no live web search. Writes back only what differs, plus `_gemini_*` bookkeeping fields. Needs `GEMINI_KEY`. | **Yes** — `gemini-meal-lookup.yml`, weekly. |
| `gemini_meal_lookup.yaml` | Config for `gemini_meal_lookup.py` (not a workflow): which Gemini models to try in fallback order, batch shape (`items_per_call`, `calls_per_run`, `skip_inactive`), rate limiting, and the prompt template. | — (config) |

## Adding new products (`kroger_new_items.py`)

Grows the pool with products the 2022 export never had. Config lives in
`kroger_new_items.yaml`. It runs 4× a day and adds up to 20 new products
per run.

**One run:** it pulls Kroger's live catalog one page at a time (searching
broad terms like `bowl`, `meal`, `breakfast`, `pizza` … round-robin) and
keeps pulling until it has added 20 new products — it may pull 100 or more
to find 20. For each Kroger product:

1. **Skipped for free** if it isn't in a frozen category, has no price at
   the configured store, or its Kroger UPC is already known (recorded on a
   pool item, or in `kroger_skipped_upcs.json`).
2. **Fuzzy name match, same brand only.** The Kroger name and the pool
   `PRODUCT_NAME`s are cleaned the same way an Instacart search is built —
   cut at the first comma, sizes (`27 oz`, `6 ct`), other numbers,
   punctuation and the word "frozen" removed, lowercased — and compared
   with rapidfuzz `token_sort_ratio` (0–100). Only pool items with a
   **matching brand** are compared: a different brand is never a match,
   however alike the names look. (The pool's `BRAND` column is unreliable —
   e.g. `Homestyle Bakes` on Banquet products — so a brand also counts as
   matching when it appears in the other product's name; near-identical
   spellings like `Birds Eye` / `Birdseye` match too, see
   `dedup.brand_min_similarity`.)
3. **Score above 90 → already exists.** The Kroger UPC is written to the
   same-brand pool item with the highest score (`_kroger_upc`, or
   `_kroger_upc_aliases` if it already has one), so later pulls skip it,
   and the run moves on to the next Kroger product.
4. **Score 90 or below → new.** A new pool object is built:

   | Field | Source |
   |---|---|
   | `_kroger_upc`, `PRODUCT_NAME`, `BRAND`, `PRICE_CURRENT` / `PRICE_RETAIL` | Kroger (price at the store picked from `KROGER_ZIP` or `kroger.location_id`; `PRICE_CURRENT` is the promo price when there is one, else regular) |
   | `calories` | The **highest** of USDA, Open Food Facts and Gemini, **rounded to the nearest 10**. Winning source in `_calorie_max_source`, every source's raw number in `_calorie_sources`. |
   | `servings_per_container` | USDA FoodData Central, pulled the way `build_meal_pool.py` does (`householdServingFullText`, else `packageWeight`); `N/A` if USDA has no match |
   | `PRODUCT_URL`, `SKU` | Serper.dev search `site:walmart.com <brand> <name>` (10 results). A result only counts if it's the **same product**: the product name in the Walmart URL must contain Kroger's brand and score at least `serper.min_name_match_score` (85) against Kroger's name — Serper returns the *closest* page, which is often a different flavor or brand. The first result that passes is used (its score is saved as `_walmart_name_match_score`); if none does, the product is skipped and its UPC remembered. The URL (with `?fulfillmentIntent=Pickup`) and the numeric id from it are stored. If that SKU is already **active** in the pool, the product *is* that item and its UPC is recorded there instead. If the SKU is already in the pool on a row that's **inactive** (or was never checked), that row is refreshed in place rather than duplicated: `PRODUCT_URL` becomes the looked-up URL, `active` becomes `true`, and everything else in this table is filled in as for a new product (the row keeps its `index` and any UPC it already had; `_reactivated_at` is set). A refreshed row counts toward the run's 20. |
   | `INSTACART_URL` | **Built** from the cleaned name (`https://www.instacart.com/store/s?k=…`) — never searched for, not verified |
   | `image_url` | DuckDuckGo Images, searched with the product's Walmart URL as the query; the top result wins, whoever hosts it (retry/backoff as in `AddImageUrl.py`) |
   | `SOURCE`, `active` | `"Kroger"`; `true` (Serper confirmed a live Walmart page) |

   A new product is only added if it has a Kroger price, a verified
   same-product Walmart URL/SKU, an image, and at least one calorie number. Otherwise it isn't added; if
   the reason is lasting (no matching Walmart page, no image results, no calories
   anywhere) its UPC goes in `kroger_skipped_upcs.json` (delete an entry to
   retry it), and if it might be temporary (network error, rate limit,
   Gemini quota) nothing is recorded and a later run tries again.

Guardrails in `kroger_new_items.yaml`: `run.max_enrichment_attempts` caps
how many new products go through the quota-limited Serper/DuckDuckGo steps
per run, and `run.max_runtime_minutes` stops the run in time for the
workflow to commit what it did.

**Kroger store for prices:** Kroger only returns prices for a specific
store. The script uses the first of these that is set: the
`KROGER_LOCATION_ID` env var/repo variable, `kroger.location_id` in
`kroger_new_items.yaml`, or `KROGER_ZIP` — the ZIP code secret, which is
turned into the *nearest* Kroger-family store at the start of every run
(within `kroger.location_search_radius_miles`, default 100) and logged. To
pin a specific store instead of the nearest one, list candidates with
`python kroger_new_items.py --find-location <ZIP>` and set its id. With
none of the three set, the run is skipped.

```
pip install requests pyyaml ddgs rapidfuzz
python kroger_new_items.py --dry-run          # pull + fuzzy-match only, writes nothing
python kroger_new_items.py --max-new-items 5  # small real run
```

## Workflows

| File | What it does |
|---|---|
| `.github/workflows/check-active-urls.yml` | Runs `check_active_urls.py --only-unknown --limit 100` hourly (`17 * * * *`) to keep dead Walmart links tagged `active: false`. Runnable manually with an overridable `batch_size`. |
| `.github/workflows/gemini-meal-lookup.yml` | Runs `gemini_meal_lookup.py` weekly (`0 0 * * 2`, Tuesdays 00:00 UTC) and commits the updated `candidate_pool.json`. Runnable manually with overridable `items_per_call` and `calls_per_run` (default 10 each). |
| `.github/workflows/kroger-new-items.yml` | Runs `kroger_new_items.py` 4× a day (`45 1,7,13,19 * * *`, UTC) and commits `candidate_pool.json` and `kroger_skipped_upcs.json`. Runnable manually with an overridable `max_new_items` (default 20). |

### Disabling a scheduled workflow

Two ways, without touching code:
1. **GitHub UI** — repo → Actions tab → select the workflow → "..." menu →
   "Disable workflow". Re-enable the same way. Nothing to commit.
2. **Edit the YAML** — remove or comment out the `schedule:` block (the
   `cron:` line). The `workflow_dispatch:` trigger, if left in place,
   still lets you run it manually from the Actions tab.

## Design notes

- **Gemini has no web search.** `gemini_meal_lookup.py` and
  `kroger_new_items.py` ask Gemini from its own training knowledge only.
  The `google_search` grounding tool was removed because its separate quota
  effectively needs a linked billing account. Consequences: Gemini
  *prices* are rough, possibly stale estimates (rows carry
  `_gemini_price_is_estimate: true`); calories hold up better since
  nutrition facts change less often; and Gemini is never asked for a source
  URL (with no search it would just be a plausible-looking fabrication). If
  billing is ever set up, adding `"tools": [{"google_search": {}}]` to the
  request body in `call_gemini_batch_single_model()` in
  `gemini_meal_lookup.py` restores live lookups (that tool can't be
  combined with the forced-JSON `responseMimeType` setting, so drop that
  too).
- **Gemini calorie guesses can be too low**, which is why calories use
  "largest wins" (`Max_Calories_Count.py`, and the same rule in
  `kroger_new_items.py`): a full entrée showing 5–10 calories essentially
  never happens, so taking the max of several sources rarely overshoots.
  It's a heuristic — spot-check `calorie_max_check_diff.csv`.
- **New Kroger rows are marked `active: true` right away.** Serper already
  confirmed a live walmart.com page, and `check_active_urls.py`'s
  Playwright checks are often bot-blocked, leaving items stuck at `null`.
  `check_active_urls.py` can still flip a row to `false` later.
- **Instacart links are guesses.** `INSTACART_URL` is a search-results URL
  built from the product name; only `check_instacart_urls.py` verifies
  anything, and only if you run it.

## Required secrets / environment variables

No API keys are hardcoded anywhere in this repo. Each script reads its key
from an environment variable at runtime:

| Variable | Used by | Required for |
|---|---|---|
| `USDA_API_KEY` | `build_meal_pool.py`, `Max_Calories_Count.py`, `kroger_new_items.py` | USDA FoodData Central calorie and serving lookups. |
| `SERPER_API_KEY` | `check_walmart_links.py`, `check_instacart_urls.py`, `kroger_new_items.py` | Serper.dev Google searches — Walmart/Instacart link checks, and finding each new product's Walmart URL/SKU. |
| `GEMINI_KEY` | `gemini_meal_lookup.py`, `kroger_new_items.py` | The weekly Gemini estimate workflow, and the Gemini calorie estimate for new products. |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | `kroger_new_items.py` | Kroger's OAuth client-credentials app (register at developer.kroger.com) — product catalog search and store lookup. |
| `KROGER_ZIP` | `kroger_new_items.py` | ZIP code, turned into the nearest Kroger-family store whose prices are used. Secret or repo variable. Not needed if a store id is set below. |
| `KROGER_LOCATION_ID` *(optional)* | `kroger_new_items.py` | Pins one specific store; wins over `KROGER_ZIP` and over `kroger.location_id` in `kroger_new_items.yaml`. |

Open Food Facts needs no key. `check_active_urls.py` and `AddImageUrl.py`
don't need any key either.

For GitHub Actions, the keys need to be repo secrets (Settings → Secrets
and variables → Actions), referenced in the relevant workflow's `env:`
block. `KROGER_ZIP` can be a secret or a repo variable (the workflow reads
either); `KROGER_LOCATION_ID` is read from a repo *variable*.
