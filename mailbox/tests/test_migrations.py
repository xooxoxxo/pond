"""Tests for the additive migrations: Pond 1.1.0's side tables (schema 1), the
append-only revisions, the every-open backfill, Pond 1.2.0's bugs table
(schema 2), and that Pond 1.0.0 and 1.1.0 code can still read the database
after a rollback."""
import json
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import pytest

from mailbox import migrations
from mailbox.revisions import assign_ids, canonical, revision_hash
from mailbox.store import Item, Store

DOC = {"v": 1, "title": "Rotate the key", "urgency": "today",
       "steps": [{"do": "Open the console"}, {"do": "Rotate", "code": "rotate --now"}]}
CREATED = "2026-10-20T09:00:00+00:00"


def _old_database(path):
    """A Pond 1.0.0 database: the old schema, no side tables, user_version 0."""
    conn = sqlite3.connect(str(path))
    holder = SimpleNamespace(conn=conn)
    Store._schema(holder)
    Store._notifications_table(holder)
    conn.commit()
    return conn


def _old_insert(conn, *, status="open", instructable=None, message_id=None, title=None):
    """An item written the way Pond 1.0.0 code writes one."""
    item_id = str(uuid4())
    payload = {"key": item_id[:16], "why": None, "cmd": None, "link": None,
               "tg_chat_id": 12345, "tg_message_id": message_id, "note": None}
    if instructable is not None:
        payload["instructable"] = instructable
    conn.execute(
        """INSERT INTO items (id, source, title, status, lane, payload, links, created, updated)
           VALUES (?, 'xo-mailbox', ?, ?, 'personal', ?, '[]', ?, ?)""",
        (item_id, title or (instructable or {}).get("title") or "legacy ask", status,
         json.dumps(payload), CREATED, CREATED),
    )
    conn.execute("INSERT INTO events (item_id, kind, actor, text, at) VALUES (?, 'posted', 'session', NULL, ?)",
                 (item_id, CREATED))
    conn.commit()
    return item_id


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "mailbox.db"
    conn = _old_database(path)
    ids = {
        "legacy": _old_insert(conn, message_id=101),
        "open": _old_insert(conn, instructable=DOC, message_id=102),
        "closed": _old_insert(conn, status="done", instructable={**DOC, "title": "Closed one"}, message_id=103),
    }
    conn.close()
    return path, ids


def _open(path):
    store = Store(path)
    store.open()
    return store


def test_a_pond_1_0_0_database_migrates(old_db):
    path, ids = old_db
    store = _open(path)
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION == 6
        rows = store.conn.execute("SELECT item_id, rev, hash, prev, at, reason, body FROM revisions").fetchall()
        assert {r[0] for r in rows} == {ids["open"], ids["closed"]}
        for item_id, rev, digest, prev, at, reason, body in rows:
            assert (rev, prev, at, reason) == (1, "", CREATED, "")
            assert digest == revision_hash(1, "", CREATED, "", body)
            assert json.loads(body)["steps"][0]["id"] == "s1"
        open_body = next(r[6] for r in rows if r[0] == ids["open"])
        assert open_body == canonical(assign_ids(DOC)[0])
        messages = store.conn.execute("SELECT message_id, item_id, rev FROM tg_messages ORDER BY message_id").fetchall()
        assert messages == [(101, ids["legacy"], 1), (102, ids["open"], 1), (103, ids["closed"], 1)]
    finally:
        store.close()


def test_migrating_twice_changes_nothing(old_db):
    path, _ = old_db
    store = _open(path)
    before = (store.conn.execute("SELECT * FROM revisions ORDER BY item_id").fetchall(),
              store.conn.execute("SELECT * FROM tg_messages ORDER BY message_id").fetchall())
    migrations.migrate(store.conn)
    after = (store.conn.execute("SELECT * FROM revisions ORDER BY item_id").fetchall(),
             store.conn.execute("SELECT * FROM tg_messages ORDER BY message_id").fetchall())
    store.close()
    assert before == after
    store = _open(path)
    again = (store.conn.execute("SELECT * FROM revisions ORDER BY item_id").fetchall(),
             store.conn.execute("SELECT * FROM tg_messages ORDER BY message_id").fetchall())
    store.close()
    assert again == before


def test_an_item_inserted_by_old_code_after_migration_gets_rev_1_on_next_open(old_db):
    path, _ = old_db
    _open(path).close()
    conn = sqlite3.connect(str(path))
    late = _old_insert(conn, instructable={**DOC, "title": "Posted during a rollback"}, message_id=104)
    conn.close()
    store = _open(path)
    try:
        assert [r.rev for r in store.revisions(late)] == [1]
        assert store.tg_message(104) == (late, 1)
    finally:
        store.close()


def test_revisions_are_append_only(old_db):
    path, _ = old_db
    store = _open(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="revisions are append-only"):
            store.conn.execute("UPDATE revisions SET body = 'x'")
        store.conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="revisions are append-only"):
            store.conn.execute("DELETE FROM revisions")
        store.conn.rollback()
    finally:
        store.close()


@dataclass
class OldEvent:
    """Event as Pond 1.0.0 code declares it."""
    seq: int
    item_id: str
    kind: str
    actor: str
    text: Optional[str]
    at: str


def test_pond_1_0_0_code_still_reads_the_migrated_database(old_db):
    """Rollback check: 1.0.0 builds Event(**cols) and Item(**cols) from SELECT *."""
    path, _ = old_db
    _open(path).close()
    conn = sqlite3.connect(str(path))
    try:
        cursor = conn.execute("SELECT * FROM events")
        cols = [d[0] for d in cursor.description]
        events = [OldEvent(**dict(zip(cols, row))) for row in cursor.fetchall()]
        cursor = conn.execute("SELECT * FROM items")
        cols = [d[0] for d in cursor.description]
        items = [Item(**dict(zip(cols, row))) for row in cursor.fetchall()]
    finally:
        conn.close()
    assert len(events) == 3 and len(items) == 3


def test_a_new_database_is_migrated_at_creation(tmp_path):
    store = _open(tmp_path / "new.db")
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"revisions", "edit_keys", "accepts", "event_meta", "tg_messages", "owner_keys", "settings",
                "bugs", "revision_meta", "docs", "ask_docs"} <= tables
    finally:
        store.close()


def test_no_column_is_added_to_items_or_events(old_db):
    path, _ = old_db
    store = _open(path)
    try:
        items = [r[1] for r in store.conn.execute("PRAGMA table_info(items)")]
        events = [r[1] for r in store.conn.execute("PRAGMA table_info(events)")]
    finally:
        store.close()
    assert events == ["seq", "item_id", "kind", "actor", "text", "at"]
    assert "rev" not in items and "auth" not in items


# --- schema 2: the bugs side table (Pond 1.2.0) ------------------------------

def _v1_database(path):
    """A Pond 1.1.0 database: schema 1, an ask with its revision, no bugs table."""
    conn = _old_database(path)
    item_id = _old_insert(conn, instructable=DOC, message_id=201)
    conn.close()
    store = Store(path)
    store.open()
    store.conn.execute("DROP TABLE bugs")
    store.conn.execute("PRAGMA user_version = 1")
    store.conn.commit()
    store.close()
    return item_id


def test_a_schema_1_database_migrates_to_4_and_keeps_its_data(tmp_path):
    path = tmp_path / "v1.db"
    item_id = _v1_database(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'bugs'").fetchone() is None
    before = (conn.execute("SELECT * FROM items").fetchall(), conn.execute("SELECT * FROM revisions").fetchall(),
              conn.execute("SELECT * FROM events").fetchall(), conn.execute("SELECT * FROM tg_messages").fetchall())
    conn.close()
    store = _open(path)
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        after = (store.conn.execute("SELECT * FROM items").fetchall(),
                 store.conn.execute("SELECT * FROM revisions").fetchall(),
                 store.conn.execute("SELECT * FROM events").fetchall(),
                 store.conn.execute("SELECT * FROM tg_messages").fetchall())
        assert after == before
        assert [r.rev for r in store.revisions(item_id)] == [1]
        cols = [r[1] for r in store.conn.execute("PRAGMA table_info(bugs)")]
        assert cols == ["item_id", "signature", "count", "first_seen", "last_seen", "excerpt"]
        indexes = {r[1] for r in store.conn.execute("PRAGMA index_list(bugs)")}
        assert "bugs_signature" in indexes
    finally:
        store.close()


def test_migrating_twice_changes_nothing(tmp_path):
    path = tmp_path / "v1.db"
    _v1_database(path)
    store = _open(path)
    schema = store.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    migrations.migrate(store.conn)
    assert store.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    store.close()


def test_pond_1_1_0_code_still_reads_a_schema_2_database_with_a_bug(tmp_path):
    """Rollback check: 1.1.0 builds Item(**cols) from SELECT * and never reads `bugs`."""
    path = tmp_path / "v2.db"
    store = _open(path)
    bug = Item(id=str(uuid4()), source="xo-mailbox", title="Hook crash: EXC_BAD_ACCESS (a3f9c2e17b04)",
               project_id="Hook", lane="personal", payload=json.dumps({"key": "bug-a3f9c2e17b04"}))
    store.insert_bug(bug, "a3f9c2e17b04", "Crash signature: hook-crash-a3f9c2e17b04")
    store.close()
    conn = sqlite3.connect(str(path))
    try:
        cursor = conn.execute("SELECT * FROM items")
        cols = [d[0] for d in cursor.description]
        items = [Item(**dict(zip(cols, row))) for row in cursor.fetchall()]
        cursor = conn.execute("SELECT * FROM events")
        cols = [d[0] for d in cursor.description]
        events = [OldEvent(**dict(zip(cols, row))) for row in cursor.fetchall()]
        # 1.1.0's migrate() is gated by user_version < 1: a schema 2 database is left alone.
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    finally:
        conn.close()
    assert [i.id for i in items] == [bug.id] and [e.kind for e in events] == ["posted"]


# --- schema 3: the revision_meta side table (Pond 1.9.0) ----------------------

def _v2_database(path):
    """A Pond 1.8.0 database: schema 2, an ask with its revision, no revision_meta table."""
    conn = _old_database(path)
    item_id = _old_insert(conn, instructable=DOC, message_id=301)
    conn.close()
    store = Store(path)
    store.open()
    store.conn.execute("DROP TABLE revision_meta")
    store.conn.execute("PRAGMA user_version = 2")
    store.conn.commit()
    store.close()
    return item_id


def test_schema_3_adds_revision_meta(tmp_path):
    path = tmp_path / "v2.db"
    item_id = _v2_database(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'revision_meta'").fetchone() is None
    columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in ("items", "events")}
    before = (conn.execute("SELECT * FROM items").fetchall(), conn.execute("SELECT * FROM revisions").fetchall(),
              conn.execute("SELECT * FROM events").fetchall())
    conn.close()
    store = _open(path)
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert {t: [r[1] for r in store.conn.execute(f"PRAGMA table_info({t})")]
                for t in ("items", "events")} == columns
        after = (store.conn.execute("SELECT * FROM items").fetchall(),
                 store.conn.execute("SELECT * FROM revisions").fetchall(),
                 store.conn.execute("SELECT * FROM events").fetchall())
        assert after == before
        cols = [r[1] for r in store.conn.execute("PRAGMA table_info(revision_meta)")]
        assert cols == ["item_id", "rev", "kind"]
        assert store.conn.execute("SELECT COUNT(*) FROM revision_meta").fetchone()[0] == 0   # no backfill
        assert store.revision_kinds(item_id) == {}
    finally:
        store.close()


# --- schema 5: owner_keys.last_used and owner_revoke_requests (Pond 1.11.0) -----

_OLD_OWNER_COLS = "id, key_hash, label, status, code, created, expires, approved_at, revoked_at, tg_message_id"


def _v3_database(path):
    """A Pond 1.9.0 database: schema 3, owner_keys without last_used, one approved key,
    no owner_revoke_requests table."""
    store = Store(path)
    store.open()
    store.conn.execute("DROP TABLE owner_keys")
    store.conn.execute("DROP TABLE owner_revoke_requests")
    store.conn.execute(migrations._DDL_1[7])                        # 1.1.0's CREATE TABLE owner_keys
    store.conn.execute(
        f"INSERT INTO owner_keys ({_OLD_OWNER_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("k1", "ab" * 32, "Studio", "approved", "123456", "2026-11-04T09:00:00+00:00",
         "2026-11-04T09:10:00+00:00", "2026-11-04T09:01:00+00:00", None, 7))
    store.conn.execute("PRAGMA user_version = 3")
    store.conn.commit()
    store.close()


def test_schema_5_adds_last_used_and_the_revoke_requests_table(tmp_path):
    path = tmp_path / "v3.db"
    _v3_database(path)
    conn = sqlite3.connect(str(path))
    assert "last_used" not in [r[1] for r in conn.execute("PRAGMA table_info(owner_keys)")]
    before = conn.execute(f"SELECT {_OLD_OWNER_COLS} FROM owner_keys").fetchall()
    conn.close()
    store = _open(path)
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        cols = [r[1] for r in store.conn.execute("PRAGMA table_info(owner_keys)")]
        assert cols[-1] == "last_used"
        assert store.conn.execute(f"SELECT {_OLD_OWNER_COLS} FROM owner_keys").fetchall() == before
        k = store.get_owner_key("k1")
        assert (k.label, k.status, k.last_used) == ("Studio", "approved", None)
        cols = [r[1] for r in store.conn.execute("PRAGMA table_info(owner_revoke_requests)")]
        assert cols == ["id", "key_id", "status", "code", "created", "expires", "decided_at", "tg_message_id"]
        schema = store.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        migrations.migrate(store.conn)                                 # a second run changes nothing
        assert store.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    finally:
        store.close()


def test_a_schema_4_database_also_runs_step_5(tmp_path):
    """Schema 4 is Pond 1.10.0's (documents): a database it migrated still gets step 5."""
    path = tmp_path / "v4.db"
    _v3_database(path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    store = _open(path)
    try:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert "last_used" in [r[1] for r in store.conn.execute("PRAGMA table_info(owner_keys)")]
    finally:
        store.close()


def test_pond_1_9_0_code_still_reads_and_writes_owner_keys_at_schema_5(tmp_path):
    """Rollback check: 1.9.0 names its owner_keys columns, so last_used is ignored."""
    path = tmp_path / "v5.db"
    _open(path).close()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"INSERT INTO owner_keys ({_OLD_OWNER_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("k2", "cd" * 32, "Laptop", "pending", "654321", "t", "t", None, None, None))
        assert conn.execute(f"SELECT {_OLD_OWNER_COLS} FROM owner_keys").fetchall()[0][:3] == (
            "k2", "cd" * 32, "Laptop")
    finally:
        conn.close()
