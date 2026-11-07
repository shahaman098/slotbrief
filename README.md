# SlotBrief

Front-desk briefing packs for indie aesthetics clinics.

Paste one day's — or one whole week's — appointments into a CSV. SlotBrief flags overlaps and tight turnarounds, drafts patient confirmation texts, writes a local HTML board you can open in a browser, and exports an importable calendar file. No accounts, no cloud, no patient data leaving the machine.

Built solo for [Next Byte Hacks V5](https://next-byte-hacks-v5.devpost.com) (7 Nov 2026). The wedge matches work I do on [Boto](https://boto.clinic): clinic ops that stay useful when the Wi-Fi dies.

## Why

Small clinics often live in a spreadsheet and a WhatsApp thread. Double-bookings and "can you remind them?" messages eat the morning. SlotBrief turns that CSV into:

1. A **today board** (schedule + flags)
2. Ready-to-send **confirmation drafts**
3. A **calendar file** (`day.ics` / `week.ics`, RFC 5545) that drops into Apple Calendar or Google Calendar
4. A machine-readable **report.json** for later tooling

## Quick start

**Web app** (recommended) — drag-and-drop CSV, interactive chair timeline, week view, one-click copy for each confirmation text, calendar and report export, in a dark editorial interface:

```bash
python3 slotbrief.py --serve        # opens http://127.0.0.1:8765
```

It binds to `127.0.0.1` only, so nothing you load leaves the machine. Try **Load sample day / week** on the start screen, or deep-link `http://127.0.0.1:8765/?sample=week&view=overview`.

**Command line** — the same engine, writing files:

```bash
# one day (gap, overlap and late-start flags firing)
python3 slotbrief.py samples/demo-day.csv \
  --clinic "Boto Demo Clinic" \
  --day 2026-11-07 \
  --out out

# a whole week (Mon–Fri sample in samples/week/)
python3 slotbrief.py --week samples/week/ \
  --clinic "Boto Demo Clinic" \
  --out out/week
```

`samples/demo-all-flags.csv` trips all five flags at once (including double-booked patient and unusable phone number).

Open `out/today-board.html` in a browser. Read `out/confirmations.txt`. Double-click `out/day.ics` to drop the day into your calendar. Inspect `out/report.json`.

Week mode writes `week-board.html`, `week.ics`, confirmations grouped per day, and a `report.json` with a block per day plus weekly totals.

## Auto-fix: the minimal-move rescheduler

Detecting a clash is easy; fixing it without wrecking the day is the hard part. `resolve_day()` does it in two steps:

1. **Keep the most bookings that already coexist.** Conflicts (overlaps, turnarounds under the minimum gap, a patient booked twice) are modelled as a weighted interval-scheduling problem. Sorting by end time and binary-searching (`bisect`) the latest compatible predecessor gives the optimal keep-set in O(n log n). The unit tests check it against a brute-force search of every subset on 250 random days.
2. **Re-place only the displaced bookings** at the free start time (5-minute grid, inside opening hours, respecting the gap to every neighbour) closest to where each one was. If nothing fits, the booking is reported as unplaceable rather than silently dropped.

In the web app, **Suggested fixes** shows the moves and the before/after conflict count; **Apply fixes** re-runs the analysis on the fixed schedule, rewrites the affected confirmations as "we need to move your appointment" messages, and lets you download the corrected schedule as a CSV. The sample day goes from 4 scheduling conflicts to 0 by moving 2 bookings. Things that moving can't fix (a bad phone number, a late start) stay flagged.

## CSV columns

| Column | Required | Example |
|--------|----------|---------|
| `time` | yes | `09:30` |
| `duration_min` | yes | `45` |
| `patient` | yes | `Amira Khan` |
| `treatment` | yes | `Hydrafacial` |
| `phone` | yes | `+4474…` |
| `notes` | no | `First visit` |
| `date` | for `--week` on a single CSV | `2026-11-03` |

For `--week`, pass either one CSV with a `date` column or a directory of files named `YYYY-MM-DD.csv` — which is exactly what `samples/week/` is. Single-day runs ignore `date`, so existing CSVs keep working unchanged.

## Flags

- **overlap** — next booking starts before the current one ends
- **gap** — turnaround shorter than `--min-gap` (default 10 minutes)
- **late_start** — first booking is at/after 11:00 (morning capacity unused)
- **double_booked_patient** — the same patient holds two slots that overlap (a confirmation for either one would be a lie)
- **bad_phone** — a number too short or containing letters, so the drafted SMS could not be sent anyway

Week mode additionally reports **utilisation**: booked chair-minutes as a share of the `--hours` open window (default `09:00-18:00`). It is deliberately uncapped — over 100% means the chairs are double-booked.

## How week mode works

Rows are grouped by date *before* analysis, so `analyse()` runs once per day: Friday 16:00 is never compared against Monday 09:00 as if they shared a timeline. Every day reuses the exact same flag logic as single-day mode, then totals, busiest day and utilisation roll up into `report.json`.

## Performance

The stdlib-only constraint is not a liability. `benchmarks/bench.py` generates
deterministic synthetic loads and times the real pipeline stages (best of 3):

| rows | days | CSV load | per-day flags | render + write | full CLI | peak mem |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 50 | 9 ms | 17 ms | 232 ms | **299 ms** | 4.3 MB |
| 10,000 | 500 | 96 ms | 173 ms | 2.34 s | **2.65 s** | 43.8 MB |
| 100,000 | 5,000 | 1.01 s | 1.79 s | 23.6 s | **27.2 s** | 437 MB |

Measured on an Apple M4 Pro, Python 3.9.6. A thousand appointments — fifty busy
clinic days — brief end-to-end in under a third of a second; even 100k rows
(5,000 days) complete in under half a minute, with render+write dominated by
serialising the generated HTML/ICS/JSON files. Reproduce with
`python3 benchmarks/bench.py`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

35 tests covering flag logic, RFC 5545 escaping/folding/UIDs, week grouping and aggregation, the rescheduler (including a brute-force optimality check on random schedules), CLI round-trips for both modes, and the web app's API (including a live HTTP round-trip).

## Screenshots

Web app:

- `screenshots/07-app-day-view.png` — day view: KPIs, chair timeline with conflicts highlighted, schedule, needs-attention list
- `screenshots/08-app-week-overview.png` — week overview with per-day utilisation and flags
- `screenshots/09-app-week-day.png` — a single day in week mode
- `screenshots/10-app-fixes-applied.png` — after Apply fixes: moved bookings marked, conflicts cleared
- `screenshots/06-app-landing.png` — landing screen (the hero is the product drawn as line-art: a timeline with one collision)

CLI / static boards:

- `screenshots/01-cli-summary.png` — day run with all three flags firing and the calendar export listed
- `screenshots/02-today-board.png` — the generated today board
- `screenshots/03-confirmations.png` — ready-to-send confirmation drafts
- `screenshots/04-week-board.png` — week board: five days, per-day schedules, flags and utilisation
- `screenshots/05-week-summary.png` — week CLI summary with per-day roll-up

## Stack

- Python 3.9+ standard library only (`argparse`, `csv`, `json`, `html`, `datetime`, `dataclasses`, `hashlib`, `itertools`)
- `http.server` + a dependency-free single-page UI (`web/index.html`, vanilla JS/CSS, responsive, keyboard-accessible, dark editorial design) served locally
- Static HTML/CSS boards for the CLI, RFC 5545 for the calendar

## What I would add next

- Google Sheets / Notion import
- Opt-in SMS send through a clinic's own provider (never store credentials in-repo)
- Hook into Boto's live booking calendar

## License

MIT
