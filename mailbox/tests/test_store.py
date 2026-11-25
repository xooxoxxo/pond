"""Tests for the store layer."""
import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from mailbox.store import Event, Item, Notification, Store


@pytest.fixture
def tmp_store(tmp_path):
    """A temporary store for testing."""
    store = Store(tmp_path / "test.db")
    store.open()
    yield store
    store.close()


def test_store_creates_database(tmp_path):
    """Store.open creates the database and schema."""
    db_path = tmp_path / "mailbox.db"
    assert not db_path.exists()
    store = Store(db_path)
    store.open()
    assert db_path.exists()
    store.close()


def test_insert_and_get_item(tmp_store):
    """Insert an item and retrieve it."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="test ask",
        project_id="Orbit",
        lane="personal",
        payload=json.dumps({"key": "abc123", "why": "testing"}),
    )
    inserted = tmp_store.insert_item(item)
    assert inserted.id == item.id
    assert inserted.status == "open"

    fetched = tmp_store.get_item(item.id)
    assert fetched is not None
    assert fetched.title == "test ask"


def test_list_items_by_status(tmp_store):
    """List items filtered by status."""
    item1 = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="open ask",
        lane="personal",
        payload=json.dumps({"key": "k1"}),
    )
    item2 = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="done ask",
        status="done",
        lane="personal",
        payload=json.dumps({"key": "k2"}),
    )
    tmp_store.insert_item(item1)
    tmp_store.insert_item(item2)

    open_items = tmp_store.list_items(status="open")
    assert len(open_items) == 1
    assert open_items[0].title == "open ask"


def test_update_item(tmp_store):
    """Update specific fields."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)
    updated = tmp_store.update_item(item.id, {"status": "done"})
    assert updated.status == "done"

    fetched = tmp_store.get_item(item.id)
    assert fetched.status == "done"


def test_append_and_read_events(tmp_store):
    """Append events and read them back."""
    item_id = str(uuid4())
    item = Item(
        id=item_id,
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)

    event1 = tmp_store.append_event(item_id, "posted", "session", None)
    assert event1.seq > 0

    event2 = tmp_store.append_event(item_id, "acked", "session", None)
    assert event2.seq > event1.seq

    events, cursor = tmp_store.read_events(after_seq=0)
    assert len(events) >= 2
    assert events[0].kind == "posted"


def test_dedup_lookup(tmp_store):
    """Lookup an open item by payload.key within 24 hours."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "abc123", "why": "test"}),
    )
    tmp_store.insert_item(item)

    found = tmp_store.lookup_dedup_key("abc123")
    assert found is not None
    assert found.id == item.id

    not_found = tmp_store.lookup_dedup_key("other_key")
    assert not_found is None


def test_to_wire_format(tmp_store):
    """Item.to_wire() produces camelCase envelope."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        anchor_date="2026-09-23",
        anchor_time="14:30",
        project_id="Orbit",
        lane="personal",
        payload=json.dumps({"key": "k", "cmd": "git status"}),
        links=json.dumps(["https://example.com"]),
    )
    wire = item.to_wire()

    assert "payloadType" in wire
    assert "projectId" in wire
    assert "completedAt" in wire
    assert wire["status"] == "open"
    assert wire["anchor"]["date"] == "2026-09-23"
    assert wire["anchor"]["time"] == "14:30"


def test_health_check(tmp_store):
    """Health check detects readable/writable DB."""
    assert tmp_store.health_check() is True


# --- Fix round 1: thread safety (finding 1) ---

def test_get_item_from_other_thread(tmp_store):
    """A Store opened on one thread must be usable from another (Task 4 shares one Store
    between the Telegram poll thread and ThreadingHTTPServer request threads)."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)

    result: dict = {}

    def worker():
        try:
            result["item"] = tmp_store.get_item(item.id)
        except Exception as exc:  # noqa: BLE001 - capture for assertion in main thread
            result["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert "error" not in result, f"get_item raised on another thread: {result.get('error')!r}"
    assert result["item"] is not None
    assert result["item"].id == item.id


def test_concurrent_append_event_from_many_threads(tmp_store):
    """8 threads each appending 25 events on one Store must yield exactly 200 events
    with strictly increasing seq — no crash, no lost writes, no duplicate seq."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)

    errors: list = []

    def worker():
        try:
            for _ in range(25):
                tmp_store.append_event(item.id, "posted", "session", None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"append_event raised in a worker thread: {errors!r}"

    events, _ = tmp_store.read_events(after_seq=0, limit=1000)
    assert len(events) == 200
    seqs = [e.seq for e in events]
    assert all(seqs[i] < seqs[i + 1] for i in range(len(seqs) - 1)), "seq must be strictly increasing"


# --- Fix round 1: update_item field allowlist (finding 2) ---

@pytest.mark.parametrize("bad_field", ["id", "created", "source", "updated", "nonexistent_field"])
def test_update_item_rejects_non_updatable_fields(tmp_store, bad_field):
    """update_item must reject any field outside the allowlist before touching the DB."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)
    before = tmp_store.get_item(item.id)

    with pytest.raises(ValueError):
        tmp_store.update_item(item.id, {bad_field: "HACKED"})

    after = tmp_store.get_item(item.id)
    assert after == before


# --- Fix round 1: update_item completed_at derivation (finding 3) ---

def test_update_item_status_done_derives_completed_at(tmp_store):
    """Setting status to done derives completed_at (now, UTC) when not supplied."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)
    assert tmp_store.get_item(item.id).completed_at is None

    updated = tmp_store.update_item(item.id, {"status": "done"})
    assert updated.status == "done"
    assert updated.completed_at is not None


def test_update_item_status_dismissed_derives_completed_at(tmp_store):
    """Setting status to dismissed also derives completed_at when not supplied."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)

    updated = tmp_store.update_item(item.id, {"status": "dismissed"})
    assert updated.status == "dismissed"
    assert updated.completed_at is not None


def test_update_item_explicit_completed_at_is_kept(tmp_store):
    """An explicit completed_at in fields is kept as given, not overridden."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)

    explicit = "2026-01-01T00:00:00+00:00"
    updated = tmp_store.update_item(item.id, {"status": "done", "completed_at": explicit})
    assert updated.completed_at == explicit


def test_update_item_status_open_clears_completed_at_and_snooze(tmp_store):
    """Reopening (status -> open) clears completed_at and snooze_until."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="ask",
        lane="personal",
        payload=json.dumps({"key": "k"}),
    )
    tmp_store.insert_item(item)
    tmp_store.update_item(item.id, {"status": "done"})
    tmp_store.update_item(item.id, {"snooze_until": "2026-01-01T00:00:00+00:00"})

    reopened = tmp_store.update_item(item.id, {"status": "open"})
    assert reopened.completed_at is None
    assert reopened.snooze_until is None


# --- Fix round 1 (finding 1): per-item event history without a store-wide cap ---


def test_events_for_item_ignores_the_store_wide_cap(tmp_store):
    """S25: events_for_item must return ALL of one item's events, in seq
    order, even when the store holds more events (belonging to other items)
    than read_events' default page size."""
    filler_id = str(uuid4())
    for _ in range(1005):
        tmp_store.append_event(filler_id, "reply", "owner", "noise")

    target_id = str(uuid4())
    e1 = tmp_store.append_event(target_id, "posted", "session", None)
    e2 = tmp_store.append_event(target_id, "acked", "session", None)

    events = tmp_store.events_for_item(target_id)
    assert [e.seq for e in events] == [e1.seq, e2.seq]
    assert [e.kind for e in events] == ["posted", "acked"]


# --- Task 4 (ruling S7): snapshots go through the store, under its lock ---


def test_snapshot_copies_the_database(tmp_store, tmp_path):
    """snapshot() writes a file that opens and holds the same items."""
    for n in range(3):
        tmp_store.insert_item(Item(
            id=str(uuid4()),
            source="xo-mailbox",
            title=f"ask {n}",
            lane="personal",
            payload=json.dumps({"key": f"k{n}"}),
        ))

    dest = tmp_path / "snap.db"
    tmp_store.snapshot(dest)
    assert dest.exists()

    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
        titles = [r[0] for r in conn.execute("SELECT title FROM items ORDER BY title")]
        assert titles == ["ask 0", "ask 1", "ask 2"]
    finally:
        conn.close()

    # The original is still usable afterwards.
    assert len(tmp_store.list_items(status="open")) == 3


def test_snapshot_includes_writes_made_after_open(tmp_store, tmp_path):
    """A snapshot taken while the WAL holds recent writes still sees them."""
    item = Item(
        id=str(uuid4()),
        source="xo-mailbox",
        title="late ask",
        lane="personal",
        payload=json.dumps({"key": "late"}),
    )
    tmp_store.insert_item(item)
    tmp_store.append_event(item.id, "posted", "session", None)

    dest = tmp_path / "snap2.db"
    tmp_store.snapshot(dest)

    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM items").fetchone()[0] == "late ask"
    finally:
        conn.close()


def test_reopen_item_only_changes_a_closed_item(tmp_store):
    """reopen_item: None for an unknown id, False for an open item, True
    exactly once for a closed one, which comes back open with no completed_at."""
    item = Item(id=str(uuid4()), source="xo-mailbox", title="t", payload="{}")
    tmp_store.insert_item(item)
    assert tmp_store.reopen_item("nope") is None
    assert tmp_store.reopen_item(item.id) is False

    tmp_store.update_item(item.id, {"status": "dismissed"})
    assert tmp_store.get_item(item.id).completed_at

    results = []
    threads = [threading.Thread(target=lambda: results.append(tmp_store.reopen_item(item.id)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 7 + [True]
    got = tmp_store.get_item(item.id)
    assert (got.status, got.completed_at) == ("open", None)


# --- notifications (Task 6) -------------------------------------------------


def _notification(**over):
    base = dict(id="n1", kind="reminder", ref="f1", title="call the notary", body="today 14:00",
                actions='[{"id": "done", "label": "Done"}]', tg_chat_id=12345, tg_message_id=9,
                action_taken=None, acted_at=None, created="2026-10-17T11:50:00+00:00")
    return Notification(**{**base, **over})


def test_notifications_table_is_added_to_an_existing_database(tmp_path):
    """A database created before Phase 1 gains the table on the next open."""
    path = tmp_path / "old.db"
    store = Store(path)
    store.open()
    store.conn.execute("DROP TABLE notifications")
    store.conn.commit()
    store.close()
    again = Store(path)
    again.open()
    try:
        again.insert_notification(_notification())
        assert again.get_notification("n1").ref == "f1"
    finally:
        again.close()


def test_notification_round_trip_and_ref_lookup(tmp_store):
    tmp_store.insert_notification(_notification())
    assert tmp_store.get_notification("n1") == _notification()
    assert tmp_store.get_notification_by_ref("f1") == _notification()
    assert tmp_store.get_notification("nope") is None
    assert tmp_store.get_notification_by_ref("nope") is None
    with pytest.raises(sqlite3.IntegrityError):
        tmp_store.insert_notification(_notification(id="n2"))      # ref is unique


def test_notification_action_is_set_once(tmp_store):
    tmp_store.insert_notification(_notification())
    first = tmp_store.set_notification_action("n1", "done")
    assert first.action_taken == "done" and first.acted_at
    second = tmp_store.set_notification_action("n1", "tomorrow")
    assert (second.action_taken, second.acted_at) == ("done", first.acted_at)
    assert tmp_store.set_notification_action("nope", "done") is None


def test_notifications_survive_reopen_and_never_show_as_items(tmp_path):
    """The dedupe row is on disk: a mailbox restart still finds the ref. The
    row is not an ask: /asks and the dedup lookup never see it."""
    path = tmp_path / "n.db"
    store = Store(path)
    store.open()
    store.insert_notification(_notification())
    store.close()
    again = Store(path)
    again.open()
    try:
        assert again.get_notification_by_ref("f1").id == "n1"
        assert again.list_items() == []
        assert again.get_by_tg_message_id(9) is None
    finally:
        again.close()


def _open_item(store, status="open"):
    item = Item(id=str(uuid4()), source="xo-mailbox", title="t", payload="{}", status=status)
    store.insert_item(item)
    return item


def test_append_events_writes_a_batch_in_one_transaction(tmp_store):
    item = _open_item(tmp_store)
    events = tmp_store.append_events(item.id, "answer", "owner", ["a", "b"], only_if_open=True)
    assert [(e.kind, e.actor, e.text) for e in events] == [("answer", "owner", "a"), ("answer", "owner", "b")]
    assert events[1].seq == events[0].seq + 1
    with pytest.raises(sqlite3.Error):
        tmp_store.append_events(item.id, "answer", "owner", ["c", object()])
    assert [e.text for e in tmp_store.events_for_item(item.id)] == ["a", "b"]


def test_append_events_only_if_open_writes_nothing_on_a_closed_item(tmp_store):
    item = _open_item(tmp_store, status="done")
    assert tmp_store.append_events(item.id, "answer", "owner", ["a"], only_if_open=True) is None
    assert tmp_store.append_events("nope", "answer", "owner", ["a"], only_if_open=True) is None
    assert tmp_store.events_for_item(item.id) == []


# --- revisions, accepts, Telegram messages, owner keys, settings (Pond 1.1.0) ---

from mailbox.revisions import canonical, revision_hash  # noqa: E402
from mailbox.store import AskNotOpen, OwnerKey, Revision, StaleRevision  # noqa: E402

AT1 = "2026-10-29T10:00:00+00:00"
AT2 = "2026-10-29T10:05:00+00:00"


def _rev(item_id, rev, prev, at, reason, body_obj):
    body = canonical(body_obj)
    return Revision(item_id=item_id, rev=rev, hash=revision_hash(rev, prev, at, reason, body),
                    prev=prev, at=at, reason=reason, body=body)


def _ask(store, *, message_id=None, edit_key_hash="ab" * 32):
    payload = {"key": str(uuid4())[:16], "tg_chat_id": 1, "tg_message_id": message_id}
    item = Item(id=str(uuid4()), source="xo-mailbox", title="t", payload=json.dumps(payload),
                created=AT1, updated=AT1)
    rev1 = _rev(item.id, 1, "", AT1, "", {"v": 1, "title": "t", "steps": [{"do": "x", "id": "s1"}]})
    store.insert_ask(item, rev1, edit_key_hash)
    return item, rev1


def _amend(store, item, base, reason="fix", at=AT2):
    new = _rev(item.id, base.rev + 1, base.hash, at, reason,
               {"v": 1, "title": "t", "steps": [{"do": f"y{base.rev}", "id": "s1"}]})
    return store.amend_tx(item.id, base_rev=base.rev, base_hash=base.hash, new=new, event_text=reason)


def test_insert_ask_writes_item_revision_key_event_and_message(tmp_store):
    item, rev1 = _ask(tmp_store, message_id=77)
    assert tmp_store.get_item(item.id).title == "t"
    assert tmp_store.revisions(item.id) == [rev1]
    assert tmp_store.current_revision(item.id) == rev1
    assert tmp_store.edit_key_hash(item.id) == "ab" * 32
    assert [(e.kind, e.actor) for e in tmp_store.events_for_item(item.id)] == [("posted", "session")]
    assert tmp_store.tg_message(77) == (item.id, 1)
    assert tmp_store.get_by_tg_message_id(77).id == item.id


def test_insert_ask_without_an_edit_key(tmp_store):
    item, _ = _ask(tmp_store, edit_key_hash=None)
    assert tmp_store.edit_key_hash(item.id) is None


def test_amend_tx_appends_a_revision_and_moves_updated(tmp_store):
    item, rev1 = _ask(tmp_store)
    rev2 = _amend(tmp_store, item, rev1)
    assert rev2.rev == 2 and rev2.prev == rev1.hash
    assert tmp_store.current_revision(item.id) == rev2
    assert tmp_store.get_item(item.id).updated == AT2
    revised = tmp_store.events_for_item(item.id)[-1]
    assert (revised.kind, revised.actor, revised.text, revised.rev) == ("revised", "session", "fix", 2)


def test_amend_tx_with_a_wrong_base_hash_is_stale(tmp_store):
    item, rev1 = _ask(tmp_store)
    wrong = Revision(**{**rev1.__dict__, "hash": "0" * 64})
    with pytest.raises(StaleRevision) as caught:
        _amend(tmp_store, item, wrong)
    assert caught.value.current.rev == 1
    assert len(tmp_store.revisions(item.id)) == 1


def test_two_concurrent_amends_with_the_same_base_one_wins(tmp_store):
    item, rev1 = _ask(tmp_store)
    outcomes = []
    barrier = threading.Barrier(2)

    def worker(n):
        barrier.wait()
        try:
            _amend(tmp_store, item, rev1, reason=f"fix {n}")
            outcomes.append("ok")
        except StaleRevision:
            outcomes.append("stale")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "stale"]
    assert [r.rev for r in tmp_store.revisions(item.id)] == [1, 2]


def test_amend_tx_on_a_closed_ask(tmp_store):
    item, rev1 = _ask(tmp_store)
    tmp_store.update_item(item.id, {"status": "done"})
    with pytest.raises(AskNotOpen):
        _amend(tmp_store, item, rev1)


class _FailOnEventInsert:
    """Wraps the store's connection and fails the `revised` event insert."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.lstrip().startswith("INSERT INTO events"):
            raise sqlite3.OperationalError("injected")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_amend_tx_rolls_back_everything_on_a_failure(tmp_store):
    item, rev1 = _ask(tmp_store)
    before = tmp_store.get_item(item.id).updated
    real = tmp_store.conn
    tmp_store.conn = _FailOnEventInsert(real)
    try:
        with pytest.raises(sqlite3.OperationalError):
            _amend(tmp_store, item, rev1)
    finally:
        tmp_store.conn = real
    assert [r.rev for r in tmp_store.revisions(item.id)] == [1]
    assert tmp_store.get_item(item.id).updated == before


def test_append_event_with_rev_and_auth_reads_back(tmp_store):
    item, _ = _ask(tmp_store)
    tmp_store.append_event(item.id, "reply", "owner", "ok", rev=2, auth="owner-key")
    tmp_store.append_events(item.id, "answer", "owner", ["a"], rev=1, auth="telegram")
    events = tmp_store.events_for_item(item.id)
    assert [(e.kind, e.rev, e.auth) for e in events] == [
        ("posted", None, None), ("reply", 2, "owner-key"), ("answer", 1, "telegram")]
    read, _ = tmp_store.read_events(after_seq=0)
    assert [(e.rev, e.auth) for e in read] == [(None, None), (2, "owner-key"), (1, "telegram")]


def test_tg_messages_find_the_ask_and_the_newest_message(tmp_store):
    item, rev1 = _ask(tmp_store, message_id=10)
    tmp_store.record_tg_message(11, item.id, 2)
    assert tmp_store.get_by_tg_message_id(11).id == item.id
    assert tmp_store.newest_tg_message(item.id) == (11, 2)
    assert tmp_store.tg_message(10) == (item.id, 1)
    assert tmp_store.tg_message(99) is None
    assert tmp_store.newest_tg_message("nope") is None


def test_accept_the_same_rev_twice_is_not_an_error(tmp_store):
    item, rev1 = _ask(tmp_store)
    rev2 = _amend(tmp_store, item, rev1)
    assert tmp_store.accepted_rev(item.id) == 1
    first = tmp_store.accept_tx(item.id, 2, rev2.hash, "owner-key")
    second = tmp_store.accept_tx(item.id, 2, rev2.hash, "owner-key")
    assert (first.kind, first.actor, first.rev, first.auth) == ("accepted", "owner", 2, "owner-key")
    assert second.seq > first.seq
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM accepts").fetchone()[0] == 1
    assert tmp_store.accepted_rev(item.id) == 2


def test_owner_keys_and_settings(tmp_store):
    key = OwnerKey(id="k1", key_hash="cd" * 32, label="Studio", status="pending", code="012345",
                   created=AT1, expires=AT2, approved_at=None, revoked_at=None, tg_message_id=None)
    tmp_store.insert_enrollment(key)
    assert tmp_store.pending_enrollments() == [key]
    assert tmp_store.owner_key_by_hash("cd" * 32) == key
    approved = tmp_store.set_owner_key_status("k1", "approved", tg_message_id=5)
    assert approved.status == "approved" and approved.approved_at and approved.tg_message_id == 5
    assert tmp_store.pending_enrollments() == []
    revoked = tmp_store.set_owner_key_status("k1", "revoked")
    assert revoked.revoked_at and revoked.tg_message_id == 5
    assert tmp_store.owner_keys() == [revoked]
    assert tmp_store.set_owner_key_status("nope", "approved") is None

    assert tmp_store.get_setting("x") is None
    tmp_store.set_setting("x", "1")
    tmp_store.set_setting("x", "0")
    assert tmp_store.get_setting("x") == "0"
    tmp_store.delete_setting("x")
    assert tmp_store.get_setting("x") is None


# --- bugs (Pond 1.2.0) ---------------------------------------------------------

SIG = "a3f9c2e17b04"


def _bug(title="Hook crash: EXC_BAD_ACCESS (a3f9c2e17b04)", created="2026-11-03T09:00:00+00:00"):
    return Item(id=str(uuid4()), source="xo-mailbox", title=title, project_id="Hook", lane="personal",
                payload=json.dumps({"key": f"bug-{SIG}", "tg_message_id": None}), created=created, updated=created)


def _plain(title="plain ask"):
    return Item(id=str(uuid4()), source="xo-mailbox", title=title, project_id="Orbit", lane="personal",
                payload=json.dumps({"key": "k-" + title}))


def test_insert_bug_writes_the_item_its_bug_row_and_a_posted_event(tmp_store):
    bug = tmp_store.insert_bug(_bug(), SIG, "trace line")
    assert tmp_store.get_item(bug.id).title.startswith("Hook crash")
    assert tmp_store.bug_meta(bug.id) == {"signature": SIG, "count": 1, "firstSeen": bug.created,
                                          "lastSeen": bug.created, "excerpt": "trace line"}
    assert [(e.kind, e.actor) for e in tmp_store.events_for_item(bug.id)] == [("posted", "hook")]
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM tg_messages").fetchone()[0] == 0


def test_insert_bug_is_one_transaction(tmp_store):
    bug = _bug()
    tmp_store.insert_bug(bug, SIG, "x")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_store.insert_bug(bug, "b" * 12, "x")        # the same item id again
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM bugs").fetchone()[0] == 1
    assert len(tmp_store.events_for_item(bug.id)) == 1


def test_bug_meta_is_none_for_a_plain_ask(tmp_store):
    ask = tmp_store.insert_item(_plain())
    assert tmp_store.bug_meta(ask.id) is None
    assert tmp_store.bug_meta("missing") is None


def test_open_bug_finds_the_open_one_and_ignores_closed_bugs(tmp_store):
    assert tmp_store.open_bug(SIG) is None
    first = tmp_store.insert_bug(_bug(), SIG, "x")
    assert tmp_store.open_bug(SIG).id == first.id
    assert tmp_store.open_bug("b" * 12) is None
    tmp_store.update_item(first.id, {"status": "done"})
    assert tmp_store.open_bug(SIG) is None
    second = tmp_store.insert_bug(_bug(), SIG, "y")
    assert tmp_store.open_bug(SIG).id == second.id


def test_add_occurrence_advances_the_count_last_seen_and_excerpt(tmp_store):
    bug = tmp_store.insert_bug(_bug(), SIG, "first")
    assert tmp_store.add_occurrence(bug.id, "2026-11-03T10:00:00+00:00", "second") == 2
    assert tmp_store.add_occurrence(bug.id, "2026-11-03T11:00:00+00:00", "third") == 3
    meta = tmp_store.bug_meta(bug.id)
    assert (meta["count"], meta["firstSeen"], meta["lastSeen"], meta["excerpt"]) == (
        3, "2026-11-03T09:00:00+00:00", "2026-11-03T11:00:00+00:00", "third")
    occurrences = [e for e in tmp_store.events_for_item(bug.id) if e.kind == "occurrence"]
    assert [(e.actor, json.loads(e.text)) for e in occurrences] == [("hook", {"count": 2}), ("hook", {"count": 3})]
    assert tmp_store.get_item(bug.id).updated == "2026-11-03T11:00:00+00:00"


def test_list_items_filters_by_kind(tmp_store):
    ask = tmp_store.insert_item(_plain())
    bug = tmp_store.insert_bug(_bug(created=ask.created), SIG, "x")
    assert {i.id for i in tmp_store.list_items(status="open")} == {ask.id, bug.id}
    assert [i.id for i in tmp_store.list_items(status="open", kind="bug")] == [bug.id]
    assert [i.id for i in tmp_store.list_items(status="open", kind="ask")] == [ask.id]
    assert [i.id for i in tmp_store.list_items(kind="bug")] == [bug.id]
    tmp_store.update_item(bug.id, {"status": "done"})
    assert tmp_store.list_items(status="open", kind="bug") == []
    with pytest.raises(ValueError):
        tmp_store.list_items(kind="other")
