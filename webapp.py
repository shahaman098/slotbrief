"""Local web app for SlotBrief: a JSON API plus the single-page UI in web/.

Standard library only, bound to 127.0.0.1 so appointment data never leaves
the machine. All analysis goes through the same functions the CLI uses.
"""

from __future__ import annotations

import csv
import io
import json
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import slotbrief as sb

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
SAMPLES_DIR = ROOT / "samples"
MAX_BODY = 2 * 1024 * 1024


LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def is_local_host(value: str) -> bool:
    """True for 127.0.0.1 / localhost / [::1], with or without a port.

    Checking the Host header stops DNS-rebinding pages from reaching the API.
    """
    try:
        return (urlsplit("//" + value).hostname or "") in LOCAL_HOSTNAMES
    except ValueError:
        return False


def is_local_origin(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or "") in LOCAL_HOSTNAMES
    except ValueError:
        return False


class InputError(ValueError):
    """The request was understood but the CSV/options were not usable."""


def _read_files(files: list[dict[str, Any]], day: str) -> list[sb.Appointment]:
    if not files:
        raise InputError("Add at least one CSV file.")
    rows: list[sb.Appointment] = []
    for entry in files:
        name = str(entry.get("name", "upload.csv"))
        text = str(entry.get("text", ""))
        try:
            appts = sb.parse_appointments(io.StringIO(text, newline=""))
        except SystemExit as exc:  # the CLI parser reports errors this way
            raise InputError(f"{name}: {exc.code}") from exc
        file_day = sb._day_from_name(Path(name))
        for a in appts:
            a.date = a.date or file_day or day
            rows.append(a)
    if not rows:
        raise InputError("No appointments found in the CSV.")
    return rows


def analyse_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn uploaded CSV text + options into the JSON the UI renders."""
    clinic = str(payload.get("clinic") or "Indie Clinic").strip()[:80] or "Indie Clinic"
    day = str(payload.get("day") or datetime.now().strftime(sb.DATE_FMT))
    try:
        datetime.strptime(day, sb.DATE_FMT)
        min_gap = int(payload.get("min_gap", 10))
    except (TypeError, ValueError) as exc:
        raise InputError("Day must be YYYY-MM-DD and min gap a whole number.") from exc
    if not 0 <= min_gap <= 240:
        raise InputError("Minimum gap must be between 0 and 240 minutes.")
    try:
        hours = sb.parse_hours(str(payload.get("hours") or "09:00-18:00"))
    except SystemExit as exc:
        raise InputError(str(exc.code)) from exc

    rows = _read_files(list(payload.get("files") or []), day)
    rows.sort(key=lambda a: (a.date, a.start))
    autofix = bool(payload.get("autofix"))
    days = []
    final_groups = []
    for label, original in sb.group_by_day(rows):
        fixed, moves, unplaced = sb.resolve_day(original, hours, min_gap)
        fix = {
            "moves": [asdict(m) for m in moves],
            "unplaced": [{"patient": a.patient, "time": a.time} for a in unplaced],
            "conflicts_before": sb.conflict_count(original, min_gap),
            "conflicts_after": sb.conflict_count(fixed, min_gap),
        }
        appts = fixed if autofix else original
        final_groups.append((label, appts))
        issues = sb.analyse(appts, min_gap_min=min_gap)
        marks = sb._classify(appts, issues)
        days.append(
            {
                "day": label,
                "weekday": datetime.strptime(label, sb.DATE_FMT).strftime("%A"),
                "appointments": [
                    {
                        **asdict(a),
                        "end": a.end_label,
                        "status": marks.get(id(a), "ok"),
                        "confirmation": (
                            sb.reschedule_text(clinic, label, a)
                            if a.moved_from
                            else sb.confirmation_text(clinic, label, a)
                        ),
                    }
                    for a in appts
                ],
                "issues": [
                    {**asdict(i), "label": sb.KIND_LABELS.get(i.kind, i.kind)} for i in issues
                ],
                "stats": {
                    "bookings": len(appts),
                    "chair_minutes": sum(a.duration_min for a in appts),
                    "flags": len(issues),
                    "utilisation_pct": sb.utilisation(appts, hours),
                },
                "fix": fix,
            }
        )
    all_issues = [i for d in days for i in d["issues"]]
    busiest = max(days, key=lambda d: d["stats"]["chair_minutes"])
    return {
        "clinic": clinic,
        "mode": "week" if len(days) > 1 else "day",
        "hours": {"open": hours[0].strftime(sb.TIME_FMT), "close": hours[1].strftime(sb.TIME_FMT)},
        "min_gap": min_gap,
        "autofix": autofix,
        "days": days,
        "totals": {
            "days": len(days),
            "bookings": sum(d["stats"]["bookings"] for d in days),
            "chair_minutes": sum(d["stats"]["chair_minutes"] for d in days),
            "flags": len(all_issues),
            "busiest_day": busiest["day"],
            "avg_utilisation_pct": round(
                sum(d["stats"]["utilisation_pct"] for d in days) / len(days), 1
            ),
        },
        "ics": sb._ics_calendar(clinic, final_groups),
        "csv": _schedule_csv(final_groups),
    }


def fit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Where can one more booking go on a given day, and who would have to move?"""
    clinic = str(payload.get("clinic") or "Indie Clinic").strip()[:80] or "Indie Clinic"
    try:
        duration = int(payload.get("duration", 45))
        min_gap = int(payload.get("min_gap", 10))
    except (TypeError, ValueError) as exc:
        raise InputError("Duration and minimum gap must be whole numbers.") from exc
    if not 5 <= duration <= 480:
        raise InputError("Duration must be between 5 and 480 minutes.")
    if not 0 <= min_gap <= 240:
        raise InputError("Minimum gap must be between 0 and 240 minutes.")
    prefer = str(payload.get("prefer") or "").strip()
    if prefer:
        try:
            datetime.strptime(prefer, sb.TIME_FMT)
        except ValueError as exc:
            raise InputError("Preferred time must look like HH:MM.") from exc
    try:
        hours = sb.parse_hours(str(payload.get("hours") or "09:00-18:00"))
    except SystemExit as exc:
        raise InputError(str(exc.code)) from exc
    treatment = str(payload.get("treatment") or "Walk-in").strip()[:60] or "Walk-in"
    default_day = str(payload.get("day") or datetime.now().strftime(sb.DATE_FMT))
    rows = _read_files(list(payload.get("files") or []), default_day)
    rows.sort(key=lambda a: (a.date, a.start))
    groups = dict(sb.group_by_day(rows))
    label = str(payload.get("for_day") or next(iter(groups)))
    if label not in groups:
        raise InputError(f"No schedule loaded for {label}.")
    options = sb.find_fit(
        groups[label], hours, duration, min_gap, prefer=prefer, treatment=treatment
    )
    _, base_moves, _ = sb.resolve_day(groups[label], hours, min_gap)
    return {
        "day": label,
        "duration": duration,
        "treatment": treatment,
        "base_moves": len(base_moves),
        "options": [
            {
                "time": o.time,
                "end": o.end,
                "free_from": o.free_from,
                "free_to": o.free_to,
                "disruption_min": o.disruption_min,
                "moves": [
                    {
                        **asdict(m),
                        "message": sb.reschedule_text(
                            clinic,
                            label,
                            sb.Appointment(
                                m.new, m.duration_min, m.patient, m.treatment, "", moved_from=m.old
                            ),
                        ),
                    }
                    for m in o.moves
                ],
            }
            for o in options
        ],
    }


def _schedule_csv(groups: list[tuple[str, list[sb.Appointment]]]) -> str:
    """The schedule as it stands (after fixes, if applied), ready to re-import."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["date", "time", "duration_min", "patient", "treatment", "phone", "notes"])
    for label, appts in groups:
        for a in appts:
            writer.writerow([label, a.time, a.duration_min, a.patient, a.treatment, a.phone, a.notes])
    return out.getvalue()


def sample_payload(kind: str) -> dict[str, Any]:
    """The bundled demo data, shaped like an upload."""
    if kind == "week":
        paths = sorted((SAMPLES_DIR / "week").glob("*.csv"))
    else:
        paths = [SAMPLES_DIR / "demo-all-flags.csv"]
    return {
        "files": [{"name": p.name, "text": p.read_text(encoding="utf-8")} for p in paths],
        "clinic": "Boto Demo Clinic",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SlotBrief"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
        return

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _guard(self) -> bool:
        """Local-only: refuse foreign Host headers and cross-site Origins."""
        origin = self.headers.get("Origin")
        if not is_local_host(self.headers.get("Host", "")) or (origin and not is_local_origin(origin)):
            self._json(403, {"error": "SlotBrief only answers to localhost."})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if not self._guard():
            return
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/sample":
            kind = "week" if "kind=week" in query else "day"
            self._json(200, sample_payload(kind))
        elif path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        handlers = {"/api/analyse": analyse_payload, "/api/fit": fit_payload}
        handler = handlers.get(self.path)
        if handler is None:
            self._json(404, {"error": "Not found"})
            return
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            self._json(415, {"error": "Send JSON (Content-Type: application/json)."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._json(400, {"error": "Bad Content-Length."})
            return
        if length > MAX_BODY:
            self._json(413, {"error": "That upload is too large (2 MB max)."})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise InputError("Request body must be a JSON object.")
            self._json(200, handler(payload))
        except (InputError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})


def make_server(port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(port: int = 8765, open_browser: bool = True) -> int:
    server = make_server(port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"SlotBrief is running at {url}  (Ctrl+C to stop)")
    print("Local only: nothing you load leaves this machine.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
