"""
Looks up an estimated calories-per-serving and price for a rotating batch
of candidate_pool.json items by asking Gemini from its own training
knowledge -- no Google Search grounding -- and writes the results back
into candidate_pool.json.

NOTE: without search grounding, Gemini cannot actually browse
walmart.com. It answers from what it saw during training, which means:
  - Prices will often be stale or approximate, not today's real price.
  - Newer or less common products may come back "not found" even if
    they're really on the site.
  - Calories-per-serving tends to be more reliable than price, since
    nutrition facts change far less often than pricing.
Grounding was dropped specifically to avoid the separate "Grounding with
Google Search" quota (which returns 429s independently of, and often
well below, the plain per-model RPM/RPD limits on the free tier -- see
2026-09 conversation). If/when billing is set up and that quota stops
being the bottleneck, re-adding `"tools": [{"google_search": {}}]` to
the request body in call_gemini_single() below restores live lookups.

Modeled on SportsDashboard's scripts/gemini_predictions.py: same model
fallback chain / rate limiter / retry shape, adapted here for a single
sequential per-item lookup instead of per-game predictions. Unlike that
script, this one CAN use forced JSON response mode (responseMimeType) --
that only conflicts with the google_search tool, which isn't in use here.

Config: gemini_meal_lookup.yaml (models, batch size, rate limiting, the
per-item prompt template).

Selection: each run picks the `items_per_run` items with the oldest (or
missing) "_gemini_checked_at" timestamp, so repeated runs rotate through
the whole pool over time rather than hammering the same items. Items with
active === false are skipped by default (see batch.skip_inactive) --
already-confirmed-dead Walmart listings aren't worth a Gemini call.

Env var required: GEMINI_KEY. If missing, the run is skipped entirely
(exit 0), same as gemini_predictions.py.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "gemini_meal_lookup.yaml")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


class DailyQuotaExceeded(RuntimeError):
    """Every model in the fallback chain is rate-limited, over quota, or
    unavailable right now; retrying today is pointless."""


class _ModelUnavailable(Exception):
    """Raised internally when a single model in the fallback chain returns
    a 4xx -- rate limited, quota exhausted, model retired/renamed, etc.
    Signals the caller to move on to the next model immediately rather
    than burning retries on a model that just told us no."""


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


#---------------------------------------------------------------------------
# Config
#---------------------------------------------------------------------------

def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


#---------------------------------------------------------------------------
# Pool I/O
#---------------------------------------------------------------------------

def load_pool(path):
    with open(path) as f:
        return json.load(f)


def save_pool(path, pool):
    with open(path, "w") as f:
        json.dump(pool, f, indent=2)
        f.write("\n")


def select_batch(pool, items_per_run, skip_inactive):
    """Returns up to `items_per_run` entries, oldest-checked (or never
    checked) first, so runs rotate through the whole pool over time."""
    eligible = [
        item for item in pool
        if not (skip_inactive and item.get("active") is False)
    ]
    eligible.sort(key=lambda item: item.get("_gemini_checked_at", ""))
    return eligible[:items_per_run]


#---------------------------------------------------------------------------
# Prompt + response parsing
#---------------------------------------------------------------------------

def build_prompt(template, item):
    return template.format(
        product_name=item.get("PRODUCT_NAME", ""),
        brand=item.get("BRAND", ""),
        sku=item.get("SKU", ""),
        product_url=item.get("PRODUCT_URL", ""),
    )


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text):
    """Leniently pulls a JSON object out of a Gemini response that may be
    wrapped in ```json fences or have stray text around it."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object found in response: {text[:200]!r}")

    return json.loads(match.group(0))


def normalize_result(raw):
    if not isinstance(raw, dict):
        raise ValueError("Gemini response was not a JSON object")

    recognized = bool(raw.get("recognized"))

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

    notes = str(raw.get("notes", "")).strip()

    return {
        "recognized": recognized,
        "calories": calories,
        "price": price,
        "notes": notes,
    }


#---------------------------------------------------------------------------
# Gemini API calls
#---------------------------------------------------------------------------

def call_gemini_single(prompt, gemini_key, model, cfg, rate_limiter):
    """Try exactly one model. Raises _ModelUnavailable immediately (no
    retry) on any 4xx response, so the caller can fall back to the next
    model without burning this model's retry budget on a request that's
    never going to succeed. Network errors / 5xx / unusable responses
    retry the SAME model up to max_retries times."""
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
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            log(f"  {model}: request error -- retrying in {retry_delay:.0f}s "
                f"(attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(retry_delay)
            continue

        if 400 <= resp.status_code < 500:
            # Rate limited, quota exhausted, or a model name that doesn't
            # exist / was retired -- none of these get better by retrying
            # THIS model, so bail out immediately and let the caller move
            # on to the next one in the chain.
            raise _ModelUnavailable(f"{resp.status_code} {resp.reason}: {resp.text[:200]}")

        if resp.status_code >= 500:
            last_err = requests.exceptions.HTTPError(
                f"{resp.status_code} {resp.reason} for url: {resp.url}", response=resp,
            )
            if attempt == max_retries - 1:
                break
            log(f"  {model}: {resp.status_code} -- retrying in {retry_delay:.0f}s "
                f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_delay)
            continue

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise requests.exceptions.HTTPError(
                f"{resp.status_code} {resp.reason} for url: {resp.url}: {resp.text[:300]}",
            ) from e

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = extract_json(text)
            return normalize_result(parsed)
        except Exception as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            log(f"  {model}: returned an unusable response -- retrying in "
                f"{retry_delay:.0f}s (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(retry_delay)
            continue

    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Gemini call to {model} failed for unknown reason")


def call_gemini(prompt, gemini_key, cfg, rate_limiter):
    """Tries each model in cfg's fallback chain in order. Returns
    (result_dict, model_used). Only once EVERY model has been tried and
    rejected does this raise DailyQuotaExceeded."""
    last_unavailable = None
    for model in cfg["gemini"]["models"]:
        try:
            result = call_gemini_single(prompt, gemini_key, model, cfg, rate_limiter)
            return result, model
        except _ModelUnavailable as e:
            last_unavailable = e
            log(f"  {model} unavailable ({e}) -- falling back to next model in the chain.")
            continue

    raise DailyQuotaExceeded(
        f"every model in the fallback chain ({', '.join(cfg['gemini']['models'])}) is "
        f"rate-limited, over quota, or unavailable right now -- remaining items will be "
        f"picked up on a future run (last error: {last_unavailable})"
    )


#---------------------------------------------------------------------------
# Main
#---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                         help="Path to gemini_meal_lookup.yaml")
    parser.add_argument("--limit", type=int, default=None,
                         help="Override batch.items_per_run for this run")
    args = parser.parse_args()

    gemini_key = os.environ.get("GEMINI_KEY")
    if not gemini_key:
        log("No GEMINI_KEY set -- skipping Gemini meal lookup.")
        return

    cfg = load_config(args.config)
    items_per_run = args.limit or cfg["batch"]["items_per_run"]
    skip_inactive = cfg["batch"]["skip_inactive"]
    save_every = cfg["batch"]["save_every"]
    input_path = os.path.join(_SCRIPT_DIR, cfg["pool"]["input_path"])
    output_path = os.path.join(_SCRIPT_DIR, cfg["pool"]["output_path"])
    prompt_template = cfg["prompt"]["template"]

    pool = load_pool(input_path)
    batch = select_batch(pool, items_per_run, skip_inactive)

    if not batch:
        log("Gemini meal lookup: no eligible items to check.")
        return

    log(f"Gemini meal lookup: checking {len(batch)} item(s), starting with "
        f"{cfg['gemini']['models'][0]} and falling back through "
        f"{', '.join(cfg['gemini']['models'][1:])} if rate-limited "
        f"(~{60.0 / cfg['gemini']['min_call_interval_seconds']:.0f}/min per model)...")

    rate_limiter = _RateLimiter(cfg["gemini"]["min_call_interval_seconds"])
    quota_exhausted = False
    checked, failed, skipped, recognized_count = 0, 0, 0, 0

    for i, item in enumerate(batch, start=1):
        label = item.get("PRODUCT_NAME", item.get("SKU", "unknown item"))

        if quota_exhausted:
            skipped += 1
            log(f"  Skipping {label} -- fallback chain exhausted; will check on a future run.")
            continue

        prompt = build_prompt(prompt_template, item)

        try:
            result, model_used = call_gemini(prompt, gemini_key, cfg, rate_limiter)
        except DailyQuotaExceeded as e:
            quota_exhausted = True
            skipped += 1
            log(f"  Gemini fallback chain exhausted at {label}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 -- one bad item shouldn't kill the run
            failed += 1
            log(f"  Gemini call failed for {label}: {e}")
            continue

        checked_at = datetime.now(timezone.utc).isoformat()
        item["_gemini_checked_at"] = checked_at
        item["_gemini_model"] = model_used
        item["_gemini_recognized"] = result["recognized"]
        item["_gemini_notes"] = result["notes"]
        # No source_url here -- without search grounding Gemini can't verify
        # a real walmart.com link, so we don't ask it for one (see module
        # docstring). Price is likewise an estimate, not a live price --
        # flagged explicitly so downstream consumers (e.g. index.html) can
        # treat it differently from a confirmed-active-URL price.
        item["_gemini_price_is_estimate"] = True

        if result["recognized"] and result["calories"] is not None:
            item["calories"] = result["calories"]
        if result["recognized"] and result["price"] is not None:
            item["PRICE_CURRENT"] = f"{result['price']:.2f}"

        if result["recognized"]:
            recognized_count += 1

        checked += 1
        log(f"  [{i}/{len(batch)}] {label}: recognized={result['recognized']} "
            f"calories={result['calories']} price={result['price']} ({model_used})")

        if checked % save_every == 0:
            save_pool(output_path, pool)

    log(f"Gemini meal lookup: {checked} checked ({recognized_count} recognized), "
        f"{failed} failed, {skipped} skipped (fallback chain exhausted).")

    if checked:
        save_pool(output_path, pool)


if __name__ == "__main__":
    main()
