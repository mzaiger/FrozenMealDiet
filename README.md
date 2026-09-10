# Freezer Week — 7-Day Frozen Meal Planner

Pulls frozen breakfast items and frozen meals from your local Kroger,
looks up real calorie counts from USDA FoodData Central, and generates a
7-day / 21-meal plan where each day lands within ±250 calories of a target
you set (default 1500/day).

Same shape as your other trackers: a Python script exports data to JSON,
a GitHub Action runs it on a schedule, and a static page reads the JSON.
The one difference here is that assigning meals to days happens **in the
browser**, not in Python — so changing the calorie target and hitting
"Generate week" is instant and doesn't need your API keys to be present
client-side.

## How it works

1. `build_meal_pool.py` (runs weekly via GitHub Actions):
   - Authenticates with Kroger using a client-credentials OAuth flow
   - Finds your nearest store by zip code
   - Searches several frozen-breakfast terms and several general
     frozen-meal terms, de-duping by UPC
   - Looks up each item's calories from USDA FoodData Central by UPC
   - Writes `candidate_pool.json` with two lists: `breakfast` and `general`
2. `index.html`:
   - Fetches `candidate_pool.json`
   - You type a calorie target (default 1500)
   - JS picks a breakfast item, then searches the general pool for a
     lunch+dinner pair whose combined calories keep the whole day within
     ±250 of your target, for all 7 days
   - Hit "Generate week" any time for a new random plan against the same
     pool

## One-time setup

### 1. Kroger API credentials (free)
1. Create an account at https://developer.kroger.com and register a new app.
2. You'll get a **Client ID** and **Client Secret** — no manual approval
   needed for the `product.compact` scope used here.

### 2. USDA FoodData Central API key (free, instant)
1. Sign up at https://api.data.gov/signup/ — the key arrives immediately
   by email, no approval wait.

### 3. GitHub repo secrets
In your repo's Settings → Secrets and variables → Actions, add:
- `KROGER_CLIENT_ID`
- `KROGER_CLIENT_SECRET`
- `USDA_API_KEY`
- `KROGER_ZIP` (optional — defaults to `68508` / Lincoln, NE if unset)

### 4. Enable GitHub Pages
Settings → Pages → serve from the branch this repo lives on, root folder.
(`index.html` needs to be fetched over http/https for `fetch()` to work —
opening it directly as a local file will fail on the `candidate_pool.json`
load due to browser CORS rules.)

### 5. First run
The workflow runs every Monday at 09:00 UTC automatically, or trigger it
manually any time from the Actions tab ("Run workflow" on "Update meal
pool") to generate the first `candidate_pool.json`.

## Notes / things worth knowing

- **Kroger's product search is a keyword search, not a strict category
  filter.** The search terms in `build_meal_pool.py` (`BREAKFAST_TERMS`,
  `GENERAL_TERMS`) are tuned to pull relevant frozen items, but you'll
  likely want to skim `candidate_pool.json` after the first run and adjust
  the term lists if anything odd sneaks in (e.g. a frozen breakfast search
  returning a non-breakfast item that just has "breakfast" in a bundle
  name).
- **Items without a calorie match on USDA FoodData Central are dropped**
  from the pool rather than included with a guessed value — so a thin pool
  usually means USDA's branded-food database didn't have that UPC, not
  that the script is broken.
- **If a day can't hit the ±250 window** (pool too small/homogeneous that
  week), the front end falls back to the closest combination it can find
  and flags that day so you know it's outside the target rather than
  silently showing a number that looks fine but isn't.
- This hasn't been run against live Kroger/USDA traffic in the environment
  that built it (sandboxed, no network access to those domains) — the
  request shapes match both APIs' published contracts, but budget a first
  debugging pass once real secrets are in place.
