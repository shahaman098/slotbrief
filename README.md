# SlotBrief

Front-desk briefing packs for indie aesthetics clinics.

Paste one day's appointments into a CSV. SlotBrief flags overlaps and tight turnarounds, drafts patient confirmation texts, and writes a local HTML today-board you can open in a browser. No accounts, no cloud, no patient data leaving the machine.

Built solo for [Next Byte Hacks V5](https://next-byte-hacks-v5.devpost.com) (7 Nov 2026). The wedge matches work I do on [Boto](https://boto.clinic): clinic ops that stay useful when the Wi-Fi dies.

## Why

Small clinics often live in a spreadsheet and a WhatsApp thread. Double-bookings and "can you remind them?" messages eat the morning. SlotBrief turns that CSV into:

1. A **today board** (schedule + flags)
2. Ready-to-send **confirmation drafts**
3. An **`day.ics` calendar file** (RFC 5545) you can import into Apple Calendar / Google Calendar
4. A machine-readable **report.json** for later tooling

## Quick start

```bash
python3 slotbrief.py samples/day.csv \
  --clinic "Boto Demo Clinic" \
  --day 2026-11-07 \
  --out out
```

Open `out/today-board.html` in a browser. Read `out/confirmations.txt`. Double-click `out/day.ics` to drop the day into your calendar. Inspect `out/report.json`.

## CSV columns

| Column | Required | Example |
|--------|----------|---------|
| `time` | yes | `09:30` |
| `duration_min` | yes | `45` |
| `patient` | yes | `Amira Khan` |
| `treatment` | yes | `Hydrafacial` |
| `phone` | yes | `+4474…` |
| `notes` | no | `First visit` |

## Flags

- **overlap** — next booking starts before the current one ends
- **gap** — turnaround shorter than `--min-gap` (default 10 minutes)
- **late_start** — first booking is at/after 11:00 (morning capacity unused)

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Screenshots

See `screenshots/` for the CLI summary and the generated today board.

## Stack

- Python 3.10+ standard library only (`argparse`, `csv`, `json`, `html`, `datetime`)
- Static HTML/CSS for the board

## What I would add next

- Google Sheets / Notion import
- Opt-in SMS send through a clinic's own provider (never store credentials in-repo)
- Hook into Boto's live booking calendar

## License

MIT
