"""Tests for the document storage (schema 4): the docs and ask_docs side tables
and the Store methods that write and read them."""
import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest

from mailbox import migrations
from mailbox.store import Item, Store

AT = "2026-11-20T09:00:00+00:00"
BODY_A = b"# Rotate the key\r\n\r\nStep one.\n\xef\xbb\xbfcaf\xc3\xa9 e\xcc\x81\n"
BODY_B = b"# Second note\n\nOther text.\n"
SHA_A = hashlib.sha256(BODY_A).hexdigest()
SHA_B = hashlib.sha256(BODY_B).hexdigest()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "docs.db")
    s.open()
    yield s
    s.close()


def _ask(store, title="An ask"):
    item = Item(id=str(uuid4()), source="xo-mailbox", title=title, project_id="Demo", lane="personal",
                payload=json.dumps({"key": uuid4().hex[:16]}))
    store.insert_ask(item, None, None)
    return item.id


def _count(store, table):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- migration ---------------------------------------------------------------

def _v3_database(path):
    """A Pond 1.9.0 database: schema 3, an ask, no docs or ask_docs table."""
    s = Store(path)
    s.open()
    item_id = _ask(s, "Before documents")
    s.conn.execute("DROP TABLE ask_docs")
    s.conn.execute("DROP TABLE docs")
    s.conn.execute("PRAGMA user_version = 3")
    s.conn.commit()
    s.close()
    return item_id


def test_schema_4_is_a_step():
    """1.11.0 adds schema 5 on top; schema 4 stays the documents' step."""
    assert migrations.SCHEMA_VERSION >= 4 and dict(migrations._STEPS)[4] is migrations._DDL_4


def test_a_schema_3_database_migrates_past_4_and_keeps_its_rows(tmp_path):
    path = tmp_path / "v3.db"
    item_id = _v3_database(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('docs', 'ask_docs')").fetchall() == []
    columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in ("items", "events")}
    before = (conn.execute("SELECT * FROM items").fetchall(), conn.execute("SELECT * FROM events").fetchall(),
              conn.execute("SELECT * FROM revisions").fetchall())
    conn.close()
    s = Store(path)
    s.open()
    try:
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert {t: [r[1] for r in s.conn.execute(f"PRAGMA table_info({t})")] for t in ("items", "events")} == columns
        after = (s.conn.execute("SELECT * FROM items").fetchall(), s.conn.execute("SELECT * FROM events").fetchall(),
                 s.conn.execute("SELECT * FROM revisions").fetchall())
        assert after == before
        assert [r[1] for r in s.conn.execute("PRAGMA table_info(docs)")] == ["sha256", "bytes", "body", "at"]
        assert [r[1] for r in s.conn.execute("PRAGMA table_info(ask_docs)")] == ["item_id", "sha256", "released"]
        assert _count(s, "docs") == 0 and _count(s, "ask_docs") == 0
        assert s.ask_docs(item_id) == set()
    finally:
        s.close()


def test_a_migrated_database_with_docs_opens_unchanged(tmp_path):
    path = tmp_path / "v4.db"
    s = Store(path)
    s.open()
    item_id = _ask(s)
    s.put_doc(SHA_A, BODY_A, AT)
    s.link_docs(item_id, [SHA_A])
    schema = s.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    rows = (s.conn.execute("SELECT * FROM docs").fetchall(), s.conn.execute("SELECT * FROM ask_docs").fetchall())
    s.close()
    s = Store(path)
    s.open()
    try:
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert s.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert (s.conn.execute("SELECT * FROM docs").fetchall(),
                s.conn.execute("SELECT * FROM ask_docs").fetchall()) == rows
        migrations.migrate(s.conn)
        assert s.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    finally:
        s.close()


# --- put_doc / get_doc ---------------------------------------------------------

def test_put_doc_stores_the_exact_bytes(store):
    store.put_doc(SHA_A, BODY_A, AT)
    assert store.get_doc(SHA_A) == BODY_A
    assert isinstance(store.get_doc(SHA_A), bytes)
    row = store.conn.execute("SELECT bytes, at, typeof(body) FROM docs WHERE sha256 = ?", (SHA_A,)).fetchone()
    assert row == (len(BODY_A), AT, "blob")


def test_put_doc_twice_is_one_row_and_keeps_the_first(store):
    store.put_doc(SHA_A, BODY_A, AT)
    store.put_doc(SHA_A, BODY_A, "2026-11-21T10:00:00+00:00")
    assert _count(store, "docs") == 1
    assert store.conn.execute("SELECT at FROM docs WHERE sha256 = ?", (SHA_A,)).fetchone()[0] == AT


def test_get_doc_unknown_is_none(store):
    assert store.get_doc(SHA_B) is None


# --- link_docs / ask_docs / doc_status -------------------------------------------

def test_link_docs_and_ask_docs(store):
    item_id = _ask(store)
    store.put_doc(SHA_A, BODY_A, AT)
    store.put_doc(SHA_B, BODY_B, AT)
    store.link_docs(item_id, [SHA_A, SHA_B])
    store.link_docs(item_id, iter([SHA_A]))           # any iterable; linking twice is one row
    assert store.ask_docs(item_id) == {SHA_A, SHA_B}
    assert _count(store, "ask_docs") == 2
    assert store.doc_status(item_id, SHA_A) == "kept"
    assert store.ask_docs(_ask(store, "Other")) == set()


def test_doc_status_for_an_unknown_hash_is_none(store):
    item_id = _ask(store)
    store.put_doc(SHA_A, BODY_A, AT)
    assert store.doc_status(item_id, SHA_A) is None    # stored, but this ask never linked it
    assert store.doc_status(item_id, SHA_B) is None
    assert store.doc_status("no-such-ask", SHA_A) is None


def test_doc_status_is_released_when_the_text_is_missing(store):
    item_id = _ask(store)
    store.link_docs(item_id, [SHA_A])                  # linked, never stored
    assert store.doc_status(item_id, SHA_A) == "released"


# --- _release_docs ----------------------------------------------------------------

def test_release_keeps_text_another_ask_still_links(store):
    first, second = _ask(store, "First"), _ask(store, "Second")
    store.put_doc(SHA_A, BODY_A, AT)
    store.put_doc(SHA_B, BODY_B, AT)
    store.link_docs(first, [SHA_A, SHA_B])
    store.link_docs(second, [SHA_A])

    assert store._release_docs(first) == 2
    assert store.get_doc(SHA_A) == BODY_A              # second still links it unreleased
    assert store.get_doc(SHA_B) is None                # nobody else links it
    assert store.doc_status(first, SHA_A) == "released"
    assert store.doc_status(first, SHA_B) == "released"
    assert store.doc_status(second, SHA_A) == "kept"
    assert store.ask_docs(first) == {SHA_A, SHA_B}     # the rows stay, marked
    assert store._release_docs(first) == 0             # a second call marks nothing

    assert store._release_docs(second) == 1
    assert store.get_doc(SHA_A) is None
    assert _count(store, "docs") == 0
    assert store.doc_status(second, SHA_A) == "released"


def test_release_of_an_ask_without_docs_is_0(store):
    assert store._release_docs(_ask(store)) == 0


def test_link_docs_after_a_release_sets_the_row_back(store):
    item_id = _ask(store)
    store.put_doc(SHA_A, BODY_A, AT)
    store.link_docs(item_id, [SHA_A])
    store._release_docs(item_id)
    assert store.doc_status(item_id, SHA_A) == "released"
    store.put_doc(SHA_A, BODY_A, AT)
    store.link_docs(item_id, [SHA_A])
    assert store.doc_status(item_id, SHA_A) == "kept"
    assert _count(store, "ask_docs") == 1
    assert store._release_docs(item_id) == 1


# --- the same transaction as the ask ------------------------------------------

def test_writes_join_an_open_transaction_and_roll_back_with_it(store):
    """put_doc and link_docs leave the commit to a caller that holds a transaction,
    so a document lands with the change to its ask or not at all."""
    item_id = _ask(store)
    with pytest.raises(RuntimeError):
        with store.event_transaction():
            store.put_doc(SHA_A, BODY_A, AT)
            store.link_docs(item_id, [SHA_A])
            store.update_item(item_id, {"title": "Changed"})
            assert store.conn.in_transaction
            raise RuntimeError("abort")
    assert store.get_doc(SHA_A) is None and store.ask_docs(item_id) == set()
    assert store.get_item(item_id).title == "An ask"

    with store.event_transaction():
        store.put_doc(SHA_A, BODY_A, AT)
        store.link_docs(item_id, [SHA_A])
        store.update_item(item_id, {"title": "Changed"})
    assert store.doc_status(item_id, SHA_A) == "kept"
    assert store.get_item(item_id).title == "Changed"


def test_release_joins_an_open_transaction(store):
    item_id = _ask(store)
    store.put_doc(SHA_A, BODY_A, AT)
    store.link_docs(item_id, [SHA_A])
    with pytest.raises(RuntimeError):
        with store.event_transaction():
            assert store._release_docs(item_id) == 1
            raise RuntimeError("abort")
    assert store.doc_status(item_id, SHA_A) == "kept" and store.get_doc(SHA_A) == BODY_A


def test_release_never_touches_text_the_ask_did_not_link(store):
    """A release deletes only text the releasing ask linked: a text another ask links,
    or one stored and not linked yet, survives an unrelated ask's release."""
    releasing, other = _ask(store, "Releasing"), _ask(store, "Other")
    body_c = b"# Third note\n"
    sha_c = hashlib.sha256(body_c).hexdigest()
    store.put_doc(SHA_A, BODY_A, AT)
    store.link_docs(releasing, [SHA_A])
    store.put_doc(SHA_B, BODY_B, AT)
    store.link_docs(other, [SHA_B])
    store._release_docs(other)                         # other's text is gone, its row released
    store.put_doc(SHA_B, BODY_B, AT)                   # stored again, linked by nobody unreleased
    store.put_doc(sha_c, body_c, AT)                   # stored, never linked
    assert store._release_docs(releasing) == 1
    assert store.get_doc(SHA_A) is None
    assert store.get_doc(SHA_B) == BODY_B
    assert store.get_doc(sha_c) == body_c


# --- put_doc's guard ---------------------------------------------------------------

@pytest.mark.parametrize("sha, body", [
    (SHA_B, BODY_A),                                       # not its hash
    (hashlib.sha256(b"").hexdigest(), b""),                # empty
    (hashlib.sha256(b"x" * 262145).hexdigest(), b"x" * 262145),   # over MAX_DOC_BYTES
], ids=["not-its-hash", "empty", "too-large"])
def test_put_doc_refuses_a_body_that_is_not_a_document_with_that_hash(store, sha, body):
    with pytest.raises(ValueError):
        store.put_doc(sha, body, AT)
    assert _count(store, "docs") == 0


def test_put_doc_takes_the_largest_document(store):
    body = b"x" * 262144
    store.put_doc(hashlib.sha256(body).hexdigest(), body, AT)
    assert _count(store, "docs") == 1


# --- documents in the ask's own transaction ----------------------------------------

from mailbox.store import AskNotOpen, StaleRevision  # noqa: E402
from mailbox.tests.test_store import AT1, AT2, _rev  # noqa: E402


def _ask_with(store, docs, item_id=None):
    item = Item(id=item_id or str(uuid4()), source="xo-mailbox", title="With docs", project_id="Demo",
                lane="personal", payload=json.dumps({"key": uuid4().hex[:16]}), created=AT1, updated=AT1)
    rev1 = _rev(item.id, 1, "", AT1, "", {"v": 1, "title": "t", "steps": [{"do": "x", "id": "s1"}]})
    store.insert_ask(item, rev1, "ab" * 32, docs=docs)
    return item, rev1


def test_insert_ask_stores_and_links_its_documents(store):
    item, _ = _ask_with(store, {SHA_A: BODY_A, SHA_B: BODY_B})
    assert store.ask_docs(item.id) == {SHA_A, SHA_B}
    assert store.get_doc(SHA_A) == BODY_A and store.get_doc(SHA_B) == BODY_B
    assert store.doc_status(item.id, SHA_B) == "kept"


def test_a_failed_insert_ask_leaves_no_document(store):
    item, _ = _ask_with(store, None)
    with pytest.raises(sqlite3.IntegrityError):        # the same item id again
        _ask_with(store, {SHA_A: BODY_A}, item_id=item.id)
    with pytest.raises(ValueError):                    # put_doc's guard, inside the transaction
        _ask_with(store, {SHA_A: BODY_A, SHA_B: BODY_A})
    assert _count(store, "docs") == 0 and _count(store, "ask_docs") == 0
    assert _count(store, "items") == 1


def _next(item, base, body_steps):
    return _rev(item.id, base.rev + 1, base.hash, AT2, "fix", {"v": 1, "title": "t", "steps": body_steps})


def test_amend_tx_stores_and_links_its_documents(store):
    item, rev1 = _ask_with(store, {SHA_A: BODY_A})
    new = _next(item, rev1, [{"do": "y", "id": "s1"}])
    store.amend_tx(item.id, base_rev=1, base_hash=rev1.hash, new=new, event_text="fix", docs={SHA_B: BODY_B})
    assert store.ask_docs(item.id) == {SHA_A, SHA_B}
    assert store.get_doc(SHA_B) == BODY_B
    assert store.conn.execute("SELECT at FROM docs WHERE sha256 = ?", (SHA_B,)).fetchone()[0] == AT2


def test_a_refused_amend_tx_leaves_no_document(store):
    item, rev1 = _ask_with(store, None)
    new = _next(item, rev1, [{"do": "y", "id": "s1"}])
    with pytest.raises(StaleRevision):
        store.amend_tx(item.id, base_rev=1, base_hash="0" * 64, new=new, event_text="fix", docs={SHA_A: BODY_A})
    store.amend_tx(item.id, base_rev=1, base_hash=rev1.hash, new=new, event_text="fix")
    with pytest.raises(StaleRevision):                 # rev 1 is no longer current
        store.amend_tx(item.id, base_rev=1, base_hash=rev1.hash, new=new, event_text="fix", docs={SHA_A: BODY_A})
    store.update_item(item.id, {"status": "done"})
    with pytest.raises(AskNotOpen):
        store.append_tx(item.id, base_rev=2, base_hash=new.hash, new=_next(item, new, [{"do": "z", "id": "s1"}]),
                        kind="append", advance=False, event_text="more", docs={SHA_B: BODY_B})
    assert _count(store, "docs") == 0 and _count(store, "ask_docs") == 0


# --- final wave: tidying (W8) ------------------------------------------------------

def test_the_unchecked_release_is_private():
    """Store._release_docs checks nothing about the ask: its callers check first."""
    assert hasattr(Store, "_release_docs") and not hasattr(Store, "release_docs")


def test_one_hash_pattern_for_instructable_and_docs():
    """Defined once, in instructable, and imported by docs (re.compile caches equal
    patterns, so `is` alone would not tell)."""
    import inspect
    from mailbox import docs, instructable
    assert docs._SHA256 is instructable._SHA256
    assert "_SHA256 = " in inspect.getsource(instructable) and "_SHA256 = " not in inspect.getsource(docs)
