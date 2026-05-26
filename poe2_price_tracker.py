"""
PoE2 white-base price tracker.

Reads items.json, queries the official PoE2 trade API across 4 currencies per item,
converts all listings to exalt-equivalents via poe2scout rates, computes the median
of the cheapest 10 listings, and writes:
  - latest.json:  slim current state, for downstream consumers (item filter step)
  - prices.db:    append-only SQLite history with currency rates per run

Designed to run as a Claude Routine on a 1-3hr schedule, using the cloned repo as
persistent storage. Also works locally.

Environment variables:
  POESESSID     (required)  Path of Exile session cookie
  POE2_LEAGUE   (optional)  default "Standard"
  POE2_CONTACT  (optional)  email put in User-Agent string
"""

import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv


# ============================================================
# Configuration
# ============================================================

# Loads a .env file sitting next to this script if present. No-op in the
# Claude Routine environment, which injects secrets directly.
load_dotenv(Path(__file__).parent / ".env")

# Windows consoles default to cp1252 and choke on the unicode in our progress
# output (≥, →, ✓, ⚠️). Force UTF-8 so the script runs the same on Windows
# and Linux.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LEAGUE = os.environ.get("POE2_LEAGUE", "Standard")
POESESSID = os.environ.get("POESESSID")
CONTACT = os.environ.get("POE2_CONTACT", "anonymous@example.com")
USER_AGENT = (
    f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    f"PoE2PriceTracker/0.1 (contact: {CONTACT})"
)

CURRENCIES = ["chaos", "exalted", "divine", "annul"]
# Per-item, only these may appear in the optional "exclude" list. Chaos and
# exalted are always queried — they're the bulk of white-base listings, and
# excluding them risks ending up with zero data points.
EXCLUDABLE_CURRENCIES = {"annul", "divine"}
TOP_N_PER_CURRENCY = 10        # cheapest hashes pulled per currency
TOP_N_COMBINED = 10            # cheapest after conversion to exalts

# Rate-limit pacing: 1 search per 6.5s keeps us at ~77% of the binding 60/300s
# IP tier on the search endpoint. Fetch piggybacks behind search so doesn't
# need its own pacing.
SEARCH_INTERVAL_SECONDS = 6.5
# Warn (don't auto-throttle) if the 60/300s tier state climbs to this value or
# higher. 48 = 80% of 60. Smoke alarm in case GGG tightens limits or someone
# else hits the same IP.
RATE_LIMIT_WARN_THRESHOLD = 50

# File paths (alongside this script)
ROOT = Path(__file__).parent
ITEMS_PATH = ROOT / "items.json"
LATEST_PATH = ROOT / "latest.json"
DB_PATH = ROOT / "prices.db"

# Endpoints
TRADE_BASE = "https://www.pathofexile.com/api/trade2"
SCOUT_BASE = "https://poe2scout.com/api"
# Realm path for PoE2 in poe2scout. If rate fetches return 404, verify the
# realm value at https://poe2scout.com/api/Realms in a browser.
SCOUT_REALM_PATH = "poe2"


# ============================================================
# PoE2 Trade API client
# ============================================================

class TradeClient:
    def __init__(self, league: str, poesessid: str, user_agent: str):
        self.league = league
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if poesessid:
            self.session.cookies.set(
                "POESESSID", poesessid, domain=".pathofexile.com"
            )
        self._last_search_at = 0.0  # monotonic-ish pacing anchor

    def _pace_search(self) -> None:
        elapsed = time.time() - self._last_search_at
        wait = SEARCH_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_search_at = time.time()

    def _check_rate_warnings(self, response: requests.Response, label: str) -> None:
        """Parse X-Rate-Limit-* headers; print a warning if any tier is at or
        above the 80% threshold, or if we're banned."""
        rules = response.headers.get("X-Rate-Limit-Rules", "")
        for rule in [r.strip() for r in rules.split(",") if r.strip()]:
            state = response.headers.get(f"X-Rate-Limit-{rule}-State", "")
            for tier in state.split(","):
                parts = tier.split(":")
                if len(parts) != 3:
                    continue
                try:
                    used, window, ban_left = (int(p) for p in parts)
                except ValueError:
                    continue
                if ban_left > 0:
                    print(
                        f"  🚫 BANNED [{label}] rule={rule} window={window}s "
                        f"remaining={ban_left}s",
                        file=sys.stderr,
                    )
                elif window == 300 and used >= RATE_LIMIT_WARN_THRESHOLD:
                    print(
                        f"  ⚠️  rate warning [{label}] {rule} tier={window}s "
                        f"used={used} (threshold={RATE_LIMIT_WARN_THRESHOLD})",
                        file=sys.stderr,
                    )

    def search(self, base: str, min_ilvl: int, currency: str) -> dict | None:
        self._pace_search()
        url = f"{TRADE_BASE}/search/poe2/{quote(self.league)}"
        body = {
            "query": {
                "status": {"option": "securable"},  # instant-buyout only
                "type": base,
                "stats": [{"type": "and", "filters": []}],
                "filters": {
                    "type_filters": {"filters": {
                        "rarity": {"option": "normal"},
                        "ilvl": {"min": min_ilvl},
                    }},
                    "misc_filters": {"filters": {
                        "corrupted": {"option": "false"},
                    }},
                    "trade_filters": {"filters": {
                        "price": {"option": currency},
                    }},
                },
            },
            "sort": {"price": "asc"},
        }
        try:
            r = self.session.post(url, data=json.dumps(body), timeout=30)
        except requests.RequestException as e:
            print(f"  search network error [{base}/{currency}]: {e}", file=sys.stderr)
            return None
        self._check_rate_warnings(r, "search")
        if r.status_code != 200:
            print(
                f"  search HTTP {r.status_code} [{base}/{currency}]: "
                f"{r.text[:150]}",
                file=sys.stderr,
            )
            return None
        return r.json()

    def fetch(self, query_id: str, hashes: list) -> list:
        if not hashes:
            return []
        url = f"{TRADE_BASE}/fetch/{','.join(hashes[:10])}"
        try:
            r = self.session.get(url, params={"query": query_id}, timeout=30)
        except requests.RequestException as e:
            print(f"  fetch network error: {e}", file=sys.stderr)
            return []
        self._check_rate_warnings(r, "fetch")
        if r.status_code != 200:
            print(f"  fetch HTTP {r.status_code}: {r.text[:150]}", file=sys.stderr)
            return []
        return r.json().get("result") or []


# ============================================================
# Currency converter (poe2scout)
# ============================================================

class CurrencyConverter:
    """Converts amounts to exalt-equivalents using poe2scout rates."""

    def __init__(self, league: str):
        self.league = league
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        self.rates_to_exalt: dict = {"exalted": 1.0}  # base unit

    def fetch_rates(self) -> None:
        """Populate self.rates_to_exalt for chaos, divine, annul."""
        for currency in ["chaos", "divine", "annul"]:
            url = (
                f"{SCOUT_BASE}/{SCOUT_REALM_PATH}"
                f"/Leagues/{quote(self.league)}/Currencies/{currency}"
            )
            try:
                r = self.session.get(
                    url, params={"ReferenceCurrency": "exalted"}, timeout=30
                )
            except requests.RequestException as e:
                print(f"  rate fetch error [{currency}]: {e}", file=sys.stderr)
                self.rates_to_exalt[currency] = None
                continue
            if r.status_code != 200:
                print(
                    f"  rate fetch HTTP {r.status_code} [{currency}]: "
                    f"{r.text[:150]}",
                    file=sys.stderr,
                )
                self.rates_to_exalt[currency] = None
                continue
            try:
                data = r.json()
                rate = data.get("CurrentPrice")
            except ValueError:
                rate = None
            if rate is None:
                print(f"  no CurrentPrice for {currency}", file=sys.stderr)
                self.rates_to_exalt[currency] = None
            else:
                self.rates_to_exalt[currency] = float(rate)
                print(f"  rate: 1 {currency} = {rate:.4f} exalted")

    def to_exalts(self, amount: float, currency: str) -> float | None:
        rate = self.rates_to_exalt.get(currency)
        if rate is None:
            return None
        return amount * rate


# ============================================================
# Per-item pipeline
# ============================================================

def process_item(client: TradeClient, converter: CurrencyConverter,
                 item: dict) -> dict:
    base = item["base"]
    min_ilvl = item["min_ilvl"]
    excluded = set(item.get("exclude") or [])
    currencies = [c for c in CURRENCIES if c not in excluded]
    exalt_values: list[float] = []
    total_listings = 0  # sum of search.total across queried currencies

    for currency in currencies:
        search = client.search(base, min_ilvl, currency)
        if not search:
            continue
        total_listings += search.get("total", 0)
        hashes = (search.get("result") or [])[:TOP_N_PER_CURRENCY]
        if not hashes:
            continue
        listings = client.fetch(search["id"], hashes)
        for listing in listings:
            price = (listing.get("listing") or {}).get("price") or {}
            amt = price.get("amount")
            cur = price.get("currency")
            if amt is None or cur is None:
                continue
            exalt_value = converter.to_exalts(amt, cur)
            if exalt_value is not None:
                exalt_values.append(exalt_value)

    exalt_values.sort()
    cheapest = exalt_values[:TOP_N_COMBINED]
    median = statistics.median(cheapest) if cheapest else None

    return {
        "base": base,
        "min_ilvl": min_ilvl,
        "median_exalts": median,
        "num_listings": total_listings,
    }


# ============================================================
# Persistence
# ============================================================

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp             TEXT NOT NULL,
            league                TEXT NOT NULL,
            rate_chaos_to_exalt   REAL,
            rate_divine_to_exalt  REAL,
            rate_annul_to_exalt   REAL
        );
        CREATE TABLE IF NOT EXISTS item_prices (
            run_id         INTEGER NOT NULL REFERENCES runs(run_id),
            base           TEXT    NOT NULL,
            min_ilvl       INTEGER NOT NULL,
            median_exalts  REAL,
            num_listings   INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_item_prices_lookup
            ON item_prices(base, min_ilvl, run_id);
    """)
    conn.commit()
    return conn


def write_outputs(results: list, rates: dict, league: str,
                  db_path: Path, latest_path: Path) -> None:
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    conn = init_db(db_path)
    cur = conn.execute(
        """INSERT INTO runs
           (timestamp, league, rate_chaos_to_exalt,
            rate_divine_to_exalt, rate_annul_to_exalt)
           VALUES (?, ?, ?, ?, ?)""",
        (timestamp, league, rates.get("chaos"),
         rates.get("divine"), rates.get("annul")),
    )
    run_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO item_prices
           (run_id, base, min_ilvl, median_exalts, num_listings)
           VALUES (?, ?, ?, ?, ?)""",
        [(run_id, r["base"], r["min_ilvl"],
          r["median_exalts"], r["num_listings"]) for r in results],
    )
    conn.commit()
    conn.close()

    slim = [
        {"base": r["base"], "min_ilvl": r["min_ilvl"],
         "median_exalts": r["median_exalts"]}
        for r in results
    ]
    with open(latest_path, "w") as f:
        json.dump(slim, f, indent=2)


# ============================================================
# Notifications (Telegram, opt-in via env vars)
# ============================================================

TELEGRAM_API = "https://api.telegram.org"


def notify(message: str) -> None:
    """Best-effort Telegram ping. No-op if TELEGRAM_BOT_TOKEN or
    TELEGRAM_CHAT_ID is unset. Failures are logged but never raise —
    the tracker's exit code reflects the data run, not the notification."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        r = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message[:3500],  # well under Telegram's 4096 cap
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(
                f"  notify: Telegram returned HTTP {r.status_code}: "
                f"{r.text[:150]}",
                file=sys.stderr,
            )
    except requests.RequestException as e:
        print(f"  notify: send failed: {e}", file=sys.stderr)


def build_success_message(results: list, league: str) -> str:
    """Compact one-mobile-notification summary. Lists up to 5 priciest bases."""
    priced = [r for r in results if r["median_exalts"] is not None]
    no_data = len(results) - len(priced)
    head = f"✓ PoE2 {league}: {len(priced)} priced"
    if no_data:
        head += f" ({no_data} no-data)"
    if not priced:
        return head
    top = sorted(priced, key=lambda r: r["median_exalts"], reverse=True)[:5]
    items = ", ".join(f"{r['base']} {r['median_exalts']:.1f}ex" for r in top)
    if len(priced) > 5:
        items += f", +{len(priced) - 5} more"
    return f"{head} — {items}"


# ============================================================
# Main
# ============================================================

def main() -> int:
    if not POESESSID:
        print("ERROR: POESESSID env var not set.", file=sys.stderr)
        print("Set it via: export POESESSID=<your_session_id>", file=sys.stderr)
        notify("✗ PoE2 tracker: POESESSID env var not set")
        return 1

    if not ITEMS_PATH.exists():
        print(f"ERROR: {ITEMS_PATH} not found.", file=sys.stderr)
        notify(f"✗ PoE2 tracker: {ITEMS_PATH.name} not found")
        return 1

    with open(ITEMS_PATH) as f:
        items = json.load(f)

    # Validate the optional "exclude" field on each item. Fail fast on bad
    # config — a typo here would silently distort the data otherwise.
    for i, item in enumerate(items):
        excl = item.get("exclude")
        if excl is None:
            continue
        if not isinstance(excl, list):
            msg = (f"item {i} ({item.get('base')!r}): 'exclude' must be a list, "
                   f"got {type(excl).__name__}")
            print(f"ERROR: {msg}", file=sys.stderr)
            notify(f"✗ PoE2 tracker config error: {msg}")
            return 1
        invalid = set(excl) - EXCLUDABLE_CURRENCIES
        if invalid:
            msg = (f"item {i} ({item.get('base')!r}): cannot exclude "
                   f"{sorted(invalid)}; only {sorted(EXCLUDABLE_CURRENCIES)} "
                   "are excludable")
            print(f"ERROR: {msg}", file=sys.stderr)
            notify(f"✗ PoE2 tracker config error: {msg}")
            return 1

    print(f"Tracking {len(items)} items in league '{LEAGUE}'\n")

    print("Fetching currency rates from poe2scout...")
    converter = CurrencyConverter(LEAGUE)
    converter.fetch_rates()
    if all(v is None for k, v in converter.rates_to_exalt.items() if k != "exalted"):
        msg = ("no currency rates fetched from poe2scout. "
               "Check SCOUT_REALM_PATH and league name.")
        print(f"ERROR: {msg}", file=sys.stderr)
        notify(f"✗ PoE2 tracker: {msg}")
        return 1
    print()

    client = TradeClient(LEAGUE, POESESSID, USER_AGENT)
    results = []
    for i, item in enumerate(items, 1):
        excl = item.get("exclude") or []
        excl_str = f"  [excluding: {', '.join(excl)}]" if excl else ""
        print(f"[{i}/{len(items)}] {item['base']} ilvl≥{item['min_ilvl']}{excl_str}")
        result = process_item(client, converter, item)
        if result["median_exalts"] is not None:
            print(f"    → {result['median_exalts']:.2f} ex  "
                  f"({result['num_listings']} total listings)\n")
        else:
            print(f"    → no listings\n")
        results.append(result)

    write_outputs(
        results, converter.rates_to_exalt, LEAGUE, DB_PATH, LATEST_PATH
    )
    print(f"✓ Wrote {len(results)} rows to {DB_PATH.name}")
    print(f"✓ Wrote {LATEST_PATH.name}")
    notify(build_success_message(results, LEAGUE))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        # Unhandled crash — best-effort notify, then re-raise so the
        # traceback still prints and the process exits non-zero.
        try:
            notify(f"✗ PoE2 tracker crashed: {type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
