"""
Scheduler — timers, pomodoros, and reminders.

All jobs run on daemon threads and fire a spoken callback when they
elapse. Jobs live in memory for the session (no persistence yet).
"""
from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional


@dataclass
class Job:
    id: int
    kind: str                          # "timer" | "pomodoro" | "reminder"
    label: str
    fire_at: datetime
    thread: Optional[threading.Thread] = None
    cancelled: bool = field(default=False)

    def seconds_left(self) -> int:
        return max(0, int((self.fire_at - datetime.now()).total_seconds()))


class Scheduler:
    def __init__(self, notify: Callable[[str], None]) -> None:
        self.notify = notify           # called with a spoken string
        self._jobs: Dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # ────────── job creation ──────────
    def add(self, kind: str, seconds: int, label: str) -> Job:
        with self._lock:
            job = Job(
                id=next(self._ids),
                kind=kind,
                label=label,
                fire_at=datetime.now() + timedelta(seconds=seconds),
            )
            t = threading.Thread(target=self._run, args=(job,), daemon=True)
            job.thread = t
            self._jobs[job.id] = job
            t.start()
            return job

    def _run(self, job: Job) -> None:
        # Sleep in small slices so a cancel is picked up quickly
        while not job.cancelled:
            remaining = job.seconds_left()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
        if job.cancelled:
            return

        if job.kind == "pomodoro":
            self.notify("Pomodoro complete. Time for a five minute break.")
        elif job.kind == "reminder":
            self.notify(f"As requested: {job.label}")
        else:
            self.notify(f"Timer complete. {job.label or 'time is up'}.")

    def cancel(self, job_id: int) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancelled = True
        return True

    def cancel_all(self) -> int:
        n = 0
        for job in list(self._jobs.values()):
            if not job.cancelled and job.seconds_left() > 0:
                job.cancelled = True
                n += 1
        return n

    def active(self) -> List[Job]:
        return [j for j in self._jobs.values() if not j.cancelled and j.seconds_left() > 0]


# ────────── natural-language duration parser ──────────

_DUR_RE = re.compile(
    r"(?P<n>\d+)\s*(?P<u>hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_UNIT_TO_SEC = {
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60,   "min": 60,  "mins": 60,  "minute": 60, "minutes": 60,
    "s": 1,    "sec": 1,   "secs": 1,   "second": 1,  "seconds": 1,
}


def parse_duration(text: str) -> Optional[int]:
    """Turn '25 minutes', '1 hour 30 min', '90s' → seconds."""
    if not text:
        return None
    total = 0
    matched = False
    for m in _DUR_RE.finditer(text):
        matched = True
        n = int(m.group("n"))
        u = m.group("u").lower().rstrip(".")
        total += n * _UNIT_TO_SEC.get(u, 0)
    if not matched:
        # Bare number → assume minutes
        m = re.match(r"^\s*(\d+)\s*$", text)
        if m:
            return int(m.group(1)) * 60
        return None
    return total or None


def humanize(seconds: int) -> str:
    """Turn seconds → '1 hour 5 minutes' for spoken output."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)
