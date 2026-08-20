#!/usr/bin/env python3
"""
Scrape the Lucomagno pass weather station into a CSV archive.

The station page (https://alarm.lucomagno.ch/fe/measure) only ever renders a
rolling 24h window at 10-minute resolution, with wind speed, wind direction
and temperature embedded server-side as Google Charts DataTable literals.
There is no history parameter -- see PROBE_RESULTS.md -- so the only way to
build an archive is to scrape that window on a schedule and accumulate it.

Every fetch carries a full 24h of backfill, which makes this forgiving: the
first run seeds a whole day, any schedule comfortably under 24h loses nothing,
and a missed or delayed run backfills itself on the next one. That matters
because GitHub Actions cron is best-effort and can run late or be skipped.

Rows are merged into monthly CSVs keyed by timestamp, so re-running is
idempotent and the output is byte-stable -- reruns produce no diff, and real
diffs show only genuinely new readings.

Usage:
    python3 scrape.py                  # fetch and merge into data/
    python3 scrape.py --dry-run        # fetch and report, write nothing
    python3 scrape.py --stats          # summarise the archive
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("STATION_URL", "https://alarm.lucomagno.ch/fe/measure")
USER_AGENT = os.environ.get(
    "STATION_UA",
    "lucomagno-wind-archive/1.0 (+https://github.com/) personal weather archive",
)
DATA_DIR = os.environ.get("DATA_DIR", "data")

FIELDS = ["timestamp", "wind_speed_kmh", "wind_dir_deg", "temp_c"]

# The page is German. Match on a prefix so a unit tweak ("Windrichtung [°]")
# doesn't silently drop a series.
METRICS = {
    "Windgeschwindigkeit": "wind_speed_kmh",
    "Windrichtung": "wind_dir_deg",
    "Temperatur": "temp_c",
}

DATATABLE_RE = re.compile(r"new google\.visualization\.DataTable\((\{.*?\})\)\s*;", re.S)
GDATE_RE = re.compile(r"^Date\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)$")

log = logging.getLogger("scrape")


class ParseError(RuntimeError):
    """The page loaded but didn't contain the data we expect."""


def parse_gdate(value: str) -> dt.datetime:
    """Google Charts Date(y, m, d, H, M, S) -> datetime. Month is 0-indexed."""
    m = GDATE_RE.match(value.strip())
    if not m:
        raise ParseError(f"unrecognised date literal: {value!r}")
    year, month0, day, hour, minute, second = (int(g) for g in m.groups())
    return dt.datetime(year, month0 + 1, day, hour, minute, second)


def fetch_once(url: str = URL, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def fetch(url: str = URL, attempts: int = 4) -> dict[str, dict[str, str]]:
    """Fetch and parse, retrying on the failure modes this site actually has.

    Under load the server answers 200 with the page chrome but no chart data
    rather than a 5xx, so an empty parse is a transient condition to retry -
    not a layout change. Backoff is generous because the throttle takes a
    while to clear, and we have a 24h window of slack to spend.
    """
    delay = 20
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return parse(fetch_once(url))
        except (ParseError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last = exc
            if attempt == attempts:
                break
            log.warning("attempt %d/%d failed (%s); retrying in %ds",
                        attempt, attempts, exc, delay)
            time.sleep(delay)
            delay *= 2
    raise last  # type: ignore[misc]


def parse(html: str) -> dict[str, dict[str, str]]:
    """Extract all three series, keyed by ISO timestamp string."""
    tables = DATATABLE_RE.findall(html)
    if not tables:
        raise ParseError("no DataTable literals found - page layout changed?")

    rows: dict[str, dict[str, str]] = {}
    seen: set[str] = set()

    for raw in tables:
        try:
            table = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable DataTable: %s", exc)
            continue

        cols = table.get("cols", [])
        if len(cols) < 2:
            continue
        label = cols[1].get("label", "")
        field = next((f for prefix, f in METRICS.items() if label.startswith(prefix)), None)
        if field is None:
            log.debug("ignoring unknown series %r", label)
            continue
        seen.add(field)

        for row in table.get("rows", []):
            cells = row.get("c", [])
            if len(cells) < 2:
                continue
            ts_raw, value = cells[0].get("v"), cells[1].get("v")
            if ts_raw is None or value is None:
                continue  # station outages show up as nulls
            try:
                ts = parse_gdate(ts_raw)
            except ParseError as exc:
                log.warning("%s", exc)
                continue
            # Keep the page's own numeric formatting; str() of a float would
            # churn the CSV (13.68 -> 13.68 is fine, but 5 -> 5.0 is a diff).
            rows.setdefault(ts.isoformat(sep=" "), {})[field] = format_value(value)

    missing = set(METRICS.values()) - seen
    if missing:
        log.warning("series missing from page: %s", ", ".join(sorted(missing)))
    if not rows:
        raise ParseError("DataTables present but no usable rows")
    return rows


def format_value(value) -> str:
    """Stable string form so unchanged readings never produce a diff."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def month_path(ts: str) -> str:
    return os.path.join(DATA_DIR, f"{ts[:7]}.csv")  # data/YYYY-MM.csv


def read_month(path: str) -> dict[str, dict[str, str]]:
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {
            r["timestamp"]: {k: r.get(k, "") for k in FIELDS[1:]}
            for r in csv.DictReader(fh)
            if r.get("timestamp")
        }


def write_month(path: str, rows: dict[str, dict[str, str]]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for ts in sorted(rows):
            writer.writerow({"timestamp": ts, **{k: rows[ts].get(k, "") for k in FIELDS[1:]}})
    os.replace(tmp, path)  # atomic, so an interrupted run can't truncate the archive


def merge(scraped: dict[str, dict[str, str]]) -> tuple[int, int]:
    """Merge scraped rows into the monthly CSVs. Returns (added, changed)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    added = changed = 0

    by_month: dict[str, dict[str, dict[str, str]]] = {}
    for ts, values in scraped.items():
        by_month.setdefault(month_path(ts), {})[ts] = values

    for path, incoming in by_month.items():
        existing = read_month(path)
        before = json.dumps(existing, sort_keys=True)

        for ts, values in incoming.items():
            if ts not in existing:
                existing[ts] = {k: values.get(k, "") for k in FIELDS[1:]}
                added += 1
            else:
                # Only fill blanks or genuine corrections; never blank out a
                # value we already have because this fetch lacked that series.
                row = existing[ts]
                for field, value in values.items():
                    if row.get(field, "") != value:
                        row[field] = value
                        changed += 1

        if json.dumps(existing, sort_keys=True) != before:
            write_month(path, existing)
            log.info("wrote %s (%d rows)", path, len(existing))

    return added, changed


def load_archive() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if not os.path.isdir(DATA_DIR):
        return rows
    for name in sorted(os.listdir(DATA_DIR)):
        if name.endswith(".csv"):
            rows.update(read_month(os.path.join(DATA_DIR, name)))
    return rows


def show_stats() -> None:
    rows = load_archive()
    if not rows:
        print("archive is empty")
        return
    keys = sorted(rows)
    lo = dt.datetime.fromisoformat(keys[0])
    hi = dt.datetime.fromisoformat(keys[-1])
    span = hi - lo
    expected = int(span.total_seconds() // 600) + 1  # 10-minute cadence
    speeds = [float(rows[k]["wind_speed_kmh"]) for k in keys if rows[k].get("wind_speed_kmh")]
    print(f"rows        : {len(rows)}")
    print(f"range       : {keys[0]}  ->  {keys[-1]}  ({span})")
    print(f"completeness: {100 * len(rows) / expected:.1f}% of {expected} expected slots")
    if speeds:
        print(f"wind speed  : avg {sum(speeds) / len(speeds):.2f} km/h, max {max(speeds):.2f} km/h")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    ap.add_argument("--stats", action="store_true", help="summarise the archive and exit")
    ap.add_argument("--latest", action="store_true",
                    help="print the newest archived timestamp and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)sZ %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime

    if args.stats:
        show_stats()
        return 0

    if args.latest:
        rows = load_archive()
        print(max(rows) if rows else "empty")
        return 0

    try:
        rows = fetch()
    except ParseError as exc:
        log.error("%s", exc)
        return 1
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        log.error("fetch failed: %s", exc)
        return 1

    keys = sorted(rows)
    log.info("scraped %d readings: %s -> %s", len(rows), keys[0], keys[-1])

    if args.dry_run:
        log.info("dry run - nothing written")
        return 0

    added, changed = merge(rows)
    log.info("%d new, %d corrected", added, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
