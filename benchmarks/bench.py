#!/usr/bin/env python3
"""Benchmark harness for SlotBrief.

Generates deterministic synthetic appointment loads (default 1k / 10k / 100k
rows), then times the real pipeline stages with time.perf_counter:

  load     CSV read + validate + date sort (load_week)
  flags    group_by_day + analyse per day (the week-mode hot path)
  render   week board HTML + ICS + confirmations + report.json writes
  cli      full end-to-end run in a subprocess, exactly as a user would

Also reports the Python-level peak allocation of one in-process pipeline pass
(tracemalloc). Standard library only; nothing is written outside a temp dir.

Usage:
  python3 benchmarks/bench.py [--sizes 1000,10000,100000] [--repeat 3]
"""
from __future__ import annotations

import argparse
import csv
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import slotbrief as sb  # noqa: E402

TREATMENTS = [
    "Consultation",
    "Hydrafacial",
    "Botox",
    "Chemical peel",
    "Microneedling",
    "Laser resurfacing",
]
FIRST = ["Amira", "Jordan", "Sam", "Priya", "Chris", "Elena", "Noor", "Tariq", "Isla", "Marta"]
LAST = ["Khan", "Lee", "Okonkwo", "Shah", "Nolan", "Rossi", "Haddad", "Reyes", "Doyle", "Nowak"]
ROWS_PER_DAY = 20
FIELDS = ["date", "time", "duration_min", "patient", "treatment", "phone", "notes"]


def generate_rows(n: int, seed: int = 42) -> list[dict]:
    """Deterministic rows: ~20 per weekday from Mon 2026-01-05, weekends skipped."""
    rng = random.Random(seed)
    rows: list[dict] = []
    day = date(2026, 1, 5)
    for i in range(n):
        if i and i % ROWS_PER_DAY == 0:
            day += timedelta(days=1)
            while day.weekday() > 4:
                day += timedelta(days=1)
        start_min = rng.randrange(9 * 60, 17 * 60)
        rows.append(
            {
                "date": day.isoformat(),
                "time": f"{start_min // 60:02d}:{start_min % 60:02d}",
                "duration_min": rng.choice((30, 45, 60, 90)),
                "patient": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                "treatment": rng.choice(TREATMENTS),
                "phone": f"+4477009{rng.randrange(100000, 999999)}",
                "notes": "" if rng.random() < 0.7 else "Synthetic bench row",
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def best_of(fn, repeat: int) -> float:
    """Wall time of the fastest of `repeat` runs (min for stable benchmarks)."""
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def human(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:,.0f} ms"
    return f"{seconds:,.2f} s"


def cpu_brand() -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return platform.processor() or "unknown CPU"


def bench_size(n: int, repeat: int, work: Path) -> dict:
    rows = generate_rows(n)
    csv_path = work / f"bench-{n}.csv"
    write_csv(rows, csv_path)

    def do_load() -> list:
        return sb.load_week(csv_path)

    t_load = best_of(do_load, repeat)

    loaded = sb.load_week(csv_path)
    groups = sb.group_by_day(loaded)

    def do_flags():
        return [(d, sb.analyse(a)) for d, a in groups]

    t_flags = best_of(do_flags, repeat)

    hours = sb.parse_hours("09:00-18:00")
    days = [(d, a, sb.analyse(a)) for d, a in groups]
    out_dir = work / f"out-{n}"

    def do_render() -> None:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        sb.write_week_outputs(out_dir, "Bench Clinic", days, hours)

    t_render = best_of(do_render, repeat)

    def do_cli() -> None:
        cli_out = work / f"cli-{n}"
        if cli_out.exists():
            shutil.rmtree(cli_out)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "slotbrief.py"),
                "--week",
                str(csv_path),
                "--out",
                str(cli_out),
            ],
            check=True,
            capture_output=True,
        )

    t_cli = best_of(do_cli, repeat)

    # Peak Python allocations for one in-process pass (not timed).
    tracemalloc.start()
    piped = sb.load_week(csv_path)
    pipe_days = [
        (d, a, sb.analyse(a)) for d, a in sb.group_by_day(piped)
    ]
    temp_out = work / "peak"
    if temp_out.exists():
        shutil.rmtree(temp_out)
    sb.write_week_outputs(temp_out, "Bench Clinic", pipe_days, hours)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    shutil.rmtree(temp_out, ignore_errors=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(work / f"cli-{n}", ignore_errors=True)
    csv_path.unlink()

    return {
        "rows": n,
        "days": len(groups),
        "load": t_load,
        "flags": t_flags,
        "render": t_render,
        "cli": t_cli,
        "peak_mb": peak / (1024 * 1024),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SlotBrief benchmark harness")
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    print(f"machine : {cpu_brand()} · {platform.machine()} · {platform.mac_ver()[0] or platform.release()}")
    print(f"python  : {platform.python_version()} (stdlib only)")
    print(f"repeat  : {args.repeat} (best of) · rows/day: {ROWS_PER_DAY}")
    print()
    header = f"{'rows':>8} {'days':>6} {'load':>10} {'flags':>10} {'render+write':>14} {'full CLI':>10} {'peak mem':>10}"
    print(header)
    print("-" * len(header))
    results = []
    with tempfile.TemporaryDirectory(prefix="slotbrief-bench-") as tmp:
        for n in sizes:
            result = bench_size(n, args.repeat, Path(tmp))
            results.append(result)
            print(
                f"{result['rows']:>8,} {result['days']:>6,} "
                f"{human(result['load']):>10} {human(result['flags']):>10} "
                f"{human(result['render']):>14} {human(result['cli']):>10} "
                f"{result['peak_mb']:>8.1f} MB"
            )
    print()
    print("load = CSV parse+validate+date sort · flags = per-day grouping+analysis ·")
    print("render = week board + ICS + confirmations + report.json · cli = end-to-end subprocess")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
