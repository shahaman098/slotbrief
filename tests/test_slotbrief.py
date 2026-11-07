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


class SmartFlagTests(unittest.TestCase):
    """bad_phone and double_booked_patient: the 'a confirmation would be a lie' flags."""

    @staticmethod
    def _phone_day(phone: str) -> list:
        return [
            Appointment("09:00", 45, "Amira Khan", "Consultation", phone),
            Appointment("10:00", 30, "Jordan Lee", "Botox", "+447700900302"),
        ]

    def test_bad_phone_fires_on_short_numbers_letters_and_empty(self) -> None:
        for phone in ("5551", "call me", ""):
            with self.subTest(phone=phone):
                issues = analyse(self._phone_day(phone))
                bad = [i for i in issues if i.kind == "bad_phone"]
                self.assertEqual(len(bad), 1, f"expected bad_phone for {phone!r}")
                self.assertIn("cannot be sent", bad[0].message)

    def test_bad_phone_accepts_real_mobile_numbers(self) -> None:
        for phone in ("+447700900301", "07700 900301", "+1 (555) 010-2934"):
            with self.subTest(phone=phone):
                kinds = {i.kind for i in analyse(self._phone_day(phone))}
                self.assertNotIn("bad_phone", kinds)

    def test_double_booked_patient_fires_even_with_a_third_party_between(self) -> None:
        appts = [
            Appointment("09:00", 90, "Amira Khan", "Peel", "+447700900301"),
            Appointment("09:30", 30, "Jordan Lee", "Botox", "+447700900302"),
            Appointment("10:00", 45, "Amira Khan", "Review", "+447700900301"),
        ]
        kinds = {i.kind for i in analyse(appts)}
        self.assertIn("double_booked_patient", kinds)
        messages = [i.message for i in analyse(appts) if i.kind == "double_booked_patient"]
        self.assertIn("Amira Khan is booked twice at once (09:00-10:30 and 10:00)", messages[0])

    def test_same_patient_twice_same_day_without_overlap_is_fine(self) -> None:
        appts = [
            Appointment("09:40", 45, "Amira Khan", "Consultation", "+447700900301"),
            Appointment("15:40", 45, "Amira Khan", "Review", "+447700900301"),
        ]
        self.assertNotIn("double_booked_patient", {i.kind for i in analyse(appts)})

    def test_samples_never_trigger_the_smart_flags(self) -> None:
        day = analyse(load_appointments(ROOT / "samples" / "day.csv"))
        self.assertLessEqual(
            {i.kind for i in day}, {"overlap", "gap", "late_start"},
            "samples/day.csv must keep its original three flag kinds",
        )
        for day_file in sorted((ROOT / "samples" / "week").glob("*.csv")):
            kinds = {i.kind for i in analyse(load_appointments(day_file))}
            self.assertLessEqual(kinds, {"overlap", "gap", "late_start"}, day_file.name)

    def test_demo_day_cli_still_reports_exactly_three_flags(self) -> None:
        """The demo video narrates 'three flags' — this pins that claim to CI."""
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "slotbrief.py"),
                str(ROOT / "samples" / "demo-day.csv"),
                "--day",
                "2026-11-07",
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(ROOT),  # default --out out lands in the repo (gitignored)
        )
        self.assertIn("flags: 3", proc.stdout)
        self.assertIn("[gap]", proc.stdout)
        self.assertIn("[overlap]", proc.stdout)
        self.assertIn("[late_start]", proc.stdout)
        self.assertNotIn("bad_phone", proc.stdout)
        self.assertNotIn("double_booked_patient", proc.stdout)


class AllFlagsSampleTests(unittest.TestCase):
    def test_demo_all_flags_sample_trips_every_flag_kind(self) -> None:
        from slotbrief import analyse, load_appointments

        issues = analyse(load_appointments(ROOT / "samples" / "demo-all-flags.csv"))
        self.assertEqual(
            {i.kind for i in issues},
            {"overlap", "gap", "late_start", "double_booked_patient", "bad_phone"},
        )


class WebAppTests(unittest.TestCase):
    """The local web app: payload analysis plus a real HTTP round-trip."""

    def test_sample_day_payload_marks_conflicts_and_builds_ics(self) -> None:
        import webapp

        result = webapp.analyse_payload(webapp.sample_payload("day"))
        self.assertEqual(result["mode"], "day")
        day = result["days"][0]
        statuses = {a["patient"] + a["time"]: a["status"] for a in day["appointments"]}
        self.assertEqual(statuses["Jordan Lee12:05"], "bad")
        self.assertEqual(statuses["Jordan Lee12:55"], "bad")
        self.assertEqual(statuses["Sam Okonkwo16:05"], "ok")
        self.assertEqual(result["ics"].count("BEGIN:VEVENT"), 6)
        self.assertIn("Reply YES", day["appointments"][0]["confirmation"])

    def test_week_payload_groups_files_by_filename_date(self) -> None:
        import webapp

        result = webapp.analyse_payload(webapp.sample_payload("week"))
        self.assertEqual(result["mode"], "week")
        self.assertEqual(result["totals"]["bookings"], 26)
        self.assertEqual(result["totals"]["busiest_day"], "2026-11-06")
        self.assertEqual([d["day"] for d in result["days"]][0], "2026-11-02")

    def test_autofix_payload_clears_conflicts_and_rewrites_messages(self) -> None:
        import webapp

        payload = webapp.sample_payload("day")
        before = webapp.analyse_payload(payload)
        self.assertFalse(before["autofix"])
        fix = before["days"][0]["fix"]
        self.assertEqual((fix["conflicts_before"], fix["conflicts_after"]), (4, 0))
        self.assertEqual(len(fix["moves"]), 2)

        after = webapp.analyse_payload({**payload, "autofix": True})
        day = after["days"][0]
        self.assertTrue(after["autofix"])
        self.assertEqual(len(day["appointments"]), 6)
        self.assertLess(day["stats"]["flags"], before["days"][0]["stats"]["flags"])
        moved = [a for a in day["appointments"] if a["moved_from"]]
        self.assertEqual(len(moved), 2)
        self.assertIn("move your", moved[0]["confirmation"])
        self.assertEqual(after["csv"].splitlines()[0], "date,time,duration_min,patient,treatment,phone,notes")
        self.assertEqual(len(after["csv"].strip().splitlines()), 7)

    def test_bad_input_raises_input_error_not_a_crash(self) -> None:
        import webapp

        with self.assertRaises(webapp.InputError):
            webapp.analyse_payload({"files": []})
        with self.assertRaises(webapp.InputError):
            webapp.analyse_payload({"files": [{"name": "a.csv", "text": "time\n09:00\n"}]})
        with self.assertRaises(webapp.InputError):
            webapp.analyse_payload({"files": [{"name": "a.csv", "text": "x"}], "hours": "late"})

    def test_http_round_trip_and_error_status(self) -> None:
        import threading
        import urllib.error
        import urllib.request

        import webapp

        server = webapp.make_server(port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            page = urllib.request.urlopen(base + "/").read().decode("utf-8")
            self.assertIn("SlotBrief", page)
            sample = json.loads(urllib.request.urlopen(base + "/api/sample?kind=day").read())
            req = urllib.request.Request(
                base + "/api/analyse",
                data=json.dumps(sample).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            result = json.loads(urllib.request.urlopen(req).read())
            self.assertEqual(result["totals"]["flags"], 6)
            bad = urllib.request.Request(
                base + "/api/analyse", data=b"{}", method="POST", headers={"Content-Type": "application/json"}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()


class ResolveTests(unittest.TestCase):
    """The rescheduler: optimal keep-set, nearest-slot placement, safety."""

    HOURS = ("09:00", "18:00")

    @staticmethod
    def _hours():
        from slotbrief import parse_hours

        return parse_hours("09:00-18:00")

    @staticmethod
    def _appt(time, dur, name="P"):
        return Appointment(time, dur, name, "Treatment", "+447400100001")

    def test_sample_day_goes_from_conflicts_to_none_with_two_moves(self) -> None:
        from slotbrief import conflict_count, resolve_day

        appts = load_appointments(ROOT / "samples" / "demo-all-flags.csv")
        fixed, moves, unplaced = resolve_day(appts, self._hours(), 10)
        self.assertEqual(conflict_count(appts), 4)
        self.assertEqual(conflict_count(fixed), 0)
        self.assertEqual(len(moves), 2)
        self.assertEqual(unplaced, [])
        self.assertEqual(len(fixed), len(appts))
        self.assertTrue(all(a.moved_from for a in fixed if a.time != a.moved_from and a.moved_from))

    def test_clean_schedule_is_left_alone(self) -> None:
        from slotbrief import resolve_day

        appts = [self._appt("09:00", 30, "A"), self._appt("10:00", 30, "B"), self._appt("12:00", 60, "C")]
        fixed, moves, unplaced = resolve_day(appts, self._hours(), 10)
        self.assertEqual(moves, [])
        self.assertEqual([a.time for a in fixed], ["09:00", "10:00", "12:00"])

    def test_keep_set_matches_brute_force_optimum_on_random_days(self) -> None:
        import itertools
        import random

        from slotbrief import _keep_set

        rng = random.Random(7)
        for _ in range(250):
            n = rng.randint(1, 8)
            gap = rng.choice([0, 5, 10, 15])
            appts = [
                self._appt(f"{9 + rng.randint(0, 6):02d}:{rng.choice(['00', '15', '30', '45'])}", rng.choice([20, 30, 45, 60, 90]), f"P{i}")
                for i in range(n)
            ]

            def compatible(group):
                ordered = sorted(group, key=lambda a: a.start)
                return all(
                    (b.start - a.end).total_seconds() / 60 >= gap for a, b in zip(ordered, ordered[1:])
                )

            best = max(
                len(c)
                for r in range(n + 1)
                for c in itertools.combinations(appts, r)
                if compatible(c)
            )
            kept = _keep_set(appts, gap)
            self.assertEqual(len(kept), best, (gap, [(a.time, a.duration_min) for a in appts]))
            self.assertTrue(compatible([appts[i] for i in kept]))

    def test_fixed_schedules_are_conflict_free_and_inside_opening_hours(self) -> None:
        import random

        from slotbrief import conflict_count, resolve_day

        rng = random.Random(11)
        hours = self._hours()
        for _ in range(150):
            appts = [
                self._appt(f"{9 + rng.randint(0, 7):02d}:{rng.choice(['00', '20', '40'])}", rng.choice([30, 45, 60]), f"P{i}")
                for i in range(rng.randint(2, 9))
            ]
            fixed, moves, unplaced = resolve_day(appts, hours, 10)
            self.assertEqual(len(fixed), len(appts))
            if not unplaced:
                self.assertEqual(conflict_count(fixed), 0)
            for a in fixed:
                if a.moved_from:
                    self.assertGreaterEqual(a.start, hours[0])
                    self.assertLessEqual(a.end, hours[1])

    def test_overfull_day_reports_unplaceable_bookings(self) -> None:
        from slotbrief import parse_hours, resolve_day

        appts = [self._appt("09:00", 30, "A"), self._appt("09:10", 30, "B"), self._appt("09:20", 30, "C")]
        fixed, moves, unplaced = resolve_day(appts, parse_hours("09:00-10:00"), 10)
        self.assertTrue(unplaced)
        self.assertEqual(len(fixed), 3)


class LocalOnlyServerTests(unittest.TestCase):
    """The server only answers to localhost, so a web page can't reach patient data."""

    def _request(self, method, path, headers=None, body=b""):
        import http.client
        import threading

        import webapp

        server = webapp.make_server(port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            conn.putrequest(method, path, skip_host=True)
            hdrs = {"Host": f"127.0.0.1:{server.server_address[1]}", **(headers or {})}
            for key, value in hdrs.items():
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            return resp.status
        finally:
            server.shutdown()
            server.server_close()

    def test_foreign_host_header_is_refused(self) -> None:
        self.assertEqual(self._request("GET", "/api/sample?kind=day", {"Host": "evil.example:8765"}), 403)

    def test_localhost_and_ipv4_hosts_are_accepted(self) -> None:
        self.assertEqual(self._request("GET", "/healthz", {"Host": "localhost:8765"}), 200)
        self.assertEqual(self._request("GET", "/healthz"), 200)

    def test_cross_site_origin_is_refused(self) -> None:
        hdrs = {"Origin": "https://evil.example", "Content-Type": "application/json"}
        self.assertEqual(self._request("POST", "/api/analyse", hdrs, b"{}"), 403)

    def test_malformed_payloads_are_input_errors_not_crashes(self) -> None:
        import webapp

        for files in ([1], "x", {"a": 1}, [None], []):
            for fn in (webapp.analyse_payload, webapp.fit_payload):
                with self.assertRaises(webapp.InputError, msg=repr(files)):
                    fn({"files": files})

    def test_post_without_json_content_type_is_refused(self) -> None:
        self.assertEqual(self._request("POST", "/api/analyse", {"Content-Type": "text/plain"}, b"{}"), 415)

    def test_host_helper_handles_ports_and_ipv6(self) -> None:
        import webapp

        for ok in ("127.0.0.1", "127.0.0.1:8765", "localhost:1", "[::1]:8765"):
            self.assertTrue(webapp.is_local_host(ok), ok)
        for bad in ("", "evil.example", "127.0.0.1.evil.example:80", "10.0.0.5:8765"):
            self.assertFalse(webapp.is_local_host(bad), bad)


class CsvRobustnessTests(unittest.TestCase):
    """Real spreadsheet exports, not just tidy CSVs."""

    HEAD = "time,duration_min,patient,treatment,phone\n"

    @staticmethod
    def _parse(text):
        import io

        from slotbrief import parse_appointments

        return parse_appointments(io.StringIO(text))

    def test_excel_bom_does_not_hide_the_first_column(self) -> None:
        rows = self._parse("\ufeff" + self.HEAD + "09:00,30,A,B,+447400100001\n")
        self.assertEqual(rows[0].time, "09:00")

    def test_bom_file_on_disk_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"
            path.write_bytes(b"\xef\xbb\xbf" + (self.HEAD + "09:00,30,A,B,+447400100001\n").encode())
            self.assertEqual(len(load_appointments(path)), 1)

    def test_cp1252_file_with_accents_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"
            path.write_bytes((self.HEAD + "09:00,30,Zo\xeb,B,+447400100001\n").encode("cp1252"))
            self.assertEqual(load_appointments(path)[0].patient, "Zo\xeb")

    def test_semicolon_separated_csv_and_messy_headers(self) -> None:
        rows = self._parse("Time; Duration_Min ;Patient;Treatment;PHONE\n09:00;30;A;B;+447400100001\n")
        self.assertEqual((rows[0].time, rows[0].duration_min), ("09:00", 30))

    def test_times_are_normalised(self) -> None:
        rows = self._parse(
            self.HEAD + "9:30,30,A,B,+447400100001\n2:15 pm,30,C,B,+447400100001\n09.45,30,D,B,+447400100001\n12:05 AM,30,E,B,+447400100001\n"
        )
        self.assertEqual(sorted(a.time for a in rows), ["00:05", "09:30", "09:45", "14:15"])

    def test_bad_values_give_a_line_numbered_message_not_a_traceback(self) -> None:
        for body, needle in (
            ("noon,30,A,B,+447400100001\n", "Line 2: time must look like"),
            ("09:00,0,A,B,+447400100001\n", "between 1 and 720"),
            ("09:00,-5,A,B,+447400100001\n", "between 1 and 720"),
            ("09:00,9999,A,B,+447400100001\n", "between 1 and 720"),
            ("09:00,thirty,A,B,+447400100001\n", "must be an integer"),
            ("09:00,30,,B,+447400100001\n", "patient is empty"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                self._parse(self.HEAD + body)
            self.assertIn(needle, str(ctx.exception.code))

    def test_analyse_does_not_depend_on_input_order(self) -> None:
        a = Appointment("09:00", 60, "A", "T", "+447400100001")
        b = Appointment("09:30", 30, "B", "T", "+447400100002")
        c = Appointment("14:00", 30, "C", "T", "+447400100003")
        forward = [(i.kind, i.message) for i in analyse([a, b, c])]
        shuffled = [(i.kind, i.message) for i in analyse([c, b, a])]
        self.assertEqual(forward, shuffled)
        self.assertEqual([k for k, _ in forward].count("overlap"), 1)


class CliFixTests(unittest.TestCase):
    """--fix: the rescheduler from the terminal, with files you can re-import."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "slotbrief.py"), *args], capture_output=True, text=True, check=True
        )

    def test_day_fix_writes_a_conflict_free_reimportable_schedule(self) -> None:
        from slotbrief import conflict_count

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = self._run(str(ROOT / "samples" / "demo-all-flags.csv"), "--out", str(out), "--fix")
            self.assertIn("scheduling conflicts: 4 -> 0 by moving 2 booking(s)", proc.stdout)
            fixed = load_appointments(out / "fixed-schedule.csv")
            self.assertEqual(len(fixed), 6)
            self.assertEqual(conflict_count(fixed), 0)
            texts = (out / "reschedule-messages.txt").read_text(encoding="utf-8")
            self.assertEqual(texts.count("We need to move your"), 2)

    def test_week_fix_keeps_days_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            proc = self._run("--week", str(ROOT / "samples" / "week"), "--out", str(out), "--fix")
            self.assertIn("-> 0 by moving", proc.stdout)
            rows = load_week(out / "fixed-schedule.csv")
            self.assertEqual(len({a.date for a in rows}), 5)

    def test_fix_on_a_clean_day_moves_nobody(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "clean.csv"
            src.write_text(
                "time,duration_min,patient,treatment,phone\n09:00,30,A,B,+447400100001\n11:00,30,C,B,+447400100002\n",
                encoding="utf-8",
            )
            out = Path(tmp) / "out"
            proc = self._run(str(src), "--out", str(out), "--fix")
            self.assertIn("0 -> 0 by moving 0 booking(s)", proc.stdout)
            self.assertIn("No bookings needed to move.", (out / "reschedule-messages.txt").read_text())


class CliValidationTests(unittest.TestCase):
    """Bad flags and files get a one-line message, never a traceback."""

    CSV = str(ROOT / "samples" / "demo-day.csv")

    def _fail(self, *args):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "slotbrief.py"), *args], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        return proc.stderr

    def test_each_bad_input_is_rejected_with_a_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = ["--out", tmp]
            for args, needle in (
                (["no-such-file.csv"], "file not found"),
                ([self.CSV, "--day", "tomorrow"], "--day must look like"),
                ([self.CSV, "--min-gap", "-5"], "can't be negative"),
                ([self.CSV, "--fit", "0"], "between 5 and 480"),
                ([self.CSV, "--fit", "9999"], "between 5 and 480"),
                ([self.CSV, "--fit", "30", "--prefer", "25:99"], "--prefer must look like"),
                (["--week", str(ROOT / "samples" / "week"), "--fit", "30"], "single day"),
            ):
                self.assertIn(needle, self._fail(*args, *out), args)

    def test_fit_on_an_overfull_day_returns_nothing_quickly(self) -> None:
        import time

        from slotbrief import find_fit, parse_hours

        appts = [Appointment(f"{9 + i // 6:02d}:{(i % 6) * 10:02d}", 45, f"P{i}", "T", "+447400100001") for i in range(40)]
        started = time.time()
        self.assertEqual(find_fit(appts, parse_hours("09:00-18:00"), 30), [])
        self.assertLess(time.time() - started, 2.0)


class FitTests(unittest.TestCase):
    """The fit finder: where can one more booking go, and who has to budge?"""

    @staticmethod
    def _hours():
        from slotbrief import parse_hours

        return parse_hours("09:00-18:00")

    @staticmethod
    def _appt(time, dur, name="P"):
        return Appointment(time, dur, name, "Treatment", "+447400100001")

    def _random_clean_day(self, rng):
        from slotbrief import resolve_day

        appts = [
            self._appt(f"{9 + rng.randint(0, 7):02d}:{rng.choice(['00', '15', '30', '45'])}", rng.choice([30, 45, 60]), f"P{i}")
            for i in range(rng.randint(1, 6))
        ]
        fixed, _, unplaced = resolve_day(appts, self._hours(), 10)
        return None if unplaced else fixed

    def test_free_windows_match_a_direct_scan_of_every_start(self) -> None:
        import random
        from datetime import timedelta

        from slotbrief import conflict_count, find_fit

        rng = random.Random(3)
        hours = self._hours()
        for _ in range(60):
            base = self._random_clean_day(rng)
            if base is None:
                continue
            dur = rng.choice([30, 45, 60])
            expected = set()
            t = hours[0]
            while t + timedelta(minutes=dur) <= hours[1]:
                trial = sorted(base + [self._appt(t.strftime("%H:%M"), dur, "NEW")], key=lambda a: a.start)
                if conflict_count(trial) == 0:
                    expected.add(t.strftime("%H:%M"))
                t += timedelta(minutes=5)
            got = set()
            for o in find_fit(base, hours, dur, limit=1000):
                if not o.moves:
                    cur = datetime.strptime(o.free_from, "%H:%M")
                    while cur <= datetime.strptime(o.free_to, "%H:%M"):
                        got.add(cur.strftime("%H:%M"))
                        cur += timedelta(minutes=5)
            self.assertEqual(got, expected)

    def test_displacement_count_matches_brute_force_minimum(self) -> None:
        import itertools
        import random

        from slotbrief import resolve_day

        rng = random.Random(21)
        hours = self._hours()
        for _ in range(200):
            base = self._random_clean_day(rng)
            if base is None:
                continue
            new = self._appt(f"{9 + rng.randint(0, 6):02d}:{rng.choice(['00', '20', '40'])}", rng.choice([30, 60]), "NEW")
            trial = base + [new]
            n = len(trial)

            def compatible(group):
                ordered = sorted(group, key=lambda a: a.start)
                return all((b.start - a.end).total_seconds() / 60 >= 10 for a, b in zip(ordered, ordered[1:]))

            best_keep = max(
                len(c) for r in range(1, n + 1) for c in itertools.combinations(trial, r) if new in c and compatible(c)
            )
            _, moves, unplaced = resolve_day(trial, hours, 10, pinned={n - 1})
            self.assertEqual(len(moves) + len(unplaced), n - best_keep)

    def test_every_option_is_a_conflict_free_day_and_the_new_booking_never_moves(self) -> None:
        import random

        from slotbrief import conflict_count, find_fit, resolve_day

        rng = random.Random(5)
        hours = self._hours()
        for _ in range(60):
            base = self._random_clean_day(rng)
            if base is None:
                continue
            for o in find_fit(base, hours, 45, prefer="12:00", limit=6):
                day = [a for a in base]
                moved = {(m.patient, m.old): m for m in o.moves}
                day = [
                    self._appt(moved[(a.patient, a.time)].new, a.duration_min, a.patient)
                    if (a.patient, a.time) in moved
                    else a
                    for a in day
                ] + [self._appt(o.time, 45, "NEW")]
                day.sort(key=lambda a: a.start)
                self.assertEqual(conflict_count(day), 0, o)
                self.assertTrue(all(m.patient != "New patient" for m in o.moves))

    def test_ranking_prefers_fewer_people_moved_then_less_shifting(self) -> None:
        from slotbrief import find_fit, parse_hours

        appts = [self._appt("09:00", 60, "A"), self._appt("10:10", 60, "B"), self._appt("11:20", 60, "C")]
        options = find_fit(appts, parse_hours("09:00-12:30"), 60, limit=10)
        keys = [(len(o.moves), o.disruption_min) for o in options]
        self.assertEqual(keys, sorted(keys))

    def test_prefer_picks_the_closest_start_inside_a_free_window(self) -> None:
        from slotbrief import find_fit

        appts = [self._appt("09:00", 30, "A"), self._appt("16:00", 30, "B")]
        opt = find_fit(appts, self._hours(), 30, prefer="13:00", limit=1)[0]
        self.assertEqual(opt.moves, [])
        self.assertEqual(opt.time, "13:00")
        self.assertEqual((opt.free_from, opt.free_to), ("09:40", "15:20"))

    def test_nothing_fits_when_the_treatment_outlasts_the_day(self) -> None:
        from slotbrief import find_fit, parse_hours

        self.assertEqual(find_fit([], parse_hours("09:00-10:00"), 90), [])

    def test_web_fit_payload_returns_ranked_options_with_messages(self) -> None:
        import webapp

        payload = webapp.sample_payload("day")
        out = webapp.fit_payload({**payload, "duration": 45, "prefer": "14:00", "treatment": "Hydrafacial"})
        self.assertTrue(out["options"])
        moving = [o for o in out["options"] if o["moves"]]
        self.assertTrue(moving)
        self.assertIn("We need to move your", moving[0]["moves"][0]["message"])
        with self.assertRaises(webapp.InputError):
            webapp.fit_payload({**payload, "duration": 2})
        with self.assertRaises(webapp.InputError):
            webapp.fit_payload({**payload, "prefer": "teatime"})

    def test_cli_fit_lists_free_and_moving_options(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "slotbrief.py"), str(ROOT / "samples" / "demo-day.csv"), "--fit", "45", "--prefer", "14:00"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("free: nobody moves", proc.stdout)
        self.assertIn("moves 1:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
