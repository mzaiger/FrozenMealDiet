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
| `kroger_new_items.py` *(new today)* | Finds frozen products NOT already in the pool by searching Kroger's live public catalog with broad terms (`bowl`, `meal`, `breakfast`, `dinner`, `pizza`, etc.), keeping only results whose Kroger category labels actually mention "frozen" (Kroger has no "date added" field, so "new" = in Kroger's catalog today, category-filtered to frozen, and not already in the pool by product name). For each candidate: resolves a real Walmart `PRODUCT_URL` + `SKU` via Serper.dev (`site:walmart.com <brand> <product name>`, checking each result for the actual `/ip/<slug>/<id>` product-page shape — same service `check_walmart_links.py` uses), reads `PRODUCT_NAME` from that same URL's slug rather than Kroger's description, finds an `image_url` via DuckDuckGo, and asks Gemini (no search, same model chain as `gemini_meal_lookup.py`) for calories/price/servings. An item is only added if it got a real Walmart link, an image, AND complete calories/price/servings — no half-filled entries. Needs `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET`, `SERPER_API_KEY`, `GEMINI_KEY`. Config lives in `kroger_new_items.yaml`. | **Yes** — `.github/workflows/kroger-new-items.yml`, daily. |

## YAML files

| File | Purpose |
|---|---|
| `.github/workflows/check-active-urls` | GitHub Actions workflow. Runs `check_active_urls.py` hourly to keep dead Walmart links tagged `active: false`. |
| `.github/workflows/gemini-meal-lookup.yml` | GitHub Actions workflow. Runs `gemini_meal_lookup.py` every 4 hours (`0 */4 * * *`), commits the updated `candidate_pool.json`. Also runnable manually from the Actions tab (`workflow_dispatch`, with an overridable `limit` input, default 10). |
| `gemini_meal_lookup.yaml` | Config for `gemini_meal_lookup.py` — not a workflow file, just settings the script reads at runtime: which Gemini models to try (in fallback order), batch size, rate limiting, and the prompt template. |
| `.github/workflows/kroger-new-items.yml` *(new today)* | GitHub Actions workflow. Runs `kroger_new_items.py` once daily (`45 7 * * *`), commits any new items added to `candidate_pool.json`. Runnable manually (`workflow_dispatch`, overridable `max_new_items` input, default 20). |
| `kroger_new_items.yaml` *(new today)* | Config for `kroger_new_items.py` — Kroger search terms, Serper/DuckDuckGo/Gemini settings, batch size, the Gemini prompt template. |

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
   UPC — never for price. Search terms are deliberately broad/generic
   (`bowl`, `meal`, `breakfast`, `dinner`, `pizza`, etc. — not `frozen
   bowl`), since Kroger's term search is a literal text match on the
   product description and an over-narrow phrase wasted most of a
   50-result page on near-duplicate matches. What actually keeps results
   scoped to frozen items is a check against Kroger's own category
   labels — a candidate is dropped unless at least one of its Kroger
   categories mentions "frozen" — not the search term itself, so
   broadening the terms doesn't let non-frozen products slip in. Pulls
   up to 3 pages (150 results) per term now instead of 1 (50), since the
   broader terms return far more than a single page's worth.
2. Each candidate's real Walmart page is found via Serper.dev
   (`site:walmart.com <brand> <product name>`), the exact same service
   and request shape `check_walmart_links.py` already uses — reuses
   `SERPER_API_KEY` rather than introducing a second search-API key.
   Every organic result is checked (not just the top one) for the actual
   `/ip/<slug>/<numeric-id>` product-page shape, and `SKU` is parsed
   straight out of the first one that matches. `PRODUCT_NAME` is also
   read from that same URL's slug (hyphens/underscores → spaces, and any
   `%XX` URL-encoding decoded, e.g. `%27` → `'`) rather than from
   Kroger's description — Kroger sometimes repeats the brand name twice
   in its description, so Walmart's own title for the exact linked page
   is the more accurate name to display. Kroger's description is kept
   only as a fallback for the rare case a link is found but the slug
   somehow can't be parsed. (Two earlier versions of this: it first used
   SerpApi — a different, unrelated service — but that key kept 401ing,
   so it was swapped for Serper; searching by UPC instead of product name
   was also tried first, but Walmart's product pages don't reliably
   surface the raw UPC as indexable text, so that returned
   `no_search_results` almost every time — product name works the way
   searching for it by hand does.)
3. `image_url` comes from DuckDuckGo Images, same technique/rate-limit
   handling as `AddImageUrl.py`.
4. Calories/price/servings come from Gemini, no search — same model
   chain and "price is a rough estimate" caveat as `gemini_meal_lookup.py`.
   The prompt asks about each product by its Walmart-slug-derived name
   (the same one that goes in `PRODUCT_NAME`), not Kroger's raw
   description, so Gemini is asked about the exact same title the linked
   page and the pool record will show. Gemini is also allowed one
   fallback: if a real Walmart link was found but no SKU could be parsed
   out of its URL, Gemini can supply a plausible-looking `sku_guess`
   instead of leaving it blank — that guess is written to `SKU` but
   flagged `_sku_is_estimate: true` so it's never confused with a
   verified one. Gemini is never allowed to guess a SKU when there's no
   real link at all.
5. **An item is only appended if the Serper link lookup, the DDG image
   lookup, AND a complete Gemini result (recognized, with calories,
   price, AND servings_per_container all present) all succeeded.**
   Missing any one → skipped, not added half-filled.
6. Every row this script adds gets a plain `"SOURCE": "Kroger"` column,
   so it's easy to tell apart from the original 2022 Walmart-CSV rows
   (which have no `SOURCE` field at all).
7. New records get `"active": true` directly — since a record is only
   ever assembled after Serper already confirmed a real walmart.com
   page for that UPC (step 2 above), and `check_active_urls.py`'s
   Playwright-based checks are unreliable here (Walmart bot-blocks it
   often enough that items were sitting stuck at `null`), so that
   confirmation is trusted rather than waiting on a check that mostly
   can't get through. This script's own check is still stored separately
   under `_serper_*` fields, never `_active_check_*` — and
   `check_active_urls.py` can still flip a row to `false` later if
   Walmart genuinely delists it and a check happens to succeed.
8. Capped at 20 new items/run (`run.max_new_items` in
   `kroger_new_items.yaml`) since each one costs a Serper call and a DDG
   image search, both of which are rate/quota-limited.

## Required secrets / environment variables

No API keys are hardcoded anywhere in this repo. Each script reads its
key from an environment variable at runtime:

| Variable | Used by | Required for |
|---|---|---|
| `USDA_API_KEY` | `build_meal_pool.py` | Calorie/serving lookups when (re)building the pool from CSV. |
| `SERPER_API_KEY` | `check_walmart_links.py`, `kroger_new_items.py` | `check_walmart_links.py` only if you actually run it (not wired into a workflow); `kroger_new_items.py` needs it for the daily new-item-discovery workflow, to resolve each newly discovered product's real Walmart `PRODUCT_URL`/`SKU` via `site:walmart.com <brand> <product name>`. Same key, same serper.dev service, used by both scripts now. |
| `GEMINI_KEY` | `gemini_meal_lookup.py`, `kroger_new_items.py` | The 4-hourly calorie/price estimate workflow, and the daily new-item discovery workflow. |
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | `kroger_new_items.py` | Kroger's OAuth client-credentials app (register at developer.kroger.com) — used only to search Kroger's live product catalog for discovery, never for price/location. |

For GitHub Actions, these need to be repo secrets (Settings → Secrets
and variables → Actions), referenced in the relevant workflow's `env:`
block. `check_active_urls.py` and `AddImageUrl.py` don't need any key.
