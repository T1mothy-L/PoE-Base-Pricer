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

## Running on a schedule

GGG's trade API blocks most cloud-provider IPs (Claude Routines, GitHub Actions hosted runners, etc.) at the source — `code 6 "Forbidden"`. The fix is to run the tracker on a residential IP, i.e. your own PC. The repo includes a PowerShell wrapper and a setup script that wires it into Windows Task Scheduler.

### One-time Task Scheduler setup

1. Make sure the local dev steps above work (`python poe2_price_tracker.py` finishes with `✓` on this machine).
2. Open PowerShell **as Administrator**, `cd` into this repo, and run:

   ```powershell
   .\setup_schedule.ps1
   ```

   This registers a scheduled task named **PoE2 Price Tracker** that runs every 4 hours (anchored at midnight), wakes the laptop from sleep if needed, and only runs when plugged in. Edit the variables at the top of the script to change the interval, anchor, or timeout.

3. Confirm it works end-to-end with one manual run:

   ```powershell
   Start-ScheduledTask -TaskName 'PoE2 Price Tracker'
   ```

   You should see the task go from Ready → Running in Task Scheduler, get a Telegram ping when it finishes, and a `prices: <timestamp>` commit appear on GitHub.

### Laptop-closed power settings

For the task to fire while the lid is closed, Windows needs to be allowed to wake and not sleep too aggressively. Once-off:

1. **Settings → System → Power & battery → Power mode**: any "Balanced" or "Best performance" mode is fine.
2. **Control Panel → Power Options → Choose what closing the lid does**: when **Plugged in**, set both *Sleep button* and *Close the lid* to **Do nothing** (or **Sleep** if you'd rather the system actually suspend between runs — Task Scheduler's `WakeToRun` flag handles waking it back up either way).
3. **Control Panel → Power Options → Change plan settings → Change advanced power settings → Sleep → Allow wake timers**: set to **Enable** for both On battery and Plugged in (or just Plugged in if you skip battery runs).

If the laptop is set to *Hibernate* on lid close, wake timers don't fire — use *Sleep* instead.

### Wrapper script details

`run_tracker.ps1` is what Task Scheduler invokes. It:

- runs `python poe2_price_tracker.py` in the repo dir,
- on exit 0, stages `prices.db` + `latest.json`, commits them as `prices: <ISO timestamp>`, and pushes to the configured remote,
- on non-zero exit, does nothing — the Python script has already sent a Telegram ping with the failure reason.

`git push` uses Windows Credential Manager; once you've pushed once interactively, subsequent pushes from the scheduled task work without prompting.

### Why not a Claude Routine

Routine sandboxes run on cloud-provider IPs, which GGG's trade API rejects with `403 code 6 "Forbidden"`. We confirmed this with byte-for-byte identical secrets across local and Routine — local works, Routine 403s on every call. The poe2scout currency-rate endpoint still works in a Routine (different host, different rules), so a future variant could pull pricing from poe2scout if you ever want to move off the local schedule.

## Troubleshooting

- **`HTTP 403` from the trade API on a cloud runner (Routine, GitHub Actions, etc.)** — GGG is blocking the source IP. Run the tracker locally via Task Scheduler instead; see "Running on a schedule" above.
- **`HTTP 403` from a residential IP** — `POESESSID` is stale or wrong. Re-export from devtools.
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
