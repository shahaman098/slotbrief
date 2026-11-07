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

from slotbrief import analyse, load_appointments  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
