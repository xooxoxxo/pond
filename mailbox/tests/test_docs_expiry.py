"""Documents of a closed ask expire DOCS_KEEP_DAYS after it closed (final wave, W1):
Service.sweep_docs, its one atomic step per ask, and where the server runs it.

The store stamps `completed_at` with its own clock; the `synced` fixture makes that
clock read the service clock, so a test moves both. Every text here is synthetic."""
import logging
import threading
from datetime import datetime, timedelta
from unittest import mock

import pytest

from mailbox.docs import DOCS_KEEP_DAYS
from mailbox.tests.test_docs_post import (  # noqa: F401 — `pond` is a fixture
    NOTES_TEXT, PLAN_TEXT, _counts, _posted, ask, pond, read, sha,
)
from mailbox.tests.test_docs_read import GONE, _events, _items, close, get_doc, read_json, release
from mailbox.tests.test_server import hcall

WEEK = timedelta(days=DOCS_KEEP_DAYS)
JUST_SHORT = WEEK - timedelta(hours=1)          # 6 days 23 hours


@pytest.fixture
def synced(pond):
    """The pond, with the store's clock (completed_at, updated) following the service clock."""
    srv, base_url, token, clock = pond

    class Synced(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            now = clock()
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    with mock.patch("mailbox.store.datetime", Synced):
        yield srv, base_url, token, clock


def _reads(base_url, token, item_id, text=PLAN_TEXT):
    status, _, body = get_doc(base_url, item_id, sha(text), token)
    return status if status != 200 or body == text.encode("utf-8") else "wrong bytes"


def reopen(base_url, token, item_id):
    status, data = hcall(base_url, f"/asks/{item_id}/reopen", token, method="POST", body={"actor": "session"})
    assert status == 200 and data["item"]["status"] == "open", data


def test_the_keep_time_is_seven_days():
    assert DOCS_KEEP_DAYS == 7


def test_closed_6_days_23_hours_is_kept(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += JUST_SHORT
    assert srv.service.sweep_docs() == 0
    assert _reads(base_url, token, item_id) == 200


def test_closed_7_days_is_released(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += WEEK
    assert srv.service.sweep_docs() == 1
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE
    assert srv.service.store.get_doc(sha(PLAN_TEXT)) is None


def test_a_dismissed_ask_expires_like_a_done_one(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id, verdict="declined")
    assert srv.service.store.get_item(item_id).status == "dismissed"
    clock.now += WEEK
    assert srv.service.sweep_docs() == 1
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


def test_an_open_ask_never_expires(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    clock.now += timedelta(days=400)
    assert srv.service.sweep_docs() == 0
    assert _reads(base_url, token, item_id) == 200


def test_a_reopen_stops_the_clock_and_a_new_close_starts_it_anew(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += timedelta(days=6)
    reopen(base_url, token, item_id)
    clock.now += timedelta(days=3)                       # 9 days after the first close, open
    assert srv.service.sweep_docs() == 0
    assert _reads(base_url, token, item_id) == 200
    close(base_url, token, item_id)
    clock.now += timedelta(days=6)                       # 15 days after the first close
    assert srv.service.sweep_docs() == 0
    assert _reads(base_url, token, item_id) == 200
    clock.now += timedelta(days=1)                       # 7 days after the second close
    assert srv.service.sweep_docs() == 1
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


def test_text_another_ask_still_keeps_stays(synced):
    srv, base_url, token, clock = synced
    first, _ = _posted(base_url, token, ask(read(), read(NOTES_TEXT, path="/var/tmp/notes.md", do="Read the notes")),
                       {sha(PLAN_TEXT): PLAN_TEXT, sha(NOTES_TEXT): NOTES_TEXT})
    second, _ = _posted(base_url, token, ask(read(), title="Another ask"), {sha(PLAN_TEXT): PLAN_TEXT})
    close(base_url, token, first)
    clock.now += timedelta(days=3)
    close(base_url, token, second)
    clock.now += timedelta(days=4)                       # first: 7 days closed; second: 4
    assert srv.service.sweep_docs() == 2
    assert read_json(base_url, first, sha(PLAN_TEXT), token) == GONE
    assert read_json(base_url, first, sha(NOTES_TEXT), token) == GONE
    assert _reads(base_url, token, second) == 200
    assert srv.service.store.get_doc(sha(NOTES_TEXT)) is None
    clock.now += timedelta(days=3)
    assert srv.service.sweep_docs() == 1
    assert read_json(base_url, second, sha(PLAN_TEXT), token) == GONE
    assert _counts(srv)["docs"] == 0


def test_a_second_sweep_releases_nothing(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += WEEK
    assert srv.service.sweep_docs() == 1
    assert srv.service.sweep_docs() == 0


def test_the_sweep_writes_no_event_and_changes_no_item(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    events, items = _events(srv), _items(srv)
    clock.now += WEEK
    assert srv.service.sweep_docs() == 1
    assert _events(srv) == events and _items(srv) == items


def test_an_explicit_release_before_the_week_leaves_the_sweep_nothing(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += timedelta(days=1)
    assert release(base_url, item_id, token) == (200, {"released": 1})
    clock.now += WEEK
    assert srv.service.sweep_docs() == 0
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


def test_the_sweep_runs_with_the_switch_off(synced):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    srv.service.store.delete_setting("docs_enabled")
    close(base_url, token, item_id)
    clock.now += WEEK
    assert srv.service.sweep_docs() == 1
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


def test_the_sweep_logs_counts_only(synced, caplog):
    srv, base_url, token, clock = synced
    marker = "Synthetic marker for the sweep log"
    text = f"# Plan\n\n{marker}\n"
    item_id, _ = _posted(base_url, token, ask(read(text), read(NOTES_TEXT, path="/var/tmp/n.md", do="Notes")),
                         {sha(text): text, sha(NOTES_TEXT): NOTES_TEXT})
    close(base_url, token, item_id)
    clock.now += WEEK
    caplog.set_level(logging.INFO, logger="mailbox")
    assert srv.service.sweep_docs() == 2
    lines = [r.getMessage() for r in caplog.records]
    assert "[mailbox] document sweep: 1 asks, 2 released" in lines
    assert not [line for line in lines if marker in line or item_id in line or sha(text) in line]
    caplog.clear()
    assert srv.service.sweep_docs() == 0
    assert not [r for r in caplog.records if "document sweep" in r.getMessage()]   # quiet when nothing expired


def test_an_ask_with_no_close_time_is_left_alone(synced):
    """A closed ask always carries completed_at (update_item stamps it on every close);
    one without it, which only a hand edit can make, is never guessed at."""
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    store = srv.service.store
    store.conn.execute("UPDATE items SET completed_at = NULL WHERE id = ?", (item_id,))
    store.conn.commit()
    clock.now += timedelta(days=400)
    assert srv.service.sweep_docs() == 0
    assert _reads(base_url, token, item_id) == 200


# --- one atomic step per ask -----------------------------------------------------------------

def test_no_reopen_lands_between_the_sweeps_check_and_its_release(synced):
    """Another thread reopens the ask while the sweep is inside its write: the reopen
    waits until the sweep's transaction is done, never landing in between."""
    srv, base_url, token, clock = synced
    store = srv.service.store
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += WEEK
    real = store._release_docs
    seen = {}

    def racing(rid):
        rival = threading.Thread(target=store.reopen_item, args=(item_id,))
        rival.start()
        rival.join(timeout=0.5)
        seen["reopen landed first"] = not rival.is_alive()
        seen["thread"] = rival
        seen["status when released"] = store.conn.execute(
            "SELECT status FROM items WHERE id = ?", (rid,)).fetchone()[0]
        return real(rid)

    store._release_docs = racing
    assert srv.service.sweep_docs() == 1
    seen["thread"].join(timeout=10)
    assert (seen["status when released"], seen["reopen landed first"]) == ("done", False)
    assert store.get_item(item_id).status == "open"


def test_a_reopen_just_before_the_sweeps_step_keeps_the_documents(synced):
    """The ask was a candidate when the sweep listed them, then reopened before its
    one atomic step: the step sees an open ask and releases nothing."""
    srv, base_url, token, clock = synced
    store = srv.service.store
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += WEEK
    real = store.expire_ask_docs

    def racing(rid, cutoff):
        store.reopen_item(rid)
        return real(rid, cutoff)

    store.expire_ask_docs = racing
    assert srv.service.sweep_docs() == 0
    assert _reads(base_url, token, item_id) == 200


# --- where the server runs it ------------------------------------------------------------------

def test_the_nightly_loop_sweeps_after_the_backup_even_when_the_backup_fails(pond, monkeypatch):
    srv, base_url, token, clock = pond
    order = []

    def backup():
        order.append("backup")
        raise OSError("disk full")

    def sweep():
        order.append("sweep")
        if order.count("sweep") == 2:
            srv.running = False
        return 0

    monkeypatch.setattr(srv, "_next_backup_time", lambda now: now - timedelta(seconds=1))
    monkeypatch.setattr(srv, "_do_backup", backup)
    monkeypatch.setattr(srv.service, "sweep_docs", sweep)
    srv.running = True
    srv._backup_loop()
    assert order == ["backup", "sweep", "backup", "sweep"]


def test_a_failed_sweep_never_stops_the_nightly_loop(pond, monkeypatch, caplog):
    srv, base_url, token, clock = pond
    backups = []

    def backup():
        backups.append(1)
        if len(backups) == 2:
            srv.running = False

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_next_backup_time", lambda now: now - timedelta(seconds=1))
    monkeypatch.setattr(srv, "_do_backup", backup)
    monkeypatch.setattr(srv.service, "sweep_docs", boom)
    srv.running = True
    srv._backup_loop()
    assert len(backups) == 2
    assert any("document sweep failed: RuntimeError" in r.getMessage() for r in caplog.records)


def test_the_server_sweeps_once_at_start(synced, monkeypatch):
    srv, base_url, token, clock = synced
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    clock.now += WEEK
    monkeypatch.setattr(srv, "_poll_loop", lambda: None)
    monkeypatch.setattr(srv, "_backup_loop", lambda: None)
    monkeypatch.setattr(srv.http_server, "serve_forever", lambda *a, **k: None)
    srv.start()
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


def test_a_failed_sweep_at_start_never_stops_the_server(pond, monkeypatch):
    srv, base_url, token, clock = pond
    served = []

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(srv.service, "sweep_docs", boom)
    monkeypatch.setattr(srv, "_poll_loop", lambda: None)
    monkeypatch.setattr(srv, "_backup_loop", lambda: None)
    monkeypatch.setattr(srv.http_server, "serve_forever", lambda *a, **k: served.append(1))
    srv.start()
    assert served == [1]
