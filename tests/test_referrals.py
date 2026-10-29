from datetime import date

import pytest

from him_workbench import audit
from him_workbench.referrals import ReferralError, ReferralStore

T = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


@pytest.fixture
def store():
    return ReferralStore(":memory:", followup_days=14)


def make(store, **kw):
    a = dict(patient_id="p1", specialty="Cardiology", priority="routine",
             reason="evaluation of palpitations", referring_provider="Dr. Nguyen",
             created="2026-10-01", today=T)
    a.update(kw)
    return store.create(**a)


def test_create_defaults_to_sent(store):
    q = store.get(make(store))
    assert q.status == "sent" and q.created == q.updated == "2026-10-01"


def test_no_response_after_followup_window(store):
    fresh = store.get(make(store, created="2026-10-20"))        # 4 days old
    stale = store.get(make(store, created="2026-09-01"))         # 53 days old
    assert (fresh.state(T), stale.state(T)) == ("on track", "no response")
    s = store.summary(T)
    assert (s["open"], s["no_response"]) == (2, 1)


def test_scheduled_is_not_no_response_even_if_old(store):
    rid = make(store, created="2026-09-01")
    store.set_status(rid, "scheduled", today=T)
    assert store.get(rid).state(T) == "scheduled"


def test_cannot_complete_before_scheduled(store):
    rid = make(store)
    with pytest.raises(ReferralError, match="scheduled"):
        store.set_status(rid, "completed", today=T)
    store.set_status(rid, "scheduled", today=T)
    store.set_status(rid, "completed", today=T)
    assert store.get(rid).status == "completed"


def test_closed_referrals_stay_closed_and_do_not_count_as_no_response(store):
    rid = make(store, created="2026-09-01")
    store.set_status(rid, "declined", "specialist not accepting new patients", today=T)
    with pytest.raises(ReferralError, match="cannot be changed"):
        store.set_status(rid, "scheduled", today=T)
    assert store.get(rid).state(T) == "closed" and store.summary(T)["no_response"] == 0


def test_decline_needs_a_reason(store):
    rid = make(store)
    with pytest.raises(ReferralError, match="Note is required"):
        store.set_status(rid, "declined", "", today=T)


def test_history_is_kept_in_order(store):
    rid = make(store)
    store.set_status(rid, "scheduled", "appt booked 11/4", today=T)
    ev = store.events(rid)
    assert [e.what for e in ev] == ["Sent", "Scheduled"] and ev[1].note == "appt booked 11/4"


def test_bad_input_is_refused(store):
    with pytest.raises(ReferralError):
        make(store, created="2026-12-01")                       # future
    with pytest.raises(ReferralError):
        make(store, created="not a date")
    with pytest.raises(ReferralError):
        make(store, priority="asap")
    with pytest.raises(ReferralError):
        make(store, reason="")
    with pytest.raises(ReferralError):
        make(store, reason="x" * 301)
    with pytest.raises(ReferralError):
        make(store, referring_provider="")                      # now required
    with pytest.raises(ReferralError):
        make(store, coordinator="Dr. Nobody")                   # not in COORDINATORS
    with pytest.raises(ReferralError):
        make(store, icd10_codes="not-a-code")


def test_coordinator_blank_defaults_to_unassigned(store):
    q = store.get(make(store, coordinator=""))
    assert q.coordinator == "Unassigned"


def test_icd10_codes_are_normalized_and_deduplicated(store):
    q = store.get(make(store, icd10_codes="r00.2, R00.2 r07.9"))
    assert q.icd10_codes == "R00.2, R07.9"


def test_scheduling_with_an_appointment_date_shows_in_history(store):
    rid = make(store)
    store.set_status(rid, "scheduled", "booked", appointment_date="2026-11-04", today=T)
    q = store.get(rid)
    assert q.appointment_date == "2026-11-04"
    ev = store.events(rid)
    assert ev[1].what == "Scheduled for 2026-11-04"


def test_appointment_date_ignored_for_a_status_that_is_not_scheduled(store):
    rid = make(store)
    store.set_status(rid, "declined", "nope", appointment_date="2026-11-04", today=T)
    assert store.get(rid).appointment_date == ""


def test_list_puts_no_response_first_then_scheduled_then_on_track_then_closed(store):
    stale = make(store, created="2026-09-01")                    # no response
    sched = make(store, created="2026-10-15")
    store.set_status(sched, "scheduled", today=T)
    fresh = make(store, created="2026-10-22")                    # on track
    done = make(store, created="2026-09-15")
    store.set_status(done, "scheduled", today=T)
    store.set_status(done, "completed", today=T)
    assert [q.id for q in store.list(today=T)] == [stale, sched, fresh, done]
    assert [q.id for q in store.list("completed")] == [done]


def test_audit_log_never_holds_patient_or_text(store, tmp_path):
    rid = make(store, patient_id="secret-id-123", reason="very private words")
    store.set_status(rid, "scheduled", "a private note", today=T)
    log = (tmp_path / "audit.jsonl").read_text()
    assert "referral_create" in log and "secret-id-123" not in log and "private" not in log
