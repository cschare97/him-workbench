import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("flask")

from him_workbench import audit
from him_workbench.analytics import (QualityHistory, dim_date, export_csv, line_geometry, referral_facts, referral_summary,
                                     roi_by_month, roi_facts, roi_summary)
from him_workbench.demo_data import seed_demo
from him_workbench.matching import Patient
from him_workbench.quality import analyze
from him_workbench.referrals import ReferralStore
from him_workbench.roi import RoiStore
from him_workbench.webapp import create_app

T = date(2026, 10, 28)
HDR = {"Host": "127.0.0.1:5050"}


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


def day(n: int) -> date:
    return T - timedelta(days=n)


@pytest.fixture
def stores():
    refs, roi = ReferralStore(":memory:", followup_days=14), RoiStore(":memory:", days=30)
    a = refs.create("p1", "Cardiology", "routine", "secret reason text", referring_provider="Dr. X", created=day(40), today=day(40))
    refs.set_status(a, "scheduled", appointment_date=day(20), today=day(30))      # 10 days to schedule
    refs.set_status(a, "completed", today=day(20))                                # 20 days to complete
    refs.create("p2", "Cardiology", "urgent", "r", referring_provider="Dr. X", created=day(30), today=day(30))   # stalled
    refs.create("p3", "Dermatology", "routine", "r", referring_provider="Dr. X", created=day(3), today=day(3))   # on track
    on_time = roi.create("p1", "patient", "secret request text", day(50), today=day(50))
    for st, d in (("verified", 48), ("in_progress", 46), ("fulfilled", 25)):
        roi.set_status(on_time, st, today=day(d))                                 # closed day 25 of 30: on time
    late = roi.create("p2", "attorney", "r", day(60), today=day(60))
    for st, d in (("verified", 58), ("in_progress", 56), ("fulfilled", 20)):
        roi.set_status(late, st, today=day(d))                                    # closed day 40: late
    roi.create("p3", "insurer", "r", day(45), today=day(45))                      # open and overdue
    return refs, roi


def test_referral_facts_and_summary(stores):
    f = referral_facts(stores[0], T)
    by = {r["referral_id"]: r for r in f}
    assert by[1]["days_to_schedule"] == 10 and by[1]["days_to_complete"] == 20 and by[1]["status"] == "completed"
    assert by[2]["no_response"] == 1 and by[2]["days_to_schedule"] == "" and by[2]["days_open"] == 30
    assert by[3]["state"] == "on track" and by[3]["no_response"] == 0
    s = referral_summary(f)
    assert (s["total"], s["open"], s["no_response"], s["completed"]) == (3, 2, 1, 1)
    assert s["median_days_to_schedule"] == 10.0          # the stalled one is skipped, not counted as zero
    assert {r["specialty"]: r["n"] for r in s["by_specialty"]} == {"Cardiology": 2, "Dermatology": 1}


def test_facts_hold_no_patient_ids_or_free_text(stores):
    for rows in (referral_facts(stores[0], T), roi_facts(stores[1], T)):
        blob = " ".join(str(v) for r in rows for v in r.values()) + " ".join(rows[0].keys())
        assert "p1" not in blob.split() and "secret" not in blob and "patient_id" not in blob and "reason" not in blob


def test_roi_on_time_late_and_overdue(stores):
    f = {r["request_id"]: r for r in roi_facts(stores[1], T)}
    assert f[1]["closed_on_time"] == 1 and f[1]["days_to_close"] == 25 and f[1]["days_late"] == 0
    assert f[2]["closed_on_time"] == 0 and f[2]["days_to_close"] == 40 and f[2]["days_late"] == 10
    assert f[3]["closed_on"] == "" and f[3]["overdue"] == 1 and f[3]["days_left"] == -15 and f[3]["closed_on_time"] == ""
    s = roi_summary(list(f.values()))
    assert (s["total"], s["open"], s["overdue"], s["closed"], s["closed_on_time"]) == (3, 1, 1, 2, 1)
    assert s["on_time_pct"] == 50.0 and s["median_days_to_close"] == 32.5
    months = roi_by_month(list(f.values()))
    assert sum(m["on_time"] for m in months) == 1 and sum(m["late"] for m in months) == 1


def test_an_extension_moves_the_due_date_used_for_on_time():
    roi = RoiStore(":memory:", days=30)
    r = roi.create("p", "patient", "x", day(50), today=day(50))
    roi.set_status(r, "verified", today=day(48))
    roi.extend(r, "more records", today=day(25))                        # due moves from day(20) to day(-10)
    roi.set_status(r, "in_progress", today=day(24))
    roi.set_status(r, "fulfilled", today=day(10))                       # closed day 40 of 60
    f = roi_facts(roi, T)[0]
    assert f["extended"] == 1 and f["original_due"] != f["due"] and f["closed_on_time"] == 1


def test_quality_history_is_counts_only_and_one_per_day():
    h = QualityHistory(":memory:")
    pts = [Patient("1", "Ann", "Lee", "1990-01-01", "F", "9021"), Patient("2", "Bo", "Ng", "1991-02-02", "M", "90210")]
    h.record(analyze(pts), day(7), source="demo")
    h.record(analyze(pts[:1]), day(7), source="demo")                   # same day replaces
    h.record(analyze(pts), day(0))
    snaps = h.snapshots()
    assert [s["day"] for s in snaps] == [day(7).isoformat(), day(0).isoformat()] and snaps[0]["records"] == 1
    assert snaps[1]["problems"]["zip_malformed"] == 1 and snaps[1]["problems_per_100"] == 50.0
    raw = " ".join(r[0] for r in h.db.execute("SELECT report FROM snapshots"))
    assert "Ann" not in raw and "9021" not in raw
    assert {r["kind"] for r in h.rows()} == {"problem", "missing"}


def test_dim_date_is_contiguous():
    rows = dim_date(date(2026, 1, 1), date(2026, 12, 31))
    assert len(rows) == 365 and rows[0]["date"] == "2026-01-01" and rows[-1]["date"] == "2026-12-31"
    assert rows[0]["weekday"] == "Thursday" and rows[2]["is_weekend"] == 1 and rows[0]["quarter"] == "Q1"


def test_export_csv_files_and_headers(stores, tmp_path):
    h = QualityHistory(":memory:")
    h.record(analyze([Patient("1", "Ann", "Lee", "1990-01-01", "F", "90210")]), T)
    counts = export_csv(tmp_path / "out", stores[0], stores[1], h, T)
    assert set(counts) == {"referrals.csv", "roi_requests.csv", "quality_snapshots.csv", "dim_date.csv"}
    with (tmp_path / "out/referrals.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3 and "patient_id" not in rows[0] and rows[0]["specialty"]


def test_export_of_empty_stores_still_has_headers(tmp_path):
    export_csv(tmp_path, ReferralStore(":memory:"), RoiStore(":memory:"), None, T)
    assert (tmp_path / "referrals.csv").read_text().startswith("referral_id,specialty")
    assert (tmp_path / "roi_requests.csv").read_text().startswith("request_id,requester_type")


def test_line_geometry():
    g = line_geometry([14.5, 7.0, 5.0], ["a", "b", "c"])
    assert len(g["points"]) == 3 and g["points"][0]["x"] < g["points"][2]["x"] and g["points"][0]["y"] < g["points"][2]["y"]
    assert g["ticks"][0]["value"] == 0 and g["ticks"][-1]["value"] >= 14.5
    assert line_geometry([], [])["points"] == []
    assert len(line_geometry([3.0], ["a"])["points"]) == 1


# ---- demo data ----

def test_seed_demo_is_deterministic_and_never_overwrites(tmp_path):
    a = seed_demo(tmp_path / "a", today=T)
    seed_demo(tmp_path / "b", today=T)
    fa = referral_facts(ReferralStore(a["referrals"]), T)
    fb = referral_facts(ReferralStore(tmp_path / "b/referrals.db"), T)
    assert len(fa) == 160 and fa == fb
    assert len(roi_facts(RoiStore(a["roi"]), T)) == 90
    snaps = QualityHistory(a["quality"]).snapshots()
    assert len(snaps) == 12 and {s["source"] for s in snaps} == {"demo"}
    assert snaps[0]["problems_per_100"] > snaps[-1]["problems_per_100"]
    with pytest.raises(FileExistsError):
        seed_demo(tmp_path / "a", today=T)
    seed_demo(tmp_path / "a", today=T, force=True)


def test_demo_data_goes_through_the_same_rules_as_real_entries(tmp_path):
    p = seed_demo(tmp_path, today=T)
    refs = ReferralStore(p["referrals"])
    for r in refs.list(today=T):
        ev = [e.on for e in refs.events(r.id)]
        assert ev == sorted(ev) and max(ev) <= T.isoformat()           # dated, in order, none in the future


# ---- web page ----

def make(tmp_path, **kw):
    return create_app(texture_dir=tmp_path / "tex", today=lambda: T, **kw).test_client()


def test_dashboard_empty_state_points_to_demo_data(tmp_path):
    r = make(tmp_path).get("/dashboard", headers=HDR)
    assert r.status_code == 200 and r.data.count(b"--demo-data") == 3 and b"DEMO DATA" not in r.data


def test_dashboard_shows_the_numbers_and_the_demo_banner(tmp_path, stores):
    h = QualityHistory(":memory:")
    h.record(analyze([Patient("1", "Ann", "Lee", "1990-01-01", "F", "9021")]), T)
    page = make(tmp_path, referral_store=stores[0], roi_store=stores[1], quality_history=h, demo_data=True).get("/dashboard", headers=HDR).data
    assert b"DEMO DATA" in page and b"Cardiology" in page and b"Dermatology" in page
    assert b"(1 of 2)" in page                                   # ROI closed on time
    assert b"<polyline" not in page                              # one snapshot draws a dot, not a line
    assert b"role=\"img\"" in page and b"Show as a table" in page


def test_dashboard_text_from_the_data_is_escaped(tmp_path):
    refs = ReferralStore(":memory:")
    q = refs.create("p", "<script>alert(1)</script>", "routine", "r", referring_provider="Dr", created=day(10), today=day(10))
    refs.set_status(q, "scheduled", appointment_date=day(2), today=day(5))
    page = make(tmp_path, referral_store=refs).get("/dashboard", headers=HDR).data
    assert b"<script>alert(1)" not in page and b"&lt;script&gt;" in page


def test_dashboard_rejects_a_bad_host(tmp_path):
    assert make(tmp_path).get("/dashboard", headers={"Host": "evil.example"}).status_code == 403


def test_xlsx_export_has_headers_and_real_types(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from him_workbench.analytics import export_xlsx
    (tmp_path / "t.csv").write_text("id,created,month,score,note\n1,2026-01-05,2026-01,2.5,a\n2,,2026-01,,\n", encoding="utf-8")
    assert export_xlsx(tmp_path) == {"t": 2}
    ws = openpyxl.load_workbook(tmp_path / "powerbi.xlsx")["t"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("id", "created", "month", "score", "note")
    assert rows[1][0] == 1 and isinstance(rows[1][1], __import__("datetime").datetime)
    assert rows[1][2] == "2026-01" and rows[1][3] == 2.5      # year-month stays text, not a date
    assert rows[2][1] is None and rows[2][3] is None            # blanks stay blank


def test_xlsx_export_without_csvs_is_a_clear_error(tmp_path):
    pytest.importorskip("openpyxl")
    from him_workbench.analytics import export_xlsx
    with pytest.raises(RuntimeError, match="no CSV"):
        export_xlsx(tmp_path)
