# 🏖 Beach Holiday Bot

Daily scanner for cheap **3–4 day beach trips from Vienna (VIE) and Bratislava (BTS)** to 48 beach destinations across the Mediterranean, Adriatic, Aegean, Canaries, Black Sea, Turkish coast, Red Sea and North Africa. Telegram alerts with **flight + hotel total cost per person**. Sibling of the Istanbul bot and the worldwide flight bot — same free stack, zero cost, zero maintenance.

## How it works

Every day at 07:00 UTC the bot:

1. Builds candidate trips: the next **8 weekends**, testing Thursday and Friday departures with **3 and 4 night** stays (32 trip windows).
2. Pulls the **cheapest live round-trip flight** for every origin × destination × trip window from the Travelpayouts cached API (~3,000 calls, ~10 min run).
3. Adds a **seasonal hotel estimate** — per-destination nightly rates (budget 3-star, solo) with a low/high season split. Canaries, Egypt and Morocco flip to winter-sun high season.
4. Alerts when a destination's best total beats its **per-destination target**, the **global €250 cap**, or drops **25%+ below its 2-week rolling average**.
5. Sends one Telegram digest, cheapest totals first, with two links per deal: the Aviasales flight and a **Booking.com search prefilled with the exact city and dates** so live hotel prices are one tap away.

> **Why estimated hotels?** Hotellook (Travelpayouts' free hotel-price API) was shut down in October 2025, and no free live hotel API survives at this call volume. The estimate model keeps the bot free and dependable; the Booking.com deep link gives you the live number when a deal fires. `estimate_hotel_nightly()` is a single pluggable function — swap in LiteAPI or Amadeus later if live prices become worth a second API key.

## Files

| File | Purpose |
|---|---|
| `check_beach.py` | The whole bot — stdlib only, nothing to install |
| `.github/workflows/check.yml` | Daily cron + manual trigger with dry-run and threshold override |
| `price_history.json` | Rolling per-destination totals (auto-committed) |
| `deals.json` | Full scan output for an optional GitHub Pages dashboard |
| `.env.example` | All tunable variables with defaults |

## Setup

1. New repo, upload everything (mind the `.github/workflows/` path — create it via terminal with `mkdir -p`, same trap as the other bots).
2. **Secrets** (Settings → Secrets and variables → Actions → Secrets): `TP_TOKEN`, `TG_TOKEN`, `TG_CHAT_ID` — reuse the ones from the worldwide bot, or make a new @BotFather bot for a separate deal stream.
3. **Variables** (optional overrides): `ORIGINS`, `SCAN_WEEKENDS`, `TRIP_NIGHTS`, `TOTAL_THRESHOLD_EUR`, `ROLLING_DROP_PCT`, `HOTEL_SPLIT`, `MIN_COVERAGE`, `PARTIAL_COVERAGE`, `MAX_TRANSPORT_ERROR_RATE`, `MAX_WORKERS`, `PACE_SECONDS`.
4. Settings → Actions → General → Workflow permissions → **Read and write**.
5. Test: Actions → Beach holiday scan → Run workflow → dry_run `true`.

## Tuning

- `HOTEL_SPLIT=2` — travelling as a couple sharing a room; halves the hotel share of the total.
- Per-destination **targets and nightly rates** in `BEACH_DESTINATIONS` are the real signal-to-noise dial. Calibrate nightly rates against what the Booking.com links actually show.
- `TOTAL_THRESHOLD_EUR` — the global "any beach under €X" catch-all.
- First 3 days: only target-based alerts. Day 14: rolling-average drop alerts fully settled.

## Thin coverage

Travelpayouts often answers HTTP 200 with an empty `data` list for a seasonal airport once that route drops out of the cached fare calendar. The scanner counts those empty bodies on their own, separate from priced responses and from rate-limit retries.

- At or above `PARTIAL_COVERAGE` (default half of destinations), the digest is the usual full board.
- From `MIN_COVERAGE` up to that line (default 10%), the job stays green, refreshes `deals.json`, and sends the digest with a partial-data note. The dashboard shows the same note.
- Below `MIN_COVERAGE`, or when more than `MAX_TRANSPORT_ERROR_RATE` of calls fail with an HTTP error or exhausted retries (default 25%), the job exits 1, keeps the previous `deals.json`, and skips the digest.

Set any of those as a repository variable, or in `.env` for a local run. `MAX_WORKERS` defaults to 6 (it was 8). A weekend pass at 8 workers has been finishing in about 10 minutes and seeing roughly 800 rate-limit responses, so the smaller pool stays inside the 70-minute job limit. `PACE_SECONDS` adds a per-worker pause when you want fewer retries.

## Cost

Zero. ~10-minute daily run on GitHub Actions (unlimited on public repos). Travelpayouts cached flight endpoint is free; Telegram is free; hotel estimates are local.
