#!/usr/bin/env python3
"""SlotBrief: front-desk briefing packs for indie aesthetics clinics.

Load a day's appointments, flag overlaps and gaps, draft patient confirmations,
and print a local HTML board the front desk can open without a SaaS login.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

TIME_FMT = "%H:%M"
DATE_FMT = "%Y-%m-%d"


@dataclass
class Appointment:
    time: str
    duration_min: int
    patient: str
    treatment: str
    phone: str
    notes: str = ""

    @property
    def start(self) -> datetime:
        return datetime.strptime(self.time, TIME_FMT)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)

    @property
    def end_label(self) -> str:
        return self.end.strftime(TIME_FMT)


@dataclass
class Issue:
    kind: str  # overlap | gap | late_start
    message: str
    related: list[str]


def load_appointments(path: Path) -> list[Appointment]:
    rows: list[Appointment] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"time", "duration_min", "patient", "treatment", "phone"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            missing = sorted(required - set(reader.fieldnames or []))
            raise SystemExit(f"CSV missing columns: {', '.join(missing)}")
        for i, row in enumerate(reader, start=2):
            if not any((row.get(k) or "").strip() for k in reader.fieldnames):
                continue
            try:
                duration = int(str(row["duration_min"]).strip())
            except ValueError as exc:
                raise SystemExit(f"Line {i}: duration_min must be an integer") from exc
            rows.append(
                Appointment(
                    time=str(row["time"]).strip(),
                    duration_min=duration,
                    patient=str(row["patient"]).strip(),
                    treatment=str(row["treatment"]).strip(),
                    phone=str(row["phone"]).strip(),
                    notes=str(row.get("notes") or "").strip(),
                )
            )
    rows.sort(key=lambda a: a.start)
    return rows


def analyse(appts: list[Appointment], min_gap_min: int = 10) -> list[Issue]:
    issues: list[Issue] = []
    for left, right in zip(appts, appts[1:]):
        if right.start < left.end:
            overlap = int((left.end - right.start).total_seconds() // 60)
            issues.append(
                Issue(
                    kind="overlap",
                    message=(
                        f"{left.patient} ({left.time}-{left.end_label}) overlaps "
                        f"{right.patient} ({right.time}) by {overlap} min"
                    ),
                    related=[left.patient, right.patient],
                )
            )
        else:
            gap = int((right.start - left.end).total_seconds() // 60)
            if gap < min_gap_min:
                issues.append(
                    Issue(
                        kind="gap",
                        message=(
                            f"Only {gap} min between {left.patient} and "
                            f"{right.patient} (want ≥{min_gap_min})"
                        ),
                        related=[left.patient, right.patient],
                    )
                )
    if appts and appts[0].start.hour >= 11:
        issues.append(
            Issue(
                kind="late_start",
                message=f"First booking is {appts[0].time}; morning capacity unused",
                related=[appts[0].patient],
            )
        )
    return issues


def confirmation_text(clinic: str, day: str, appt: Appointment) -> str:
    note = f" Note: {appt.notes}." if appt.notes else ""
    return (
        f"Hi {appt.patient.split()[0]}, this is {clinic}. Reminder: {appt.treatment} "
        f"on {day} at {appt.time} (about {appt.duration_min} min). "
        f"Reply YES to confirm or call us if you need to reschedule.{note}"
    )


def render_board(
    clinic: str,
    day: str,
    appts: list[Appointment],
    issues: list[Issue],
) -> str:
    rows = []
    for a in appts:
        rows.append(
            "<tr>"
            f"<td>{html.escape(a.time)}</td>"
            f"<td>{html.escape(a.end_label)}</td>"
            f"<td>{html.escape(a.patient)}</td>"
            f"<td>{html.escape(a.treatment)}</td>"
            f"<td>{html.escape(a.phone)}</td>"
            f"<td>{html.escape(a.notes or '—')}</td>"
            "</tr>"
        )
    issue_items = (
        "".join(f"<li class='{html.escape(i.kind)}'>{html.escape(i.message)}</li>" for i in issues)
        or "<li class='ok'>No schedule conflicts detected.</li>"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(clinic)} — {html.escape(day)}</title>
  <style>
    :root {{
      --ink: #1c1917;
      --paper: #faf7f2;
      --accent: #0f766e;
      --warn: #b45309;
      --bad: #b91c1c;
      --line: #e7e5e4;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
      background:
        radial-gradient(circle at 12% 8%, #dbece9 0, transparent 42%),
        radial-gradient(circle at 88% 0%, #f5e6d3 0, transparent 36%),
        var(--paper);
      color: var(--ink);
      padding: 2.5rem 1.25rem 3rem;
    }}
    main {{
      max-width: 920px;
      margin: 0 auto;
    }}
    h1 {{
      font-size: clamp(1.8rem, 4vw, 2.6rem);
      margin: 0 0 0.25rem;
      letter-spacing: -0.02em;
    }}
    .sub {{
      color: #57534e;
      margin: 0 0 1.75rem;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.95rem;
    }}
    section {{
      margin-bottom: 1.75rem;
    }}
    h2 {{
      font-size: 1.05rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin: 0 0 0.75rem;
      font-family: ui-sans-serif, system-ui, sans-serif;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: rgba(255,255,255,0.72);
      border: 1px solid var(--line);
    }}
    th, td {{
      text-align: left;
      padding: 0.7rem 0.75rem;
      border-bottom: 1px solid var(--line);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.92rem;
      vertical-align: top;
    }}
    th {{
      background: #f5f5f4;
      font-size: 0.75rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: #57534e;
    }}
    ul {{
      list-style: none;
      padding: 0;
      margin: 0;
    }}
    li {{
      padding: 0.65rem 0.8rem;
      margin-bottom: 0.4rem;
      border-left: 4px solid var(--accent);
      background: rgba(255,255,255,0.72);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.92rem;
    }}
    li.overlap, li.late_start {{ border-left-color: var(--bad); }}
    li.gap {{ border-left-color: var(--warn); }}
    li.ok {{ border-left-color: var(--accent); }}
    .stat {{
      display: inline-block;
      margin-right: 0.75rem;
      padding: 0.35rem 0.65rem;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.8);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.85rem;
    }}
    footer {{
      margin-top: 2rem;
      color: #78716c;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.8rem;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(clinic)}</h1>
    <p class="sub">Today board · {html.escape(day)} · generated by SlotBrief</p>
    <section>
      <span class="stat">{len(appts)} bookings</span>
      <span class="stat">{sum(a.duration_min for a in appts)} chair-minutes</span>
      <span class="stat">{len(issues)} flags</span>
    </section>
    <section>
      <h2>Schedule</h2>
      <table>
        <thead>
          <tr>
            <th>Start</th><th>End</th><th>Patient</th>
            <th>Treatment</th><th>Phone</th><th>Notes</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </section>
    <section>
      <h2>Front-desk flags</h2>
      <ul>{issue_items}</ul>
    </section>
    <footer>Open this file locally. No patient data leaves the machine.</footer>
  </main>
</body>
</html>
"""


def write_outputs(
    out_dir: Path,
    clinic: str,
    day: str,
    appts: list[Appointment],
    issues: list[Issue],
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    board = out_dir / "today-board.html"
    confirmations = out_dir / "confirmations.txt"
    report = out_dir / "report.json"

    board.write_text(render_board(clinic, day, appts, issues), encoding="utf-8")
    lines = [confirmation_text(clinic, day, a) for a in appts]
    confirmations.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    report.write_text(
        json.dumps(
            {
                "clinic": clinic,
                "day": day,
                "appointments": [asdict(a) for a in appts],
                "issues": [asdict(i) for i in issues],
                "stats": {
                    "bookings": len(appts),
                    "chair_minutes": sum(a.duration_min for a in appts),
                    "flags": len(issues),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"board": board, "confirmations": confirmations, "report": report}


def print_summary(
    clinic: str,
    day: str,
    appts: Iterable[Appointment],
    issues: list[Issue],
    paths: dict[str, Path],
) -> None:
    appt_list = list(appts)
    print(f"SlotBrief · {clinic} · {day}")
    print(f"  bookings: {len(appt_list)}")
    print(f"  chair-minutes: {sum(a.duration_min for a in appt_list)}")
    print(f"  flags: {len(issues)}")
    if issues:
        print("  issues:")
        for issue in issues:
            print(f"    - [{issue.kind}] {issue.message}")
    else:
        print("  issues: none")
    print("  wrote:")
    for key, path in paths.items():
        print(f"    - {key}: {path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slotbrief",
        description="Generate a clinic today-board and patient confirmation drafts.",
    )
    p.add_argument("csv", type=Path, help="Appointments CSV for one day")
    p.add_argument("--clinic", default="Indie Clinic", help="Clinic display name")
    p.add_argument("--day", default="", help="ISO date shown on the board (YYYY-MM-DD)")
    p.add_argument("--out", type=Path, default=Path("out"), help="Output directory")
    p.add_argument(
        "--min-gap",
        type=int,
        default=10,
        help="Minimum minutes between appointments before a gap flag (default 10)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    day = args.day or datetime.now().strftime(DATE_FMT)
    appts = load_appointments(args.csv)
    if not appts:
        print("No appointments found in CSV.", file=sys.stderr)
        return 1
    issues = analyse(appts, min_gap_min=args.min_gap)
    paths = write_outputs(args.out, args.clinic, day, appts, issues)
    print_summary(args.clinic, day, appts, issues, paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
