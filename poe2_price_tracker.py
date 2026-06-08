"""
PoE2 white-base price tracker.

Reads items.json, queries the official PoE2 trade API across 4 currencies per item,
converts all listings to exalt-equivalents via poe2scout rates, picks the
second-cheapest listing as the price (the cheapest in thin markets), and writes:
  - latest.json:  slim current state {rates_to_exalt, items}, for downstream
                  consumers (item filter step)
  - prices.db:    append-only SQLite history with currency rates per run

Designed to run as a Claude Routine on a 1-3hr schedule, using the cloned repo as
persistent storage. Also works locally.

Environment variables:
  POESESSID     (required)  Path of Exile session cookie
  POE2_LEAGUE   (optional)  default "Standard"
  POE2_CONTACT  (optional)  email put in User-Agent string
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
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

# Currencies are queried in ascending order of unit value so the cheap
# currencies populate the top-N first. After each query we check whether
# the next currency could possibly contribute a listing to the cheapest-N:
# if the N-th cheapest exalt-equivalent we have is already lower than the
# next currency's exalt rate, no listing in that currency can fit, so we
# skip the query. The previous manual `exclude` field is gone — auto-skip
# subsumes every case it covered.
CURRENCIES = ["exalted", "chaos", "annul", "divine"]
TOP_N_PER_CURRENCY = 10        # cheapest hashes pulled per currency
TOP_N_COMBINED = 10            # cheapest after conversion (used by auto-skip)
# Price selection: take the 2nd-cheapest converted listing to dodge a single
# lowball/joke listing. Below this many total listings the market is too thin
# to trust a second, so fall back to the genuine cheapest.
SECOND_LOWEST_MIN_LISTINGS = 40

# Auto-fan-out: when an ilvl-82 item's price exceeds this many divines, do
# a follow-up search at ilvl 80 too. The idea is that a chase base worth a
# divine at ilvl 82 is probably still tradeable at ilvl 80 (which drops
# meaningfully more often), so we want filter-rule data for both. Fan-out
# is skipped if the user already has (base, 80) in items.json — their
# explicit config wins.
FANOUT_82_TO_80_DIVINE_THRESHOLD = 1.0
FANOUT_LOWER_ILVL = 80

# Per-request pacing (search + fetch share GGG's `Ip` budget). GGG sends
# scripts a tighter 300s bucket (30) than browsers (60), so we look at
# the first response to figure out which one we're in and lock the
# interval accordingly. By design, both values put us very close to the
# limit -- we keep the per-base log line + RateLimitBanned auto-pause as
# the safety net, but skip the 80% warn-print since "near limit" is the
# normal operating point.
INTERVAL_FOR_TIGHT_BUCKET = 10.5  # 30/300s -> 28.6 reqs/300s = 95% of budget
INTERVAL_FOR_LOOSE_BUCKET = 6.0   # 60/300s -> 50.0 reqs/300s = 83% of budget
LOOSE_BUCKET_LIMIT = 60           # threshold to flip to the looser interval

# After the full items.json pass, any item that hit an HTTP/network error gets
# one more attempt. We wait this long first to let a transient blip clear.
TRADE_RETRY_WAIT = 30  # seconds

# File paths (alongside this script)
ROOT = Path(__file__).parent
ITEMS_PATH = ROOT / "items.json"
LATEST_PATH = ROOT / "latest.json"
DB_PATH = ROOT / "prices.db"
LOG_PATH = ROOT / "tracker.log"
ERRORS_PATH = ROOT / "errors.json"

# Logging: warnings and errors are written to a rotating file (tracker.log) and
# echoed to stderr; normal progress stays on stdout via print(). Configured at
# import time so even the bottom-level crash handler can use it.
log = logging.getLogger("poe2_tracker")
log.setLevel(logging.INFO)
log.propagate = False
_file_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
)
_stream_handler = logging.StreamHandler(sys.stderr)
_stream_handler.setLevel(logging.WARNING)  # console keeps today's stderr look
_stream_handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_file_handler)
log.addHandler(_stream_handler)

# Endpoints
TRADE_BASE = "https://www.pathofexile.com/api/trade2"
SCOUT_BASE = "https://poe2scout.com/api"
# Realm path for PoE2 in poe2scout. If rate fetches return 404, verify the
# realm value at https://poe2scout.com/api/Realms in a browser.
SCOUT_REALM_PATH = "poe2"
# poe2scout currency-rate retry on HTTP 429. The budget is SHARED across all
# currencies in one fetch_rates() pass, not per-currency, because poe2scout's
# 429 is a single IP/endpoint bucket every currency request draws from.
SCOUT_MAX_RETRIES = 5    # total 429 retries across chaos/divine/annul
SCOUT_RETRY_WAIT = 60    # seconds to wait before each 429 retry


class TradeAuthError(Exception):
    """Raised when the trade API returns 401/403 — the whole run is doomed
    because every subsequent search would hit the same wall. Caught at the
    top of main() to abort fast and notify."""
class RateLimitBanned(Exception):
    def __init__(self, seconds: int, rule: str, window: int):
        self.seconds = seconds
        self.rule = rule
        self.window = window
        super().__init__(f"Banned on {rule} {window}s tier for {seconds}s")

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
        # Covers both search and fetch since GGG's `Ip` rule meters them
        # against the same per-IP bucket. None = no request has been sent
        # yet, so the first _pace() should not wait (we assume a clean
        # bucket on startup and let the first response reveal the
        # actual state).
        self._last_request_at: float | None = None
        # Start with the conservative interval; switch to the looser one
        # if the first response with a 300s tier shows a 60-limit.
        self.search_interval = INTERVAL_FOR_TIGHT_BUCKET
        self.last_state: dict[tuple[str, int], tuple[int, int]] = {}
        self.peak_state: dict[tuple[str, int], tuple[int, int]] = {}

    def _pace(self) -> None:
        """Sleep until self.search_interval has elapsed since the
        previous request. The very first call no-ops because we assume
        a clean bucket -- there's nothing to pace against until we've
        actually sent something."""
        if self._last_request_at is not None:
            elapsed = time.time() - self._last_request_at
            wait = self.search_interval - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.time()

    def _set_interval_from_headers(self) -> None:
        """Choose between the two pacing values based on whatever 300s
        tier we've seen so far. Any 300s tier with the looser limit
        (60) opts us into the faster pace; otherwise we stay
        conservative."""
        for (_rule, window), (_used, limit) in self.last_state.items():
            if window == 300 and limit >= LOOSE_BUCKET_LIMIT:
                self.search_interval = INTERVAL_FOR_LOOSE_BUCKET
                return
        self.search_interval = INTERVAL_FOR_TIGHT_BUCKET

    def _check_rate_warnings(self, response: requests.Response, label: str) -> None:
        """Parse X-Rate-Limit-* headers; update last/peak state, lock in
        the per-request pacing interval from the observed 300s tier
        limit, and raise RateLimitBanned if any tier shows an active
        lockout. The old 80%-warn print is intentionally gone -- we
        operate near the limit on purpose, so steady-state warnings
        would be noise."""
        rules = response.headers.get("X-Rate-Limit-Rules", "")
        for rule in [r.strip() for r in rules.split(",") if r.strip()]:
            # The -State header has used:window:ban_left per tier;
            # the bare rule header has hits:window:ban_period per tier
            # -- we need the `hits` (= limit) from the latter.
            state = response.headers.get(f"X-Rate-Limit-{rule}-State", "")
            limits = response.headers.get(f"X-Rate-Limit-{rule}", "")

            window_limits: dict[int, int] = {}
            for piece in limits.split(","):
                parts = piece.split(":")
                if len(parts) == 3:
                    try:
                        hits, window, _ = (int(p) for p in parts)
                        window_limits[window] = hits
                    except ValueError:
                        continue

            for tier in state.split(","):
                parts = tier.split(":")
                if len(parts) != 3:
                    continue
                try:
                    used, window, ban_left = (int(p) for p in parts)
                except ValueError:
                    continue

                limit = window_limits.get(window, 0)
                if limit > 0:
                    self.last_state[(rule, window)] = (used, limit)
                    prev_used, _ = self.peak_state.get((rule, window), (0, limit))
                    if used > prev_used:
                        self.peak_state[(rule, window)] = (used, limit)

                if ban_left > 0:
                    raise RateLimitBanned(ban_left, rule, window)

        # Re-evaluate which interval applies given the latest headers.
        # In practice this only matters on the first response of a run
        # (locks in tight vs loose), but it's cheap to do every time.
        self._set_interval_from_headers()

    def search(self, base: str, min_ilvl: int, currency: str) -> dict | None:
        self._pace()
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
            log.warning(f"  search network error [{base}/{currency}]: {e}")
            return None
        self._check_rate_warnings(r, "search")
        if r.status_code in (401, 403):
            raise TradeAuthError(
                f"search HTTP {r.status_code} on {base}/{currency}: "
                f"{r.text[:200]}"
            )
        if r.status_code != 200:
            log.warning(
                f"  search HTTP {r.status_code} [{base}/{currency}]: "
                f"{r.text[:150]}"
            )
            return None
        return r.json()

    def fetch(self, query_id: str, hashes: list) -> list | None:
        """Returns the listing list on success, [] when there's nothing to
        fetch, or None when an HTTP/network error occurred (so the caller
        can distinguish 'empty' from 'errored')."""
        if not hashes:
            return []
        url = f"{TRADE_BASE}/fetch/{','.join(hashes[:10])}"
        try:
            r = self.session.get(url, params={"query": query_id}, timeout=30)
        except requests.RequestException as e:
            log.warning(f"  fetch network error: {e}")
            return None
        self._check_rate_warnings(r, "fetch")
        if r.status_code in (401, 403):
            raise TradeAuthError(
                f"fetch HTTP {r.status_code}: {r.text[:200]}"
            )
        if r.status_code != 200:
            log.warning(f"  fetch HTTP {r.status_code}: {r.text[:150]}")
            return None
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
        """Populate self.rates_to_exalt for chaos, divine, annul.

        On HTTP 429 we wait SCOUT_RETRY_WAIT and retry, drawing from a single
        retry budget shared across all three currencies — poe2scout meters them
        against one IP/endpoint bucket, so a per-currency budget would only burn
        extra waits. Once the budget is exhausted, a further 429 is treated like
        any other non-200 (logged, rate left None)."""
        retries_left = SCOUT_MAX_RETRIES
        for currency in ["chaos", "divine", "annul"]:
            url = (
                f"{SCOUT_BASE}/{SCOUT_REALM_PATH}"
                f"/Leagues/{quote(self.league)}/Currencies/{currency}"
            )
            while True:
                try:
                    r = self.session.get(
                        url, params={"ReferenceCurrency": "exalted"}, timeout=30
                    )
                except requests.RequestException as e:
                    log.warning(f"  rate fetch error [{currency}]: {e}")
                    self.rates_to_exalt[currency] = None
                    break
                if r.status_code == 429 and retries_left > 0:
                    retries_left -= 1
                    log.warning(
                        f"  rate fetch HTTP 429 [{currency}]; waiting "
                        f"{SCOUT_RETRY_WAIT}s ({retries_left} retries left)"
                    )
                    time.sleep(SCOUT_RETRY_WAIT)
                    continue  # re-request the same currency
                if r.status_code != 200:
                    log.warning(
                        f"  rate fetch HTTP {r.status_code} [{currency}]: "
                        f"{r.text[:150]}"
                    )
                    self.rates_to_exalt[currency] = None
                    break
                try:
                    data = r.json()
                    rate = data.get("CurrentPrice")
                except ValueError:
                    rate = None
                if rate is None:
                    log.warning(f"  no CurrentPrice for {currency}")
                    self.rates_to_exalt[currency] = None
                else:
                    self.rates_to_exalt[currency] = float(rate)
                    print(f"  rate: 1 {currency} = {rate:.4f} exalted")
                break

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
    exalt_values: list[float] = []
    total_listings = 0  # sum of search.total across queried currencies
    errored = False  # True if any HTTP/network call failed for this item
    auto_skipped: list[str] = []  # currencies short-circuited by price floor

    for currency in CURRENCIES:
        # Auto-skip: if we already have enough cheap listings to fill our
        # top-N, and the cheapest possible listing in this currency (one
        # whole unit, since trades are quoted in whole units of the chosen
        # currency at minimum) costs more in exalts than our current N-th
        # cheapest, no listing in this currency could enter the top-N.
        # Skip the API call.
        if len(exalt_values) >= TOP_N_COMBINED:
            rate = converter.rates_to_exalt.get(currency)
            if rate is not None:
                nth_cheapest = sorted(exalt_values)[TOP_N_COMBINED - 1]
                if nth_cheapest < rate:
                    auto_skipped.append(currency)
                    continue

        search = client.search(base, min_ilvl, currency)
        if search is None:
            errored = True
            continue
        total_listings += search.get("total", 0)
        hashes = (search.get("result") or [])[:TOP_N_PER_CURRENCY]
        if not hashes:
            continue
        listings = client.fetch(search["id"], hashes)
        if listings is None:
            errored = True
            continue
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

    # Price = second-cheapest converted listing, which skips a single lowball/joke
    # listing. In a thin market (fewer than SECOND_LOWEST_MIN_LISTINGS total
    # listings) there isn't enough depth to trust a second, so use the genuine
    # cheapest. Falls back to the cheapest if only one listing was actually priced.
    if not exalt_values:
        median = None
    elif total_listings < SECOND_LOWEST_MIN_LISTINGS:
        median = exalt_values[0]
    else:
        median = exalt_values[1] if len(exalt_values) >= 2 else exalt_values[0]

    return {
        "base": base,
        "min_ilvl": min_ilvl,
        "median_exalts": median,
        "num_listings": total_listings,
        "errored": errored,
        "auto_skipped": auto_skipped,
    }


def run_item(client: TradeClient, converter: CurrencyConverter,
             item: dict) -> dict:
    """process_item wrapped with the single RateLimitBanned sleep+retry, shared
    by the main loop and the end-of-run retry round. A second ban during the
    retry — or any TradeAuthError — propagates to main()'s handler."""
    try:
        return process_item(client, converter, item)
    except RateLimitBanned as ban:
        msg = (
            f"banned on {ban.rule} {ban.window}s tier; "
            f"sleeping {ban.seconds}s + 5s buffer"
        )
        log.warning(f"🚫 {msg}")
        notify(f"⏸ PoE2 tracker paused: {msg}")
        time.sleep(ban.seconds + 5)
        # Retry the same item once the lockout clears. A second ban during
        # retry propagates so the run aborts cleanly.
        return process_item(client, converter, item)


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

    # latest.json carries the per-run poe2scout rates alongside the item
    # medians so a downstream consumer can re-express exalt prices in any
    # currency without opening prices.db. rates is converter.rates_to_exalt,
    # i.e. {"exalted": 1.0, "chaos": ..., "divine": ..., "annul": ...} with
    # None for any currency whose rate fetch failed this run.
    slim = {
        "rates_to_exalt": rates,
        "items": [
            {"base": r["base"], "min_ilvl": r["min_ilvl"],
             "median_exalts": r["median_exalts"]}
            for r in results
        ],
    }
    with open(latest_path, "w") as f:
        json.dump(slim, f, indent=2)


def write_errors_file(errored: list, league: str, path: Path) -> None:
    """Write a local, gitignored report of bases that still errored after the
    retry round, so a partial run leaves a breadcrumb of what to investigate."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "league": league,
        "items": [{"base": r["base"], "min_ilvl": r["min_ilvl"]}
                  for r in errored],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


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
            log.warning(
                f"  notify: Telegram returned HTTP {r.status_code}: "
                f"{r.text[:150]}"
            )
    except requests.RequestException as e:
        log.warning(f"  notify: send failed: {e}")


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
        log.error("ERROR: POESESSID env var not set.")
        log.error("Set it via: export POESESSID=<your_session_id>")
        notify("✗ PoE2 tracker: POESESSID env var not set")
        return 1

    if not ITEMS_PATH.exists():
        log.error(f"ERROR: {ITEMS_PATH} not found.")
        notify(f"✗ PoE2 tracker: {ITEMS_PATH.name} not found")
        return 1

    with open(ITEMS_PATH) as f:
        items = json.load(f)

    print(f"Tracking {len(items)} items in league '{LEAGUE}'\n")

    print("Fetching currency rates from poe2scout...")
    converter = CurrencyConverter(LEAGUE)
    converter.fetch_rates()
    if all(v is None for k, v in converter.rates_to_exalt.items() if k != "exalted"):
        msg = ("no currency rates fetched from poe2scout. "
               "Check SCOUT_REALM_PATH and league name.")
        log.error(f"ERROR: {msg}")
        notify(f"✗ PoE2 tracker: {msg}")
        return 1
    print()

    # Telegram heads-up with the freshly-fetched exchange rates.
    rate_bits = [
        f"1 {cur} = {converter.rates_to_exalt[cur]:.4g} ex"
        if converter.rates_to_exalt.get(cur) is not None else f"1 {cur} = n/a"
        for cur in ("chaos", "divine", "annul")
    ]
    notify(f"📊 PoE2 {LEAGUE} rates — " + ", ".join(rate_bits))

    client = TradeClient(LEAGUE, POESESSID, USER_AGENT)
    # Bases already explicitly configured at FANOUT_LOWER_ILVL — used to
    # suppress fan-out when the user has their own ilvl-80 row for that base.
    configured_lower = {
        item["base"] for item in items
        if item.get("min_ilvl") == FANOUT_LOWER_ILVL
    }
    results = []

    def print_result_line(result: dict, indent: str = "    ") -> None:
        skip_str = ""
        if result["auto_skipped"]:
            skip_str = f"  [auto-skipped: {', '.join(result['auto_skipped'])}]"
        if result["errored"]:
            print(f"{indent}→ HTTP error during fetch{skip_str}")
        elif result["median_exalts"] is not None:
            print(f"{indent}→ {result['median_exalts']:.2f} ex  "
                  f"({result['num_listings']} total listings){skip_str}")
        else:
            print(f"{indent}→ no listings{skip_str}")

    try:
        for i, item in enumerate(items, 1):
            state_suffix = ""
            if client.last_state:
                (rule, window), (used, limit) = max(
                    client.last_state.items(),
                    key=lambda kv: kv[1][0] / max(kv[1][1], 1),
                )
                state_suffix = (
                    f"  [{rule} {window}s: {used}/{limit} ({used/limit:.0%})]"
                )
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{i}/{len(items)}] {ts}  {item['base']} "
                f"ilvl≥{item['min_ilvl']}{state_suffix}"
            )
            result = run_item(client, converter, item)
            print_result_line(result)
            results.append(result)

            # Fan-out: an ilvl-82 result triggers a complementary ilvl-80
            # lookup in two cases:
            #   (1) price > 1 divine — the base is valuable enough that
            #       the ilvl-80 drop (which drops more often) is worth
            #       tracking for the filter.
            #   (2) price is None — the base had no listings at 82, so
            #       try 80 to find ANY listings before declaring it
            #       no-data.
            # Skipped in either case if the user already has (base, 80)
            # in items.json (explicit config wins) or if the 82 row
            # errored (we don't chain failures).
            fanout_reason: str | None = None
            divine_rate = converter.rates_to_exalt.get("divine")
            if (
                item["min_ilvl"] == 82
                and not result["errored"]
                and item["base"] not in configured_lower
            ):
                if (
                    result["median_exalts"] is not None
                    and divine_rate is not None
                    and result["median_exalts"]
                        > FANOUT_82_TO_80_DIVINE_THRESHOLD * divine_rate
                ):
                    fanout_reason = "82 price > 1 divine"
                elif result["median_exalts"] is None:
                    fanout_reason = "82 had no listings"

            if fanout_reason:
                fanout_item = {"base": item["base"],
                               "min_ilvl": FANOUT_LOWER_ILVL}
                print(f"     ↳ {item['base']} ilvl≥{FANOUT_LOWER_ILVL} "
                      f"(fan-out: {fanout_reason})")
                fanout_result = run_item(client, converter, fanout_item)
                print_result_line(fanout_result, indent="       ")
                results.append(fanout_result)

            print()  # trailing blank line between items

        # Second chance: items that hit an HTTP/network error on the first
        # pass often recover after a short pause. Collect them, wait once, and
        # re-run each failed query. We rebuild the query from the result dict
        # (process_item needs only base + min_ilvl), so fan-out rows are
        # covered too; we deliberately don't re-trigger fan-out for an 82 row
        # that newly succeeds on retry — a missing fan-out row is picked up
        # next run.
        errored_first = [r for r in results if r["errored"]]
        if errored_first:
            names = ", ".join(r["base"] for r in errored_first[:5])
            if len(errored_first) > 5:
                names += f", +{len(errored_first) - 5} more"
            log.warning(
                f"{len(errored_first)} item(s) errored on first pass "
                f"[{names}]; retrying after {TRADE_RETRY_WAIT}s"
            )
            time.sleep(TRADE_RETRY_WAIT)
            for idx, r in enumerate(results):
                if not r["errored"]:
                    continue
                retry_item = {"base": r["base"], "min_ilvl": r["min_ilvl"]}
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"↻ {ts}  retry {retry_item['base']} "
                      f"ilvl≥{retry_item['min_ilvl']}")
                results[idx] = run_item(client, converter, retry_item)
                print_result_line(results[idx])
                print()
    except TradeAuthError as e:
        msg = (
            f"trade API returned auth error ({e}). Causes (in order of "
            "likelihood): (1) source IP is on GGG's anti-scraping blocklist "
            "-- common for cloud-provider IPs, won't happen on a residential "
            "connection; (2) POESESSID is stale -- re-grab from browser "
            "devtools."
        )
        log.error(f"ERROR: {msg}")
        notify(f"✗ PoE2 tracker: trade API forbidden (likely IP-blocked).")
        return 1

    # After the retry round, decide what to write. Items that still errored are
    # dropped from this run's outputs and recorded in a local errors.json. If
    # at least one item is good we write partial outputs and exit 0, so the
    # wrapper commits/publishes what we have; if EVERY item errored we keep the
    # last good data instead of publishing an empty latest.json.
    errored = [r for r in results if r["errored"]]
    good = [r for r in results if not r["errored"]]

    if errored:
        names = ", ".join(f"{r['base']}" for r in errored[:5])
        if len(errored) > 5:
            names += f", +{len(errored) - 5} more"
        write_errors_file(errored, LEAGUE, ERRORS_PATH)
        if not good:
            msg = (f"all {len(results)} item(s) still errored after retry "
                   f"[{names}]. Keeping last good data, not writing outputs.")
            log.error(f"ERROR: {msg}")
            notify(f"✗ PoE2 tracker: {msg}")
            return 1
        msg = (f"{len(errored)}/{len(results)} item(s) still errored after "
               f"retry [{names}]; writing partial outputs.")
        log.warning(msg)
        notify(f"⚠ PoE2 {LEAGUE}: partial run, {len(errored)} base(s) "
               f"errored [{names}]")
    else:
        # Clean run: clear any stale report so errors.json always reflects the
        # most recent run.
        ERRORS_PATH.unlink(missing_ok=True)

    write_outputs(
        good, converter.rates_to_exalt, LEAGUE, DB_PATH, LATEST_PATH
    )
    print(f"✓ Wrote {len(good)} rows to {DB_PATH.name}")
    print(f"✓ Wrote {LATEST_PATH.name}")
    if errored:
        print(f"⚠ {len(errored)} base(s) errored — see {ERRORS_PATH.name}")
    else:
        notify(build_success_message(good, LEAGUE))
    if client.peak_state:
        peaks = ", ".join(
            f"{r} {w}s={u}/{l} ({u/l:.0%})"
            for (r, w), (u, l) in sorted(client.peak_state.items())
        )
        print(f"Peak rate-limit usage: {peaks}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        # Unhandled crash — record the traceback to the log file, best-effort
        # notify, then re-raise so the traceback still prints and the process
        # exits non-zero.
        try:
            log.exception("unhandled crash")
        except Exception:
            pass
        try:
            notify(f"✗ PoE2 tracker crashed: {type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
