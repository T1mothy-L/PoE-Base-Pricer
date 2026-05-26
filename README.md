# PoE2 Price Tracker

Polls the official PoE2 trade API for a configurable list of white (normal) item bases at minimum item levels, converts all listings to exalt-equivalents via poe2scout rates, and stores the median of the cheapest 10 listings per item.

Outputs two things every run:

- **`latest.json`** — slim current state `[{base, min_ilvl, median_exalts}, ...]`. For downstream consumers (e.g. the future item filter step).
- **`prices.db`** — append-only SQLite history with currency rates stored per run, so historical medians can be re-expressed in any currency later.

Designed to run as a Claude Routine using this repo as persistent storage. Also works locally.

## Local setup

```bash
pip install -r requirements.txt

# Copy the template and fill in POESESSID (grab it from browser devtools →
# Application → Cookies → pathofexile.com). The .env is gitignored.
cp .env.example .env
$EDITOR .env

python poe2_price_tracker.py
```

Alternatively, export the vars directly instead of using `.env`:

```bash
export POESESSID=<your_session_cookie>
export POE2_LEAGUE="Standard"      # optional
export POE2_CONTACT="you@example.com"  # optional
```

Edit `items.json` to add bases. Same `base` can appear twice with different `min_ilvl`:

```json
[
  {"base": "Expert Laced Boots", "min_ilvl": 82},
  {"base": "Expert Laced Boots", "min_ilvl": 81, "exclude": ["annul"]},
  {"base": "Ancestral Tiara",    "min_ilvl": 82, "exclude": ["divine", "annul"]}
]
```

Each item has two required fields and one optional one:

- `base` (required) — exact base name as it appears in-game / on the trade site
- `min_ilvl` (required) — minimum item level filter
- `exclude` (optional) — list of currencies to skip querying for this item. Only `"annul"` and `"divine"` are allowed; `"chaos"` and `"exalted"` are always queried because they account for the bulk of white-base listings. Skipping a currency cuts that item's API cost by 1 search + 1 fetch (~6.5s saved).

Bad values fail at startup before any API call.

## Telegram notifications (optional)

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are both set, every run sends a one-line summary to your Telegram chat. Success looks like:

```
✓ PoE2 Standard: 2 priced — Sekhema Sandals 209.0ex, Ancestral Tiara 28.8ex
```

Failures (missing POESESSID, bad config, no rates fetched, unhandled crash) send a `✗`-prefixed reason. If either env var is unset the feature silently no-ops.

**Setup (~2 minutes):**

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts → copy the API token.
2. Open a chat with your new bot and send it any message (the bot can't message you first).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser, find the `"chat":{"id":...}` value.
4. Put both values in `.env` (locally) and/or as Routine secrets.

## Pacing and rate limits

The script paces searches at one every **6.5 seconds**, which keeps you at ~77% of the binding 60-per-300s IP tier on the trade search endpoint. Each item costs 4 searches (one per currency) + 4 fetches, so ~26s per item. Ten items ≈ 4.5 minutes per run.

On every response the script parses `X-Rate-Limit-*-State` and prints a warning to stderr if the 60/300s tier crosses 48 used (80%), or if a ban is in effect. This is a smoke alarm only — no auto-throttling, since 6.5s pacing should always be safe.

## Running as a Claude Routine

Routines clone this repo at the start of every run, so committing changes back is how state persists across runs.

1. **Push this repo to GitHub** (private is fine).
2. **Create a Claude Routine** at [claude.ai/code](https://claude.ai/code) → `/schedule`. Configure:
   - **Repo:** this one
   - **Schedule:** every 2–4 hours (match your daily quota — Pro plans have a low daily run cap)
   - **Network:** allow `pathofexile.com` and `poe2scout.com` (and `api.telegram.org` if using notifications)
   - **Setup script:** `pip install requests python-dotenv` (inlining the deps avoids cwd-sensitivity in the Routine sandbox; `requirements.txt` is the source of truth for local installs)
   - **Secrets:** `POESESSID` (and optionally `POE2_LEAGUE`, `POE2_CONTACT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
   - **Prompt (the routine's task):** something like:

     ```
     Run the price tracker:

         python poe2_price_tracker.py

     If the script exits 0, commit the changes to prices.db and latest.json with
     message "prices: <timestamp>" and push to main. If it exits non-zero or
     prints a rate-limit ban warning, do not commit; report the error and stop.
     ```

3. **Verify the first run** by checking that `prices.db` and `latest.json` appear as commits on the default branch.

## Troubleshooting

- **`HTTP 403` from the trade API** — `POESESSID` is stale or wrong. Re-export from devtools.
- **`HTTP 403` with `Cloudflare`** — the trade User-Agent might need refreshing. Try copying a real cURL from your browser.
- **`HTTP 404` from poe2scout** — `SCOUT_REALM_PATH` in the script may need updating; check `https://poe2scout.com/api/Realms` for the current value.
- **Banned (1800s remaining)** — you hit the 60/300s tier. Should not happen with default pacing; check for concurrent runs.
- **"no listings" for an item** — likely a misspelled base name in `items.json`. Verify on `https://www.pathofexile.com/trade2`.

## Schema

```sql
CREATE TABLE runs (
  run_id                INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp             TEXT NOT NULL,
  league                TEXT NOT NULL,
  rate_chaos_to_exalt   REAL,
  rate_divine_to_exalt  REAL,
  rate_annul_to_exalt   REAL
);

CREATE TABLE item_prices (
  run_id         INTEGER NOT NULL REFERENCES runs(run_id),
  base           TEXT    NOT NULL,
  min_ilvl       INTEGER NOT NULL,
  median_exalts  REAL,                -- null when no listings
  num_listings   INTEGER NOT NULL     -- sum of search.total across 4 currencies
);

CREATE INDEX idx_item_prices_lookup ON item_prices(base, min_ilvl, run_id);
```

Example analytics query (median over time for one base, expressed in divine):

```sql
SELECT r.timestamp,
       ip.median_exalts / r.rate_divine_to_exalt AS median_divine
FROM item_prices ip
JOIN runs r ON r.run_id = ip.run_id
WHERE ip.base = 'Ancestral Tiara' AND ip.min_ilvl = 82
ORDER BY r.timestamp;
```

## Not affiliated

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.
