"""
Estimates calories-per-serving and price for a rotating batch of
candidate_pool.json items by asking Gemini from its own training
knowledge -- no Google Search, no live walmart.com visit -- and writes
back only the fields that turn out to differ from what's currently
stored.

Modeled on SportsDashboard's scripts/gemini_predictions.py: same model
fallback chain / rate limiter / retry shape. Bundles several items into
each Gemini call (batching) instead of one call per item.

Why no search here: verifying whether a listing is still active, or
finding its correct URL, genuinely requires a live web visit -- Gemini
has no reliable knowledge of today's walmart.com state, or even of
Walmart's catalog specifically, from training. So this version doesn't
ask for "active" or "product_url" at all; asking without the ability to
check would just produce confident-looking guesses. It only asks for
calories and price, which Gemini can reasonably estimate for well-known
products from general food knowledge -- treat "price" as an estimate,
not today's real price. See gemini_meal_lookup.yaml's header comment for
more, and for what to use instead if you want active/URL verification
(check_active_urls.py, or the search-grounded version of this script).

Why batching: at items_per_call=10 and calls_per_run=10 (config
defaults), one run checks 100 items in 10 Gemini calls instead of 100.
At 6 runs/day (every 4 hours) that's 600 items/day, so the pool's ~1800
items get a full refresh pass roughly every 3 days.

Config: gemini_meal_lookup.yaml (models, batch shape, rate limiting, the
prompt template).

Selection: each run picks items_per_call x calls_per_run items with the
oldest (or missing) "_gemini_checked_at" timestamp, so repeated runs
rotate through the whole pool over time. Items already active === false
are skipped by default (batch.skip_inactive) -- this script can't
un-delist something anyway, so there's no reason to spend a call there;
that's a real difference from the search-grounded version, which leaves
skip_inactive off so a delisted item gets a chance to be found active
again.

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


def select_batch(pool, total_items, skip_inactive):
    """Returns up to `total_items` entries, oldest-checked (or never
    checked) first, so runs rotate through the whole pool over time."""
    eligible = [
        item for item in pool
        if not (skip_inactive and item.get("active") is False)
    ]
    eligible.sort(key=lambda item: item.get("_gemini_checked_at", ""))
    return eligible[:total_items]


def chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


#---------------------------------------------------------------------------
# Prompt building
#---------------------------------------------------------------------------

def build_batch_prompt(cfg, items):
    item_line_tmpl = cfg["prompt"]["item_line"]
    lines = []
    for n, item in enumerate(items, start=1):
        lines.append(item_line_tmpl.format(
            n=n,
            product_name=item.get("PRODUCT_NAME", ""),
            brand=item.get("BRAND", ""),
            sku=item.get("SKU", ""),
        ).rstrip("\n"))

    products_block = "\n".join(lines)
    return cfg["prompt"]["intro"].format(count=len(items), products_block=products_block)


#---------------------------------------------------------------------------
# Response parsing
#---------------------------------------------------------------------------

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def extract_json_array(text):
    """Leniently pulls a JSON array out of a Gemini response that may be
    wrapped in ```json fences or have stray text around it."""
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


def normalize_result(raw):
    if not isinstance(raw, dict):
        raise ValueError(f"batch result entry was not a JSON object: {raw!r}")

    index = raw.get("index")
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = None

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
        "index": index,
        "recognized": recognized,
        "calories": calories,
        "price": price,
        "notes": notes,
    }


def match_results_to_items(items, raw_results):
    """Returns a list the same length as `items`, each slot either a
    normalized result dict or None if no usable result was returned for
    that position. Matches primarily by the "index" field Gemini echoes
    back (1-based); falls back to raw list position if the counts line
    up and indices look unreliable."""
    normalized = []
    for raw in raw_results:
        try:
            normalized.append(normalize_result(raw))
        except ValueError as e:
            log(f"    skipping unparseable batch result entry: {e}")

    by_index = {r["index"]: r for r in normalized if r["index"] is not None}
    use_index_matching = len(by_index) >= max(1, len(items) // 2)

    matched = []
    for i in range(1, len(items) + 1):
        if use_index_matching and i in by_index:
            matched.append(by_index[i])
        elif not use_index_matching and i - 1 < len(normalized):
            matched.append(normalized[i - 1])
        else:
            matched.append(None)
    return matched


#---------------------------------------------------------------------------
# Diffing / applying updates
#---------------------------------------------------------------------------

def apply_result(item, result):
    """Updates `item` in place with any fields from `result` that differ
    from what's currently stored. Returns a list of (field, old, new)
    tuples describing what actually changed. Only touches
    calories/PRICE_CURRENT -- this no-search version never touches
    active or PRODUCT_URL."""
    changes = []

    if not result["recognized"]:
        return changes

    if result["calories"] is not None and item.get("calories") != result["calories"]:
        changes.append(("calories", item.get("calories"), result["calories"]))
        item["calories"] = result["calories"]

    if result["price"] is not None:
        new_price_str = f"{result['price']:.2f}"
        if item.get("PRICE_CURRENT") != new_price_str:
            changes.append(("PRICE_CURRENT", item.get("PRICE_CURRENT"), new_price_str))
            item["PRICE_CURRENT"] = new_price_str

    return changes


#---------------------------------------------------------------------------
# Gemini API calls
#---------------------------------------------------------------------------

def call_gemini_batch_single_model(prompt, gemini_key, model, cfg, rate_limiter):
    """Try exactly one model for one batch prompt. Raises
    _ModelUnavailable immediately (no retry) on any 4xx response, so the
    caller can fall back to the next model without burning this model's
    retry budget on a request that's never going to succeed. Network
    errors / 5xx / unusable responses retry the SAME model up to
    max_retries times."""
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
            return extract_json_array(text)
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


def call_gemini_batch(prompt, gemini_key, cfg, rate_limiter):
    """Tries each model in cfg's fallback chain in order for one batch
    prompt. Returns (raw_results_list, model_used). Only once EVERY model
    has been tried and rejected does this raise DailyQuotaExceeded."""
    last_unavailable = None
    for model in cfg["gemini"]["models"]:
        try:
            raw_results = call_gemini_batch_single_model(prompt, gemini_key, model, cfg, rate_limiter)
            return raw_results, model
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
    parser.add_argument("--items-per-call", type=int, default=None,
                         help="Override batch.items_per_call for this run")
    parser.add_argument("--calls-per-run", type=int, default=None,
                         help="Override batch.calls_per_run for this run")
    args = parser.parse_args()

    gemini_key = os.environ.get("GEMINI_KEY")
    if not gemini_key:
        log("No GEMINI_KEY set -- skipping Gemini pool estimate.")
        return

    cfg = load_config(args.config)
    items_per_call = args.items_per_call or cfg["batch"]["items_per_call"]
    calls_per_run = args.calls_per_run or cfg["batch"]["calls_per_run"]
    skip_inactive = cfg["batch"]["skip_inactive"]
    save_every_calls = cfg["batch"]["save_every_calls"]
    input_path = os.path.join(_SCRIPT_DIR, cfg["pool"]["input_path"])
    output_path = os.path.join(_SCRIPT_DIR, cfg["pool"]["output_path"])

    pool = load_pool(input_path)
    total_items = items_per_call * calls_per_run
    batch_items = select_batch(pool, total_items, skip_inactive)

    if not batch_items:
        log("Gemini pool estimate: no eligible items to check.")
        return

    chunks = list(chunk(batch_items, items_per_call))
    log(f"Gemini pool estimate: {len(batch_items)} item(s) across {len(chunks)} call(s) of "
        f"up to {items_per_call} each, starting with {cfg['gemini']['models'][0]} and "
        f"falling back through {', '.join(cfg['gemini']['models'][1:])} if rate-limited...")

    rate_limiter = _RateLimiter(cfg["gemini"]["min_call_interval_seconds"])
    quota_exhausted = False
    checked, recognized_count, missing, failed, skipped = 0, 0, 0, 0, 0
    field_change_counts = {"calories": 0, "PRICE_CURRENT": 0}

    for call_idx, items in enumerate(chunks, start=1):
        if quota_exhausted:
            skipped += len(items)
            log(f"  [call {call_idx}/{len(chunks)}] skipped -- fallback chain exhausted; "
                f"will check on a future run.")
            continue

        prompt = build_batch_prompt(cfg, items)

        try:
            raw_results, model_used = call_gemini_batch(prompt, gemini_key, cfg, rate_limiter)
        except DailyQuotaExceeded as e:
            quota_exhausted = True
            skipped += len(items)
            log(f"  [call {call_idx}/{len(chunks)}] Gemini fallback chain exhausted: {e}")
            continue
        except Exception as e:  # noqa: BLE001 -- one bad call shouldn't kill the run
            failed += len(items)
            log(f"  [call {call_idx}/{len(chunks)}] Gemini call failed: {e}")
            continue

        matched = match_results_to_items(items, raw_results)
        checked_at = datetime.now(timezone.utc).isoformat()

        for item, result in zip(items, matched):
            label = item.get("PRODUCT_NAME", item.get("SKU", "unknown item"))

            if result is None:
                missing += 1
                log(f"    no usable result for {label} -- will retry on a future run.")
                continue

            changes = apply_result(item, result)
            item["_gemini_checked_at"] = checked_at
            item["_gemini_model"] = model_used
            item["_gemini_notes"] = result["notes"]
            item["_gemini_price_is_estimate"] = True

            for field, old, new in changes:
                field_change_counts[field] += 1
                log(f"    {label}: {field} changed {old!r} -> {new!r}")

            if result["recognized"]:
                recognized_count += 1
            checked += 1

        log(f"  [call {call_idx}/{len(chunks)}] {model_used}: {len(items)} item(s) processed.")

        if call_idx % save_every_calls == 0:
            save_pool(output_path, pool)

    total_changes = sum(field_change_counts.values())
    log(f"Gemini pool estimate: {checked} checked ({recognized_count} recognized), "
        f"{total_changes} field(s) updated (calories={field_change_counts['calories']}, "
        f"price={field_change_counts['PRICE_CURRENT']}), {missing} missing results, "
        f"{failed} failed, {skipped} skipped (fallback chain exhausted).")

    if checked:
        save_pool(output_path, pool)


if __name__ == "__main__":
    main()
