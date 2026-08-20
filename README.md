# Lucomagno wind archive

Scrapes wind speed, wind direction and temperature from the Lucomagno pass
weather station ([alarm.lucomagno.ch/fe/measure](https://alarm.lucomagno.ch/fe/measure))
and accumulates them into a CSV archive committed to this repo.

The station page only ever shows a **rolling 24-hour window** at 10-minute
resolution. It has no history parameter — see [PROBE_RESULTS.md](PROBE_RESULTS.md)
for the evidence — so the only way to get a longer record is to scrape that
window on a schedule and keep the results.

## Data

`data/YYYY-MM.csv`, one row per 10-minute reading, sorted by timestamp:

```csv
timestamp,wind_speed_kmh,wind_dir_deg,temp_c
2026-08-19 02:10:00,11.64,347.5,13.82
```

- **timestamp** — station-local wall clock (Europe/Zurich), as displayed on the
  page. Read from the page content, so it does not depend on the machine that
  runs the scraper. Note this means the archive inherits DST discontinuities.
- **wind_speed_kmh**, **wind_dir_deg** (0–360), **temp_c**

Blank fields mean the station reported no value for that slot.

## How it runs

[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) runs `scrape.py`
every 6 hours and commits anything new.

Because each fetch carries a full 24h of backfill, the schedule has roughly 18
hours of slack. That matters: GitHub's cron is best-effort and can fire late or
skip a slot entirely under load. A missed run simply backfills on the next one,
and the first run seeds a whole day of history immediately.

You can also trigger a catch-up run by hand from the Actions tab
(**Run workflow**), which is the fix if the archive ever falls behind.

> **Note:** GitHub disables scheduled workflows in public repos after 60 days
> with no commits. This one commits data regularly, so it stays alive on its
> own — but if the station goes offline for two months, re-enable it in the
> Actions tab.

## Local use

Stdlib only, no dependencies:

```bash
python3 scrape.py             # fetch and merge into data/
python3 scrape.py --dry-run   # fetch and report, write nothing
python3 scrape.py --stats     # rows, date range, completeness, wind stats
```

Merging is keyed on timestamp and the CSV output is byte-stable, so re-running
is idempotent — an unchanged fetch produces no diff.

## Being a good citizen

The site throttles: under load it responds `200` with the page chrome but **no
chart data**, rather than a `429`. `scrape.py` treats an empty parse as a
transient condition and retries with backoff rather than failing or hammering.

Four requests a day is negligible load. If you fork this, please don't tighten
the schedule — a 10-minute cron would gain you nothing (the data only updates
every 10 minutes anyway, and each page already contains 24h) while multiplying
traffic 36×.
