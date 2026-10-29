"""Scheduling/EHR sync diagnostic: finds the three ways a patient-messaging platform and an EHR drift apart.

Generic on purpose. "Platform" is any patient-facing scheduling and texting tool; "EHR" is the chart system of
record. No vendor names, no real clinic rules, no real data: the demo data is invented, and a real export would
need to be mapped into the small JSON shape below (see `load`).

Three rules, each one a failure a front desk actually sees:

  1. silent_reschedule   An appointment time was changed by staff or the system, and no notice reached the
                         patient within `notify_within_min` minutes. (Changes the patient made themselves need
                         no notice.)
  2. boundary            An appointment exists that the clinic's own scheduling rules would not allow staff to
                         book: outside clinic hours, provider not working that day, visit type the provider does
                         not see, or overlapping / too close to another visit for the same provider.
  3. arrival             A patient texted "I'm here" and the EHR check-in never happened, happened late, or the
                         back office was never alerted (or was alerted late).

Input is one dict (JSON file) with: rules, appointments, changes, notices, arrivals. Times are ISO 8601 without
a time zone (clinic-local). Every finding carries the appointment id, a plain-language reason, and a generic
first thing to check. It finds records that look wrong; it cannot say why a particular system did it.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SILENT, BOUNDARY, ARRIVAL = "silent_reschedule", "boundary", "arrival"
RULE_TITLES = {
    SILENT: "Appointment moved without telling the patient",
    BOUNDARY: "Booking the clinic's own rules would not allow",
    ARRIVAL: "Arrival text did not complete check-in or alert the back office",
}
NOTICE_KINDS = {"reschedule_notice", "cancellation_notice"}


class DataError(ValueError):
    """The input is not in the expected shape."""


@dataclass(frozen=True)
class Finding:
    rule: str
    appt: str
    reason: str          # what is wrong, plain language
    detail: str          # the specific times/values
    check_first: str     # a generic first thing to look at
    booked_by: str = ""  # who created the appointment (patient/staff), for the boundary rule

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Rules:
    hours: dict          # weekday (0=Mon) -> (open_minute, close_minute)
    providers: dict      # provider id -> {"days": [weekday...], "visit_types": [...]}
    buffer_minutes: int = 0
    notify_within_min: int = 60
    checkin_within_min: int = 5
    alert_within_min: int = 5


@dataclass
class Report:
    findings: list
    counts: dict         # rule -> number of findings
    checked: dict        # rule -> number of records looked at
    notes: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.findings)


# ---- input ------------------------------------------------------------------------------------------------

def _dt(value, what: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        raise DataError(f"{what}: {value!r} is not an ISO date-time") from None


def _minutes(hhmm: str) -> int:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        raise DataError(f"{hhmm!r} is not HH:MM") from None


def parse_rules(raw: dict) -> Rules:
    try:
        hours = {int(d): (_minutes(a), _minutes(b)) for d, (a, b) in raw["hours"].items()}
        providers = {str(p): {"days": [int(d) for d in v["days"]], "visit_types": list(v["visit_types"])}
                     for p, v in raw["providers"].items()}
    except (KeyError, TypeError, ValueError) as e:
        raise DataError(f"rules are missing or malformed: {e}") from None
    r = Rules(hours=hours, providers=providers)
    for key in ("buffer_minutes", "notify_within_min", "checkin_within_min", "alert_within_min"):
        if key in raw:
            setattr(r, key, int(raw[key]))
    return r


def load(path) -> dict:
    p = Path(path)
    if p.stat().st_size > 20_000_000:
        raise DataError("file is over 20 MB")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DataError(f"cannot read {p.name}: {e}") from None
    if not isinstance(data, dict):
        raise DataError("top level must be an object")
    return data


# ---- the three rules --------------------------------------------------------------------------------------

def _silent(data: dict, rules: Rules, notes: list) -> tuple[list, int]:
    notices: dict = {}
    for n in data.get("notices", []):
        if n.get("kind") in NOTICE_KINDS:
            notices.setdefault(n["appt"], []).append(_dt(n["at"], "notice.at"))
    out, looked = [], 0
    for c in data.get("changes", []):
        old, new = _dt(c.get("old_start"), "change.old_start"), _dt(c.get("new_start"), "change.new_start")
        if old is None or new is None or old == new:
            continue
        if c.get("by") == "patient":
            continue
        looked += 1
        at = _dt(c.get("at"), "change.at")
        limit = at + timedelta(minutes=rules.notify_within_min)
        sent = [t for t in notices.get(c["appt"], []) if at <= t <= limit]
        if sent:
            continue
        late = [t for t in notices.get(c["appt"], []) if t > limit]
        if late:
            detail = f"moved {old:%a %m-%d %H:%M} -> {new:%a %m-%d %H:%M} by {c.get('by', '?')}; notice sent {int((min(late) - at).total_seconds() // 60)} min later (limit {rules.notify_within_min})"
        else:
            detail = f"moved {old:%a %m-%d %H:%M} -> {new:%a %m-%d %H:%M} by {c.get('by', '?')}; no notice on record"
        out.append(Finding(SILENT, c["appt"], "Time changed by " + str(c.get("by", "?")) + " and the patient was not told in time",
                           detail, "Is the move made in a screen or by a process that does not trigger the platform's reschedule message?"))
    return out, looked


def _boundary(data: dict, rules: Rules, notes: list) -> tuple[list, int]:
    appts = []
    for a in data.get("appointments", []):
        if a.get("status") == "cancelled":
            continue
        start = _dt(a.get("start"), "appointment.start")
        appts.append((a, start, start + timedelta(minutes=int(a.get("duration", 20)))))
    out = []

    def add(a, reason, detail, hint):
        out.append(Finding(BOUNDARY, a["id"], reason, detail, hint, booked_by=a.get("booked_by", "")))

    for a, s, e in appts:
        day = s.weekday()
        sm, em = s.hour * 60 + s.minute, e.hour * 60 + e.minute
        if e.date() != s.date() or day not in rules.hours or sm < rules.hours[day][0] or em > rules.hours[day][1]:
            add(a, "Outside clinic hours", f"{s:%a %H:%M}-{e:%H:%M}",
                "Does the platform read the same hours the EHR schedule uses, or its own copy?")
        prov = rules.providers.get(str(a.get("provider")))
        if prov is None:
            notes.append(f"{a['id']}: provider {a.get('provider')!r} has no rules; provider checks skipped")
        else:
            if day not in prov["days"]:
                add(a, "Provider does not work that day", f"{a['provider']} on {s:%a}",
                    "Is the provider's template shared with the platform, or only the open slots?")
            if a.get("visit_type") not in prov["visit_types"]:
                add(a, "Provider does not see this visit type", f"{a['provider']} / {a.get('visit_type')}",
                    "Are visit-type restrictions mapped to the platform's appointment types?")
    by_prov: dict = {}
    for item in appts:
        by_prov.setdefault(item[0].get("provider"), []).append(item)
    for items in by_prov.values():
        items.sort(key=lambda t: (t[1], t[0]["id"]))
        for (pa, ps, pe), (a, s, e) in zip(items, items[1:]):
            gap = (s - pe).total_seconds() / 60
            if gap < 0:
                add(a, "Overlaps another visit for the same provider", f"{s:%a %H:%M} overlaps {pa['id']} (ends {pe:%H:%M})",
                    "Does the platform hold a slot until the EHR confirms it, or can two bookings land at once?")
            elif gap < rules.buffer_minutes:
                add(a, "Too close to the previous visit", f"{int(gap)} min after {pa['id']}; buffer is {rules.buffer_minutes}",
                    "Is the buffer set in the EHR but not in the platform?")
    return out, len(appts)


def _arrival(data: dict, rules: Rules, notes: list) -> tuple[list, int]:
    out, looked = [], 0
    for r in data.get("arrivals", []):
        text = _dt(r.get("text_at"), "arrival.text_at")
        if text is None:
            continue
        looked += 1
        chk, alert = _dt(r.get("checkin_at"), "arrival.checkin_at"), _dt(r.get("alert_at"), "arrival.alert_at")
        problems = []
        if chk is None:
            problems.append("EHR check-in never happened")
        elif (chk - text).total_seconds() / 60 > rules.checkin_within_min:
            problems.append(f"check-in {int((chk - text).total_seconds() // 60)} min after the text")
        if alert is None:
            problems.append("back office never alerted")
        elif (alert - text).total_seconds() / 60 > rules.alert_within_min:
            problems.append(f"alert {int((alert - text).total_seconds() // 60)} min after the text")
        if not problems:
            continue
        hint = ("Does the platform's 'arrived' message write back to the EHR, or only update its own status?"
                if chk is None else "Is the alert tied to the EHR status change, or sent separately?")
        out.append(Finding(ARRIVAL, r["appt"], (lambda t: t[0].upper() + t[1:])("; ".join(problems)), f"text at {text:%H:%M}", hint))
    return out, looked


def diagnose(data: dict) -> Report:
    if "rules" not in data:
        raise DataError("missing 'rules'")
    rules = parse_rules(data["rules"])
    notes: list = []
    findings, checked = [], {}
    for name, fn in ((SILENT, _silent), (BOUNDARY, _boundary), (ARRIVAL, _arrival)):
        got, looked = fn(data, rules, notes)
        findings += got
        checked[name] = looked
    counts = {name: sum(1 for f in findings if f.rule == name) for name in (SILENT, BOUNDARY, ARRIVAL)}
    return Report(findings=findings, counts=counts, checked=checked, notes=notes)


def text_report(rep: Report, limit: int = 8) -> str:
    lines = ["Scheduling / EHR sync diagnostic (synthetic data)", ""]
    for rule in (SILENT, BOUNDARY, ARRIVAL):
        lines.append(f"{RULE_TITLES[rule]}: {rep.counts[rule]} of {rep.checked[rule]} checked")
        shown = [f for f in rep.findings if f.rule == rule][:limit]
        for f in shown:
            lines.append(f"  {f.appt}  {f.reason}")
            lines.append(f"      {f.detail}")
        if rep.counts[rule] > len(shown):
            lines.append(f"  ... and {rep.counts[rule] - len(shown)} more")
        if shown:
            lines.append(f"  First thing to check: {shown[0].check_first}")
        lines.append("")
    if rep.notes:
        lines.append("Notes:")
        lines += [f"  {n}" for n in rep.notes[:10]]
        lines.append("")
    lines.append("This finds records that look wrong. It cannot tell you why a given system did it.")
    return "\n".join(lines)


# ---- invented demo data (with known, injected defects) ----------------------------------------------------

DEMO_RULES = {
    "hours": {"0": ["08:00", "17:00"], "1": ["08:00", "17:00"], "2": ["08:00", "17:00"],
              "3": ["08:00", "17:00"], "4": ["08:00", "12:00"]},
    "providers": {
        "PROV-A": {"days": [0, 1, 2, 3], "visit_types": ["follow-up", "new-patient", "physical"]},
        "PROV-B": {"days": [1, 2, 3, 4], "visit_types": ["follow-up", "physical"]},
    },
    "buffer_minutes": 0, "notify_within_min": 60, "checkin_within_min": 5, "alert_within_min": 5,
}


def make_demo(seed: int = 7, n: int = 120, start_day: str = "2026-03-02"):
    """Return (data, truth). `truth` lists the appointment ids that were deliberately broken, per rule, so tests
    can confirm the diagnostic finds exactly those. All people and ids are invented.

    Normal appointments sit on a clean grid of valid slots (weeks 0-3). Boundary breaks are extra appointments on
    otherwise empty weeks (5 and 6), so they cannot collide with anything else."""
    rnd = random.Random(seed)
    rules = parse_rules(DEMO_RULES)
    base = datetime.fromisoformat(start_day)  # a Monday
    grid = []
    for prov, pr in rules.providers.items():
        for week in range(4):
            for day in pr["days"]:
                o, c = rules.hours[day]
                for m in range(o, c - 20 + 1, 20):
                    grid.append((prov, base + timedelta(days=day + 7 * week, minutes=m)))
    rnd.shuffle(grid)
    n_moved = 30
    picks, spare = grid[:n], grid[n:n + n_moved]
    appts, changes, notices, arrivals = [], [], [], []
    truth = {SILENT: [], BOUNDARY: [], ARRIVAL: []}
    for i, (prov, when) in enumerate(picks):
        appts.append({"id": f"A{i + 1:04d}", "patient": f"P{rnd.randrange(1, 900):03d}", "provider": prov,
                      "visit_type": rnd.choice(rules.providers[prov]["visit_types"]), "start": when.isoformat(),
                      "duration": 20, "booked_by": rnd.choice(["patient", "staff"]), "status": "booked"})
    # 1. moved by staff or system. Every third one is not (or only late) told to the patient.
    for k in range(n_moved):
        a = appts[k]
        old = datetime.fromisoformat(a["start"])
        new_prov, new = spare[k]
        a["provider"], a["start"] = new_prov, new.isoformat()
        if a["visit_type"] not in rules.providers[new_prov]["visit_types"]:
            a["visit_type"] = "follow-up"
        at = old - timedelta(days=3, hours=rnd.randrange(0, 5))
        changes.append({"appt": a["id"], "at": at.isoformat(), "by": "staff" if k % 2 else "system",
                        "old_start": old.isoformat(), "new_start": new.isoformat()})
        if k % 3 == 0:
            truth[SILENT].append(a["id"])
            if k % 6 == 0:  # told, but two hours late
                notices.append({"appt": a["id"], "at": (at + timedelta(minutes=120)).isoformat(), "kind": "reschedule_notice"})
        else:
            notices.append({"appt": a["id"], "at": (at + timedelta(minutes=rnd.randrange(1, 30))).isoformat(),
                            "kind": "reschedule_notice"})
    # a move the patient made themselves needs no notice and must not be flagged
    p = appts[n_moved]
    when = datetime.fromisoformat(p["start"])
    changes.append({"appt": p["id"], "at": (when - timedelta(days=5)).isoformat(), "by": "patient",
                    "old_start": (when - timedelta(days=1)).isoformat(), "new_start": when.isoformat()})
    # 2. boundary breaks: extra patient-channel bookings on empty weeks 5 and 6
    def extra(i, prov, start, vtype="follow-up"):
        a = {"id": f"B{i:04d}", "patient": f"P{900 + i}", "provider": prov, "visit_type": vtype,
             "start": start.isoformat(), "duration": 20, "booked_by": "patient", "status": "booked"}
        appts.append(a)
        return a
    w5, w6 = base + timedelta(weeks=5), base + timedelta(weeks=6)
    for j in range(3):
        truth[BOUNDARY].append(extra(10 + j, "PROV-A", w5 + timedelta(hours=18, minutes=20 * j))["id"])      # after hours (Mon)
        truth[BOUNDARY].append(extra(20 + j, "PROV-B", w5 + timedelta(hours=8, minutes=20 * j))["id"])       # PROV-B off on Monday
        truth[BOUNDARY].append(extra(30 + j, "PROV-B", w5 + timedelta(days=1, hours=8, minutes=20 * j), "new-patient")["id"])  # type
        first = extra(40 + j, "PROV-A", w6 + timedelta(hours=8 + j))
        truth[BOUNDARY].append(extra(50 + j, "PROV-A", datetime.fromisoformat(first["start"]) + timedelta(minutes=10))["id"])  # overlap
    # 3. arrival texts on normal appointments that were not moved
    steady = appts[n_moved + 1:n_moved + 61]
    for k, a in enumerate(steady):
        t = datetime.fromisoformat(a["start"]) - timedelta(minutes=rnd.randrange(3, 15))
        rec = {"appt": a["id"], "text_at": t.isoformat(), "checkin_at": (t + timedelta(minutes=1)).isoformat(),
               "alert_at": (t + timedelta(minutes=1)).isoformat()}
        if k % 4 == 0:
            rec["checkin_at"] = None
        elif k % 4 == 1:
            rec["alert_at"] = None
        elif k % 8 == 2:
            rec["alert_at"] = (t + timedelta(minutes=20)).isoformat()
        else:
            arrivals.append(rec)
            continue
        truth[ARRIVAL].append(a["id"])
        arrivals.append(rec)
    data = {"rules": DEMO_RULES, "appointments": appts, "changes": changes, "notices": notices, "arrivals": arrivals}
    return data, {k: sorted(v) for k, v in truth.items()}
