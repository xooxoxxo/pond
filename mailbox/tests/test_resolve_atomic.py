"""Deterministic commit/network interleavings for owner Done."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from mailbox.service import AmendError, AskClosed, Corrected, RequirementsUnmet, Service
from mailbox.store import Store
from mailbox.telegram import FakeTelegramClient


@pytest.fixture
def ask():
    store = Store(":memory:"); store.open()
    telegram = FakeTelegramClient()
    service = Service(store, telegram, "demo")
    store.set_setting("corrections_enabled", "1")
    import hashlib
    item, _ = service.post("Demo", "Check preview", instructable={"v": 1, "title": "Check preview", "urgency": "today",
        "steps": [{"id": "check", "do": "Check preview", "required": True}]},
        edit_key_hash=hashlib.sha256(b"synthetic-edit-key").hexdigest())
    rev = store.current_revision(item.id)
    service.progress(item.id, ["check"], [], rev=rev.rev, hash=rev.hash, auth="owner-key")
    yield service, store, telegram, item.id, rev
    store.close()


def hold(monkeypatch, target, method):
    entered, release = threading.Event(), threading.Event()
    original = getattr(target, method)
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test must release held network"
        return original(*args, **kwargs)
    monkeypatch.setattr(target, method, held)
    return entered, release


def correction(service, id, rev):
    body = json.loads(rev.body); body["steps"][0]["do"] = "Check corrected preview"
    service.utcnow = lambda: __import__("datetime").datetime.fromisoformat(rev.at) + timedelta(seconds=61)
    return lambda: service.amend(id, "synthetic-edit-key", rev.rev, rev.hash, "Use corrected preview", body)


def test_done_commits_before_held_telegram_and_refuses_later_untick_and_amend(ask, monkeypatch):
    service, store, telegram, id, rev = ask
    entered, release = hold(monkeypatch, telegram, "edit_message_text")
    with ThreadPoolExecutor() as threads:
        done = threads.submit(service.resolve, id, "done", actor="owner", rev=rev.rev, hash=rev.hash)
        try:
            assert entered.wait(2)
            # A network call must not keep the DB locked, and status/event land together.
            read = threads.submit(lambda: (store.get_item(id).status, store.events_for_item(id)[-1].kind))
            assert read.result(timeout=2) == ("done", "resolved")
            with pytest.raises(AskClosed):
                service.progress(id, [], ["check"], rev=rev.rev, hash=rev.hash, auth="owner-key")
            with pytest.raises(AmendError) as failure:
                correction(service, id, rev)()
            assert failure.value.body["error"] == "ask is closed"
        finally:
            release.set()
        assert done.result(timeout=2).status == "done"


def test_untick_committed_while_its_telegram_is_held_prevents_done(ask, monkeypatch):
    service, store, telegram, id, rev = ask
    entered, release = hold(monkeypatch, telegram, "edit_message_reply_markup")
    with ThreadPoolExecutor() as threads:
        untick = threads.submit(service.progress, id, [], ["check"], rev=rev.rev, hash=rev.hash, auth="owner-key")
        try:
            assert entered.wait(2)
            with pytest.raises(RequirementsUnmet):
                service.resolve(id, "done", actor="owner", rev=rev.rev, hash=rev.hash)
        finally:
            release.set()
        untick.result(timeout=2)
    assert store.get_item(id).status == "open"
    assert not any(e.kind == "resolved" for e in store.events_for_item(id))


def test_correction_committed_while_telegram_is_held_prevents_old_done(ask, monkeypatch):
    service, store, telegram, id, rev = ask
    entered, release = hold(monkeypatch, telegram, "edit_message_text")
    with ThreadPoolExecutor() as threads:
        amended = threads.submit(correction(service, id, rev))
        try:
            assert entered.wait(2)
            with pytest.raises(Corrected):
                service.resolve(id, "done", actor="owner", rev=rev.rev, hash=rev.hash)
        finally:
            release.set()
        amended.result(timeout=2)
    assert store.get_item(id).status == "open"
    assert not any(e.kind == "resolved" for e in store.events_for_item(id))


def test_resolve_status_rolls_back_if_event_append_fails(ask, monkeypatch):
    service, store, telegram, id, rev = ask
    before = copy.deepcopy(telegram.calls)
    original = store._insert_event
    def fail(*args, **kwargs):
        if args[1] == "resolved":
            raise RuntimeError("event write failed")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "_insert_event", fail)
    with pytest.raises(RuntimeError, match="event write failed"):
        service.resolve(id, "done", actor="owner", rev=rev.rev, hash=rev.hash)
    assert store.get_item(id).status == "open"
    assert not any(e.kind == "resolved" for e in store.events_for_item(id))
    assert telegram.calls == before


def test_reopen_commits_during_held_done_and_final_telegram_is_open(ask, monkeypatch):
    service, store, telegram, id, rev = ask
    entered, release = hold(monkeypatch, telegram, "edit_message_text")
    with ThreadPoolExecutor() as threads:
        done = threads.submit(service.resolve, id, "done", actor="owner", rev=rev.rev, hash=rev.hash)
        try:
            assert entered.wait(2)
            reopened = threads.submit(service.reopen, id)
            # Signal the committed reopen independently of its blocked presentation.
            # Read via another thread: no DB lock survives the Telegram call.
            from time import monotonic, sleep
            deadline = monotonic() + 2
            while store.get_item(id).status != "open" and monotonic() < deadline:
                sleep(0.001)
            assert store.get_item(id).status == "open"
        finally:
            release.set()
        done.result(timeout=2); reopened.result(timeout=2)
    assert store.events_for_item(id)[-1].kind == "reopened"
    assert telegram.edited_messages[-1]["reply_markup"]["inline_keyboard"]
