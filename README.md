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
| `.github/workflows/` | The four GitHub Actions workflows — see [Workflows](#workflows). |
| `DataCleaning/` | Every script, config file and source-data file used to build and maintain the pool — see below. |

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
| `update_instacart_urls.py` | Sets/refreshes `INSTACART_URL` on **every** row in `candidate_pool.json` from each row's own `PRODUCT_NAME` (text cut at the first comma, apostrophes kept as `%27`, everything else non-alphanumeric turned into `+`). Pure local transform — no network, no API key. `--only-missing` fills blanks only; `--dry-run` previews. (`kroger_new_items.py` builds its Instacart URLs with a slightly stricter cleanup that also drops sizes and numbers, so URLs on new Kroger rows can differ in form from ones this script writes.) | No — run manually. |
| `Max_Calories_Count.py` | Cross-checks each item's `calories` against USDA and Open Food Facts and keeps the **largest** of {current, USDA, Open Food Facts} — only ever raises a value, never lowers it. Adds `_calorie_max_checked_at` / `_calorie_max_source` to each item (that's what makes it resumable) and writes `calorie_max_check_diff.csv` listing every change, for spot-checking. `--limit`, `--recheck-all`, `--dry-run`. Needs `USDA_API_KEY`; Open Food Facts needs none. | No — run manually. |
| `gemini_meal_lookup.py` | Asks Gemini for an estimated calories-per-serving and price for a rotating batch of pool items (oldest/never-checked first), from Gemini's own knowledge — no live web search. Writes back only what differs, plus `_gemini_*` bookkeeping fields. Needs `GEMINI_KEY`. | **Yes** — `gemini-meal-lookup.yml`, weekly. |
| `gemini_meal_lookup.yaml` | Config for `gemini_meal_lookup.py` (not a workflow): which Gemini models to try in fallback order, batch shape (`items_per_call`, `calls_per_run`, `skip_inactive`), rate limiting, and the prompt template. | — (config) |
| `kroger_new_items.py` | Finds new frozen products in Kroger's catalog and adds up to 20 per run. Skips anything already in the pool (fuzzy name match, same brand); a new product gets its Kroger price, a Walmart URL/SKU (Serper.dev), calories (highest of USDA / Open Food Facts / Gemini), serving, image and Instacart link. Products it can't add are remembered in `kroger_skipped_upcs.json`. `--dry-run`, `--max-new-items`. Needs the Kroger, Serper, Gemini and USDA keys. | **Yes** — `kroger-new-items.yml`, 4× a day. |
| `kroger_new_items.yaml` | Config for `kroger_new_items.py` (also read by `kroger_add_skipped_items.py`): fuzzy-match threshold, Kroger search terms and store, per-run caps, and the pool / skipped-UPC file paths. | — (config) |
| `kroger_add_skipped_items.py` | Finishes the products in `kroger_skipped_upcs.json` that you gave a `walmart_url` by hand — same pipeline as `kroger_new_items.py` with your URL in place of the Serper Walmart lookup. Tags the UPC onto an existing row if that SKU is already in the pool. `--allow-missing-image`, `--dry-run`, `--limit`, `--max-minutes`. | No — manual run of `kroger-add-skipped-items.yml`. |
| `kroger_skipped_upcs.json` | Kroger UPCs `kroger_new_items.py` couldn't add (no matching Walmart page, no image, no calories), so they aren't retried. Add a `walmart_url` to an entry for `kroger_add_skipped_items.py` to pick up; it writes `status` / `detail` back onto the entries it handles. | — (data) |
| `skus_to_update.txt` | A plain list of Walmart SKUs, one per line. Not read by any script here. | — (data) |

## Workflows

| File | What it does |
|---|---|
| `.github/workflows/check-active-urls.yml` | Runs `check_active_urls.py --only-unknown --limit 100` hourly (`17 * * * *`) to keep dead Walmart links tagged `active: false`. Runnable manually with an overridable `batch_size`. |
| `.github/workflows/gemini-meal-lookup.yml` | Runs `gemini_meal_lookup.py` weekly (`0 0 * * 2`, Tuesdays 00:00 UTC) and commits the updated `candidate_pool.json`. Runnable manually with overridable `items_per_call` and `calls_per_run` (default 10 each). |
| `.github/workflows/kroger-new-items.yml` | Runs `kroger_new_items.py` 4× a day (`45 1,7,13,19 * * *`, UTC) and commits `candidate_pool.json` and `kroger_skipped_upcs.json`. Runnable manually with an overridable `max_new_items` (default 20). |
| `.github/workflows/kroger-add-skipped-items.yml` | Manual only (Actions tab → "Run workflow"): runs `kroger_add_skipped_items.py --allow-missing-image --max-minutes 100` — so products DuckDuckGo finds no image for are still added, with a blank `image_url` — and commits `candidate_pool.json` and `kroger_skipped_upcs.json`. Optional inputs: `limit` (max entries, 0 = all) and `dry_run`. Shares a concurrency group with `kroger-new-items.yml` so the two never edit the pool at the same time. |

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
| `USDA_API_KEY` | `build_meal_pool.py`, `Max_Calories_Count.py`, `kroger_new_items.py`, `kroger_add_skipped_items.py` | USDA FoodData Central calorie and serving lookups. |
| `SERPER_API_KEY` | `check_walmart_links.py`, `check_instacart_urls.py`, `kroger_new_items.py` | Serper.dev Google searches — Walmart/Instacart link checks, and finding each new product's Walmart URL/SKU. |
| `GEMINI_KEY` | `gemini_meal_lookup.py`, `kroger_new_items.py`, `kroger_add_skipped_items.py` | The weekly Gemini estimate workflow, and the Gemini calorie estimate for new products. |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | `kroger_new_items.py`, `kroger_add_skipped_items.py` | Kroger's OAuth client-credentials app (register at developer.kroger.com) — product catalog search and store lookup. |
| `KROGER_ZIP` | `kroger_new_items.py`, `kroger_add_skipped_items.py` | ZIP code, turned into the nearest Kroger-family store whose prices are used. Secret or repo variable. Not needed if a store id is set below. |
| `KROGER_LOCATION_ID` *(optional)* | `kroger_new_items.py`, `kroger_add_skipped_items.py` | Pins one specific store; wins over `KROGER_ZIP` and over `kroger.location_id` in `kroger_new_items.yaml`. |

Open Food Facts needs no key. `check_active_urls.py` and `AddImageUrl.py`
don't need any key either.

For GitHub Actions, the keys need to be repo secrets (Settings → Secrets
and variables → Actions), referenced in the relevant workflow's `env:`
block. `KROGER_ZIP` can be a secret or a repo variable (the workflow reads
either); `KROGER_LOCATION_ID` is read from a repo *variable*.
