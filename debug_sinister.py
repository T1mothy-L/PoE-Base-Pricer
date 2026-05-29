"""
Step-by-step debug script for Sinister Quarterstaff ilvl-82 pricing.
Usage:  POESESSID=xxx python3 debug_sinister.py
"""
import json, os, sys, time
import requests
from urllib.parse import quote

load_dotenv_ok = False
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent / ".env")
    load_dotenv_ok = True
except ImportError:
    pass

POESESSID = os.environ.get("POESESSID")
LEAGUE    = os.environ.get("POE2_LEAGUE", "Standard")
BASE      = "Sinister Quarterstaff"
MIN_ILVL  = 82

TRADE_BASE  = "https://www.pathofexile.com/api/trade2"
SCOUT_BASE  = "https://poe2scout.com/api/poe2"
CURRENCIES  = ["exalted", "chaos", "annul", "divine"]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 PoE2DebugScript/0.1",
    "Content-Type": "application/json",
    "Accept": "application/json",
})
if POESESSID:
    session.cookies.set("POESESSID", POESESSID, domain=".pathofexile.com")
else:
    print("WARNING: POESESSID not set — trade API calls will fail.\n")

# ── Step 1: Currency rates ────────────────────────────────────────────────────
print("=" * 60)
print("STEP 1 — poe2scout currency rates")
print("=" * 60)
rates = {"exalted": 1.0}
for cur in ["chaos", "divine", "annul"]:
    url = f"{SCOUT_BASE}/Leagues/{quote(LEAGUE)}/Currencies/{cur}"
    print(f"\nGET {url}?ReferenceCurrency=exalted")
    r = session.get(url, params={"ReferenceCurrency": "exalted"}, timeout=30)
    print(f"  HTTP {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Full response: {json.dumps(data, indent=2)}")
        rates[cur] = data.get("CurrentPrice")
    else:
        print(f"  Body: {r.text[:300]}")
        rates[cur] = None
print(f"\nRates: {rates}")

# ── Step 2: Search + fetch per currency ──────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 2 — trade API search + fetch per currency")
print("=" * 60)

all_prices = []   # (currency, amount, exalt_value)

for currency in CURRENCIES:
    print(f"\n── Currency: {currency} ─────────────────────")
    body = {
        "query": {
            "status": {"option": "securable"},
            "type": BASE,
            "stats": [{"type": "and", "filters": []}],
            "filters": {
                "type_filters": {"filters": {
                    "rarity": {"option": "normal"},
                    "ilvl": {"min": MIN_ILVL},
                }},
                "misc_filters": {"filters": {"corrupted": {"option": "false"}}},
                "trade_filters": {"filters": {"price": {"option": currency}}},
            },
        },
        "sort": {"price": "asc"},
    }
    search_url = f"{TRADE_BASE}/search/poe2/{quote(LEAGUE)}"
    print(f"POST {search_url}")
    print(f"Body: {json.dumps(body, indent=2)}")
    time.sleep(7)  # respect rate limit

    sr = session.post(search_url, data=json.dumps(body), timeout=30)
    print(f"\nHTTP {sr.status_code}")
    if sr.status_code != 200:
        print(f"Error body: {sr.text[:400]}")
        continue

    sdata = sr.json()
    total = sdata.get("total", 0)
    hashes = (sdata.get("result") or [])[:10]
    query_id = sdata.get("id", "")
    print(f"total listings: {total}")
    print(f"first {len(hashes)} hashes: {hashes}")

    if not hashes:
        print("No results — skipping fetch.")
        continue

    fetch_url = f"{TRADE_BASE}/fetch/{','.join(hashes)}"
    print(f"\nGET {fetch_url}?query={query_id}")
    fr = session.get(fetch_url, params={"query": query_id}, timeout=30)
    print(f"HTTP {fr.status_code}")
    if fr.status_code != 200:
        print(f"Error body: {fr.text[:400]}")
        continue

    listings = fr.json().get("result") or []
    print(f"\nListings ({len(listings)}):")
    for i, lst in enumerate(listings, 1):
        price = (lst.get("listing") or {}).get("price") or {}
        amt = price.get("amount")
        cur_tag = price.get("currency")
        rate = rates.get(cur_tag)
        ex_val = amt * rate if (amt is not None and rate) else None
        print(f"  [{i}] {amt} {cur_tag}  →  {ex_val:.2f} exalts"
              if ex_val is not None else f"  [{i}] {amt} {cur_tag}  (no rate)")
        if ex_val is not None:
            all_prices.append((cur_tag, amt, ex_val))

# ── Step 3: Median calculation ────────────────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 3 — median of cheapest 10")
print("=" * 60)
all_prices.sort(key=lambda x: x[2])
top10 = all_prices[:10]
print(f"\nAll collected prices sorted ({len(all_prices)} total):")
for i, (cur, amt, ex_val) in enumerate(all_prices, 1):
    marker = " <-- median candidate" if i <= 10 else ""
    print(f"  [{i}] {amt} {cur} = {ex_val:.2f} ex{marker}")

if top10:
    import statistics
    exalt_vals = [x[2] for x in top10]
    median = statistics.median(exalt_vals)
    print(f"\nMedian of cheapest {len(top10)}: {median:.4f} exalts")
    print(f"In divine: {median / rates.get('divine', 1):.4f}")
else:
    print("No listings — no median.")
