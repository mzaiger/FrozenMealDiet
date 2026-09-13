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
| `gemini_meal_lookup.py` *(new today)* | Asks Gemini for an estimated calories-per-serving and price for a rotating batch of pool items, from Gemini's own knowledge (no live web search — see "Today's session" below for why). Writes results back into `candidate_pool.json`. Needs `GEMINI_KEY`. Config lives in `gemini_meal_lookup.yaml`. | **Yes** — `.github/workflows/gemini-meal-lookup.yml`, every 4 hours. |

## YAML files

| File | Purpose |
|---|---|
| `.github/workflows/check-active-urls` | GitHub Actions workflow. Runs `check_active_urls.py` hourly to keep dead Walmart links tagged `active: false`. |
| `.github/workflows/gemini-meal-lookup.yml` *(new today)* | GitHub Actions workflow. Runs `gemini_meal_lookup.py` every 4 hours (`0 */4 * * *`), commits the updated `candidate_pool.json`. Also runnable manually from the Actions tab (`workflow_dispatch`, with an overridable `limit` input, default 10). |
| `gemini_meal_lookup.yaml` *(new today)* | Config for `gemini_meal_lookup.py` — not a workflow file, just settings the script reads at runtime: which Gemini models to try (in fallback order), batch size, rate limiting, and the prompt template. |

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

## Required secrets / environment variables

No API keys are hardcoded anywhere in this repo. Each script reads its
key from an environment variable at runtime:

| Variable | Used by | Required for |
|---|---|---|
| `USDA_API_KEY` | `build_meal_pool.py` | Calorie/serving lookups when (re)building the pool from CSV. |
| `SERPER_API_KEY` | `check_walmart_links.py` | Only if you actually run this script — it's not wired into a workflow. |
| `GEMINI_KEY` | `gemini_meal_lookup.py` | The new 4-hourly calorie/price estimate workflow. |

For GitHub Actions, these need to be repo secrets (Settings → Secrets
and variables → Actions), referenced in the relevant workflow's `env:`
block. `check_active_urls.py` and `AddImageUrl.py` don't need any key.
