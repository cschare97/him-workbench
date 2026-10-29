"""Specialty referral tracker. Synthetic referrals only.

A referral is logged when a referring provider sends a patient to a specialist (receiving provider),
optionally assigned to a referral coordinator. From there it should move sent -> scheduled -> completed.
The diagnostic value is catching the ones that stall: sent with no scheduled appointment after a while,
or scheduled with no completed visit. This module keeps that list and flags the stuck ones; it does not
touch any EHR's own referral or transaction table.

What it enforces:
- A referral cannot be marked completed before it is scheduled.
- Closed referrals (completed, declined) cannot be reopened. Every change is kept in a dated history.
- Declining a referral must say why.
- An ICD-10-CM code, if given, has to look like one (shape only; not checked against a loaded code file).

"No response" is a tracking setting, not a regulation (unlike the ROI tracker's HIPAA deadline):
how many days of no status change before a sent-but-not-scheduled referral is worth a second look.
Default 14 days; set HIM_REFERRAL_FOLLOWUP_DAYS to change it.

The audit log records that an action happened, never who or what it was about.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import audit
from .icd10 import format_code, looks_like_code

STATUSES = ("sent", "scheduled", "completed", "declined")
OPEN = ("sent", "scheduled")
CLOSED = ("completed", "declined")
# From each status, the statuses it may move to. Completing needs a scheduled appointment first.
MOVES = {
    "sent": ("scheduled", "declined"),
    "scheduled": ("completed", "declined"),
}
PRIORITIES = ("routine", "urgent")
# Synthetic names only, so this list is clearly a fake roster and not tied to any real organization.
COORDINATORS = ("Unassigned", "Jordan Reyes", "Casey Lin", "Morgan Patel")
LABELS = {"sent": "Sent", "scheduled": "Scheduled", "completed": "Completed", "declined": "Declined"}
MAX_TEXT = 300
MAX_CODES = 10


class ReferralError(ValueError):
    """A referral the tracker will not accept. The message says why, in plain words."""


def default_followup_days() -> int:
    try:
        n = int(os.environ.get("HIM_REFERRAL_FOLLOWUP_DAYS", "14"))
    except ValueError:
        return 14
    return n if 1 <= n <= 90 else 14


@dataclass
class Referral:
    id: int
    patient_id: str
    specialty: str
    priority: str
    reason: str
    diagnosis: str
    status: str
    created: str
    updated: str
    referring_provider: str = ""
    receiving_provider: str = ""
    coordinator: str = ""
    notes: str = ""
    icd10_codes: str = ""
    appointment_date: str = ""

    def is_open(self) -> bool:
        return self.status in OPEN

    def days_since_update(self, today: date) -> int:
        return (today - date.fromisoformat(self.updated)).days

    def state(self, today: date) -> str:
        """"closed", "no response" (sent, nothing's moved in a while), "scheduled" or "on track"."""
        if not self.is_open():
            return "closed"
        if self.status == "sent" and self.days_since_update(today) > self.followup_days:
            return "no response"
        return "scheduled" if self.status == "scheduled" else "on track"


@dataclass
class Event:
    on: str
    what: str
    note: str


def _text(value, name: str, required: bool = True) -> str:
    v = " ".join(str(value or "").split())
    if required and not v:
        raise ReferralError(f"{name} is required.")
    if len(v) > MAX_TEXT:
        raise ReferralError(f"{name} is too long (limit {MAX_TEXT} characters).")
    return v


def _day(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ReferralError(f"{name} must be a date like 2026-10-24.") from None


def _icd_codes(value) -> str:
    """A comma/space-separated list of ICD-10-CM codes, normalized and de-duplicated. Shape only
    (A00.0-style); not checked against a loaded code file, so a well-formed but made-up code passes."""
    tokens = [t.strip() for t in re.split(r"[,\s]+", str(value or "")) if t.strip()]
    if not tokens:
        return ""
    if len(tokens) > MAX_CODES:
        raise ReferralError(f"Too many codes (limit {MAX_CODES}).")
    out: list[str] = []
    for t in tokens:
        if not looks_like_code(t):
            raise ReferralError(f"'{t}' does not look like an ICD-10-CM code.")
        c = format_code(t)
        if c not in out:
            out.append(c)
    return ", ".join(out)


class ReferralStore:
    def __init__(self, path: str | Path = "data/referrals.db", followup_days: int | None = None):
        self.path = Path(path)
        self.followup_days = followup_days or default_followup_days()
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, specialty TEXT NOT NULL,
                priority TEXT NOT NULL, reason TEXT NOT NULL, diagnosis TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
                referring_provider TEXT NOT NULL DEFAULT '', receiving_provider TEXT NOT NULL DEFAULT '',
                coordinator TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                icd10_codes TEXT NOT NULL DEFAULT '', appointment_date TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS referral_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, referral_id INTEGER NOT NULL, on_day TEXT NOT NULL,
                what TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
        """)

    # ---- reading ----
    _COLS = ("id", "patient_id", "specialty", "priority", "reason", "diagnosis", "status", "created", "updated",
             "referring_provider", "receiving_provider", "coordinator", "notes", "icd10_codes", "appointment_date")

    def _row(self, r) -> Referral:
        ref = Referral(*r)
        ref.followup_days = self.followup_days
        return ref

    def get(self, rid: int) -> Referral | None:
        r = self.db.execute(f"SELECT {','.join(self._COLS)} FROM referrals WHERE id=?", (rid,)).fetchone()
        return self._row(r) if r else None

    def list(self, status: str | None = None, today: date | None = None) -> list[Referral]:
        sql = f"SELECT {','.join(self._COLS)} FROM referrals"
        args: tuple = ()
        if status in STATUSES:
            sql += " WHERE status=?"
            args = (status,)
        rows = [self._row(r) for r in self.db.execute(sql, args)]
        today = today or date.today()
        rank = {"no response": 0, "scheduled": 1, "on track": 2, "closed": 3}
        rows.sort(key=lambda q: (rank[q.state(today)], q.updated if q.is_open() else "", -q.id))
        return rows

    def events(self, rid: int) -> list[Event]:
        return [Event(*r) for r in self.db.execute(
            "SELECT on_day,what,note FROM referral_events WHERE referral_id=? ORDER BY id", (rid,))]

    def summary(self, today: date) -> dict:
        rows = self.list(today=today)
        open_ = [q for q in rows if q.is_open()]
        return {"total": len(rows), "open": len(open_),
                "no_response": sum(1 for q in open_ if q.state(today) == "no response"),
                "scheduled": sum(1 for q in open_ if q.status == "scheduled")}

    # ---- changing ----
    def _event(self, rid: int, on: date, what: str, note: str = "") -> None:
        self.db.execute("INSERT INTO referral_events (referral_id,on_day,what,note) VALUES (?,?,?,?)",
                        (rid, on.isoformat(), what, note))

    def create(self, patient_id: str, specialty: str, priority: str, reason: str, diagnosis: str = "",
               referring_provider: str = "", receiving_provider: str = "", coordinator: str = "",
               notes: str = "", icd10_codes: str = "", created: str | date | None = None,
               today: date | None = None) -> int:
        today = today or date.today()
        pid = _text(patient_id, "Patient")
        spec = _text(specialty, "Specialty")
        if priority not in PRIORITIES:
            raise ReferralError("Pick a priority from the list.")
        why = _text(reason, "Reason for referral")
        dx = _text(diagnosis, "Diagnosis", required=False)
        ref_by = _text(referring_provider, "Referring provider")
        ref_to = _text(receiving_provider, "Receiving provider", required=False)
        coord = str(coordinator or "").strip() or "Unassigned"
        if coord not in COORDINATORS:
            raise ReferralError("Pick a coordinator from the list.")
        note_text = _text(notes, "Notes", required=False)
        codes = _icd_codes(icd10_codes)
        made = today if created is None else (created if isinstance(created, date) else _day(created, "Date sent"))
        if made > today:
            raise ReferralError("Date sent cannot be in the future.")
        cur = self.db.execute(
            "INSERT INTO referrals (patient_id,specialty,priority,reason,diagnosis,status,created,updated,"
            "referring_provider,receiving_provider,coordinator,notes,icd10_codes,appointment_date) "
            "VALUES (?,?,?,?,?,'sent',?,?,?,?,?,?,?,'')",
            (pid, spec, priority, why, dx, made.isoformat(), made.isoformat(),
             ref_by, ref_to, coord, note_text, codes))
        rid = cur.lastrowid
        self._event(rid, made, "Sent")
        self.db.commit()
        audit.log_call("referral_create", {}, "ok")
        return rid

    def set_status(self, rid: int, status: str, note: str = "", appointment_date: str | date | None = None,
                   today: date | None = None) -> None:
        today = today or date.today()
        q = self.get(rid)
        if q is None:
            raise ReferralError("No such referral.")
        if q.status in CLOSED:
            raise ReferralError(f"This referral is {q.status} and cannot be changed.")
        if status not in MOVES[q.status]:
            if status == "completed" and q.status != "scheduled":
                raise ReferralError("It cannot be completed until an appointment is scheduled.")
            raise ReferralError(f"From {LABELS[q.status].lower()} it can only go to: "
                               + ", ".join(LABELS[s].lower() for s in MOVES[q.status]) + ".")
        n = _text(note, "Note", required=status == "declined")   # a decline must say why
        appt = ""
        if status == "scheduled" and appointment_date:
            appt = (appointment_date if isinstance(appointment_date, date)
                    else _day(appointment_date, "Appointment date")).isoformat()
        if appt:
            self.db.execute("UPDATE referrals SET status=?, updated=?, appointment_date=? WHERE id=?",
                            (status, today.isoformat(), appt, rid))
        else:
            self.db.execute("UPDATE referrals SET status=?, updated=? WHERE id=?",
                            (status, today.isoformat(), rid))
        what = f"{LABELS[status]} for {appt}" if appt else LABELS[status]
        self._event(rid, today, what, n)
        self.db.commit()
        audit.log_call("referral_status", {"to": status}, "ok")
