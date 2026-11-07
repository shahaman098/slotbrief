#!/usr/bin/env python3
"""SlotBrief: front-desk briefing packs for indie aesthetics clinics.

Load a day's appointments, flag overlaps and gaps, draft patient confirmations,
and print a local HTML board the front desk can open without a SaaS login.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Iterable, TextIO

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
    date: str = ""  # YYYY-MM-DD when the CSV carries a date column (week mode)
    moved_from: str = ""  # original HH:MM when the rescheduler moved this booking

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
    kind: str  # overlap | gap | late_start | double_booked_patient | bad_phone
    message: str
    related: list[str]


def load_appointments(path: Path) -> list[Appointment]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")  # Excel's "CSV UTF-8" starts with a BOM
    except UnicodeDecodeError:
        text = raw.decode("cp1252")  # older Excel on Windows
    return parse_appointments(io.StringIO(text, newline=""))


TIME_INPUTS = ("%H:%M", "%H.%M", "%I:%M%p", "%I:%M %p", "%I%p", "%I %p")
MAX_DURATION_MIN = 720


def _normalise_time(value: str, line: int) -> str:
    """'9:30', '09.30' or '9:30 am' all become '09:30'."""
    cleaned = " ".join(value.strip().upper().split())
    for fmt in TIME_INPUTS:
        try:
            return datetime.strptime(cleaned, fmt).strftime(TIME_FMT)
        except ValueError:
            continue
    raise SystemExit(f"Line {line}: time must look like 09:30 or 9:30am (got {value!r})")


def _delimiter(text: str) -> str:
    """Excel in much of Europe writes semicolon-separated 'CSV'."""
    header = text.split("\n", 1)[0]
    return max((",", ";", "\t"), key=header.count)


def parse_appointments(fh: TextIO) -> list[Appointment]:
    """Parse appointment rows from any open CSV text stream."""
    rows: list[Appointment] = []
    text = fh.read().lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=_delimiter(text))
    # tolerate "Time", " phone" and other spreadsheet-isms in the header row
    reader.fieldnames = [(name or "").strip().lower() for name in reader.fieldnames or []]
    required = {"time", "duration_min", "patient", "treatment", "phone"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        missing = sorted(required - set(reader.fieldnames or []))
        raise SystemExit(f"CSV missing columns: {', '.join(missing)}")
    for i, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        try:
            duration = int(str(row["duration_min"]).strip())
        except ValueError as exc:
            raise SystemExit(f"Line {i}: duration_min must be an integer") from exc
        if not 1 <= duration <= MAX_DURATION_MIN:
            raise SystemExit(
                f"Line {i}: duration_min must be between 1 and {MAX_DURATION_MIN} (got {duration})"
            )
        patient = str(row["patient"] or "").strip()
        if not patient:
            raise SystemExit(f"Line {i}: patient is empty")
        date_value = str(row.get("date") or "").strip()
        if date_value:
            try:
                datetime.strptime(date_value, DATE_FMT)
            except ValueError as exc:
                raise SystemExit(f"Line {i}: date must be YYYY-MM-DD") from exc
        rows.append(
            Appointment(
                time=_normalise_time(str(row["time"] or ""), i),
                duration_min=duration,
                patient=patient,
                treatment=str(row["treatment"] or "").strip(),
                phone=str(row["phone"] or "").strip(),
                notes=str(row.get("notes") or "").strip(),
                date=date_value,
            )
        )
    rows.sort(key=lambda a: (a.date, a.start))
    return rows


def _day_from_name(path: Path) -> str:
    """A file named 2026-11-03.csv is its own date column."""
    stem = path.stem
    try:
        datetime.strptime(stem, DATE_FMT)
    except ValueError:
        return ""
    return stem


def load_week(path: Path) -> list[Appointment]:
    """Load a whole week: a directory of YYYY-MM-DD.csv files, or one CSV with a date column."""
    rows: list[Appointment] = []
    if path.is_dir():
        files = sorted(path.glob("*.csv"))
        if not files:
            raise SystemExit(f"No CSV files found in {path}")
        for file in files:
            file_day = _day_from_name(file)
            for appt in load_appointments(file):
                if not appt.date:
                    if not file_day:
                        raise SystemExit(
                            f"{file.name}: needs a 'date' column or a YYYY-MM-DD filename"
                        )
                    appt.date = file_day
                rows.append(appt)
    else:
        rows = load_appointments(path)
        dated = [a for a in rows if a.date]
        if not dated:
            raise SystemExit(
                "--week needs a CSV with a 'date' column (YYYY-MM-DD), "
                "or a directory of YYYY-MM-DD.csv files"
            )
        if len(dated) != len(rows):
            raise SystemExit(
                "--week: every row needs a date once any row has one "
                "(dated and undated rows cannot be mixed)"
            )
    rows.sort(key=lambda a: (a.date, a.start))
    return rows


def group_by_day(appts: list[Appointment]) -> list[tuple[str, list[Appointment]]]:
    """Split into (day, appointments), ordered by date, each day in start order.

    Grouping first is what stops a Friday 16:00 slot being compared against
    Monday 09:00 as if they shared a timeline.
    """

    def day_key(a: Appointment) -> str:
        return a.date

    return [(day, list(group)) for day, group in groupby(appts, key=day_key)]


def analyse(appts: list[Appointment], min_gap_min: int = 10) -> list[Issue]:
    appts = sorted(appts, key=lambda a: a.start)  # callers need not pre-sort
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
    issues.extend(_patient_collisions(appts))
    issues.extend(_bad_phones(appts))
    return issues


def _patient_collisions(appts: list[Appointment]) -> list[Issue]:
    """Same patient holding two slots that overlap — a confirmation would lie."""
    issues: list[Issue] = []
    by_patient: dict[str, list[Appointment]] = {}
    for a in appts:
        by_patient.setdefault(a.patient, []).append(a)
    for patient, slots in by_patient.items():
        own = sorted(slots, key=lambda s: s.start)
        for left, right in zip(own, own[1:]):
            if right.start < left.end:
                issues.append(
                    Issue(
                        kind="double_booked_patient",
                        message=(
                            f"{patient} is booked twice at once "
                            f"({left.time}-{left.end_label} and {right.time})"
                        ),
                        related=[patient],
                    )
                )
    return issues


def _bad_phones(appts: list[Appointment]) -> list[Issue]:
    """Numbers that cannot receive the drafted SMS (too short, or letters in them)."""
    issues: list[Issue] = []
    for a in appts:
        digits = sum(ch.isdigit() for ch in a.phone)
        if digits < 10 or any(ch.isalpha() for ch in a.phone):
            shown = a.phone or "no number"
            issues.append(
                Issue(
                    kind="bad_phone",
                    message=(
                        f"{a.patient} has an unusable number ({shown}); "
                        "confirmation SMS cannot be sent"
                    ),
                    related=[a.patient],
                )
            )
    return issues


def parse_hours(spec: str) -> tuple[datetime, datetime]:
    """Parse an open window like '09:00-18:00' into (open, close) datetimes."""
    try:
        open_s, close_s = (part.strip() for part in spec.split("-"))
        open_dt = datetime.strptime(open_s, TIME_FMT)
        close_dt = datetime.strptime(close_s, TIME_FMT)
    except ValueError as exc:
        raise SystemExit(f"--hours must look like HH:MM-HH:MM, e.g. 09:00-18:00 (got {spec!r})") from exc
    if close_dt <= open_dt:
        raise SystemExit(f"--hours: closing time {close_s} must be after opening time {open_s}")
    return open_dt, close_dt


def utilisation(appts: Iterable[Appointment], hours: tuple[datetime, datetime]) -> float:
    """Booked chair-minutes as a percentage of the open window.

    Deliberately not capped: over 100% means the chairs are double-booked.
    """
    window_min = (hours[1] - hours[0]).total_seconds() / 60
    if window_min <= 0:
        return 0.0
    booked = sum(a.duration_min for a in appts)
    return round(100.0 * booked / window_min, 1)


@dataclass
class Move:
    patient: str
    treatment: str
    old: str
    new: str
    shift_min: int
    duration_min: int = 0


CONFLICT_KINDS = {"overlap", "gap", "double_booked_patient"}
SLOT_STEP_MIN = 5


PIN_WEIGHT = 10**9


def _keep_set(
    appts: list[Appointment], min_gap_min: int, pinned: frozenset[int] | set[int] = frozenset()
) -> set[int]:
    """Largest set of bookings that need no change (weighted interval scheduling).

    Every kept pair is separated by at least ``min_gap_min`` so neither overlaps
    nor tight turnarounds survive. Sorting by end time lets ``bisect`` find, for
    each booking, the latest booking it can follow: O(n log n) overall. Weights
    make count dominate, then total booked minutes break ties. Pinned bookings
    (indices into ``appts``) outweigh everything else, so they are never displaced.
    """
    order = sorted(range(len(appts)), key=lambda i: (appts[i].end, appts[i].start))
    ends = [appts[i].end for i in order]
    gap = timedelta(minutes=min_gap_min)
    prev: list[int] = []
    for i in order:
        # last booking (in end order) finishing early enough to precede this one
        prev.append(bisect_right(ends, appts[i].start - gap) - 1)
    weight = [10_000 + appts[i].duration_min + (PIN_WEIGHT if i in pinned else 0) for i in order]
    best = [0] * (len(order) + 1)
    take = [False] * len(order)
    for k in range(len(order)):
        with_k = weight[k] + best[prev[k] + 1]
        take[k] = with_k > best[k]
        best[k + 1] = max(best[k], with_k)
    kept: set[int] = set()
    k = len(order) - 1
    while k >= 0:
        if take[k]:
            kept.add(order[k])
            k = prev[k]
        else:
            k -= 1
    return kept


def resolve_day(
    appts: list[Appointment],
    hours: tuple[datetime, datetime],
    min_gap_min: int = 10,
    pinned: frozenset[int] | set[int] = frozenset(),
) -> tuple[list[Appointment], list[Move], list[Appointment]]:
    """Fix overlaps, tight gaps and double-bookings with as few moves as possible.

    1. Keep the maximum set of bookings that already coexist legally.
    2. Re-place each displaced booking at the free start time (5-minute grid,
       inside opening hours) closest to where it was.
    ``pinned`` holds indices of bookings that must not move (used by ``find_fit``).
    Returns (schedule after fixes, moves made, bookings that could not be placed).
    """
    kept = _keep_set(appts, min_gap_min, pinned)
    gap = timedelta(minutes=min_gap_min)
    placed = [a for i, a in enumerate(appts) if i in kept]
    moves: list[Move] = []
    unplaced: list[Appointment] = []
    open_dt, close_dt = hours
    for i in sorted((i for i in range(len(appts)) if i not in kept), key=lambda i: appts[i].start):
        a = appts[i]
        span = timedelta(minutes=a.duration_min)
        slots = []
        t = open_dt
        while t + span <= close_dt:
            slots.append(t)
            t += timedelta(minutes=SLOT_STEP_MIN)
        slots.sort(key=lambda t: (abs((t - a.start).total_seconds()), t))
        chosen = next(
            (
                t
                for t in slots
                if all(t >= o.end + gap or t + span <= o.start - gap for o in placed)
            ),
            None,
        )
        if chosen is None:
            unplaced.append(a)
            placed.append(a)  # leave it where it is so the rest stay consistent
            continue
        moved = replace(a, time=chosen.strftime(TIME_FMT), moved_from=a.moved_from or a.time)
        placed.append(moved)
        moves.append(
            Move(
                patient=a.patient,
                treatment=a.treatment,
                old=a.time,
                new=moved.time,
                shift_min=int((chosen - a.start).total_seconds() // 60),
                duration_min=a.duration_min,
            )
        )
    fixed = sorted(placed, key=lambda a: a.start)
    return fixed, moves, unplaced


@dataclass
class FitOption:
    """One way to squeeze an extra booking into the day."""

    time: str  # recommended start
    end: str
    moves: list[Move]  # existing bookings that must shift to make room (empty = free)
    free_from: str = ""  # for free options: earliest start in the same contiguous free window
    free_to: str = ""  # ... and the latest

    @property
    def disruption_min(self) -> int:
        return sum(abs(m.shift_min) for m in self.moves)


def find_fit(
    appts: list[Appointment],
    hours: tuple[datetime, datetime],
    duration_min: int,
    min_gap_min: int = 10,
    prefer: str = "",
    limit: int = 4,
    treatment: str = "Walk-in",
    patient: str = "New patient",
) -> list[FitOption]:
    """Where can one more booking go, and who has to budge for it?

    Tries every start on the 5-minute grid. For each, the new booking is pinned and
    the same minimal-move solver re-places whatever it displaces, so every option is
    a complete conflict-free day. Options are ranked by (people moved, total minutes
    shifted, distance from ``prefer``). Starts that move nobody are collapsed into
    free windows. The existing schedule is first fixed with ``resolve_day`` so the
    cost of an option is only what the new booking causes.
    """
    base, _, left = resolve_day(appts, hours, min_gap_min)
    if left:  # the day is already overfull: nothing added to it can fit
        return []
    open_dt, close_dt = hours
    span = timedelta(minutes=duration_min)
    pref = datetime.strptime(prefer, TIME_FMT) if prefer else None
    free: list[datetime] = []
    tried: dict[tuple, tuple[datetime, list[Move]]] = {}
    t = open_dt
    while t + span <= close_dt:
        new = Appointment(
            time=t.strftime(TIME_FMT), duration_min=duration_min, patient=patient, treatment=treatment, phone=""
        )
        trial = base + [new]
        _, moves, unplaced = resolve_day(trial, hours, min_gap_min, pinned={len(trial) - 1})
        if not unplaced:
            if not moves:
                free.append(t)
            else:
                sig = tuple((m.patient, m.old, m.new) for m in moves)
                cost = (len(moves), sum(abs(m.shift_min) for m in moves))
                dist = abs((t - pref).total_seconds()) if pref else 0
                best = tried.get(sig)
                if best is None or dist < best[2]:
                    tried[sig] = (t, moves, dist, cost)
        t += timedelta(minutes=SLOT_STEP_MIN)

    def fmt(d: datetime) -> str:
        return d.strftime(TIME_FMT)

    options: list[tuple[tuple, FitOption]] = []
    step = timedelta(minutes=SLOT_STEP_MIN)
    run: list[datetime] = []
    for d in free + [None]:  # type: ignore[list-item]
        if run and (d is None or d - run[-1] != step):
            pick = min(run, key=lambda x: (abs((x - pref).total_seconds()) if pref else 0, x))
            dist = abs((pick - pref).total_seconds()) if pref else 0
            opt = FitOption(fmt(pick), fmt(pick + span), [], fmt(run[0]), fmt(run[-1]))
            options.append(((0, 0, dist, pick), opt))
            run = []
        if d is not None:
            run.append(d)
    for start, moves, dist, cost in tried.values():
        opt = FitOption(fmt(start), fmt(start + span), moves)
        options.append(((cost[0], cost[1], dist, start), opt))
    options.sort(key=lambda kv: kv[0])
    return [o for _, o in options[:limit]]


def conflict_count(appts: list[Appointment], min_gap_min: int = 10) -> int:
    """How many overlap / tight-gap / double-booking flags a schedule trips."""
    return sum(1 for i in analyse(appts, min_gap_min=min_gap_min) if i.kind in CONFLICT_KINDS)


def reschedule_text(clinic: str, day: str, appt: Appointment) -> str:
    return (
        f"Hi {appt.patient.split()[0]}, this is {clinic}. We need to move your "
        f"{appt.treatment} on {day} from {appt.moved_from} to {appt.time} "
        f"(about {appt.duration_min} min). Reply YES to confirm the new time or "
        "call us to pick another."
    )


def confirmation_text(clinic: str, day: str, appt: Appointment) -> str:
    note = f" Note: {appt.notes}." if appt.notes else ""
    return (
        f"Hi {appt.patient.split()[0]}, this is {clinic}. Reminder: {appt.treatment} "
        f"on {day} at {appt.time} (about {appt.duration_min} min). "
        f"Reply YES to confirm or call us if you need to reschedule.{note}"
    )


def _ics_escape(text: str) -> str:
    """Escape per RFC 5545 §3.3.11 (backslash, semicolon, comma, newline)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line: str) -> list[str]:
    """Fold long content lines at 74 octets (RFC 5545 §3.1)."""
    if len(line) <= 75:
        return [line]
    parts, current = [], ""
    for ch in line:
        if len(current) >= 74:
            parts.append(current)
            current = " " + ch
        else:
            current += ch
    parts.append(current)
    return parts


def _ics_calendar(clinic: str, groups: list[tuple[str, list[Appointment]]]) -> str:
    """Render (day, appointments) groups as one RFC 5545 VCALENDAR."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//SlotBrief//{clinic}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for day, appts in groups:
        day_date = datetime.strptime(day, DATE_FMT).date()
        for a in appts:
            start = datetime.combine(day_date, a.start.time())
            end = start + timedelta(minutes=a.duration_min)
            uid = hashlib.sha1(
                f"{day}|{a.time}|{a.patient}|{a.treatment}".encode("utf-8")
            ).hexdigest()[:16]
            event = [
                "BEGIN:VEVENT",
                f"UID:{uid}@slotbrief",
                f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{_ics_escape(f'{a.treatment} - {a.patient}')}",
                f"DESCRIPTION:{_ics_escape(a.notes)}",
                f"LOCATION:{_ics_escape(clinic)}",
                "END:VEVENT",
            ]
            for raw in event:
                lines.extend(_ics_fold(raw))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def ics_text(clinic: str, day: str, appts: list[Appointment]) -> str:
    """Render the day as an RFC 5545 VCALENDAR the front desk can import."""
    return _ics_calendar(clinic, [(day, appts)])


def ics_week_text(clinic: str, groups: list[tuple[str, list[Appointment]]]) -> str:
    """Render every day of the week into a single importable VCALENDAR."""
    return _ics_calendar(clinic, groups)


def _classify(appts: list[Appointment], issues: Iterable[Issue]) -> dict[int, str]:
    """Map id(appointment) -> 'bad' (time conflict) or 'warn' (worth a look).

    Conflicts come from the geometry itself, so two bookings for the same
    patient are each marked rather than guessed from names.
    """
    marks: dict[int, str] = {}
    for i, left in enumerate(appts):
        for right in appts[i + 1 :]:
            if left.start < right.end and right.start < left.end:
                marks[id(left)] = marks[id(right)] = "bad"
    warn_names = {
        name
        for issue in issues
        if issue.kind in {"gap", "late_start", "bad_phone"}
        for name in issue.related
    }
    for a in appts:
        if a.patient in warn_names:
            marks.setdefault(id(a), "warn")
    return marks


def _schedule_rows(appts: Iterable[Appointment], marks: dict[int, str] | None = None) -> str:
    """Table rows shared by the day board and the week board."""
    marks = marks or {}
    rows = []
    for a in appts:
        cls = marks.get(id(a), "")
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{html.escape(a.time)}</td>"
            f"<td>{html.escape(a.end_label)}</td>"
            f"<td>{html.escape(a.patient)}</td>"
            f"<td>{html.escape(a.treatment)}</td>"
            f"<td>{html.escape(a.phone)}</td>"
            f"<td>{html.escape(a.notes or '—')}</td>"
            "</tr>"
        )
    return "".join(rows)


KIND_LABELS = {
    "overlap": "Overlap",
    "gap": "Tight gap",
    "late_start": "Late start",
    "double_booked_patient": "Double-booked",
    "bad_phone": "Bad number",
}


def _issue_items(issues: Iterable[Issue]) -> str:
    issue_list = list(issues)
    return (
        "".join(
            f"<li class='{html.escape(i.kind)}'>"
            f"<span class='tag'>{html.escape(KIND_LABELS.get(i.kind, i.kind))}</span>"
            f"{html.escape(i.message)}</li>"
            for i in issue_list
        )
        or "<li class='ok'>No schedule conflicts detected.</li>"
    )


def _timeline(
    appts: list[Appointment],
    issues: Iterable[Issue],
    window: tuple[datetime, datetime] | None = None,
) -> str:
    """A chair-time strip: one bar per booking, stacked into lanes when they collide."""
    if not appts:
        return ""
    marks = _classify(appts, issues)
    lo = min(a.start for a in appts)
    hi = max(a.end for a in appts)
    if window:
        lo, hi = min(lo, window[0]), max(hi, window[1])
    lo = lo.replace(minute=0, second=0)
    if hi.minute or hi.second:
        hi = hi.replace(minute=0, second=0) + timedelta(hours=1)
    total = (hi - lo).total_seconds() / 60

    lane_ends: list[datetime] = []
    placed: list[tuple[Appointment, int]] = []
    for a in sorted(appts, key=lambda x: x.start):
        for lane, ends in enumerate(lane_ends):
            if ends <= a.start:
                lane_ends[lane] = a.end
                break
        else:
            lane = len(lane_ends)
            lane_ends.append(a.end)
        placed.append((a, lane))

    ticks = []
    hour = lo
    while hour <= hi:
        left = (hour - lo).total_seconds() / 60 / total * 100
        ticks.append(f"<i class='tick' style='left:{left:.2f}%'><span>{hour.strftime('%H')}</span></i>")
        hour += timedelta(hours=1)

    bars = []
    for a, lane in placed:
        left = (a.start - lo).total_seconds() / 60 / total * 100
        width = a.duration_min / total * 100
        cls = marks.get(id(a), "ok")
        tip = f"{a.patient} · {a.treatment} · {a.time}-{a.end_label}"
        bars.append(
            f"<div class='bar {cls}' style='left:{left:.2f}%;width:{width:.2f}%;top:{lane * 44}px' "
            f"title='{html.escape(tip, quote=True)}'>"
            f"<b>{html.escape(a.patient)}</b><small>{html.escape(a.time)}</small></div>"
        )
    height = len(lane_ends) * 44 + 4
    return (
        "<div class='timeline'>"
        f"<div class='lanes' style='height:{height}px'>{''.join(ticks)}{''.join(bars)}</div>"
        "<div class='legend'><span class='k ok'></span>ok"
        "<span class='k warn'></span>worth a look"
        "<span class='k bad'></span>time conflict</div>"
        "</div>"
    )


TIMELINE_CSS = """
    .timeline { margin-bottom: 1.5rem; }
    .lanes {
      position: relative;
      margin: 1.4rem 0 0.4rem;
      border-top: 1px solid var(--line);
    }
    .tick {
      position: absolute; top: 0; bottom: 0; width: 0;
      border-left: 1px dashed var(--line);
    }
    .tick span {
      position: absolute; top: -1.2rem; left: -0.6rem;
      font: 0.7rem ui-sans-serif, system-ui, sans-serif; color: #78716c;
    }
    .bar {
      position: absolute; height: 40px; overflow: hidden;
      padding: 3px 7px; border-radius: 6px; border: 1px solid #5eada5;
      background: #d3ece8; color: #134e4a;
      font-family: ui-sans-serif, system-ui, sans-serif;
      display: flex; flex-direction: column; justify-content: center;
      white-space: nowrap; text-overflow: ellipsis;
    }
    .bar b { font-size: 0.78rem; overflow: hidden; text-overflow: ellipsis; }
    .bar small { font-size: 0.68rem; opacity: 0.75; }
    .bar.warn { background: #fde7c4; border-color: var(--warn); color: #78350f; }
    .bar.bad { background: #fbd0d0; border-color: var(--bad); color: #7f1d1d; box-shadow: 0 0 0 2px rgba(185,28,28,0.18); }
    .legend {
      font: 0.74rem ui-sans-serif, system-ui, sans-serif; color: #57534e;
    }
    .legend .k {
      display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
      margin: 0 0.3rem 0 0.9rem; vertical-align: -1px; border: 1px solid;
    }
    .legend .k:first-child { margin-left: 0; }
    .k.ok { background: #d3ece8; border-color: #5eada5; }
    .k.warn { background: #fde7c4; border-color: var(--warn); }
    .k.bad { background: #fbd0d0; border-color: var(--bad); }
    .tablewrap { overflow-x: auto; }
    tr.bad td:first-child { box-shadow: inset 4px 0 0 var(--bad); }
    tr.bad td { background: #fff1f1; }
    tr.warn td:first-child { box-shadow: inset 4px 0 0 var(--warn); }
    tr.warn td { background: #fffaf0; }
    .tag {
      display: inline-block; margin-right: 0.6rem; padding: 0.05rem 0.45rem;
      font-size: 0.68rem; font-weight: 700; letter-spacing: 0.05em;
      text-transform: uppercase; border-radius: 3px; background: #e7e5e4; color: #44403c;
    }
    li.overlap .tag, li.double_booked_patient .tag { background: #fbd0d0; color: #7f1d1d; }
    li.gap .tag, li.late_start .tag, li.bad_phone .tag { background: #fde7c4; color: #78350f; }
    .utilbar {
      height: 6px; border-radius: 3px; background: #e7e5e4; margin: 0 0 0.9rem; overflow: hidden;
    }
    .utilbar i { display: block; height: 100%; background: var(--accent); }
    .utilbar i.over { background: var(--bad); }
    @media print {
      body { background: #fff; padding: 0; }
      .bar, .tag, tr.bad td, tr.warn td { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    }
"""


def render_board(
    clinic: str,
    day: str,
    appts: list[Appointment],
    issues: list[Issue],
) -> str:
    rows_html = _schedule_rows(appts, _classify(appts, issues))
    issue_items = _issue_items(issues)
    timeline_html = _timeline(appts, issues)
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
    li.overlap, li.double_booked_patient {{ border-left-color: var(--bad); }}
    li.late_start, li.bad_phone {{ border-left-color: var(--warn); }}
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
{TIMELINE_CSS}
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
      <h2>Chair timeline</h2>
      {timeline_html}
    </section>
    <section>
      <h2>Schedule</h2>
      <div class="tablewrap"><table>
        <thead>
          <tr>
            <th>Start</th><th>End</th><th>Patient</th>
            <th>Treatment</th><th>Phone</th><th>Notes</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table></div>
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


WEEK_CSS = """
    :root {
      --ink: #1c1917;
      --paper: #faf7f2;
      --accent: #0f766e;
      --warn: #b45309;
      --bad: #b91c1c;
      --line: #e7e5e4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
      background:
        radial-gradient(circle at 12% 8%, #dbece9 0, transparent 42%),
        radial-gradient(circle at 88% 0%, #f5e6d3 0, transparent 36%),
        var(--paper);
      color: var(--ink);
      padding: 2.5rem 1.25rem 3rem;
    }
    main { max-width: 980px; margin: 0 auto; }
    h1 {
      font-size: clamp(1.8rem, 4vw, 2.6rem);
      margin: 0 0 0.25rem;
      letter-spacing: -0.02em;
    }
    .sub {
      color: #57534e;
      margin: 0 0 1.5rem;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.95rem;
    }
    .stats { margin-bottom: 1.75rem; }
    .stat {
      display: inline-block;
      margin-right: 0.5rem;
      padding: 0.4rem 0.75rem;
      background: rgba(255,255,255,0.8);
      border: 1px solid var(--line);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.85rem;
    }
    .stat b { color: var(--accent); }
    section { margin-bottom: 2.25rem; }
    h2 {
      font-size: 1.05rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin: 0 0 0.35rem;
      font-family: ui-sans-serif, system-ui, sans-serif;
    }
    .daystats {
      color: #57534e;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.85rem;
      margin: 0 0 0.75rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: rgba(255,255,255,0.72);
      border: 1px solid var(--line);
      margin-bottom: 0.75rem;
    }
    th, td {
      text-align: left;
      padding: 0.55rem 0.7rem;
      border-bottom: 1px solid var(--line);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.88rem;
      vertical-align: top;
    }
    th {
      background: #f5f5f4;
      font-size: 0.72rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: #57534e;
    }
    ul { list-style: none; padding: 0; margin: 0; }
    li {
      padding: 0.5rem 0.75rem;
      margin-bottom: 0.4rem;
      border-left: 4px solid var(--accent);
      background: rgba(255,255,255,0.72);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.88rem;
    }
    li.overlap, li.double_booked_patient { border-left-color: var(--bad); }
    li.bad_phone { border-left-color: var(--warn); }
    li.gap { border-left-color: var(--warn); }
    li.late_start { border-left-color: var(--warn); }
    li.ok { border-left-color: var(--accent); color: #166534; }
    footer {
      margin-top: 2.5rem;
      color: #78716c;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: 0.8rem;
    }
"""


def render_week_board(
    clinic: str,
    days: list[tuple[str, list[Appointment], list[Issue]]],
    hours: tuple[datetime, datetime],
) -> str:
    """A week-at-a-glance board: one schedule section per day plus totals."""
    all_appts = [a for _, appts, _ in days for a in appts]
    all_issues = [i for _, _, issues in days for i in issues]
    busiest = max(days, key=lambda d: sum(a.duration_min for a in d[1]))
    busiest_day = datetime.strptime(busiest[0], DATE_FMT).strftime("%a %d %b")
    avg_util = round(
        sum(utilisation(appts, hours) for _, appts, _ in days) / len(days), 1
    )
    open_label = hours[0].strftime(TIME_FMT)
    close_label = hours[1].strftime(TIME_FMT)

    sections = []
    for day, appts, issues in days:
        label = datetime.strptime(day, DATE_FMT).strftime("%a %d %b %Y")
        util = utilisation(appts, hours)
        bar_cls = "over" if util > 100 else ""
        flag_word = "flag" if len(issues) == 1 else "flags"
        sections.append(
            f"""
    <section>
      <h2>{html.escape(label)}</h2>
      <p class="daystats">{len(appts)} bookings · {sum(a.duration_min for a in appts)} chair-minutes · {util}% of open hours · {len(issues)} {flag_word}</p>
      <div class="utilbar"><i class="{bar_cls}" style="width:{min(util, 100)}%"></i></div>
      {_timeline(appts, issues, window=hours)}
      <div class="tablewrap"><table>
        <thead>
          <tr>
            <th>Start</th><th>End</th><th>Patient</th>
            <th>Treatment</th><th>Phone</th><th>Notes</th>
          </tr>
        </thead>
        <tbody>
          {_schedule_rows(appts, _classify(appts, issues))}
        </tbody>
      </table></div>
      <ul>{_issue_items(issues)}</ul>
    </section>"""
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(clinic)} — week of {html.escape(days[0][0])}</title>
  <style>
{WEEK_CSS}{TIMELINE_CSS}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(clinic)}</h1>
    <p class="sub">Week board · {html.escape(days[0][0])} → {html.escape(days[-1][0])} · open {open_label}–{close_label} · generated by SlotBrief</p>
    <div class="stats">
      <span class="stat"><b>{len(days)}</b> days</span>
      <span class="stat"><b>{len(all_appts)}</b> bookings</span>
      <span class="stat"><b>{sum(a.duration_min for a in all_appts)}</b> chair-minutes</span>
      <span class="stat"><b>{len(all_issues)}</b> flags</span>
      <span class="stat"><b>{avg_util}%</b> avg utilisation</span>
      <span class="stat">busiest: <b>{html.escape(busiest_day)}</b></span>
    </div>{''.join(sections)}

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
    calendar = out_dir / "day.ics"
    report = out_dir / "report.json"

    board.write_text(render_board(clinic, day, appts, issues), encoding="utf-8")
    lines = [confirmation_text(clinic, day, a) for a in appts]
    confirmations.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    calendar.write_text(ics_text(clinic, day, appts), encoding="utf-8")
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
    return {"board": board, "confirmations": confirmations, "calendar": calendar, "report": report}


def write_fix_outputs(
    out_dir: Path,
    clinic: str,
    groups: list[tuple[str, list[Appointment]]],
    hours: tuple[datetime, datetime],
    min_gap_min: int,
) -> dict[str, object]:
    """Apply the minimal-move rescheduler to every day and write what changed.

    Writes fixed-schedule.csv (same columns as the input, so it re-imports) and
    reschedule-messages.txt (one ready-to-send text per patient who moved).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "fixed-schedule.csv"
    msg_path = out_dir / "reschedule-messages.txt"
    before = after = moved = 0
    unplaced: list[tuple[str, Appointment]] = []
    messages: list[str] = []
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["date", "time", "duration_min", "patient", "treatment", "phone", "notes"])
    for day, appts in groups:
        fixed, moves, left = resolve_day(appts, hours, min_gap_min)
        before += conflict_count(appts, min_gap_min)
        after += conflict_count(fixed, min_gap_min)
        moved += len(moves)
        unplaced.extend((day, a) for a in left)
        for a in fixed:
            writer.writerow([day, a.time, a.duration_min, a.patient, a.treatment, a.phone, a.notes])
            if a.moved_from and a.moved_from != a.time:
                messages.append(reschedule_text(clinic, day, a))
    csv_path.write_text(buf.getvalue(), encoding="utf-8")
    msg_path.write_text(("\n\n".join(messages) + "\n") if messages else "No bookings needed to move.\n", encoding="utf-8")
    return {
        "csv": csv_path,
        "messages": msg_path,
        "before": before,
        "after": after,
        "moved": moved,
        "unplaced": unplaced,
    }


def print_fix_summary(result: dict[str, object]) -> None:
    print("  auto-fix (minimal moves):")
    print(f"    - scheduling conflicts: {result['before']} -> {result['after']} by moving {result['moved']} booking(s)")
    for day, a in result["unplaced"]:  # type: ignore[union-attr]
        print(f"    - could not place {a.patient} ({day} {a.time}); call them")
    print(f"    - fixed schedule: {result['csv']}")
    print(f"    - reschedule texts: {result['messages']}")


def write_week_outputs(
    out_dir: Path,
    clinic: str,
    days: list[tuple[str, list[Appointment], list[Issue]]],
    hours: tuple[datetime, datetime],
) -> dict[str, Path]:
    """Write week-mode artifacts: board, confirmations, calendar and report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    board = out_dir / "week-board.html"
    confirmations = out_dir / "confirmations.txt"
    calendar = out_dir / "week.ics"
    report = out_dir / "report.json"

    board.write_text(render_week_board(clinic, days, hours), encoding="utf-8")

    blocks = []
    for day, appts, _ in days:
        lines = [f"[{day}]"] + [confirmation_text(clinic, day, a) for a in appts]
        blocks.append("\n".join(lines))
    confirmations.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

    calendar.write_text(
        ics_week_text(clinic, [(day, appts) for day, appts, _ in days]),
        encoding="utf-8",
    )

    day_reports = []
    for day, appts, issues in days:
        day_reports.append(
            {
                "day": day,
                "appointments": [asdict(a) for a in appts],
                "issues": [asdict(i) for i in issues],
                "stats": {
                    "bookings": len(appts),
                    "chair_minutes": sum(a.duration_min for a in appts),
                    "flags": len(issues),
                    "utilisation_pct": utilisation(appts, hours),
                },
            }
        )
    all_appts = [a for _, appts, _ in days for a in appts]
    all_issues = [i for _, _, issues in days for i in issues]
    busiest = max(days, key=lambda d: sum(a.duration_min for a in d[1]))
    report.write_text(
        json.dumps(
            {
                "clinic": clinic,
                "range": {"start": days[0][0], "end": days[-1][0]},
                "hours": f"{hours[0].strftime(TIME_FMT)}-{hours[1].strftime(TIME_FMT)}",
                "days": day_reports,
                "totals": {
                    "days": len(days),
                    "bookings": len(all_appts),
                    "chair_minutes": sum(a.duration_min for a in all_appts),
                    "flags": len(all_issues),
                    "busiest_day": busiest[0],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"board": board, "confirmations": confirmations, "calendar": calendar, "report": report}


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


def print_week_summary(
    clinic: str,
    days: list[tuple[str, list[Appointment], list[Issue]]],
    hours: tuple[datetime, datetime],
    paths: dict[str, Path],
) -> None:
    all_appts = [a for _, appts, _ in days for a in appts]
    all_issues = [i for _, _, issues in days for i in issues]
    print(f"SlotBrief · {clinic} · week {days[0][0]} → {days[-1][0]}")
    print(f"  days: {len(days)}")
    print(f"  bookings: {len(all_appts)}")
    print(f"  chair-minutes: {sum(a.duration_min for a in all_appts)}")
    print(f"  flags: {len(all_issues)}")
    print("  per day:")
    for day, appts, issues in days:
        kinds = ", ".join(sorted({i.kind for i in issues})) or "clear"
        print(
            f"    - {day}: {len(appts)} bookings · "
            f"{utilisation(appts, hours)}% utilised · "
            f"{len(issues)} flags ({kinds})"
        )
    print("  wrote:")
    for key, path in paths.items():
        print(f"    - {key}: {path}")


def print_fit(clinic: str, day: str, appts: list[Appointment], args: argparse.Namespace) -> None:
    hours = parse_hours(args.hours)
    options = find_fit(
        appts, hours, args.fit, args.min_gap, prefer=args.prefer, treatment=args.treatment
    )
    print(f"{clinic} · {day} · where can a {args.fit}-minute {args.treatment} go?")
    _, base_moves, _ = resolve_day(appts, hours, args.min_gap)
    if base_moves:
        print(f"  (judged on the day after its {len(base_moves)} existing conflict fix(es); see --serve > Suggested fixes)")
    if not options:
        print("  Nowhere: the day is full even after moving everyone who can move.")
        return
    for n, o in enumerate(options, 1):
        if not o.moves:
            window = f" (any start {o.free_from}-{o.free_to})" if o.free_from != o.free_to else ""
            print(f"  {n}. {o.time}-{o.end}  free: nobody moves{window}")
            continue
        who = "; ".join(f"{m.patient} {m.old}->{m.new}" for m in o.moves)
        print(f"  {n}. {o.time}-{o.end}  moves {len(o.moves)}: {who}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slotbrief",
        description="Generate a clinic today-board and patient confirmation drafts.",
    )
    p.add_argument(
        "csv",
        type=Path,
        nargs="?",
        help="Appointments CSV for one day, or a week with --week (not needed with --serve)",
    )
    p.add_argument("--clinic", default="Indie Clinic", help="Clinic display name")
    p.add_argument("--day", default="", help="ISO date shown on the board (YYYY-MM-DD)")
    p.add_argument("--out", type=Path, default=Path("out"), help="Output directory")
    p.add_argument(
        "--min-gap",
        type=int,
        default=10,
        help="Minimum minutes between appointments before a gap flag (default 10)",
    )
    p.add_argument(
        "--week",
        action="store_true",
        help=(
            "Whole-week mode: takes a CSV with a 'date' column, or a directory "
            "of YYYY-MM-DD.csv files; writes week-board.html, week.ics and a "
            "per-day report.json"
        ),
    )
    p.add_argument(
        "--hours",
        default="09:00-18:00",
        help="Open window used for utilisation %% in week mode (default 09:00-18:00)",
    )
    p.add_argument(
        "--fix",
        action="store_true",
        help="Also run the minimal-move rescheduler: writes fixed-schedule.csv and reschedule-messages.txt",
    )
    p.add_argument(
        "--fit",
        type=int,
        metavar="MINUTES",
        help="Where can one more booking of this length go, and who has to move? (single-day CSV)",
    )
    p.add_argument("--prefer", default="", help="With --fit, the time the patient would like (HH:MM)")
    p.add_argument("--treatment", default="Walk-in", help="With --fit, the treatment name")
    p.add_argument(
        "--serve",
        action="store_true",
        help="Run the local web app (drag-and-drop CSV, interactive boards) instead of writing files",
    )
    p.add_argument("--port", type=int, default=8765, help="Port for --serve (default 8765)")
    p.add_argument("--no-open", action="store_true", help="With --serve, don't open a browser tab")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serve:
        import webapp

        return webapp.serve(args.port, open_browser=not args.no_open)
    if args.csv is None:
        parser.error("a CSV path is required (or use --serve for the web app)")
    if not args.csv.exists():
        parser.error(f"file not found: {args.csv}")
    if args.min_gap < 0:
        parser.error("--min-gap can't be negative")
    if args.day:
        try:
            datetime.strptime(args.day, DATE_FMT)
        except ValueError:
            parser.error(f"--day must look like 2026-11-07 (got {args.day!r})")
    if args.fit is not None:
        if args.week:
            parser.error("--fit looks at a single day; drop --week")
        if not 5 <= args.fit <= 480:
            parser.error("--fit takes a length between 5 and 480 minutes")
    if args.prefer:
        try:
            datetime.strptime(args.prefer, TIME_FMT)
        except ValueError:
            parser.error(f"--prefer must look like 14:00 (got {args.prefer!r})")
    day = args.day or datetime.now().strftime(DATE_FMT)
    if args.week:
        rows = load_week(args.csv)
        if not rows:
            print("No appointments found in CSV.", file=sys.stderr)
            return 1
        hours = parse_hours(args.hours)
        days = [
            (day_label, day_appts, analyse(day_appts, min_gap_min=args.min_gap))
            for day_label, day_appts in group_by_day(rows)
        ]
        paths = write_week_outputs(args.out, args.clinic, days, hours)
        print_week_summary(args.clinic, days, hours, paths)
        if args.fix:
            print_fix_summary(
                write_fix_outputs(args.out, args.clinic, [(d, a) for d, a, _ in days], hours, args.min_gap)
            )
        return 0
    appts = load_appointments(args.csv)
    if not appts:
        print("No appointments found in CSV.", file=sys.stderr)
        return 1
    if args.fit is not None:
        print_fit(args.clinic, day, appts, args)
        return 0
    issues = analyse(appts, min_gap_min=args.min_gap)
    paths = write_outputs(args.out, args.clinic, day, appts, issues)
    print_summary(args.clinic, day, appts, issues, paths)
    if args.fix:
        print_fix_summary(
            write_fix_outputs(args.out, args.clinic, [(day, appts)], parse_hours(args.hours), args.min_gap)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
