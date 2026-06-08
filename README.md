# PoE2 Price Tracker

Polls the official PoE2 trade API for a configurable list of white (normal) item bases at minimum item levels, converts all listings to exalt-equivalents via poe2scout rates, and stores the second-cheapest listing as the price per item (the cheapest in thin markets — see below).

Outputs two things every run:

- **`latest.json`** — slim current state `{rates_to_exalt: {exalted, chaos, divine, annul}, items: [{base, min_ilvl, median_exalts}, ...]}`. The per-run poe2scout rates ride along so consumers can re-express exalt prices in any currency without opening `prices.db`. For downstream consumers (e.g. the future item filter step).
- **`prices.db`** — append-only SQLite history with currency rates stored per run, so historical prices can be re-expressed in any currency later.

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
  {"base": "Expert Laced Boots", "min_ilvl": 81},
  {"base": "Ancestral Tiara",    "min_ilvl": 82}
]
```

Each item has two fields:

- `base` (required) — exact base name as it appears in-game / on the trade site
- `min_ilvl` (required) — minimum item level filter

**Price selection.** The stored price (`median_exalts`) is the **second-cheapest** listing in exalt-equivalents — this skips a single lowball / joke listing without averaging in pricier ones. In a **thin market** (fewer than 40 total listings for the base) there isn't enough depth to trust a second, so the **cheapest** listing is used instead. (The `median_exalts` field keeps its name for backward compatibility with `prices.db` history and downstream consumers, but it's no longer a median.)

**Auto-skip.** Currencies are queried in ascending order of unit value: exalted → chaos → annul → divine. After each query, if the script already has 10+ listings and the 10th cheapest is worth less than one whole unit of the next currency (per the current poe2scout rate), it skips that currency's API call — no listing in it could enter the cheapest-10. Typical white base ilvl 82: only exalted gets queried (chaos/annul/divine all auto-skipped), saving ~20s per item.

**Auto-fan-out (ilvl 82 → 80).** If an ilvl-82 entry's price exceeds one divine, the script also queries the same base at ilvl 80 and adds a separate row to `latest.json` / `prices.db`. The rationale: a chase base worth a divine at ilvl 82 is usually still tradeable at ilvl 80, and ilvl 80 drops meaningfully more often, so the filter benefits from having both prices. Fan-out is skipped if you've already put `{"base": "...", "min_ilvl": 80}` in `items.json` for that base — your explicit config wins.

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

The script paces searches at one every **6.5 seconds**, which keeps you at ~77% of the binding 60-per-300s IP tier on the trade search endpoint. With auto-skip, most cheap white bases only need 1 search + 1 fetch (~6.5s/item); valuable ones priced above a divine each may need all 4 (~26s). 80 mixed items is roughly 15-35 min/run depending on the price profile of your list.

On every response the script parses `X-Rate-Limit-*-State` and prints a warning to stderr if the 60/300s tier crosses 48 used (80%), or if a ban is in effect. This is a smoke alarm only — no auto-throttling, since 6.5s pacing should always be safe.

## Running on a schedule

GGG's trade API blocks most cloud-provider IPs (Claude Routines, GitHub Actions hosted runners, etc.) at the source — `code 6 "Forbidden"`. The fix is to run the tracker on a residential IP, i.e. your own PC. The repo includes a PowerShell wrapper and a setup script that wires it into Windows Task Scheduler.

### One-time Task Scheduler setup

1. Make sure the local dev steps above work (`python poe2_price_tracker.py` finishes with `✓` on this machine).
2. Open PowerShell **as Administrator**, `cd` into this repo, and run:

   ```powershell
   .\setup_schedule.ps1
   ```

   This registers a background scheduled task named **PoE2 Price Tracker** that runs every couple of hours and only when plugged in. It runs **whether or not you're logged on** (an S4U logon — no console window pops up, no stored password), so it fires even with the lid closed, and it works even with Windows Hello-only sign-in enforced. Edit the variables at the top of the script to change the interval, anchor, or timeout.

   > Because an S4U task can't read Windows Credential Manager, `git push` is done over SSH with a repo-scoped deploy key — do the one-time **[SSH deploy key setup](#git-push-from-the-background-task-ssh-deploy-key)** below *before* the first scheduled run, or the push will fail.

3. Confirm it works end-to-end with one manual run:

   ```powershell
   Start-ScheduledTask -TaskName 'PoE2 Price Tracker'
   ```

   You should see the task go from Ready → Running in Task Scheduler, get a Telegram ping when it finishes, and a `prices: <timestamp>` commit appear on GitHub.

### Laptop-closed behavior

The task is registered to **run whether you're logged on or not**, so it runs in the background (no window) and doesn't need you to be logged in. On a **Modern Standby (S0 Low Power Idle)** laptop — most newer machines; check with `powercfg /a` — Windows keeps running with the lid closed, so the task fires on schedule during standby on its own. No wake-timer or lid-action tweaks are needed.

The one requirement on this machine type: **keep the laptop on AC power** while the lid is closed. Windows throttles background work during Modern Standby on battery (and may hibernate after a while), so battery runs are unreliable — the task is configured AC-only by default.

> On an older **S3-sleep** laptop, closing the lid truly suspends the machine, so you'd additionally need lid-close set to *Sleep* (not *Hibernate* — wake timers don't fire from hibernate) and **Allow wake timers** enabled under Power Options; the `WakeToRun` flag then wakes it for each run. On a Modern Standby machine `WakeToRun` is just a harmless no-op.

### git push from the background task (SSH deploy key)

The task runs under an **S4U** logon (so it needs no stored password and isn't blocked by Windows Hello-only sign-in). S4U can't read Windows Credential Manager, so HTTPS git auth won't work from it — instead the repo pushes over **SSH** using a passphrase-less, repo-scoped **deploy key**. One-time setup (PowerShell, in the repo):

```powershell
# 1. Generate a passphrase-less key (cmd /c gives a reliable empty passphrase)
cmd /c "ssh-keygen -t ed25519 -C `"poe2-tracker@$env:COMPUTERNAME`" -f `"$env:USERPROFILE\.ssh\poe2_tracker`" -N `"`" -q"

# 2. Pre-trust github.com so the non-interactive push never hits a host-key prompt
ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # one connection seeds known_hosts

# 3. Pin THIS repo's git to use only that key, via Windows OpenSSH
git config --local core.sshCommand 'C:/Windows/System32/OpenSSH/ssh.exe -i ~/.ssh/poe2_tracker -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'

# 4. Point the remote at SSH
git remote set-url origin git@github.com:T1mothy-L/PoE-Base-Pricer.git
```

Then add `~/.ssh/poe2_tracker.pub` to the GitHub repo under **Settings → Deploy keys → Add deploy key**, with **Allow write access** ticked. Verify with `ssh -i $env:USERPROFILE\.ssh\poe2_tracker -T git@github.com` (expect `Hi <owner>/<repo>! You've successfully authenticated`) and a manual `git push`. The `core.sshCommand` / remote settings live in `.git/config` (local, never committed), so they're per-machine.

### Wrapper script details

`run_tracker.ps1` is what Task Scheduler invokes. It:

- runs `python poe2_price_tracker.py` in the repo dir,
- on exit 0, stages `prices.db` + `latest.json`, commits them as `prices: <ISO timestamp>`, and pushes to the configured remote (over SSH, per above),
- on non-zero exit, does nothing — the Python script has already sent a Telegram ping with the failure reason.

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
  median_exalts  REAL,                -- selected price (2nd-cheapest, or cheapest in thin markets); null when no listings
  num_listings   INTEGER NOT NULL     -- sum of search.total across 4 currencies
);

CREATE INDEX idx_item_prices_lookup ON item_prices(base, min_ilvl, run_id);
```

Example analytics query (price over time for one base, expressed in divine):

```sql
SELECT r.timestamp,
       ip.median_exalts / r.rate_divine_to_exalt AS price_divine
FROM item_prices ip
JOIN runs r ON r.run_id = ip.run_id
WHERE ip.base = 'Ancestral Tiara' AND ip.min_ilvl = 82
ORDER BY r.timestamp;
```

## Not affiliated

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.
