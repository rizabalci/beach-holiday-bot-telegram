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
SCAN_WEEKENDS = int((os.environ.get("SCAN_WEEKENDS") or "6"))

# Trip lengths in nights to test for each departure day
TRIP_NIGHTS = [int(n) for n in (os.environ.get("TRIP_NIGHTS") or "2,3,4,5").split(",")]

# Departure weekdays to scan (Mon=0 ... Sun=6). Default Thu, Fri, Sat.
DEPART_DAYS = [int(x) for x in (os.environ.get("DEPART_DAYS") or "3,4,5").split(",")]

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

# Whether a rental car is genuinely required, or merely convenient.
#   essential = public transport won't realistically get you to the beaches
#   optional  = buses, trains or ferries work fine; a car just opens up more
# Destinations absent from this dict need no car at all.
CAR_ADVICE = {
    # --- car is the only realistic option ---
    "AOK": "essential",  # Karpathos, barely any buses
    "BDS": "essential",  # Salento beaches are scattered and poorly served
    "EFL": "essential",  # Kefalonia, sparse and infrequent buses
    "FUE": "essential",  # Fuerteventura, best beaches are remote
    "MAH": "essential",  # Menorca, the good coves have no bus
    "OLB": "essential",  # Costa Smeralda beaches are spread out
    "PVK": "essential",  # Lefkada west coast beaches are unserved
    "RMF": "essential",  # Marsa Alam, resort transfers or nothing
    "SMI": "essential",  # Samos, limited bus network
    "VOL": "essential",  # Pelion villages and beaches need wheels

    # --- car helps, but you can manage without ---
    "ACE": "optional",   # Lanzarote, inter-resort buses
    "AGA": "optional",   # Agadir, cheap taxis
    "AGP": "optional",   # Cercanías train along the Costa del Sol
    "AHO": "optional",   # Alghero, buses to Lido and Fertilia
    "BRI": "optional",   # Ferrovie del Sud Est to Polignano and Monopoli
    "CAG": "optional",   # Cagliari, good city buses to Poetto
    "CFU": "optional",   # Corfu green buses to the main beaches
    "CHQ": "optional",   # KTEL buses reach Elafonissi and Balos in summer
    "DJE": "optional",   # Djerba, resort plus taxis
    "FAO": "optional",   # Algarve train, Faro to Lagos
    "GRO": "optional",   # Costa Brava coach network
    "HER": "optional",   # KTEL buses along the north coast
    "HRG": "optional",   # Hurghada, resort plus taxis
    "IBZ": "optional",   # Ibiza summer buses and the discobus
    "KGS": "optional",   # Kos buses to the main beaches
    "KLX": "optional",   # Kalamata town is walkable
    "KVA": "optional",   # Ferry to Thassos, island buses
    "LPA": "optional",   # Gran Canaria Global buses
    "MRS": "optional",   # Marseille transit, boats to the Calanques
    "NBE": "optional",   # Hammamet, taxis and louages
    "PMI": "optional",   # Mallorca buses plus the Sóller and Inca trains
    "PUY": "optional",   # Pula local buses to the beaches
    "RHO": "optional",   # Rhodes east and west coast buses
    "SKG": "optional",   # KTEL buses into Halkidiki
    "SSH": "optional",   # Sharm, resort plus taxis
    "TFS": "optional",   # Tenerife TITSA network is excellent
    "TGD": "optional",   # Cheap buses down to Bar and Budva
    "TIA": "optional",   # Furgon minibuses to Himarë and Sarandë
    "TPS": "optional",   # Seasonal buses to San Vito Lo Capo
    "ZTH": "optional",   # Zakynthos buses to the main resorts
}

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
    """Cheapest cached DIRECT round-trip price (EUR) or None. No layovers."""
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": depart.isoformat(),
        "return_at": ret.isoformat(),
        "one_way": "false",
        "direct": "true",
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
    # Belt-and-suspenders: reject anything with transfers even if API misreports
    if t.get("transfers", 0) > 0 or t.get("return_transfers", 0) > 0:
        return None
    price = t.get("price")
    link = t.get("link")
    if not price:
        return None
    return {
        "price": float(price),
        "link": link,
        "departure_at": t.get("departure_at"),
        "return_at": t.get("return_at"),
    }


def aviasales_search_url(origin, dest, depart, ret):
    """Deprecated — kept for backwards compat but returns Skyscanner direct-only."""
    return skyscanner_direct_url(origin, dest, depart, ret)


def skyscanner_direct_url(origin, dest, depart, ret):
    """Skyscanner search URL pre-filtered to direct flights only.
    Format: /transport/flights/{from}/{to}/{yymmdd}/{yymmdd}/?adults=1&preferdirects=true
    """
    d1 = depart.strftime("%y%m%d")
    d2 = ret.strftime("%y%m%d")
    return (
        f"https://www.skyscanner.net/transport/flights/"
        f"{origin.lower()}/{dest.lower()}/{d1}/{d2}/"
        f"?adults=1&preferdirects=true"
    )


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


# --- peak periods ----------------------------------------------------------
# The low/high season split is too blunt on its own: 1 August and Ferragosto
# weekend are both "high season" but nowhere near the same price. These
# multipliers sit on top of the seasonal rate, and also drive the crowd flag.

def _gregorian_easter(year):
    """Anonymous Gregorian algorithm. Western Easter Sunday."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, 0
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lp = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lp) // 451
    month = (h + lp - 7 * m + 114) // 31
    day = ((h + lp - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _orthodox_easter(year):
    """Julian computus shifted to the Gregorian calendar."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    julian = date(year, month, day)
    return julian + timedelta(days=13)  # valid 1900-2099


ORTHODOX_COUNTRIES = {"Greece", "Cyprus"}


def peak_multiplier(dest, d):
    """Uplift on the seasonal nightly rate. Returns (multiplier, label|None)."""
    country = BEACH_DESTINATIONS[dest][1].split()[-1]

    # Ferragosto: Italy effectively closes and the coast fills up.
    if d.month == 8 and 10 <= d.day <= 20:
        if country == "Italy":
            return 1.45, "Ferragosto"
        return 1.20, "mid-August peak"

    # New Year
    if (d.month == 12 and d.day >= 27) or (d.month == 1 and d.day <= 2):
        return 1.35, "New Year"

    # Easter week, Orthodox where relevant
    easter = (_orthodox_easter(d.year) if country in ORTHODOX_COUNTRIES
              else _gregorian_easter(d.year))
    if -3 <= (d - easter).days <= 3:
        return 1.30, "Easter week"

    # Broad peak summer fortnights either side of Ferragosto
    if (d.month == 7 and d.day >= 25) or (d.month == 8 and d.day <= 25):
        return 1.15, None

    return 1.0, None


def weekend_uplift(depart, nights):
    """Hotels charge more for Friday and Saturday nights. Scale by how much
    of the stay lands on them, up to +8%."""
    if nights <= 0:
        return 1.0
    weekend_nights = sum(
        1 for i in range(nights)
        if (depart + timedelta(days=i)).weekday() in (4, 5)
    )
    return 1.0 + 0.08 * (weekend_nights / nights)


# --- daily on-the-ground spend --------------------------------------------
# Per person, per day, in EUR. Covers food, drinks, local transport and small
# activities at a mid-range pace: a proper restaurant dinner, a casual lunch,
# coffee, a couple of drinks, buses plus the odd taxi, one paid activity or
# beach lounger. Not backpacking, not fine dining. Excludes car hire.
DAILY_SPEND_BY_COUNTRY = {
    "Albania": 35,
    "Egypt": 35,
    "Tunisia": 35,
    "Bulgaria": 38,
    "Morocco": 40,
    "Montenegro": 45,
    "Croatia": 55,
    "Greece": 55,
    "Malta": 55,
    "Portugal": 55,
    "Cyprus": 58,
    "Spain": 60,
    "Italy": 65,
    "France": 75,
    "Israel": 80,
    "UAE": 80,
}

# Destinations that sit well above or below their country baseline.
DAILY_SPEND_OVERRIDE = {
    # pricier than the country norm
    "JMK": 110,  # Mykonos
    "JTR": 95,   # Santorini
    "NCE": 90,   # Nice / French Riviera
    "IBZ": 90,   # Ibiza
    "TLV": 90,   # Tel Aviv
    "DXB": 85,   # Dubai
    "VCE": 85,   # Venice / Lido
    "PMI": 70,   # Palma de Mallorca
    "DBV": 70,   # Dubrovnik
    # cheaper than the country norm
    "BRI": 55,   # Bari / Puglia
    "BDS": 55,   # Brindisi / Salento
    "SUF": 55,   # Lamezia / Tropea
    "PMO": 50,   # Palermo, Sicily
    "CTA": 50,   # Catania, Sicily
    "TPS": 50,   # Trapani
    "KLX": 45,   # Kalamata / Peloponnese
    "VOL": 45,   # Volos / Pelion
    "KVA": 45,   # Kavala / Thassos
}

DAILY_SPEND_DEFAULT = 55


def estimate_daily_spend(dest):
    """Per-person daily spend in EUR, before flights and accommodation."""
    if dest in DAILY_SPEND_OVERRIDE:
        return DAILY_SPEND_OVERRIDE[dest]
    country = BEACH_DESTINATIONS[dest][1].split()[-1]
    return DAILY_SPEND_BY_COUNTRY.get(country, DAILY_SPEND_DEFAULT)


# --- flight timing quality -------------------------------------------------
# A cheap fare that lands at 23:40 and flies home at 06:30 costs you both
# bookend days. Times come from the cached fare, so treat them as indicative
# of that route's usual schedule rather than a guarantee.

LATE_ARRIVAL_HOUR = 21      # landing at or after this = evening gone
EARLY_RETURN_HOUR = 9       # flying home before this = last morning gone
RED_EYE_DEPART_HOUR = 6     # leaving before this = 3am start from home


def _parse_iso_hhmm(stamp):
    """'2026-08-01T05:45:00+02:00' -> (5, 45). None if unparseable."""
    if not stamp or "T" not in stamp:
        return None
    try:
        clock = stamp.split("T", 1)[1][:5]
        h, m = clock.split(":")
        return int(h), int(m)
    except (ValueError, IndexError):
        return None


def analyse_timing(flight, flight_hours):
    """Return dict of readable times plus flags for day-wasting schedules."""
    out = {
        "dep_time": None, "arr_time": None,
        "ret_dep_time": None, "timing_flags": [],
    }
    dep = _parse_iso_hhmm(flight.get("departure_at"))
    ret = _parse_iso_hhmm(flight.get("return_at"))
    if dep:
        out["dep_time"] = f"{dep[0]:02d}:{dep[1]:02d}"
        if flight_hours:
            total_min = dep[0] * 60 + dep[1] + int(round(flight_hours * 60))
            ah, am = (total_min // 60) % 24, total_min % 60
            out["arr_time"] = f"{ah:02d}:{am:02d}"
            if ah >= LATE_ARRIVAL_HOUR or ah < 4:
                out["timing_flags"].append("late_arrival")
        if dep[0] < RED_EYE_DEPART_HOUR:
            out["timing_flags"].append("red_eye")
    if ret:
        out["ret_dep_time"] = f"{ret[0]:02d}:{ret[1]:02d}"
        if ret[0] < EARLY_RETURN_HOUR:
            out["timing_flags"].append("early_return")
    return out


# --- getting to the beach --------------------------------------------------
# How you reach the sand from the recommended base once you've checked in.
# Independent of needs_car, which is about the trip overall.
#   walk = roughly 15 minutes on foot or less
#   bus  = regular public transport, about 15-45 minutes
#   taxi = no useful transit, short drive required
BEACH_ACCESS = {
    "AGA": ("walk", "Beach promenade runs along the town"),
    "AHO": ("walk", "Lido di Alghero ~15 min from the old town"),
    "ALC": ("walk", "Postiguet beach below the castle"),
    "ATH": ("bus", "Tram to Glyfada / Vouliagmeni, ~45 min"),
    "BCN": ("walk", "Barceloneta from the Gothic quarter"),
    "BRI": ("bus", "Pane e Pomodoro ~25 min; real Puglia beaches need a car"),
    "BDS": ("taxi", "Nearest good sand is a short drive"),
    "BOJ": ("walk", "Sunny Beach hotels sit on the sand"),
    "CAG": ("bus", "Poetto beach, bus PF/PQ ~15 min"),
    "CTA": ("bus", "Playa di Catania, bus D ~20 min"),
    "CHQ": ("walk", "Nea Chora ~10 min; Balos and Elafonissi need a car"),
    "CFU": ("bus", "Buses to Glyfada and Paleokastritsa"),
    "DJE": ("walk", "Resort strip is beachfront"),
    "DXB": ("taxi", "JBR or Kite Beach, short ride"),
    "DBV": ("walk", "Banje beach 5 min from Ploče Gate"),
    "FAO": ("bus", "Ferry or bus to the island beaches"),
    "FUE": ("walk", "Resorts sit directly on the sand"),
    "GOA": ("bus", "Boccadasse by bus; Cinque Terre by train"),
    "GRO": ("taxi", "Costa Brava coast is a drive from Girona"),
    "LPA": ("walk", "Las Canteras runs through Las Palmas"),
    "NBE": ("walk", "Hammamet resorts are beachfront"),
    "HER": ("bus", "Amoudara beach, ~20 min by bus"),
    "HRG": ("walk", "Resorts sit on the Red Sea shore"),
    "IBZ": ("bus", "Buses to Talamanca and Ses Salines"),
    "KLX": ("walk", "Town beach by the marina"),
    "AOK": ("taxi", "Pigadia beach is walkable, the rest a drive"),
    "KVA": ("bus", "Kalamitsa ~15 min; Thassos by ferry"),
    "EFL": ("taxi", "Argostoli beaches are a short drive"),
    "KGS": ("walk", "Kos town beach from the centre"),
    "SUF": ("walk", "Tropea beach sits below the old town"),
    "ACE": ("walk", "Puerto del Carmen is beachfront"),
    "LCA": ("walk", "Finikoudes on the seafront promenade"),
    "LIS": ("bus", "Cascais train from Cais do Sodré, ~40 min"),
    "AGP": ("walk", "La Malagueta ~15 min from the centre"),
    "MLA": ("bus", "Buses from Sliema to the northern bays"),
    "RMF": ("walk", "Resorts are beachfront"),
    "MRS": ("bus", "Prado beaches by bus; Calanques by boat or hike"),
    "MAH": ("taxi", "The good coves are a drive"),
    "JMK": ("bus", "Buses to Paradise, Elia and Platis Gialos"),
    "NAP": ("bus", "Circumvesuviana to Sorrento; beach clubs below town"),
    "NCE": ("walk", "Promenade des Anglais from anywhere central"),
    "OLB": ("taxi", "Pittulongu and Golfo Aranci are a short drive"),
    "PMO": ("bus", "Bus 806 to Mondello, ~30 min, €1.40"),
    "PMI": ("bus", "Cala Major and Illetas by bus"),
    "PFO": ("walk", "Harbour beaches; Coral Bay by bus"),
    "TGD": ("taxi", "The coast is over an hour away"),
    "OPO": ("bus", "Metro to Matosinhos, ~25 min"),
    "PVK": ("taxi", "Lefkada beaches need a drive"),
    "PUY": ("walk", "Hawaii and Stoja beaches ~20 min"),
    "RHO": ("walk", "Elli beach in Rhodes Town"),
    "RJK": ("bus", "Buses along the coast to Opatija and Lovran"),
    "RMI": ("walk", "Sand starts at the end of the street"),
    "SMI": ("taxi", "Beaches are a drive from Vathy"),
    "JTR": ("bus", "Buses to Perissa and Kamari"),
    "SSH": ("walk", "Resorts are beachfront"),
    "JSI": ("bus", "Bus to Koukounaries, ~30 min"),
    "SPU": ("walk", "Bačvice ~10 min from the Riva"),
    "TLV": ("walk", "Beach runs the length of the city"),
    "TFS": ("walk", "Los Cristianos and Las Américas are beachfront"),
    "SKG": ("taxi", "Halkidiki peninsulas are a drive from the city"),
    "TIA": ("taxi", "Durrës ~40 min; the Riviera is 3-4 hours"),
    "TIV": ("walk", "Bay beaches walkable; Plavi Horizonti a short drive"),
    "TPS": ("taxi", "San Vito Lo Capo is a drive from Trapani"),
    "VLC": ("bus", "Malvarrosa by tram or bus, ~20 min"),
    "VAR": ("walk", "City beach below the sea garden"),
    "VCE": ("bus", "Vaporetto to the Lido, ~15 min"),
    "VOL": ("taxi", "Pelion beaches need a drive"),
    "ZAD": ("walk", "Kolovare ~10 min from the old town"),
    "ZTH": ("bus", "Buses to Laganas and Tsilivi"),
}


def beach_access(dest):
    return BEACH_ACCESS.get(dest, ("taxi", "Check local transport on arrival"))


TIMING_WARNINGS = {
    "late_arrival": "lands late, first evening gone",
    "early_return": "early flight home, last morning gone",
    "red_eye": "pre-dawn start from home",
}
ACCESS_ICONS = {"walk": "\U0001F6B6", "bus": "\U0001F68C", "taxi": "\U0001F695"}


def _timing_line(d):
    """Departure/arrival clock times plus any day-wasting warnings."""
    if not d.get("dep_time"):
        return ""
    parts = f"   \u23F1 {d['dep_time']}"
    if d.get("arr_time"):
        parts += f"\u2192{d['arr_time']}"
    if d.get("ret_dep_time"):
        parts += f" \u00b7 back {d['ret_dep_time']}"
    flags = d.get("timing_flags") or []
    if flags:
        parts += "  \u26A0\ufe0f " + " \u00b7 ".join(
            TIMING_WARNINGS[f] for f in flags if f in TIMING_WARNINGS
        )
    return parts


def _access_line(d):
    if not d.get("beach_note"):
        return ""
    icon = ACCESS_ICONS.get(d.get("beach_access"), "\U0001F695")
    line = f"   {icon} {d['beach_note']}"
    advice = d.get("car_advice")
    if advice == "essential":
        line += "  \U0001F697 rental car essential"
    elif advice == "optional":
        line += "  \U0001F697 car optional, transport works"
    return line


def _trend_line(d):
    """Where today's price sits against this destination's own history."""
    bits = []
    pct = d.get("vs_avg_pct")
    if pct is not None:
        arrow = {"falling": "\u2193", "rising": "\u2191"}.get(d.get("trend"), "\u2192")
        sign = "+" if pct > 0 else ""
        note = {"cheap": "good time to book",
                "pricey": "worth waiting",
                "typical": "about normal"}.get(d.get("verdict"), "")
        bits.append(f"{arrow} {sign}{pct}% vs 2-week avg €{d['hist_avg']} ({note})")
    if d.get("peak_label"):
        bits.append(f"\u26A0\ufe0f {d['peak_label']} — crowded, hotels ~{d['peak_pct']}% up")
    return ("   \U0001F4C8 " + "  \u00b7  ".join(bits)) if bits else ""


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


def google_hotels_url(dest, checkin, checkout):
    """Google Hotels aggregates Booking, Expedia and the hotel's own site,
    and flags when booking direct is cheaper than the platforms."""
    city = urllib.parse.quote(BEACH_DESTINATIONS[dest][2])
    return (
        f"https://www.google.com/travel/search?q={city}"
        f"&checkin={checkin.isoformat()}&checkout={checkout.isoformat()}"
        f"&hl=en&gl=at&curr={CURRENCY}"
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


def price_trend(history, dest, today_total):
    """Compare today's best total against this destination's recent history.

    Returns dict with the rolling average, the gap to it, and a direction
    based on the most recent readings versus the ones before them.
    """
    out = {"hist_avg": None, "vs_avg_pct": None, "trend": None, "verdict": None}
    entries = sorted(history.get(dest, []), key=lambda e: e["date"])
    cutoff = (date.today() - timedelta(days=ROLLING_WINDOW)).isoformat()
    window = [e for e in entries if e["date"] >= cutoff]
    if len(window) < 3:
        return out

    vals = [e["total"] for e in window]
    avg = sum(vals) / len(vals)
    out["hist_avg"] = round(avg)
    out["vs_avg_pct"] = round((today_total - avg) / avg * 100)

    # direction: mean of the last third against the mean of the rest
    split = max(1, len(vals) // 3)
    recent, earlier = vals[-split:], vals[:-split]
    if earlier:
        r, e = sum(recent) / len(recent), sum(earlier) / len(earlier)
        change = (r - e) / e * 100
        out["trend"] = "falling" if change <= -5 else "rising" if change >= 5 else "flat"

    pct = out["vs_avg_pct"]
    if pct <= -15:
        out["verdict"] = "cheap"
    elif pct >= 10:
        out["verdict"] = "pricey"
    else:
        out["verdict"] = "typical"
    return out


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
    """Yield (label, depart_date, nights) for the next SCAN_WEEKENDS weekends.

    Departure days are configurable via DEPART_DAYS (weekday numbers, Mon=0).
    Default 3,4,5 = Thursday, Friday, Saturday — Saturday departures matter
    because a Sat->Mon or Sat->Tue trip costs only 1-2 work days off.
    """
    today = date.today()
    thursdays = []
    d = today + timedelta(days=2)  # never scan trips leaving tomorrow
    while len(thursdays) < SCAN_WEEKENDS:
        if d.weekday() == 3:  # Thursday anchors each weekend
            thursdays.append(d)
        d += timedelta(days=1)
    for thu in thursdays:
        label = f"{thu.strftime('%d %b')} weekend"
        for offset in sorted(DEPART_DAYS):
            dep = thu + timedelta(days=offset - 3)  # 3=Thu, 4=Fri, 5=Sat
            if dep < today + timedelta(days=2):
                continue
            for nights in TRIP_NIGHTS:
                yield label, dep, nights


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
            base_nightly = estimate_hotel_nightly(dest, depart)
            peak_mult, peak_label = peak_multiplier(dest, depart)
            wknd_mult = weekend_uplift(depart, nights)
            nightly = base_nightly * peak_mult * wknd_mult
            hotel_cost = (nightly * nights) / HOTEL_SPLIT

            for origin in ORIGINS:
                flight = fetch_flight_rt(origin, dest, depart, ret)
                time.sleep(PACE_SECONDS)
                if not flight:
                    continue
                total = flight["price"] + hotel_cost
                daily = estimate_daily_spend(dest)
                spend_total = daily * nights
                fhours = DEST_META.get(dest, (None, None))[0]
                timing = analyse_timing(flight, fhours)
                access_mode, access_note = beach_access(dest)
                deal = {
                    "dest": dest, "name": name, "country": country,
                    "origin": origin, "depart": depart.isoformat(),
                    "return": ret.isoformat(), "nights": nights,
                    "weekend": label,
                    "flight": round(flight["price"]),
                    "hotel_night": round(nightly),
                    "hotel_total": round(hotel_cost),
                    "peak_label": peak_label,
                    "peak_pct": round((peak_mult - 1) * 100),
                    "total": round(total),
                    "daily_spend": daily,
                    "spend_total": round(spend_total),
                    "grand_total": round(total + spend_total),
                    "beach_access": access_mode,
                    "beach_note": access_note,
                    **timing,
                    "target": target,
                    "url": skyscanner_direct_url(origin, dest, depart, ret),
                    "area": BEACH_DESTINATIONS[dest][2],
                    "needs_car": dest in CAR_ADVICE,
                    "car_advice": CAR_ADVICE.get(dest),
                    "booking_cheap": booking_url(dest, depart, ret, "price"),
                    "booking_top": booking_url(dest, depart, ret, "bayesian_review_score"),
                    "airbnb": airbnb_url(dest, depart, ret),
                    "google_hotels": google_hotels_url(dest, depart, ret),
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
        # computed before record_history so today's reading isn't in its own average
        deal.update(price_trend(history, dest, deal["total"]))
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
            f"({d['nights']}n × €{d['hotel_night']})",
            f"   💶 +€{d.get('spend_total', 0)} food & local (€{d.get('daily_spend', 0)}/day) "
            f"→ <b>all-in ~€{d.get('grand_total', d['total'])}</b>",
            f"   \U0001F4C5 {fmt_day(d['depart'])} → {fmt_day(d['return'])} · "
            f"{work_days_off(d['depart'], d['return'])} days off work",
            _timing_line(d),
            _access_line(d),
            _trend_line(d),
            f"   <a href=\"{d['url']}\">Book flight</a> · Stay in {d['area']}: "
            f"<a href=\"{d['booking_cheap']}\">Cheapest</a> · "
            f"<a href=\"{d['booking_top']}\">Best rated</a> · "
            f"<a href=\"{d['airbnb']}\">Airbnb</a>"
            + (f" · <a href=\"{d['google_hotels']}\">Compare</a>" if d.get("google_hotels") else ""),
            "",
        ]

    for d in board:
        block = deal_block(d, flame=d["dest"] in alert_dests)
        # helpers return "" when there's no data; drop those but keep the
        # single intentional "" separator at the end of each block
        lines += [ln for ln in block[:-1] if ln] + [""]

    lines.append("<i>Totals = live direct-flight fare + seasonal hotel estimate, per person. All-in adds food, drinks and local transport. 🔥 = beats target. ⚠️ marks schedules that waste a bookend day. 🚶🚌🚕 = how you reach the beach. Always verify before booking.</i>")

    if SILENT_REFRESH:
        print(f"Silent refresh: wrote deals.json ({len(board)} destinations), no Telegram sent.")
        return
    send_telegram("\n".join(lines))
    print(f"Sent digest: {len(board)} destinations, {len(top_deals)} beating target.")


def fmt_day(iso):
    """'Thu 10 Sep' from an ISO date."""
    d = datetime.fromisoformat(iso).date() if isinstance(iso, str) else iso
    return d.strftime("%a %d %b")


def work_days_off(dep_iso, ret_iso):
    """Mon-Fri days between depart and return inclusive = holiday days needed."""
    d = datetime.fromisoformat(dep_iso).date()
    end = datetime.fromisoformat(ret_iso).date()
    n = 0
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


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
