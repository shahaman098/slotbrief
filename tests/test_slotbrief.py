#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slotbrief import Appointment, analyse, ics_text, load_appointments  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
