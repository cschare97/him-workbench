"""Dashboard data: tidy tables from the referral tracker, the ROI tracker and the data-quality history.

Everything here is read-only and holds dates, counts and categories, never patient ids, names, reasons, notes or
diagnoses. The same tables feed the web dashboard and the CSV export for Power BI, so the two cannot disagree.

Definitions (each is a plain count or median, nothing modelled):
  Referrals: days to schedule = date sent -> date scheduled; days to complete = date sent -> date completed.
             "No response" = sent, still not scheduled, and nothing has changed for more than the follow-up window.
  ROI:       closed = fulfilled or denied. On time = closed on or before the due date in force (an extension moves it).
             The deadline is the tracker's setting, 30 calendar days by default.
  Quality:   "problems per 100 records" adds up the per-check counts, and one record can trip several checks, so it is
             a trend indicator, not a percentage of records.
Medians skip blanks, so a referral that was never scheduled does not count as zero days.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path

from .quality import CHECK_LABELS, FIELDS, QualityReport
from . import audit


def _d(s) -> date | None:
    return date.fromisoformat(s) if s else None


def _days(a: date | None, b: date | None):
    return (b - a).days if a and b else ""


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _first(events, prefix: str) -> date | None:
    return next((_d(e.on) for e in events if e.what.startswith(prefix)), None)


def median(values) -> float | None:
    xs = [v for v in values if v != "" and v is not None]
    return float(statistics.median(xs)) if xs else None


# ---- fact tables ---------------------------------------------------------------------------------------------

def referral_facts(store, today: date) -> list[dict]:
    rows = []
    for q in store.list(today=today):
        ev = store.events(q.id)
        created, sched = _d(q.created), _first(ev, "Scheduled")
        done, declined = _first(ev, "Completed"), _first(ev, "Declined")
        state = q.state(today)
        rows.append({
            "referral_id": q.id, "specialty": q.specialty, "priority": q.priority, "coordinator": q.coordinator,
            "referring_provider": q.referring_provider, "receiving_provider": q.receiving_provider,
            "status": q.status, "state": state, "created": q.created, "created_month": q.created[:7],
            "scheduled_on": _iso(sched), "appointment_date": q.appointment_date,
            "completed_on": _iso(done), "declined_on": _iso(declined),
            "days_to_schedule": _days(created, sched), "days_to_appointment": _days(created, _d(q.appointment_date)),
            "days_to_complete": _days(created, done),
            "days_open": (today - created).days if q.is_open() else "",
            "days_since_update": q.days_since_update(today) if q.is_open() else "",
            "no_response": 1 if state == "no response" else 0,
        })
    return rows


def roi_facts(store, today: date) -> list[dict]:
    rows = []
    for q in store.list():
        ev = store.events(q.id)
        received, due = _d(q.received), _d(q.due)
        verified = _first(ev, "Verified")
        closed = _first(ev, "Fulfilled") or _first(ev, "Denied")
        state = q.state(today)
        rows.append({
            "request_id": q.id, "requester_type": q.requester_type, "status": q.status, "state": state,
            "received": q.received, "received_month": q.received[:7],
            "original_due": _iso(received + timedelta(days=store.days)), "due": q.due, "extended": 1 if q.extended else 0,
            "verified_on": _iso(verified), "closed_on": _iso(closed), "closed_month": _iso(closed)[:7],
            "days_to_verify": _days(received, verified), "days_to_close": _days(received, closed),
            "closed_on_time": (1 if closed <= due else 0) if closed else "",
            "days_late": max(0, (closed - due).days) if closed else "",
            "days_left": (due - today).days if q.is_open() else "",
            "overdue": 1 if state == "overdue" else 0,
        })
    return rows


# ---- summaries -----------------------------------------------------------------------------------------------

def referral_summary(facts: list[dict]) -> dict:
    open_ = [f for f in facts if f["state"] != "closed"]
    specs = {}
    for f in facts:
        specs.setdefault(f["specialty"], []).append(f)
    by_spec = sorted(({"specialty": s, "n": len(rows), "median_days_to_schedule": median(r["days_to_schedule"] for r in rows),
                       "no_response": sum(r["no_response"] for r in rows)} for s, rows in specs.items()),
                     key=lambda r: (r["median_days_to_schedule"] is None, -(r["median_days_to_schedule"] or 0), r["specialty"]))
    return {"total": len(facts), "open": len(open_), "no_response": sum(f["no_response"] for f in facts),
            "completed": sum(1 for f in facts if f["status"] == "completed"),
            "declined": sum(1 for f in facts if f["status"] == "declined"),
            "median_days_to_schedule": median(f["days_to_schedule"] for f in facts),
            "median_days_to_complete": median(f["days_to_complete"] for f in facts),
            "by_specialty": by_spec}


def roi_summary(facts: list[dict]) -> dict:
    closed = [f for f in facts if f["closed_on"]]
    on_time = sum(1 for f in closed if f["closed_on_time"] == 1)
    return {"total": len(facts), "open": sum(1 for f in facts if not f["closed_on"]),
            "overdue": sum(f["overdue"] for f in facts), "closed": len(closed),
            "closed_on_time": on_time, "on_time_pct": (100.0 * on_time / len(closed)) if closed else None,
            "denied": sum(1 for f in facts if f["status"] == "denied"),
            "extended": sum(f["extended"] for f in facts),
            "median_days_to_close": median(f["days_to_close"] for f in closed)}


def roi_by_month(facts: list[dict]) -> list[dict]:
    months: dict[str, dict] = {}
    for f in facts:
        if f["closed_on"]:
            m = months.setdefault(f["closed_month"], {"month": f["closed_month"], "on_time": 0, "late": 0})
            m["on_time" if f["closed_on_time"] == 1 else "late"] += 1
    return [months[k] for k in sorted(months)]


# ---- data-quality history ------------------------------------------------------------------------------------

class QualityHistory:
    """One counts-only snapshot of a data-quality report per day. Never holds a patient value."""

    def __init__(self, path: str | Path = "data/quality.db"):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, source TEXT NOT NULL, report TEXT NOT NULL)")

    def record(self, report: QualityReport, day: date | None = None, source: str = "real") -> None:
        day = day or date.today()
        self.db.execute("INSERT OR REPLACE INTO snapshots (day,source,report) VALUES (?,?,?)",
                        (day.isoformat(), source, json.dumps(report.to_dict())))
        self.db.commit()
        audit.log_call("quality_snapshot", {"records": report.records}, "ok")

    def snapshots(self) -> list[dict]:
        out = []
        for day, source, raw in self.db.execute("SELECT day,source,report FROM snapshots ORDER BY day"):
            r = json.loads(raw)
            n = max(int(r["records"]), 1)
            problems = {k: int(r["problems"].get(k, 0)) for k in CHECK_LABELS}
            missing = {k: int(r["missing"].get(k, 0)) for k in FIELDS}
            out.append({"day": day, "source": source, "records": r["records"], "problems": problems, "missing": missing,
                        "dup_groups": r.get("same_name_dob_groups", 0),
                        "problems_per_100": 100.0 * sum(problems.values()) / n,
                        "missing_per_100": 100.0 * sum(missing.values()) / n})
        return out

    def rows(self) -> list[dict]:
        """Long format for Power BI: one row per snapshot per metric."""
        out = []
        for s in self.snapshots():
            n = max(int(s["records"]), 1)
            for kind, d, labels in (("problem", s["problems"], CHECK_LABELS), ("missing", s["missing"], {k: f"{k} is blank" for k in FIELDS})):
                for k, v in d.items():
                    out.append({"snapshot_date": s["day"], "source": s["source"], "kind": kind, "metric": k,
                                "label": labels[k], "records_affected": v, "records": s["records"],
                                "pct_of_records": round(100.0 * v / n, 3)})
        return out


# ---- dashboard view-model and export -------------------------------------------------------------------------

def dashboard_data(referral_store, roi_store, history: QualityHistory | None, today: date) -> dict:
    rf = referral_facts(referral_store, today) if referral_store else []
    of = roi_facts(roi_store, today) if roi_store else []
    snaps = history.snapshots() if history else []
    return {"today": today.isoformat(), "referrals": referral_summary(rf), "roi": roi_summary(of),
            "roi_months": roi_by_month(of), "quality": snaps,
            "quality_latest": snaps[-1] if snaps else None,
            "sources": sorted({s["source"] for s in snaps})}


def dim_date(start: date, end: date) -> list[dict]:
    rows, d = [], start
    while d <= end:
        rows.append({"date": d.isoformat(), "year": d.year, "quarter": f"Q{(d.month - 1) // 3 + 1}", "month_number": d.month,
                     "month_name": d.strftime("%B"), "year_month": d.strftime("%Y-%m"),
                     "week_start": (d - timedelta(days=d.weekday())).isoformat(), "weekday": d.strftime("%A"),
                     "is_weekend": 1 if d.weekday() >= 5 else 0})
        d += timedelta(days=1)
    return rows


REFERRAL_COLUMNS = ["referral_id", "specialty", "priority", "coordinator", "referring_provider", "receiving_provider", "status",
                    "state", "created", "created_month", "scheduled_on", "appointment_date", "completed_on", "declined_on",
                    "days_to_schedule", "days_to_appointment", "days_to_complete", "days_open", "days_since_update", "no_response"]
ROI_COLUMNS = ["request_id", "requester_type", "status", "state", "received", "received_month", "original_due", "due", "extended",
               "verified_on", "closed_on", "closed_month", "days_to_verify", "days_to_close", "closed_on_time", "days_late",
               "days_left", "overdue"]


def _write(path: Path, rows: list[dict], fields: list[str] | None = None) -> int:
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def export_csv(out_dir: str | Path, referral_store, roi_store, history: QualityHistory | None, today: date) -> dict[str, int]:
    """Write fact tables and a calendar table for Power BI. Returns {file name: row count}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rf, of = referral_facts(referral_store, today), roi_facts(roi_store, today)
    qr = history.rows() if history else []
    counts = {"referrals.csv": _write(out / "referrals.csv", rf, REFERRAL_COLUMNS), "roi_requests.csv": _write(out / "roi_requests.csv", of, ROI_COLUMNS),
              "quality_snapshots.csv": _write(out / "quality_snapshots.csv", qr,
                                              ["snapshot_date", "source", "kind", "metric", "label", "records_affected",
                                               "records", "pct_of_records"])}
    dates = [date.fromisoformat(v) for row in rf for v in (row["created"], row["completed_on"], row["declined_on"]) if v]
    dates += [date.fromisoformat(v) for row in of for v in (row["received"], row["closed_on"], row["due"]) if v]
    dates += [date.fromisoformat(r["snapshot_date"]) for r in qr]
    if dates:
        counts["dim_date.csv"] = _write(out / "dim_date.csv", dim_date(date(min(dates).year, 1, 1), date(max(max(dates), today).year, 12, 31)))
    audit.log_call("dashboard_export", {}, ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts


def line_geometry(values: list[float], labels: list[str], w: int = 640, h: int = 190, left: int = 38, right: int = 48,
                  top: int = 14, bottom: int = 26) -> dict:
    """Pixel positions for a one-series line chart that starts at zero. The page draws it as inline SVG."""
    if not values:
        return {"points": [], "poly": "", "ticks": [], "w": w, "h": h, "left": left, "right": right, "bottom_y": h - bottom}
    top_value = max(values) or 1.0
    step = 5 if top_value > 12 else 2 if top_value > 5 else 1
    nice = (int(top_value // step) + 1) * step
    inner_w, inner_h = w - left - right, h - top - bottom

    def y(v: float) -> float:
        return round(top + inner_h * (1 - v / nice), 1)

    n = len(values)
    pts = [{"x": round(left + (inner_w * i / (n - 1) if n > 1 else inner_w / 2), 1), "y": y(v), "value": round(v, 1), "label": labels[i]}
           for i, v in enumerate(values)]
    ticks = [{"y": y(t), "value": t} for t in (0, nice // 2 if nice % 2 == 0 else nice / 2, nice)]
    return {"points": pts, "poly": " ".join(f"{p['x']},{p['y']}" for p in pts), "ticks": ticks, "w": w, "h": h,
            "left": left, "right": right, "bottom_y": h - bottom}
