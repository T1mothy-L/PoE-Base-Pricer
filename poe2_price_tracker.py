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

# Currencies are queried in ascending order of unit value so the cheap
# currencies populate the top-N first. After each query we check whether
# the next currency could possibly contribute a listing to the cheapest-N:
# if the N-th cheapest exalt-equivalent we have is already lower than the
# next currency's exalt rate, no listing in that currency can fit, so we
# skip the query. The previous manual `exclude` field is gone — auto-skip
# subsumes every case it covered.
CURRENCIES = ["exalted", "chaos", "annul", "divine"]
TOP_N_PER_CURRENCY = 10        # cheapest hashes pulled per currency
TOP_N_COMBINED = 10            # cheapest after conversion to exalts

# Auto-fan-out: when an ilvl-82 item's median exceeds this many divines, do
# a follow-up search at ilvl 80 too. The idea is that a chase base worth a
# divine at ilvl 82 is probably still tradeable at ilvl 80 (which drops
# meaningfully more often), so we want filter-rule data for both. Fan-out
# is skipped if the user already has (base, 80) in items.json — their
# explicit config wins.
FANOUT_82_TO_80_DIVINE_THRESHOLD = 1.0
FANOUT_LOWER_ILVL = 80

# Initial/floor interval between any two API requests (search + fetch share
# this budget under GGG's `Ip` rule). Autotune may RAISE this based on
# observed headers; it never drops below this value.
SEARCH_INTERVAL_SECONDS = 6.5

# Of the smallest tier GGG advertises, what fraction we aim to consume.
# 0.8 = use up to 80% of the bucket on a sustained basis. Autotune sets
# self.search_interval = (window / limit) / AUTOTUNE_TARGET_FRACTION for
# whichever tier yields the most restrictive value.
AUTOTUNE_TARGET_FRACTION = 0.8

# Warn (just print, no throttle) when any tier crosses this fraction of
# its limit. Mirrors AUTOTUNE_TARGET_FRACTION by design — if the
# autotune is working, we should be steady-state below this and only see
# warnings when external contention (browser, other clients on the IP)
# pushes us over.
RATE_LIMIT_WARN_FRACTION = 0.8

# Panic-sleep when any tier crosses this fraction. Adds a one-shot hard
# pause to the next _pace() call, regardless of the running autotune
# interval. Circuit-breaker for when autotune lag + external contention
# combine to threaten a ban.
PANIC_FRACTION = 0.9
PANIC_SLEEP_SECONDS = 10.0

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
        # against the same per-IP bucket. Was `_last_search_at`.
        self._last_request_at = 0.0
        # Autotune output; starts at the configured floor. Updated by
        # _retune_pacing() after every response that came back with
        # parseable rate-limit headers.
        self.search_interval = SEARCH_INTERVAL_SECONDS
        # Panic deadline (epoch seconds). Next _pace() waits until at
        # least this point before proceeding, regardless of
        # search_interval. Set by _check_rate_warnings when any tier
        # crosses PANIC_FRACTION.
        self._panic_until = 0.0
        self.last_state: dict[tuple[str, int], tuple[int, int]] = {}
        self.peak_state: dict[tuple[str, int], tuple[int, int]] = {}

    def _pace(self) -> None:
        """Sleep until enough time has elapsed since the previous request
        (per self.search_interval) AND until any active panic-sleep
        deadline has passed -- whichever is later."""
        now = time.time()
        target = max(
            self._last_request_at + self.search_interval,
            self._panic_until,
        )
        wait = target - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.time()

    def _retune_pacing(self) -> None:
        """Set self.search_interval to the most restrictive value implied
        by the rate-limit tiers GGG currently advertises. Never goes
        BELOW the configured floor (SEARCH_INTERVAL_SECONDS), so the
        autotune can only slow us down vs the constant -- never speed us
        up past what's safe to assume before any header has been seen."""
        if not self.last_state:
            return
        candidates = [
            (window / limit) / AUTOTUNE_TARGET_FRACTION
            for (_rule, window), (_used, limit) in self.last_state.items()
            if limit > 0
        ]
        if candidates:
            self.search_interval = max(SEARCH_INTERVAL_SECONDS, *candidates)

    def _check_rate_warnings(self, response: requests.Response, label: str) -> None:
        """Parse X-Rate-Limit-* headers; update last/peak state, warn at
        80% of any tier, and raise RateLimitBanned if any tier shows an
        active lockout."""
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
                if limit > 0 and used >= limit * RATE_LIMIT_WARN_FRACTION:
                    print(
                        f"  ⚠️  rate warning [{label}] {rule} "
                        f"window={window}s used={used}/{limit} "
                        f"({used/limit:.0%})",
                        file=sys.stderr,
                    )
                if limit > 0 and used >= limit * PANIC_FRACTION:
                    # Only trigger once per panic window; if we're
                    # already inside one, don't compound multiple 90%+
                    # observations (sibling tiers in the same response,
                    # or consecutive responses while the deadline is
                    # still in the future).
                    now = time.time()
                    if now >= self._panic_until:
                        self._panic_until = now + PANIC_SLEEP_SECONDS
                        print(
                            f"  ⏸ panic-sleep [{label}] {rule} "
                            f"{window}s at {used}/{limit} "
                            f"({used/limit:.0%}); next request gated "
                            f"+{PANIC_SLEEP_SECONDS}s",
                            file=sys.stderr,
                        )

        # Reflect the latest headers in the autotune so the NEXT
        # outgoing request paces against the freshest view of GGG's
        # advertised limits.
        self._retune_pacing()

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
            print(f"  search network error [{base}/{currency}]: {e}", file=sys.stderr)
            return None
        self._check_rate_warnings(r, "search")
        if r.status_code in (401, 403):
            raise TradeAuthError(
                f"search HTTP {r.status_code} on {base}/{currency}: "
                f"{r.text[:200]}"
            )
        if r.status_code != 200:
            print(
                f"  search HTTP {r.status_code} [{base}/{currency}]: "
                f"{r.text[:150]}",
                file=sys.stderr,
            )
            return None
        return r.json()

    def fetch(self, query_id: str, hashes: list) -> list | None:
        """Returns the listing list on success, [] when there's nothing to
        fetch, or None when an HTTP/network error occurred (so the caller
        can distinguish 'empty' from 'errored')."""
        if not hashes:
            return []
        # Same per-IP budget as search -- pace identically so the
        # autotune math actually balances. Was unpaced previously.
        self._pace()
        url = f"{TRADE_BASE}/fetch/{','.join(hashes[:10])}"
        try:
            r = self.session.get(url, params={"query": query_id}, timeout=30)
        except requests.RequestException as e:
            print(f"  fetch network error: {e}", file=sys.stderr)
            return None
        self._check_rate_warnings(r, "fetch")
        if r.status_code in (401, 403):
            raise TradeAuthError(
                f"fetch HTTP {r.status_code}: {r.text[:200]}"
            )
        if r.status_code != 200:
            print(f"  fetch HTTP {r.status_code}: {r.text[:150]}", file=sys.stderr)
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
    cheapest = exalt_values[:TOP_N_COMBINED]
    median = statistics.median(cheapest) if cheapest else None

    # Thin-market outlier guard: fewer than 10 listings and median > 3× cheapest
    # signals price-joking. Use 1.5× the cheapest listing as a conservative estimate.
    if (
        cheapest
        and median is not None
        and len(cheapest) < TOP_N_COMBINED
        and median > 3 * cheapest[0]
    ):
        median = 1.5 * cheapest[0]

    return {
        "base": base,
        "min_ilvl": min_ilvl,
        "median_exalts": median,
        "num_listings": total_listings,
        "errored": errored,
        "auto_skipped": auto_skipped,
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
            try:
                result = process_item(client, converter, item)
            except RateLimitBanned as ban:
                msg = (
                    f"banned on {ban.rule} {ban.window}s tier; "
                    f"sleeping {ban.seconds}s + 5s buffer"
                )
                print(f"  🚫 {msg}", file=sys.stderr)
                notify(f"⏸ PoE2 tracker paused: {msg}")
                time.sleep(ban.seconds + 5)
                # Retry the same item once the lockout clears. A second
                # ban during retry propagates so the run aborts cleanly.
                result = process_item(client, converter, item)
            print_result_line(result)
            results.append(result)

            # Fan-out: ilvl-82 results above 1 divine get a complementary
            # ilvl-80 lookup. Skipped if the user has (base, 80) configured
            # already, or if we can't tell the divine rate.
            divine_rate = converter.rates_to_exalt.get("divine")
            if (
                item["min_ilvl"] == 82
                and not result["errored"]
                and result["median_exalts"] is not None
                and divine_rate is not None
                and result["median_exalts"]
                    > FANOUT_82_TO_80_DIVINE_THRESHOLD * divine_rate
                and item["base"] not in configured_lower
            ):
                fanout_item = {"base": item["base"],
                               "min_ilvl": FANOUT_LOWER_ILVL}
                print(f"     ↳ {item['base']} ilvl≥{FANOUT_LOWER_ILVL} "
                      "(fan-out: 82 median > 1 divine)")
                fanout_result = process_item(client, converter, fanout_item)
                print_result_line(fanout_result, indent="       ")
                results.append(fanout_result)

            print()  # trailing blank line between items
    except TradeAuthError as e:
        msg = (
            f"trade API returned auth error ({e}). Causes (in order of "
            "likelihood): (1) source IP is on GGG's anti-scraping blocklist "
            "-- common for cloud-provider IPs, won't happen on a residential "
            "connection; (2) POESESSID is stale -- re-grab from browser "
            "devtools."
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        notify(f"✗ PoE2 tracker: trade API forbidden (likely IP-blocked).")
        return 1

    # Refuse to overwrite latest.json / prices.db if any item errored. We'd
    # rather keep yesterday's good data than commit a partially-corrupt run.
    errored = [r for r in results if r["errored"]]
    if errored:
        names = ", ".join(f"{r['base']}" for r in errored[:5])
        if len(errored) > 5:
            names += f", +{len(errored) - 5} more"
        msg = (f"{len(errored)}/{len(results)} item(s) hit HTTP errors "
               f"[{names}]. Not writing outputs.")
        print(f"ERROR: {msg}", file=sys.stderr)
        notify(f"✗ PoE2 tracker: {msg}")
        return 1

    write_outputs(
        results, converter.rates_to_exalt, LEAGUE, DB_PATH, LATEST_PATH
    )
    print(f"✓ Wrote {len(results)} rows to {DB_PATH.name}")
    print(f"✓ Wrote {LATEST_PATH.name}")
    notify(build_success_message(results, LEAGUE))
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
        # Unhandled crash — best-effort notify, then re-raise so the
        # traceback still prints and the process exits non-zero.
        try:
            notify(f"✗ PoE2 tracker crashed: {type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
