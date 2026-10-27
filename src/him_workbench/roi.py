"""Release of information (ROI) request tracker. Synthetic requests only.

A person or organization asks a clinic for a patient's records. The clinic has to check who is asking and
whether they are allowed to see the records, then answer inside a deadline. This module keeps that list:
who asked, which (synthetic) patient, what was requested, when it arrived, when it is due, and what happened.

What it enforces (so the list cannot quietly say something untrue):
- A request cannot be fulfilled before it is verified. Verification means checking identity and authorization.
- Closed requests (fulfilled, denied) cannot be reopened. Every change is kept in a dated history.
- A deadline can be extended once, and only on or before the due date.

The deadline is a setting, 30 calendar days by default. The HHS page on the HIPAA right of access gives 30 days
and allows one extension of up to 30 more, with written notice inside the first 30. State law can be shorter,
so check yours and set HIM_ROI_DAYS. This is a tracking tool, not legal advice.

The audit log records that an action happened, never who or what it was about.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import audit

STATUSES = ("received", "verified", "in_progress", "fulfilled", "denied")
OPEN = ("received", "verified", "in_progress")
CLOSED = ("fulfilled", "denied")
# From each status, the statuses it may move to. Fulfilling needs verification first.
MOVES = {
    "received": ("verified", "denied"),
    "verified": ("in_progress", "denied"),
    "in_progress": ("fulfilled", "denied"),
}
REQUESTER_TYPES = ("patient", "patient's representative", "attorney", "insurer", "another provider", "other")
LABELS = {"received": "Received", "verified": "Verified", "in_progress": "In progress",
          "fulfilled": "Fulfilled", "denied": "Denied"}
EXTENSION_DAYS = 30
DUE_SOON_DAYS = 7
MAX_TEXT = 300


class RoiError(ValueError):
    """A request the tracker will not accept. The message says why, in plain words."""


def default_days() -> int:
    try:
        n = int(os.environ.get("HIM_ROI_DAYS", "30"))
    except ValueError:
        return 30
    return n if 1 <= n <= 90 else 30


@dataclass
class Request:
    id: int
    patient_id: str
    requester_type: str
    summary: str
    received: str
    due: str
    status: str
    extended: bool

    def is_open(self) -> bool:
        return self.status in OPEN

    def days_left(self, today: date) -> int:
        return (date.fromisoformat(self.due) - today).days

    def state(self, today: date) -> str:
        """"closed", "overdue", "due soon" or "on track"."""
        if not self.is_open():
            return "closed"
        left = self.days_left(today)
        return "overdue" if left < 0 else "due soon" if left <= DUE_SOON_DAYS else "on track"


@dataclass
class Event:
    on: str
    what: str
    note: str


def _text(value, name: str, required: bool = True) -> str:
    v = " ".join(str(value or "").split())
    if required and not v:
        raise RoiError(f"{name} is required.")
    if len(v) > MAX_TEXT:
        raise RoiError(f"{name} is too long (limit {MAX_TEXT} characters).")
    return v


def _day(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise RoiError(f"{name} must be a date like 2026-10-24.") from None


class RoiStore:
    def __init__(self, path: str | Path = "data/roi.db", days: int | None = None):
        self.path = Path(path)
        self.days = days or default_days()
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, requester_type TEXT NOT NULL,
                summary TEXT NOT NULL, received TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL,
                extended INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL, on_day TEXT NOT NULL,
                what TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
        """)

    # ---- reading ----
    def _row(self, r) -> Request:
        return Request(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]))

    def get(self, rid: int) -> Request | None:
        r = self.db.execute("SELECT id,patient_id,requester_type,summary,received,due,status,extended "
                            "FROM requests WHERE id=?", (rid,)).fetchone()
        return self._row(r) if r else None

    def list(self, status: str | None = None) -> list[Request]:
        sql = "SELECT id,patient_id,requester_type,summary,received,due,status,extended FROM requests"
        args: tuple = ()
        if status in STATUSES:
            sql += " WHERE status=?"
            args = (status,)
        rows = [self._row(r) for r in self.db.execute(sql, args)]
        # open requests first, soonest due first; then closed, newest first
        rows.sort(key=lambda q: (not q.is_open(), q.due if q.is_open() else "", -q.id))
        return rows

    def events(self, rid: int) -> list[Event]:
        return [Event(*r) for r in self.db.execute(
            "SELECT on_day,what,note FROM events WHERE request_id=? ORDER BY id", (rid,))]

    def summary(self, today: date) -> dict:
        rows = self.list()
        open_ = [q for q in rows if q.is_open()]
        return {"total": len(rows), "open": len(open_),
                "overdue": sum(1 for q in open_ if q.state(today) == "overdue"),
                "due_soon": sum(1 for q in open_ if q.state(today) == "due soon")}

    # ---- changing ----
    def _event(self, rid: int, on: date, what: str, note: str = "") -> None:
        self.db.execute("INSERT INTO events (request_id,on_day,what,note) VALUES (?,?,?,?)",
                        (rid, on.isoformat(), what, note))

    def create(self, patient_id: str, requester_type: str, summary: str, received: str | date,
               today: date | None = None) -> int:
        today = today or date.today()
        pid = _text(patient_id, "Patient")
        if requester_type not in REQUESTER_TYPES:
            raise RoiError("Pick who is asking from the list.")
        what = _text(summary, "What was requested")
        got = received if isinstance(received, date) else _day(received, "Date received")
        if got > today:
            raise RoiError("Date received cannot be in the future.")
        due = got + timedelta(days=self.days)
        cur = self.db.execute("INSERT INTO requests (patient_id,requester_type,summary,received,due,status) "
                              "VALUES (?,?,?,?,?,'received')", (pid, requester_type, what, got.isoformat(), due.isoformat()))
        rid = cur.lastrowid
        self._event(rid, got, "Received")
        self.db.commit()
        audit.log_call("roi_create", {}, "ok")
        return rid

    def set_status(self, rid: int, status: str, note: str = "", today: date | None = None) -> None:
        today = today or date.today()
        q = self.get(rid)
        if q is None:
            raise RoiError("No such request.")
        if q.status in CLOSED:
            raise RoiError(f"This request is {q.status} and cannot be changed.")
        if status not in MOVES[q.status]:
            if status == "fulfilled" and q.status != "in_progress":
                raise RoiError("It cannot be fulfilled until it is verified and in progress.")
            raise RoiError(f"From {LABELS[q.status].lower()} it can only go to: "
                           + ", ".join(LABELS[s].lower() for s in MOVES[q.status]) + ".")
        n = _text(note, "Note", required=status == "denied")   # a denial must say why
        self.db.execute("UPDATE requests SET status=? WHERE id=?", (status, rid))
        self._event(rid, today, LABELS[status], n)
        self.db.commit()
        audit.log_call("roi_status", {"to": status}, "ok")

    def extend(self, rid: int, reason: str, today: date | None = None) -> None:
        today = today or date.today()
        q = self.get(rid)
        if q is None:
            raise RoiError("No such request.")
        if not q.is_open():
            raise RoiError("Only an open request can be extended.")
        if q.extended:
            raise RoiError("This request was already extended once. Only one extension is allowed.")
        if today > date.fromisoformat(q.due):
            raise RoiError("The due date has passed. An extension has to be set on or before it.")
        why = _text(reason, "Reason")
        new_due = date.fromisoformat(q.due) + timedelta(days=EXTENSION_DAYS)
        self.db.execute("UPDATE requests SET due=?, extended=1 WHERE id=?", (new_due.isoformat(), rid))
        self._event(rid, today, f"Extended to {new_due.isoformat()}", why)
        self.db.commit()
        audit.log_call("roi_extend", {}, "ok")
