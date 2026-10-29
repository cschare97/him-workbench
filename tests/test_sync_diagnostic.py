import json
from copy import deepcopy

import pytest

from him_workbench import sync_diagnostic as sd


def _rules():
    return deepcopy(sd.DEMO_RULES)


def _data(**kw):
    d = {"rules": _rules(), "appointments": [], "changes": [], "notices": [], "arrivals": []}
    d.update(kw)
    return d


def _appt(aid="A1", start="2026-03-02T09:00:00", prov="PROV-A", vtype="follow-up", **kw):
    return {"id": aid, "provider": prov, "visit_type": vtype, "start": start, "duration": 20, "status": "booked", **kw}


def _found(data, rule):
    return [f for f in sd.diagnose(data).findings if f.rule == rule]


# ---- demo data vs the planted truth ----
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_demo_findings_match_planted_defects_exactly(seed):
    data, truth = sd.make_demo(seed)
    rep = sd.diagnose(data)
    for rule, ids in truth.items():
        assert sorted({f.appt for f in rep.findings if f.rule == rule}) == ids
    assert rep.notes == []


def test_demo_is_deterministic():
    assert sd.make_demo(5)[0] == sd.make_demo(5)[0]


# ---- rule 1: silent reschedule ----
CHG = {"appt": "A1", "at": "2026-03-01T10:00:00", "by": "staff", "old_start": "2026-03-02T09:00:00", "new_start": "2026-03-03T09:00:00"}


def test_move_with_no_notice_is_flagged():
    assert len(_found(_data(changes=[CHG]), sd.SILENT)) == 1


def test_move_with_timely_notice_is_fine():
    n = {"appt": "A1", "at": "2026-03-01T10:20:00", "kind": "reschedule_notice"}
    assert _found(_data(changes=[CHG], notices=[n]), sd.SILENT) == []


def test_late_notice_is_flagged_and_says_how_late():
    n = {"appt": "A1", "at": "2026-03-01T12:00:00", "kind": "reschedule_notice"}
    f = _found(_data(changes=[CHG], notices=[n]), sd.SILENT)
    assert len(f) == 1 and "120 min later" in f[0].detail


def test_notice_before_the_change_does_not_count():
    n = {"appt": "A1", "at": "2026-03-01T09:00:00", "kind": "reschedule_notice"}
    assert len(_found(_data(changes=[CHG], notices=[n]), sd.SILENT)) == 1


def test_reminder_is_not_a_reschedule_notice():
    n = {"appt": "A1", "at": "2026-03-01T10:05:00", "kind": "reminder"}
    assert len(_found(_data(changes=[CHG], notices=[n]), sd.SILENT)) == 1


def test_patient_made_change_needs_no_notice():
    assert _found(_data(changes=[{**CHG, "by": "patient"}]), sd.SILENT) == []


def test_change_with_same_time_is_ignored():
    assert _found(_data(changes=[{**CHG, "new_start": CHG["old_start"]}]), sd.SILENT) == []


# ---- rule 2: boundary ----
def test_clean_booking_has_no_findings():
    assert _found(_data(appointments=[_appt()]), sd.BOUNDARY) == []


@pytest.mark.parametrize("kw,reason", [
    ({"start": "2026-03-02T18:00:00"}, "Outside clinic hours"),
    ({"start": "2026-03-02T16:50:00"}, "Outside clinic hours"),   # ends after close
    ({"start": "2026-03-06T13:00:00", "prov": "PROV-B"}, "Outside clinic hours"),  # Friday half day
    ({"start": "2026-03-07T09:00:00"}, "Outside clinic hours"),   # Saturday
    ({"prov": "PROV-B"}, "Provider does not work that day"),      # Monday
    ({"vtype": "new-patient", "start": "2026-03-03T09:00:00", "prov": "PROV-B"}, "Provider does not see this visit type"),
])
def test_single_booking_violations(kw, reason):
    assert reason in {f.reason for f in _found(_data(appointments=[_appt(**kw)]), sd.BOUNDARY)}


def test_overlap_flags_the_later_booking_only():
    a, b = _appt("A1", "2026-03-02T09:00:00"), _appt("A2", "2026-03-02T09:10:00")
    f = _found(_data(appointments=[a, b]), sd.BOUNDARY)
    assert [x.appt for x in f] == ["A2"]


def test_back_to_back_is_fine_without_a_buffer_and_flagged_with_one():
    a, b = _appt("A1", "2026-03-02T09:00:00"), _appt("A2", "2026-03-02T09:20:00")
    assert _found(_data(appointments=[a, b]), sd.BOUNDARY) == []
    d = _data(appointments=[a, b])
    d["rules"]["buffer_minutes"] = 10
    assert [x.reason for x in _found(d, sd.BOUNDARY)] == ["Too close to the previous visit"]


def test_different_providers_do_not_overlap():
    a, b = _appt("A1", "2026-03-03T09:00:00"), _appt("A2", "2026-03-03T09:00:00", prov="PROV-B")
    assert _found(_data(appointments=[a, b]), sd.BOUNDARY) == []


def test_cancelled_appointments_are_ignored():
    assert _found(_data(appointments=[_appt(start="2026-03-02T18:00:00", status="cancelled")]), sd.BOUNDARY) == []


def test_unknown_provider_is_noted_not_crashed():
    rep = sd.diagnose(_data(appointments=[_appt(prov="PROV-Z")]))
    assert any("PROV-Z" in n for n in rep.notes)


# ---- rule 3: arrival ----
def _arr(**kw):
    return {"appt": "A1", "text_at": "2026-03-02T08:50:00", "checkin_at": "2026-03-02T08:51:00", "alert_at": "2026-03-02T08:51:00", **kw}


def test_good_arrival_is_fine():
    assert _found(_data(arrivals=[_arr()]), sd.ARRIVAL) == []


def test_no_checkin_and_no_alert_are_both_reported():
    f = _found(_data(arrivals=[_arr(checkin_at=None, alert_at=None)]), sd.ARRIVAL)
    assert len(f) == 1 and "never happened" in f[0].reason and "never alerted" in f[0].reason


def test_late_alert_is_flagged():
    f = _found(_data(arrivals=[_arr(alert_at="2026-03-02T09:10:00")]), sd.ARRIVAL)
    assert len(f) == 1 and "20 min" in f[0].reason


def test_reason_says_EHR_in_capitals():
    assert _found(_data(arrivals=[_arr(checkin_at=None)]), sd.ARRIVAL)[0].reason.startswith("EHR")


# ---- input handling ----
def test_missing_rules_is_a_clear_error():
    with pytest.raises(sd.DataError):
        sd.diagnose({"appointments": []})


def test_bad_timestamp_is_a_clear_error():
    with pytest.raises(sd.DataError, match="not an ISO"):
        sd.diagnose(_data(appointments=[_appt(start="next tuesday")]))


def test_load_rejects_non_object(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("[1,2]")
    with pytest.raises(sd.DataError):
        sd.load(p)


def test_load_rejects_bad_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{nope")
    with pytest.raises(sd.DataError):
        sd.load(p)


def test_empty_data_reports_zero():
    rep = sd.diagnose(_data())
    assert rep.total == 0 and rep.checked == {sd.SILENT: 0, sd.BOUNDARY: 0, sd.ARRIVAL: 0}


def test_cli_demo_and_file_roundtrip(tmp_path, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("cli", pathlib.Path(__file__).parents[1] / "scripts" / "sync_diag_cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    out = tmp_path / "d.json"
    assert cli.main(["--write-demo", str(out)]) == 0
    assert cli.main(["--file", str(out), "--json"]) == 0
    got = json.loads(capsys.readouterr().out.split("wrote")[-1].split("\n", 1)[1])
    assert got["counts"] == {sd.SILENT: 10, sd.BOUNDARY: 12, sd.ARRIVAL: 38}
    assert cli.main(["--file", str(tmp_path / "missing.json")]) == 2
