#!/usr/bin/env python3
"""
Beach Holiday Bot — daily scanner for cheap 3-4 day beach trips from Vienna.

Stack: Travelpayouts (Aviasales v3, cached) for live flight prices,
       built-in seasonal nightly-rate estimates for hotels (Hotellook API was
       shut down Oct 2025) with date-prefilled Booking.com links,
       Telegram for alerts, GitHub Actions for scheduling.
Sibling of the Istanbul bot and the worldwide flight bot. Free, no paid APIs.

Logic per run:
  1. Build candidate weekends: the next SCAN_WEEKENDS Thursdays and Fridays.
  2. For every (origin x destination x weekend x trip length) combo, fetch the
     cheapest cached round-trip flight price.
  3. Estimate hotel cost from the destination's seasonal nightly rate.
  4. Total = flight RT + nights x nightly rate / HOTEL_SPLIT (set HOTEL_SPLIT=2
     when sharing a double room; default 1 = solo).
  5. Alert when a total beats the destination target, the global threshold, or
     drops ROLLING_DROP_PCT below the destination's rolling average.
  6. Send one Telegram digest, cheapest totals first, grouped by weekend.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- config ----

TP_TOKEN = (os.environ.get("TP_TOKEN") or "")
TG_TOKEN = (os.environ.get("TG_TOKEN") or "")
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "")

ORIGINS = [o.strip().upper() for o in (os.environ.get("ORIGINS") or "VIE,BTS").split(",") if o.strip()]

# Scan the next N weekends (a "weekend" = one Thursday departure + one Friday departure)
SCAN_WEEKENDS = int((os.environ.get("SCAN_WEEKENDS") or "8"))

# Trip lengths in nights to test for each departure day
TRIP_NIGHTS = [int(n) for n in (os.environ.get("TRIP_NIGHTS") or "3,4,5").split(",")]

# Global fallback: alert on any total below this (EUR, per person)
TOTAL_THRESHOLD_EUR = float((os.environ.get("TOTAL_THRESHOLD_EUR") or "250"))

# Rolling-average drop alert (0.25 = 25% below the destination's recent average)
ROLLING_DROP_PCT = float((os.environ.get("ROLLING_DROP_PCT") or "0.25"))
ROLLING_WINDOW = int((os.environ.get("ROLLING_WINDOW") or "14"))  # days of history

# Hotel settings
HOTEL_SPLIT = int((os.environ.get("HOTEL_SPLIT") or "1"))     # 2 = sharing a double room

MAX_DIGEST_DEALS = int((os.environ.get("MAX_DIGEST_DEALS") or "15"))
# Compact market overview appended to every digest: best total per destination,
# cheapest first. Set 0 to disable. 73 = all destinations (observation mode).
OVERVIEW_N = int((os.environ.get("OVERVIEW_N") or "73"))

# Send a digest of the cheapest totals even when nothing beats a target
ALWAYS_DIGEST = (os.environ.get("ALWAYS_DIGEST") or "true").lower() in ("1", "true", "yes")
PACE_SECONDS = float((os.environ.get("PACE_SECONDS") or "0.2"))
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in ("1", "true", "yes")
SILENT_REFRESH = (os.environ.get("SILENT_REFRESH") or "").lower() in ("1", "true", "yes")

# Link shown at the top of each digest so you can compare everything in the browser.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL") or "https://rizabalci.github.io/beach-holiday-bot-telegram/dashboard.html"

HISTORY_FILE = "price_history.json"
SITE_DATA_FILE = "deals.json"

CURRENCY = "eur"

# ------------------------------------------------------- destinations -------
# IATA: (display name, country, booking.com city string,
#        nightly EUR low season, nightly EUR high season, target total EUR)
# High season = Jun-Sep (+ Dec-Feb for Canaries/Egypt). Nightly rates are
# budget-3-star estimates for a solo traveller; tune them as real trips
# calibrate the model. Targets are the signal-to-noise dial.

BEACH_DESTINATIONS = {
    # Spain + islands
    "PMI": ("Palma de Mallorca", "\U0001F1EA\U0001F1F8 Spain", "Palma de Mallorca", 55, 95, 260),
    "IBZ": ("Ibiza", "\U0001F1EA\U0001F1F8 Spain", "Ibiza", 70, 130, 320),
    "AGP": ("Malaga / Costa del Sol", "\U0001F1EA\U0001F1F8 Spain", "Malaga", 50, 85, 250),
    "ALC": ("Alicante / Costa Blanca", "\U0001F1EA\U0001F1F8 Spain", "Alicante", 45, 80, 240),
    "VLC": ("Valencia", "\U0001F1EA\U0001F1F8 Spain", "Valencia", 50, 80, 240),
    "BCN": ("Barcelona", "\U0001F1EA\U0001F1F8 Spain", "Barcelona", 70, 105, 280),
    "TFS": ("Tenerife", "\U0001F1EA\U0001F1F8 Spain", "Tenerife", 50, 85, 320),
    "LPA": ("Gran Canaria", "\U0001F1EA\U0001F1F8 Spain", "Las Palmas de Gran Canaria", 50, 85, 320),
    "FUE": ("Fuerteventura", "\U0001F1EA\U0001F1F8 Spain", "Fuerteventura", 50, 85, 320),
    # Greece
    "ATH": ("Athens / Riviera", "\U0001F1EC\U0001F1F7 Greece", "Glyfada", 55, 85, 240),
    "SKG": ("Thessaloniki / Halkidiki", "\U0001F1EC\U0001F1F7 Greece", "Thessaloniki", 45, 70, 230),
    "HER": ("Heraklion, Crete", "\U0001F1EC\U0001F1F7 Greece", "Heraklion", 45, 80, 270),
    "CHQ": ("Chania, Crete", "\U0001F1EC\U0001F1F7 Greece", "Chania", 50, 90, 280),
    "RHO": ("Rhodes", "\U0001F1EC\U0001F1F7 Greece", "Rhodes", 45, 85, 280),
    "KGS": ("Kos", "\U0001F1EC\U0001F1F7 Greece", "Kos", 45, 80, 270),
    "JTR": ("Santorini", "\U0001F1EC\U0001F1F7 Greece", "Santorini", 80, 150, 380),
    "JMK": ("Mykonos", "\U0001F1EC\U0001F1F7 Greece", "Mykonos", 90, 170, 400),
    "CFU": ("Corfu", "\U0001F1EC\U0001F1F7 Greece", "Corfu", 45, 85, 270),
    "ZTH": ("Zakynthos", "\U0001F1EC\U0001F1F7 Greece", "Zakynthos", 45, 85, 280),
    # Croatia + Adriatic
    "SPU": ("Split", "\U0001F1ED\U0001F1F7 Croatia", "Split", 55, 95, 260),
    "DBV": ("Dubrovnik", "\U0001F1ED\U0001F1F7 Croatia", "Dubrovnik", 65, 115, 300),
    "ZAD": ("Zadar", "\U0001F1ED\U0001F1F7 Croatia", "Zadar", 50, 85, 240),
    "PUY": ("Pula / Istria", "\U0001F1ED\U0001F1F7 Croatia", "Pula", 45, 80, 230),
    "TIV": ("Tivat / Kotor Bay", "\U0001F1F2\U0001F1EA Montenegro", "Kotor", 45, 85, 260),
    "TGD": ("Podgorica (coast 1h)", "\U0001F1F2\U0001F1EA Montenegro", "Budva", 35, 70, 230),
    "TIA": ("Tirana / Albanian Riviera", "\U0001F1E6\U0001F1F1 Albania", "Durres", 30, 55, 210),
    # Italy + Malta
    "NAP": ("Naples / Sorrento", "\U0001F1EE\U0001F1F9 Italy", "Sorrento", 60, 100, 280),
    "PMO": ("Palermo, Sicily", "\U0001F1EE\U0001F1F9 Italy", "Palermo", 50, 80, 250),
    "CTA": ("Catania, Sicily", "\U0001F1EE\U0001F1F9 Italy", "Catania", 50, 80, 250),
    "CAG": ("Cagliari, Sardinia", "\U0001F1EE\U0001F1F9 Italy", "Cagliari", 55, 90, 270),
    "OLB": ("Olbia, Sardinia", "\U0001F1EE\U0001F1F9 Italy", "Olbia", 60, 110, 300),
    "BRI": ("Bari / Puglia", "\U0001F1EE\U0001F1F9 Italy", "Bari", 50, 80, 240),
    "MLA": ("Malta", "\U0001F1F2\U0001F1F9 Malta", "Malta", 55, 95, 260),
    # Portugal
    "FAO": ("Faro / Algarve", "\U0001F1F5\U0001F1F9 Portugal", "Faro", 55, 95, 290),
    "LIS": ("Lisbon / Cascais", "\U0001F1F5\U0001F1F9 Portugal", "Cascais", 65, 100, 290),
    # Cyprus
    "LCA": ("Larnaca", "\U0001F1E8\U0001F1FE Cyprus", "Larnaca", 50, 85, 280),
    "PFO": ("Paphos", "\U0001F1E8\U0001F1FE Cyprus", "Paphos", 50, 85, 280),
    # Bulgaria
    "VAR": ("Varna", "\U0001F1E7\U0001F1EC Bulgaria", "Varna", 35, 65, 200),
    "BOJ": ("Burgas / Sunny Beach", "\U0001F1E7\U0001F1EC Bulgaria", "Burgas", 30, 60, 200),
    # Turkey coast
    # North Africa + Red Sea
    "HRG": ("Hurghada", "\U0001F1EA\U0001F1EC Egypt", "Hurghada", 35, 60, 300),
    "SSH": ("Sharm El Sheikh", "\U0001F1EA\U0001F1EC Egypt", "Sharm el-Sheikh", 35, 60, 300),
    "AGA": ("Agadir", "\U0001F1F2\U0001F1E6 Morocco", "Agadir", 40, 65, 300),
    "NBE": ("Hammamet / Enfidha", "\U0001F1F9\U0001F1F3 Tunisia", "Hammamet", 35, 60, 260),
    "DJE": ("Djerba", "\U0001F1F9\U0001F1F3 Tunisia", "Djerba", 35, 60, 280),
    # France
    "NCE": ("Nice / French Riviera", "\U0001F1EB\U0001F1F7 France", "Nice", 70, 110, 300),
    "MRS": ("Marseille / Calanques", "\U0001F1EB\U0001F1F7 France", "Marseille", 60, 90, 280),
    # Spain additions
    "MAH": ("Menorca", "\U0001F1EA\U0001F1F8 Spain", "Menorca", 55, 95, 290),
    "GRO": ("Girona / Costa Brava", "\U0001F1EA\U0001F1F8 Spain", "Lloret de Mar", 45, 75, 240),
    "ACE": ("Lanzarote", "\U0001F1EA\U0001F1F8 Spain", "Lanzarote", 50, 85, 320),
    # Italy additions
    "BDS": ("Brindisi / Salento", "\U0001F1EE\U0001F1F9 Italy", "Brindisi", 45, 80, 250),
    "SUF": ("Lamezia / Tropea", "\U0001F1EE\U0001F1F9 Italy", "Tropea", 45, 75, 250),
    "AHO": ("Alghero, Sardinia", "\U0001F1EE\U0001F1F9 Italy", "Alghero", 50, 85, 280),
    # Greece additions
    "EFL": ("Kefalonia", "\U0001F1EC\U0001F1F7 Greece", "Kefalonia", 50, 90, 290),
    "PVK": ("Preveza / Lefkada", "\U0001F1EC\U0001F1F7 Greece", "Lefkada", 45, 85, 280),
    "JSI": ("Skiathos", "\U0001F1EC\U0001F1F7 Greece", "Skiathos", 50, 95, 300),
    "KLX": ("Kalamata / Peloponnese", "\U0001F1EC\U0001F1F7 Greece", "Kalamata", 45, 80, 270),
    # Winter sun stretch
    "RMF": ("Marsa Alam", "\U0001F1EA\U0001F1EC Egypt", "Marsa Alam", 35, 60, 300),
    "TLV": ("Tel Aviv", "\U0001F1EE\U0001F1F1 Israel", "Tel Aviv", 90, 120, 380),
    "DXB": ("Dubai", "\U0001F1E6\U0001F1EA UAE", "Dubai", 60, 90, 450),
    # Coverage completions
    "OPO": ("Porto / Matosinhos", "\U0001F1F5\U0001F1F9 Portugal", "Matosinhos", 50, 80, 270),
    "RJK": ("Rijeka / Kvarner Bay", "\U0001F1ED\U0001F1F7 Croatia", "Opatija", 45, 80, 240),
    "TPS": ("Trapani / San Vito Lo Capo", "\U0001F1EE\U0001F1F9 Italy", "San Vito Lo Capo", 45, 80, 250),
    "RMI": ("Rimini / Adriatic Riviera", "\U0001F1EE\U0001F1F9 Italy", "Rimini", 45, 75, 240),
    "GOA": ("Genoa / Liguria", "\U0001F1EE\U0001F1F9 Italy", "Genoa", 55, 85, 270),
    "VCE": ("Venice / Lido & Jesolo", "\U0001F1EE\U0001F1F9 Italy", "Lido di Jesolo", 55, 90, 260),
    "KVA": ("Kavala / Thassos", "\U0001F1EC\U0001F1F7 Greece", "Kavala", 40, 70, 260),
    "VOL": ("Volos / Pelion", "\U0001F1EC\U0001F1F7 Greece", "Volos", 40, 70, 260),
    "SMI": ("Samos", "\U0001F1EC\U0001F1F7 Greece", "Samos", 40, 75, 290),
    "AOK": ("Karpathos", "\U0001F1EC\U0001F1F7 Greece", "Karpathos", 45, 80, 300),
}

# Destinations whose high season is winter sun (Nov-Mar) instead of summer
WINTER_SUN = {"TFS", "LPA", "FUE", "ACE", "HRG", "SSH", "RMF", "AGA", "DXB"}

# Static per-destination metadata for the dashboard: approx flight time from
# VIE (hours) and typical August sea temperature (°C). Used only for filtering
# and display; does not affect price logic.
DEST_META = {
    "PMI": (2.2, 26), "IBZ": (2.2, 26), "AGP": (3.0, 22), "ALC": (2.7, 26),
    "VLC": (2.5, 26), "BCN": (2.2, 25), "TFS": (5.0, 23), "LPA": (5.2, 23),
    "FUE": (5.1, 23), "ACE": (5.0, 23), "MAH": (2.3, 26), "GRO": (2.2, 25),
    "ATH": (2.3, 26), "SKG": (1.8, 26), "HER": (2.7, 26), "CHQ": (2.7, 26),
    "RHO": (2.8, 27), "KGS": (2.7, 27), "JTR": (2.9, 26), "JMK": (2.8, 26),
    "CFU": (1.8, 27), "ZTH": (2.2, 27), "EFL": (2.2, 27), "PVK": (2.0, 27),
    "JSI": (2.1, 26), "KLX": (2.6, 26), "KVA": (1.9, 26), "VOL": (2.0, 26),
    "SMI": (2.9, 27), "AOK": (3.0, 27),
    "SPU": (1.2, 25), "DBV": (1.4, 25), "ZAD": (1.2, 25), "PUY": (1.0, 24),
    "RJK": (1.1, 24), "TIV": (1.3, 25), "TGD": (1.4, 25), "TIA": (1.7, 26),
    "NAP": (1.7, 26), "PMO": (2.0, 27), "CTA": (2.0, 26), "CAG": (1.9, 26),
    "OLB": (1.6, 26), "BRI": (1.5, 26), "TPS": (2.0, 27), "RMI": (1.3, 26),
    "GOA": (1.3, 24), "VCE": (1.1, 26), "BDS": (1.6, 26), "AHO": (1.8, 25), "SUF": (1.8, 27),
    "MLA": (2.0, 27), "FAO": (3.4, 22), "LIS": (3.5, 20), "OPO": (3.5, 19),
    "LCA": (3.3, 28), "PFO": (3.4, 28), "VAR": (1.8, 26), "BOJ": (1.9, 26),
    "NCE": (1.5, 25), "MRS": (1.6, 24),
    "HRG": (4.0, 29), "SSH": (4.2, 29), "RMF": (4.3, 29), "AGA": (4.3, 22),
    "NBE": (2.3, 26), "DJE": (2.5, 27), "TLV": (3.5, 29), "DXB": (6.0, 33),
}

# ------------------------------------------------------------ http utils ----


def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "beach-holiday-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — log and continue, one bad call must not kill the run
        print(f"  ! request failed: {exc} :: {url[:120]}")
        return None


# ------------------------------------------------------------- flights ------


def fetch_flight_rt(origin, dest, depart, ret):
    """Cheapest cached round-trip price (EUR) or None."""
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": depart.isoformat(),
        "return_at": ret.isoformat(),
        "one_way": "false",
        "direct": "false",
        "sorting": "price",
        "limit": 1,
        "currency": CURRENCY,
        "token": TP_TOKEN,
    }
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    if not data or not data.get("data"):
        return None
    t = data["data"][0]
    price = t.get("price")
    link = t.get("link")
    return {"price": float(price), "link": link} if price else None


def aviasales_search_url(origin, dest, depart, ret):
    """Human-facing fallback search link."""
    d1 = depart.strftime("%d%m")
    d2 = ret.strftime("%d%m")
    return f"https://www.aviasales.com/search/{origin}{d1}{dest}{d2}1"


# -------------------------------------------------------------- hotels ------
# Hotellook's free cached-price API was shut down with the Hotellook brand in
# Oct 2025, so v1 uses a seasonal estimate model: per-destination nightly
# rates (budget 3-star, solo) with a low/high season split, plus a
# Booking.com deep link with the exact dates so live prices are one tap away.
# To go live later, replace estimate_hotel_nightly() with a real provider
# (LiteAPI / Amadeus) — the rest of the pipeline is agnostic.


def is_high_season(dest, d):
    if dest in WINTER_SUN:
        return d.month in (11, 12, 1, 2, 3)
    return d.month in (6, 7, 8, 9)


def estimate_hotel_nightly(dest, depart):
    _, _, _, low, high, _ = _dest_row(dest)
    return high if is_high_season(dest, depart) else low


def _dest_row(dest):
    name, country, city, low, high, target = BEACH_DESTINATIONS[dest]
    return name, country, city, low, high, target


def booking_url(dest, checkin, checkout, order="price"):
    """order: 'price' = cheapest first, 'bayesian_review_score' = best rated."""
    city = urllib.parse.quote(BEACH_DESTINATIONS[dest][2])
    return (
        f"https://www.booking.com/searchresults.html?ss={city}"
        f"&checkin={checkin.isoformat()}&checkout={checkout.isoformat()}"
        f"&group_adults={HOTEL_SPLIT}&no_rooms=1&order={order}"
    )


def airbnb_url(dest, checkin, checkout):
    city = urllib.parse.quote(BEACH_DESTINATIONS[dest][2])
    return (
        f"https://www.airbnb.com/s/{city}/homes"
        f"?checkin={checkin.isoformat()}&checkout={checkout.isoformat()}"
        f"&adults={HOTEL_SPLIT}"
    )


# ------------------------------------------------------------- history ------


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=1, sort_keys=True)


def rolling_average(history, dest):
    entries = history.get(dest, [])
    cutoff = (date.today() - timedelta(days=ROLLING_WINDOW)).isoformat()
    vals = [e["total"] for e in entries if e["date"] >= cutoff]
    return (sum(vals) / len(vals)) if len(vals) >= 3 else None


def record_history(history, dest, best_total):
    entries = history.setdefault(dest, [])
    today = date.today().isoformat()
    entries[:] = [e for e in entries if e["date"] != today]
    entries.append({"date": today, "total": round(best_total, 2)})
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    entries[:] = [e for e in entries if e["date"] >= cutoff]


# ------------------------------------------------------------ telegram ------


def _split_on_lines(text, limit=3500):
    """Split into <=limit chunks on newline boundaries so HTML tags stay intact."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def _tg_send_one(url, chunk):
    payload = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": chunk,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=payload)
            urllib.request.urlopen(req, timeout=20)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 429:  # rate limited — back off and retry
                wait = 3 * (attempt + 1)
                print(f"  . rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            body = exc.read().decode("utf-8", "ignore")[:200]
            print(f"  ! telegram HTTP {exc.code}: {body}")
            return False
        except Exception as exc:
            print(f"  ! telegram send failed: {exc}")
            time.sleep(2)
    return False


def send_telegram(text):
    if DRY_RUN:
        print("---- DRY RUN, telegram message below ----")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    chunks = _split_on_lines(text)
    sent = 0
    for i, chunk in enumerate(chunks):
        if _tg_send_one(url, chunk):
            sent += 1
        time.sleep(1.2)  # stay under Telegram's burst limit
    print(f"  telegram: sent {sent}/{len(chunks)} chunks")


# ------------------------------------------------------------ main scan -----


def candidate_trips():
    """Yield (label, depart_date, nights) for the next SCAN_WEEKENDS weekends."""
    today = date.today()
    thursdays = []
    d = today + timedelta(days=2)  # never scan trips leaving tomorrow
    while len(thursdays) < SCAN_WEEKENDS:
        if d.weekday() == 3:  # Thursday
            thursdays.append(d)
        d += timedelta(days=1)
    for thu in thursdays:
        fri = thu + timedelta(days=1)
        label = f"{thu.strftime('%d %b')}–{fri.strftime('%d %b')} weekend"
        for nights in TRIP_NIGHTS:
            yield label, thu, nights
            yield label, fri, nights


def main():
    if not TP_TOKEN or (not DRY_RUN and (not TG_TOKEN or not TG_CHAT_ID)):
        print("Missing TP_TOKEN / TG_TOKEN / TG_CHAT_ID")
        sys.exit(1)

    history = load_history()
    all_deals = []
    best_by_dest = {}

    trips = list(candidate_trips())
    total_calls = len(BEACH_DESTINATIONS) * len(trips) * len(ORIGINS)
    print(f"Scanning {len(BEACH_DESTINATIONS)} beach destinations x {len(trips)} trip windows "
          f"x {len(ORIGINS)} origins (~{total_calls} flight calls)")

    for dest, (name, country, city, low, high, target) in BEACH_DESTINATIONS.items():
        for label, depart, nights in trips:
            ret = depart + timedelta(days=nights)
            nightly = estimate_hotel_nightly(dest, depart)
            hotel_cost = (nightly * nights) / HOTEL_SPLIT

            for origin in ORIGINS:
                flight = fetch_flight_rt(origin, dest, depart, ret)
                time.sleep(PACE_SECONDS)
                if not flight:
                    continue
                total = flight["price"] + hotel_cost
                deal = {
                    "dest": dest, "name": name, "country": country,
                    "origin": origin, "depart": depart.isoformat(),
                    "return": ret.isoformat(), "nights": nights,
                    "weekend": label,
                    "flight": round(flight["price"]),
                    "hotel_night": round(nightly),
                    "hotel_total": round(hotel_cost),
                    "total": round(total),
                    "target": target,
                    "url": ("https://www.aviasales.com" + flight["link"])
                    if flight.get("link") else aviasales_search_url(origin, dest, depart, ret),
                    "area": BEACH_DESTINATIONS[dest][2],
                    "booking_cheap": booking_url(dest, depart, ret, "price"),
                    "booking_top": booking_url(dest, depart, ret, "bayesian_review_score"),
                    "airbnb": airbnb_url(dest, depart, ret),
                    "flight_hours": DEST_META.get(dest, (None, None))[0],
                    "sea_temp": DEST_META.get(dest, (None, None))[1],
                }
                all_deals.append(deal)
                if dest not in best_by_dest or total < best_by_dest[dest]["total"]:
                    best_by_dest[dest] = deal

    # ---- alert selection ----
    alerts = []
    for dest, deal in best_by_dest.items():
        target = BEACH_DESTINATIONS[dest][5]
        avg = rolling_average(history, dest)
        reasons = []
        if deal["total"] <= target:
            reasons.append(f"below €{target} target")
        if deal["total"] <= TOTAL_THRESHOLD_EUR:
            reasons.append(f"under €{TOTAL_THRESHOLD_EUR:.0f} global cap")
        if avg and deal["total"] <= avg * (1 - ROLLING_DROP_PCT):
            reasons.append(f"{ROLLING_DROP_PCT * 100:.0f}%+ below 2-week avg (€{avg:.0f})")
        if reasons:
            deal["reasons"] = reasons
            alerts.append(deal)
        record_history(history, dest, deal["total"])

    save_history(history)
    write_site_data(all_deals, alerts)

    if not alerts and not ALWAYS_DIGEST:
        print("No deals beat targets today. No message sent.")
        return

    alerts.sort(key=lambda d: d["total"])
    top_deals = alerts[:MAX_DIGEST_DEALS]
    alert_dests = {d["dest"] for d in alerts}

    # Full board: every destination, cheapest first, with full links.
    board = sorted(best_by_dest.values(), key=lambda d: d["total"])[:OVERVIEW_N] \
        if OVERVIEW_N > 0 else []

    today = date.today().strftime("%d %b %Y")
    if top_deals:
        header = (f"\U0001F3D6 <b>Beach Holiday Deals — {today}</b>\n{len(top_deals)} deal(s) beat target today. Full board below.\n\U0001F4CA <a href=\"{DASHBOARD_URL}\">Compare all in browser</a>")
    else:
        header = (f"\U0001F3D6 <b>Beach Holiday check-in — {today}</b>\nNo target-beating deals today. Full board of cheapest totals from Vienna:\n\U0001F4CA <a href=\"{DASHBOARD_URL}\">Compare all in browser</a>")
    lines = [header, ""]

    def deal_block(d, flame=False):
        star = " \U0001F525" if flame else ""
        return [
            f"<b>{d['country'].split(' ', 1)[0]} {d['name']}</b> — <b>€{d['total']}</b>{star}",
            f"   ✈️ €{d['flight']} RT {d['origin']}  🏨 ~€{d['hotel_total']} est. "
            f"({d['nights']}n × €{d['hotel_night']}) · {d['depart']} → {d['return']}",
            f"   <a href=\"{d['url']}\">Book flight</a> · Stay in {d['area']}: "
            f"<a href=\"{d['booking_cheap']}\">Cheapest</a> · "
            f"<a href=\"{d['booking_top']}\">Best rated</a> · "
            f"<a href=\"{d['airbnb']}\">Airbnb</a>",
            "",
        ]

    for d in board:
        lines += deal_block(d, flame=d["dest"] in alert_dests)

    lines.append("<i>Totals = live flight + seasonal hotel estimate (budget 3-star, per person). 🔥 = beats target. Stay links open live Booking.com and Airbnb for the exact dates in a beach-walkable area — no car needed. Always verify before booking.</i>")

    if SILENT_REFRESH:
        print(f"Silent refresh: wrote deals.json ({len(board)} destinations), no Telegram sent.")
        return
    send_telegram("\n".join(lines))
    print(f"Sent digest: {len(board)} destinations, {len(top_deals)} beating target.")


def write_site_data(all_deals, alerts):
    """deals.json for an optional GitHub Pages dashboard, same pattern as worldwide bot."""
    alert_keys = {(d["dest"], d["depart"], d["origin"], d["nights"]) for d in alerts}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "origins": ORIGINS,
        "deal_count": len(all_deals),
        "deals": sorted(all_deals, key=lambda d: d["total"])[:200],
        "alerts": [
            d for d in all_deals if (d["dest"], d["depart"], d["origin"], d["nights"]) in alert_keys
        ],
    }
    with open(SITE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)


if __name__ == "__main__":
    main()
