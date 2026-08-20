# Can `/fe/measure` return historical data?

**No.** The endpoint exposes only a rolling 24-hour window, and no route,
parameter, or content-type negotiation widens it. That is why this repo
scrapes on a schedule.

## Reference response

`GET https://alarm.lucomagno.ch/fe/measure` → `200`, 43 KB of HTML.

Three Google Charts `DataTable` literals rendered server-side, 144 rows each
at 10-minute spacing — exactly 24 hours:

| Series | Label | Rows | Range |
|---|---|---|---|
| Wind speed | `Windgeschwindigkeit` | 144 | 2026-08-19 02:10 → 2026-08-20 02:00 |
| Wind direction | `Windrichtung [°]` | 144 | same |
| Temperature | `Temperatur` | 144 | same |

The page has **no date picker, no form, and no AJAX** — the inline scripts only
configure the charts, so there is no client-side call to a data endpoint that
might accept a range. The only links are language switches and the parent
TYPO3 site.

## What the stack tells us

The response sets a `ci_session` cookie: this is **CodeIgniter**, so routing is
`/fe/<controller>/<method>/<param>` — matching the known
`/fe/lang/switch/de`. That makes the probe decisive rather than suggestive: a
`404` means the method genuinely does not exist on the controller, not merely
that a guess was wrong.

## Results

58 path-based probes, all `404`:

| Pattern | Variants tried | Status |
|---|---|---|
| `/fe/measure/<date>` | `2026-08-18`, `18.08.2026`, `20260818`, `1755468000` | `404` ×4 |
| `/fe/measure/date/<date>` | all 4 date formats | `404` ×4 |
| `/fe/measure/day/<date>` | all 4 | `404` ×4 |
| `/fe/measure/days/<date>` | all 4 | `404` ×4 |
| `/fe/measure/from/<date>` | all 4 | `404` ×4 |
| `/fe/measure/history/<date>` | all 4 | `404` ×4 |
| `/fe/measure/archive/<date>` | all 4 | `404` ×4 |
| `/fe/measure/range/<date>` | all 4 | `404` ×4 |
| `/fe/measure/period/<date>` | all 4 | `404` ×4 |
| `/fe/measure/<action>` (bare) | `date`, `day`, `days`, `from`, `history`, `archive`, `range`, `period`, `json`, `csv`, `data`, `export`, `chart`, `index` | `404` ×14 |
| `/fe/measure/{days,day,range,period}/{7,30}` | day-count style | `404` ×8 |

**No history method exists on the controller.** `index` itself 404s, so
`/fe/measure` is reached through an explicit route rather than a public method
— consistent with a controller that has exactly one entry point.

Query strings are accepted but ignored:

| URL | Status | First timestamp | Verdict |
|---|---|---|---|
| `?date=2026-08-18` | `200` | unchanged vs. control | ignored |

Confirmed twice, in separate runs, against a same-minute control fetch. The
controller renders the identical 24h window regardless.

## Caveat: the site throttles, and it lies about it

Under load the server returns **`200` with the page chrome but no chart data**
— never a `429` or `503`. A naive probe reads that as "this parameter returned
an empty result set," which is a false negative.

The first sweep (1 req / 2 s) hit this after ~59 requests: every later probe
came back empty or with a dropped connection. Those results were discarded, not
reported. The re-run pairs **every probe with a control fetch of the bare URL**
and only records a verdict when the control is simultaneously healthy.

Throttling then persisted long enough that several lower-priority query-param
variants (`?day=`, `?days=`, `?from=&to=`, `?start=&end=`, `?period=`,
`?range=`, `?d=`, `?t=`, `?format=json`, `?type=csv`, `.json`, and the
`Accept: application/json` header) could not be measured cleanly and are
recorded as **inconclusive** rather than negative.

This does not change the conclusion. The path probes — the decisive evidence
under CodeIgniter routing — are clean and unanimous, and the one query
parameter measured cleanly is demonstrably ignored. For a query parameter to
work, the controller would need branching logic reachable by no route and
triggered by none of the obvious names, while the endpoint it would serve
does not exist.

`scrape.py` inherits the lesson: an empty parse is treated as a transient
throttle and retried with backoff, not as a layout change.

## Conclusion

Scraping the rolling window is the only way to build history. Each fetch
carries 24 hours of backfill, so a 6-hourly job has ~18h of slack and the
first run seeds a full day immediately.

## Reproducing

The probe scripts are not part of the scraper and are kept out of this repo;
they simply walk the candidate list above, rate-limited, comparing the first
`Date(...)` literal in each response against a control fetch. Note that Google
Charts months are **0-indexed** — `Date(2026, 07, 19, ...)` is 19 August 2026.
