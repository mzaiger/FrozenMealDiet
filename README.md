# Poor Man's Walmart Frozen Meal Diet

A 7-day frozen meal planner: `candidate_pool.json` holds ~1,800 Walmart
frozen-food items (name, price, calories, image, link-liveness status),
and `index.html` builds a randomized week of meals from that pool, kept
within a calorie target and a per-meal price cap, all client-side.

Nothing here talks to Kroger — that's an older description that no
longer matches the code. The pool is sourced from a Walmart product CSV
export and enriched with USDA calorie data.

## Python scripts

| Script | What it does | Automated? |
|---|---|---|
| `Dedup.py` | Reads `frozen_food.csv`, drops a few unwanted categories (desserts, meat & seafood, produce, potatoes), de-dupes by SKU, writes `frozen_food_deduped.csv`. | No — run manually when you refresh the source CSV. |
| `build_meal_pool.py` | Reads `frozen_food_deduped.csv`, cleans each product name, looks up calories + servings-per-container from the USDA FoodData Central API, writes `candidate_pool.json`. Needs `USDA_API_KEY`. | No — run manually to (re)build the pool from scratch. |
| `AddImageUrl.py` | For every `active` item in `candidate_pool.json`, searches DuckDuckGo Images and writes the result to `image_url`. Rate-limit-conscious (jittered delays, backoff, per-name caching, checkpointing so it's safe to re-run). No API key needed. | No — run manually. |
| `check_active_urls.py` | Uses Playwright (with stealth) to visit each item's `PRODUCT_URL` on walmart.com and tag it `active: true/false/null` (null = couldn't tell, e.g. bot-blocked). Writes reason/query/checked-URL metadata per item. | **Yes** — `.github/workflows/check-active-urls`, hourly, `--only-unknown --limit 100`. |
| `check_walmart_links.py` | Alternate way to check link liveness: searches Google via Serper.dev for `site:walmart.com <product_id>` and checks whether walmart.com is the top result. Needs `SERPER_API_KEY`. | **No** — not wired into any workflow. Not currently in use; `check_active_urls.py` is the one actually running. |
| `gemini_meal_lookup.py` | Asks Gemini for an estimated calories-per-serving and price for a rotating batch of pool items, from Gemini's own knowledge (no live web search — see "Sept 13" session below for why). Writes results back into `candidate_pool.json`. Needs `GEMINI_KEY`. Config lives in `gemini_meal_lookup.yaml`. | **Yes** — `.github/workflows/gemini-meal-lookup.yml`, every 4 hours. |
| `kroger_new_items.py` *(new today)* | Finds frozen products NOT already in the pool by searching Kroger's live public catalog (Kroger has no "date added" field, so "new" = in Kroger's catalog today and not already in the pool by product name). For each candidate: resolves a real Walmart `PRODUCT_URL` + `SKU` via SerpApi (`site:walmart.com <upc>`), finds an `image_url` via DuckDuckGo, and asks Gemini (no search, same model chain as `gemini_meal_lookup.py`) for calories/price/servings. An item is only added if it got **both** a real Walmart link and an image — no half-filled entries. Needs `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET`, `SERPAPI_KEY`, `GEMINI_KEY`. Config lives in `kroger_new_items.yaml`. | **Yes** — `.github/workflows/kroger-new-items.yml`, daily. |

## YAML files

| File | Purpose |
|---|---|
| `.github/workflows/check-active-urls` | GitHub Actions workflow. Runs `check_active_urls.py` hourly to keep dead Walmart links tagged `active: false`. |
| `.github/workflows/gemini-meal-lookup.yml` | GitHub Actions workflow. Runs `gemini_meal_lookup.py` every 4 hours (`0 */4 * * *`), commits the updated `candidate_pool.json`. Also runnable manually from the Actions tab (`workflow_dispatch`, with an overridable `limit` input, default 10). |
| `gemini_meal_lookup.yaml` | Config for `gemini_meal_lookup.py` — not a workflow file, just settings the script reads at runtime: which Gemini models to try (in fallback order), batch size, rate limiting, and the prompt template. |
| `.github/workflows/kroger-new-items.yml` *(new today)* | GitHub Actions workflow. Runs `kroger_new_items.py` once daily (`45 7 * * *`), commits any new items added to `candidate_pool.json`. Runnable manually (`workflow_dispatch`, overridable `max_new_items` input, default 20). |
| `kroger_new_items.yaml` *(new today)* | Config for `kroger_new_items.py` — Kroger search terms, SerpApi/DuckDuckGo/Gemini settings, batch size, the Gemini prompt template. |

### Disabling a scheduled workflow

Two ways, without touching code:
1. **GitHub UI** — repo → Actions tab → select the workflow → "..." menu →
   "Disable workflow". Re-enable the same way. Nothing to commit.
2. **Edit the YAML** — remove or comment out the `schedule:` block (the
   `cron:` line). The `workflow_dispatch:` trigger, if left in place,
   still lets you run it manually from the Actions tab.

## Today's session (Sept 13, 2026)

Built out the Gemini-based calorie/price lookup as a new leg of the pool,
alongside the existing Playwright-based `check_active_urls.py`:

1. Created `gemini_meal_lookup.py`, `gemini_meal_lookup.yaml`, and
   `.github/workflows/gemini-meal-lookup.yml` from scratch, modeled on
   SportsDashboard's `scripts/gemini_predictions.py` (same model
   fallback chain / rate limiter / retry shape).
2. Batch size: started at 20 items/run, changed to **10**.
3. Model chain went through a few rounds:
   - Started with the non-lite `gemini-3.5-flash`, then bumped to the
     newer `gemini-3.6-flash` → `3.7-flash` → `3.8-flash` as those
     released.
   - Switched to match SportsDashboard's actual convention: the
     `-flash-lite` line, not full Flash.
   - A real run then showed `gemini-2.5-flash-lite` and
     `gemini-2.0-flash-lite` both returning **404** ("no longer
     available to new users") — genuinely retired, not rate-limited —
     so both were dropped from the chain. It's now just
     `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`, the only two
     that are actually live.
4. That same run also hit **429s on the live models** despite the
   per-model rate-limit dashboard showing plenty of headroom (6/15 RPM)
   — a strong sign it was the separate "Grounding with Google Search"
   quota, not the base model quota, and that quota effectively needs a
   linked billing account to work past a small free allowance.
5. Rather than chase billing setup, **removed the `google_search`
   grounding tool entirely**. Consequences, all reflected in the
   current prompt/code:
   - Gemini now answers from training knowledge, not a live page visit.
     **Price should be treated as a rough/stale estimate, not today's
     real price.** Calories tends to hold up better since nutrition
     facts change less often than pricing.
   - Since nothing is verified against a real page anymore, the script
     no longer asks for (or stores) a `source_url` — with no search,
     that would just be a plausible-looking fabrication.
   - Response schema changed: `found` → `recognized` (does Gemini
     actually know this specific product, or would it be guessing).
   - Forced JSON response mode (`responseMimeType: "application/json"`)
     is now enabled — that only conflicts with the `google_search`
     tool, which is no longer in use, so it's safe now and makes
     parsing more reliable.
   - Items written to the pool now get `_gemini_checked_at`,
     `_gemini_model`, `_gemini_recognized`, `_gemini_notes`, and
     `_gemini_price_is_estimate: true` (the last one exists so
     `index.html` or any other consumer can tell a Gemini-estimated
     price apart from a Walmart-confirmed one).

If billing ever gets set up and the grounding quota stops being the
blocker, re-adding `"tools": [{"google_search": {}}]` to the request
body in `call_gemini_single()` (in `gemini_meal_lookup.py`) restores
live lookups — that's called out in the script's module docstring too.

## Today's session (Sept 15, 2026)

Added `kroger_new_items.py` — a new leg of the pool focused on *growing*
it with products the 2022 Walmart CSV export never had, rather than just
refreshing/verifying what's already in it:

1. Kroger has no "date added" field, so "newer than 2022" is approximated
   as: returned by a live Kroger Product API search today AND not already
   in the pool (matched by normalized product name / Kroger UPC). Kroger
   is used purely for discovery — brand, description, categories, size,
   UPC — never for price.
2. Each candidate's real Walmart page is found via SerpApi
   (`site:walmart.com <upc>`), the same "does walmart.com top the search
   for this ID" technique `check_walmart_links.py` already uses, just on
   SerpApi instead of Serper.dev. `SKU` is parsed straight out of that
   real URL.
3. `image_url` comes from DuckDuckGo Images, same technique/rate-limit
   handling as `AddImageUrl.py`.
4. Calories/price/servings come from Gemini, no search — same model
   chain and "price is a rough estimate" caveat as `gemini_meal_lookup.py`.
   Gemini is also allowed one fallback: if a real Walmart link was found
   but no SKU could be parsed out of its URL, Gemini can supply a
   plausible-looking `sku_guess` instead of leaving it blank — that guess
   is written to `SKU` but flagged `_sku_is_estimate: true` so it's never
   confused with a verified one. Gemini is never allowed to guess a SKU
   when there's no real link at all.
5. **An item is only appended if the SerpApi link lookup, the DDG image
   lookup, AND a complete Gemini result (recognized, with calories,
   price, AND servings_per_container all present) all succeeded.**
   Missing any one → skipped, not added half-filled.
6. Every row this script adds gets a plain `"SOURCE": "Kroger"` column,
   so it's easy to tell apart from the original 2022 Walmart-CSV rows
   (which have no `SOURCE` field at all).
7. New records get `"active": true` directly — since a record is only
   ever assembled after SerpApi already confirmed a real walmart.com
   page for that UPC (step 2 above), and `check_active_urls.py`'s
   Playwright-based checks are unreliable here (Walmart bot-blocks it
   often enough that items were sitting stuck at `null`), so that
   confirmation is trusted rather than waiting on a check that mostly
   can't get through. This script's own check is still stored separately
   under `_serpapi_*` fields, never `_active_check_*` — and
   `check_active_urls.py` can still flip a row to `false` later if
   Walmart genuinely delists it and a check happens to succeed.
8. Capped at 20 new items/run (`run.max_new_items` in
   `kroger_new_items.yaml`) since each one costs a SerpApi call and a DDG
   image search, both of which are rate/quota-limited.

## Required secrets / environment variables

No API keys are hardcoded anywhere in this repo. Each script reads its
key from an environment variable at runtime:

| Variable | Used by | Required for |
|---|---|---|
| `USDA_API_KEY` | `build_meal_pool.py` | Calorie/serving lookups when (re)building the pool from CSV. |
| `SERPER_API_KEY` | `check_walmart_links.py` | Only if you actually run this script — it's not wired into a workflow. |
| `GEMINI_KEY` | `gemini_meal_lookup.py`, `kroger_new_items.py` | The 4-hourly calorie/price estimate workflow, and the daily new-item discovery workflow. |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | `kroger_new_items.py` | Kroger's OAuth client-credentials app (register at developer.kroger.com) — used only to search Kroger's live product catalog for discovery, never for price/location. |
| `SERPAPI_KEY` | `kroger_new_items.py` | Resolving each newly discovered product's real Walmart `PRODUCT_URL`/`SKU` via `site:walmart.com <upc>` (serpapi.com — a different service from `SERPER_API_KEY` above). |

For GitHub Actions, these need to be repo secrets (Settings → Secrets
and variables → Actions), referenced in the relevant workflow's `env:`
block. `check_active_urls.py` and `AddImageUrl.py` don't need any key.
