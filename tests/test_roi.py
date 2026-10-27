from datetime import date

import pytest

from him_workbench import audit
from him_workbench.roi import RoiError, RoiStore

T = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOG_PATH", tmp_path / "audit.jsonl")


@pytest.fixture
def store():
    return RoiStore(":memory:", days=30)


def make(store, **kw):
    a = dict(patient_id="p1", requester_type="attorney", summary="visit notes", received="2026-10-01", today=T)
    a.update(kw)
    return store.create(**a)


def test_due_date_is_received_plus_setting(store):
    q = store.get(make(store))
    assert q.due == "2026-10-31" and q.status == "received"
    short = RoiStore(":memory:", days=10)
    assert short.get(make(short)).due == "2026-10-11"


def test_deadline_states(store):
    on_track = store.get(make(store, received="2026-10-20"))        # due 11-19
    soon = store.get(make(store, received="2026-09-27"))            # due 10-27
    late = store.get(make(store, received="2026-09-01"))            # due 10-01
    assert (on_track.state(T), soon.state(T), late.state(T)) == ("on track", "due soon", "overdue")
    assert late.days_left(T) == -23
    s = store.summary(T)
    assert (s["open"], s["overdue"], s["due_soon"]) == (3, 1, 1)


def test_cannot_fulfil_before_verified(store):
    rid = make(store)
    with pytest.raises(RoiError, match="verified"):
        store.set_status(rid, "fulfilled", today=T)
    store.set_status(rid, "verified", today=T)
    with pytest.raises(RoiError):
        store.set_status(rid, "fulfilled", today=T)      # still has to be in progress
    store.set_status(rid, "in_progress", today=T)
    store.set_status(rid, "fulfilled", today=T)
    assert store.get(rid).status == "fulfilled"


def test_closed_requests_stay_closed_and_do_not_count_as_overdue(store):
    rid = make(store, received="2026-09-01")
    store.set_status(rid, "denied", "no signed authorization", today=T)
    with pytest.raises(RoiError, match="cannot be changed"):
        store.set_status(rid, "verified", today=T)
    assert store.get(rid).state(T) == "closed" and store.summary(T)["overdue"] == 0


def test_denial_needs_a_reason(store):
    rid = make(store)
    with pytest.raises(RoiError, match="Note is required"):
        store.set_status(rid, "denied", "", today=T)


def test_extension_once_only_and_before_due(store):
    rid = make(store, received="2026-10-10")                         # due 11-09
    store.extend(rid, "large record", today=T)
    q = store.get(rid)
    assert q.due == "2026-12-09" and q.extended
    with pytest.raises(RoiError, match="Only one extension"):
        store.extend(rid, "again", today=T)
    late = make(store, received="2026-09-01")                        # due 10-01, already past
    with pytest.raises(RoiError, match="passed"):
        store.extend(late, "too late", today=T)


def test_history_is_kept_in_order(store):
    rid = make(store)
    store.set_status(rid, "verified", "ID checked", today=T)
    ev = store.events(rid)
    assert [e.what for e in ev] == ["Received", "Verified"] and ev[1].note == "ID checked"


def test_bad_input_is_refused(store):
    with pytest.raises(RoiError):
        make(store, received="2026-12-01")                          # future
    with pytest.raises(RoiError):
        make(store, received="not a date")
    with pytest.raises(RoiError):
        make(store, requester_type="a friend")
    with pytest.raises(RoiError):
        make(store, summary="")
    with pytest.raises(RoiError):
        make(store, summary="x" * 301)


def test_list_puts_open_first_soonest_due_first(store):
    a = make(store, received="2026-10-20")
    b = make(store, received="2026-10-02")
    c = make(store, received="2026-10-03")
    store.set_status(c, "denied", "why", today=T)
    assert [q.id for q in store.list()] == [b, a, c]
    assert [q.id for q in store.list("denied")] == [c]


def test_audit_log_never_holds_patient_or_text(store, tmp_path):
    rid = make(store, patient_id="secret-id-123", summary="very private words")
    store.set_status(rid, "verified", "a private note", today=T)
    log = (tmp_path / "audit.jsonl").read_text()
    assert "roi_create" in log and "secret-id-123" not in log and "private" not in log
