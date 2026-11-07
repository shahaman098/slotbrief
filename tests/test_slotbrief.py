#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slotbrief import (  # noqa: E402
    Appointment,
    analyse,
    group_by_day,
    ics_text,
    load_appointments,
    load_week,
    parse_hours,
    utilisation,
)


class SlotBriefTests(unittest.TestCase):
    def test_sample_detects_overlap_and_tight_gap(self) -> None:
        appts = load_appointments(ROOT / "samples" / "day.csv")
        issues = analyse(appts, min_gap_min=10)
        kinds = {i.kind for i in issues}
        self.assertIn("overlap", kinds)
        self.assertTrue(any(i.kind == "gap" for i in issues))

    def test_cli_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "slotbrief.py"),
                    str(ROOT / "samples" / "day.csv"),
                    "--clinic",
                    "Boto Demo Clinic",
                    "--day",
                    "2026-11-07",
                    "--out",
                    str(out),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("bookings: 6", proc.stdout)
            self.assertTrue((out / "today-board.html").exists())
            self.assertTrue((out / "confirmations.txt").exists())
            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["stats"]["bookings"], 6)
            self.assertGreaterEqual(report["stats"]["flags"], 1)
            board = (out / "today-board.html").read_text(encoding="utf-8")
            self.assertIn("Boto Demo Clinic", board)
            self.assertIn("Amira Khan", board)


class IcsTests(unittest.TestCase):
    """RFC 5545 export: structure, datetime formatting, escaping, folding."""

    DAY = "2026-11-07"

    def test_vcalendar_wraps_every_vevent(self) -> None:
        appts = [
            Appointment("09:00", 30, "Amira Khan", "Consultation", "5551", "First visit"),
            Appointment("10:00", 45, "Jordan Lee", "Hydrafacial", "5552", ""),
        ]
        text = ics_text("Indie Clinic", self.DAY, appts)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", text)
        self.assertIn("CALSCALE:GREGORIAN", text)
        self.assertEqual(text.count("BEGIN:VEVENT"), len(appts))
        self.assertEqual(text.count("BEGIN:VEVENT"), text.count("END:VEVENT"))
        # Every line is CRLF-terminated with no bare LF anywhere.
        self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_dtstart_and_dtend_use_the_requested_day(self) -> None:
        appts = [Appointment("09:30", 45, "Amira Khan", "Consultation", "5551")]
        text = ics_text("Indie Clinic", self.DAY, appts)
        start = self._field(text, "DTSTART")
        end = self._field(text, "DTEND")
        self.assertRegex(start, r"^\d{8}T\d{6}$")
        self.assertRegex(end, r"^\d{8}T\d{6}$")
        self.assertEqual(start[:8], "20261107")
        self.assertEqual(start[9:], "093000")
        self.assertEqual(end[9:], "101500")

    def test_dtend_reflects_duration_minutes(self) -> None:
        for duration, expected_end in ((15, "091500"), (60, "100000"), (95, "103500")):
            with self.subTest(duration=duration):
                appts = [Appointment("09:00", duration, "Amira Khan", "Consultation", "5551")]
                text = ics_text("Indie Clinic", self.DAY, appts)
                self.assertEqual(self._field(text, "DTEND"), f"20261107T{expected_end}")

    def test_commas_and_semicolons_are_escaped(self) -> None:
        appts = [
            Appointment(
                "09:00",
                30,
                "Dana, Ríos",
                "Peel; light",
                "5551",
                "Allergic to retinoids; patch test, then proceed",
            )
        ]
        text = ics_text("Indie Clinic, Main St", self.DAY, appts)
        summary = self._field(text, "SUMMARY")
        description = self._field(text, "DESCRIPTION")
        location = self._field(text, "LOCATION")
        self.assertEqual(summary, r"Peel\; light - Dana\, Ríos")
        self.assertEqual(description, r"Allergic to retinoids\; patch test\, then proceed")
        self.assertEqual(location, r"Indie Clinic\, Main St")

    def test_newlines_in_notes_become_escaped_n(self) -> None:
        appts = [Appointment("09:00", 30, "Amira Khan", "Consultation", "5551", "one\ntwo")]
        text = ics_text("Indie Clinic", self.DAY, appts)
        self.assertIn(r"DESCRIPTION:one\ntwo", text)
        self.assertNotIn("one\ntwo", text.replace("\r\n", ""))

    def test_backslashes_are_escaped_before_other_rules(self) -> None:
        # A literal backslash must not survive to be read as an escape char.
        appts = [Appointment("09:00", 30, "Amira Khan", "C:\\notes", "5551", "path\\to")]
        text = ics_text("Indie Clinic", self.DAY, appts)
        self.assertIn(r"SUMMARY:C:\\notes - Amira Khan", text)
        self.assertIn(r"DESCRIPTION:path\\to", text)

    def test_long_lines_fold_and_stay_within_75_octets(self) -> None:
        long_note = "A" * 300
        appts = [Appointment("09:00", 30, "Amira Khan", "Consultation", "5551", long_note)]
        text = ics_text("Indie Clinic", self.DAY, appts)
        for line in text.split("\r\n"):
            if not line:
                continue
            self.assertLessEqual(len(line.encode("utf-8")), 75, f"line too long: {line[:40]!r}")
        # Unfolded DESCRIPTION must reconstruct the original note exactly.
        unfolded = self._unfold(text)
        self.assertIn(f"DESCRIPTION:{long_note}", unfolded)

    def test_uid_is_stable_for_identical_inputs(self) -> None:
        appts = [Appointment("09:00", 30, "Amira Khan", "Consultation", "5551")]
        first = ics_text("Indie Clinic", self.DAY, appts)
        second = ics_text("Indie Clinic", self.DAY, appts)
        self.assertEqual(self._field(first, "UID"), self._field(second, "UID"))

    def test_appointments_on_different_days_get_different_uids(self) -> None:
        appts = [Appointment("09:00", 30, "Amira Khan", "Consultation", "5551")]
        a = self._field(ics_text("Indie Clinic", "2026-11-07", appts), "UID")
        b = self._field(ics_text("Indie Clinic", "2026-11-08", appts), "UID")
        self.assertNotEqual(a, b)

    @staticmethod
    def _field(text: str, name: str) -> str:
        for raw in text.split("\r\n"):
            if raw.startswith(f"{name}:"):
                return raw[len(name) + 1 :]
        raise AssertionError(f"{name} not found")

    @staticmethod
    def _unfold(text: str) -> str:
        return text.replace("\r\n ", "")


class WeekTests(unittest.TestCase):
    """--week mode: grouping, per-day flag isolation, aggregation, artifacts."""

    WEEK = ROOT / "samples" / "week"
    DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]

    def test_week_directory_groups_by_dated_filename(self) -> None:
        rows = load_week(self.WEEK)
        groups = group_by_day(rows)
        self.assertEqual([day for day, _ in groups], self.DAYS)
        self.assertEqual([len(appts) for _, appts in groups], [5, 5, 5, 4, 7])
        # Each day's appointments arrive in start order.
        for _, appts in groups:
            times = [a.time for a in appts]
            self.assertEqual(times, sorted(times))

    def test_flags_never_leak_across_day_boundaries(self) -> None:
        rows = load_week(self.WEEK)
        groups = group_by_day(rows)
        per_day_kinds = [{i.kind for i in analyse(appts)} for _, appts in groups]
        # Seeded demo data: exactly one flagged day per flag type, boundaries clean.
        self.assertEqual(per_day_kinds[0], set())  # Mon: clean
        self.assertEqual(per_day_kinds[1], {"gap", "overlap"})  # Tue
        self.assertEqual(per_day_kinds[2], {"gap"})  # Wed
        self.assertEqual(per_day_kinds[3], {"late_start"})  # Thu
        self.assertEqual(per_day_kinds[4], set())  # Fri: clean but busiest
        total = sum(len(analyse(appts)) for _, appts in groups)
        self.assertEqual(total, 4)
        # Ungrouped, the same rows would be compared as one 1900 timeline and
        # every day boundary would look like a double-booking.
        self.assertGreater(len(analyse(rows)), total)

    def test_week_requires_dates(self) -> None:
        with self.assertRaises(SystemExit):
            load_week(ROOT / "samples" / "day.csv")  # no date column, not a directory

    def test_utilisation_is_share_of_open_hours(self) -> None:
        hours = parse_hours("09:00-18:00")  # 540 minutes
        full = [Appointment("09:00", 540, "Amira Khan", "Consultation", "5551")]
        self.assertEqual(utilisation(full, hours), 100.0)
        self.assertEqual(utilisation([], hours), 0.0)
        half = [Appointment("09:00", 270, "Amira Khan", "Consultation", "5551")]
        self.assertEqual(utilisation(half, hours), 50.0)
        # Double-booked chairs push above 100% rather than being capped.
        over = [Appointment("09:00", 600, "Amira Khan", "Consultation", "5551")]
        self.assertGreater(utilisation(over, hours), 100.0)

    def test_parse_hours_rejects_garbage(self) -> None:
        for spec in ("9am-5pm", "18:00-09:00", "09:00"):
            with self.subTest(spec=spec), self.assertRaises(SystemExit):
                parse_hours(spec)

    def test_week_cli_writes_week_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "slotbrief.py"),
                    str(self.WEEK),
                    "--week",
                    "--clinic",
                    "Boto Demo Clinic",
                    "--out",
                    str(out),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("week 2026-11-02 → 2026-11-06", proc.stdout)
            self.assertIn("bookings: 26", proc.stdout)

            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["days"]), 5)
            self.assertEqual(report["range"], {"start": "2026-11-02", "end": "2026-11-06"})
            totals = report["totals"]
            self.assertEqual(totals["bookings"], sum(d["stats"]["bookings"] for d in report["days"]))
            self.assertEqual(
                totals["chair_minutes"], sum(d["stats"]["chair_minutes"] for d in report["days"])
            )
            self.assertEqual(totals["flags"], sum(d["stats"]["flags"] for d in report["days"]))
            self.assertEqual(totals["busiest_day"], "2026-11-06")

            board = (out / "week-board.html").read_text(encoding="utf-8")
            for day in self.DAYS:
                # Day sections are labelled with the localised date.
                label = datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y")
                self.assertIn(label, board)
            self.assertIn("Week board", board)

            # read_bytes keeps the CRLF line endings ICS requires.
            calendar = (out / "week.ics").read_bytes().decode("utf-8")
            self.assertEqual(calendar.count("BEGIN:VEVENT"), 26)
            dtstart_days = {
                line[len("DTSTART:") : len("DTSTART:") + 8]
                for line in calendar.split("\r\n")
                if line.startswith("DTSTART:")
            }
            self.assertEqual(dtstart_days, {"20261102", "20261103", "20261104", "20261105", "20261106"})

            confirmations = (out / "confirmations.txt").read_text(encoding="utf-8")
            for day in self.DAYS:
                self.assertIn(f"[{day}]", confirmations)

    def test_single_day_mode_still_uses_day_report_shape(self) -> None:
        """--week off must keep the original report/board contract intact."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "slotbrief.py"),
                    str(ROOT / "samples" / "day.csv"),
                    "--day",
                    "2026-11-07",
                    "--out",
                    str(out),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertIn("stats", report)
            self.assertNotIn("days", report)
            self.assertTrue((out / "today-board.html").exists())
            self.assertTrue((out / "day.ics").exists())
            self.assertFalse((out / "week-board.html").exists())


if __name__ == "__main__":
    unittest.main()
