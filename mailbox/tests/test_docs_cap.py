"""The total limit on stored documents (Pond 1.12.0): docs.MAX_STORE_BYTES, the sum of
docs.bytes. A request whose new texts would take the total over it is refused with
409 {"error": "document store full"} and stores nothing.

Every test patches the limit small: none stores hundreds of megabytes. Every text
here is synthetic."""
import json
import threading
from uuid import uuid4

import pytest

from mailbox import docs as documents
from mailbox.docs import MAX_STORE_BYTES
from mailbox.store import Item, Store, StoreFull
from mailbox.tests.test_docs_expiry import WEEK, synced  # noqa: F401 — `synced` is a fixture
from mailbox.tests.test_docs_post import (  # noqa: F401 — `pond` is a fixture
    CHECKLIST, NOTES_TEXT, PLAN_TEXT, _amend, _append, _body, _counts, _post, _posted, ask, pond, read, sha,
)
from mailbox.tests.test_docs_read import close, release

FULL = (409, {"error": "document store full"})
ONE_BYTE = "x"


def size(text: str) -> int:
    return len(text.encode("utf-8"))


def limit(monkeypatch, n: int) -> int:
    monkeypatch.setattr(documents, "MAX_STORE_BYTES", n)
    return n


def snapshot(srv) -> dict:
    """Everything a refused request must leave as it was."""
    conn = srv.service.store.conn
    return {**_counts(srv),
            "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "telegram": len(srv.service.telegram.calls),
            "titles": sorted(row[0] for row in conn.execute("SELECT title FROM items"))}


def _post_text(base_url, token, text, title, path=CHECKLIST):
    return _post(base_url, token, ask(read(text, path=path), title=title), {sha(text): text})


def test_the_store_limit_is_256_mib():
    assert MAX_STORE_BYTES == 268435456 == 256 * 1024 * 1024


# --- over HTTP ----------------------------------------------------------------------------

def test_a_text_that_fills_the_store_exactly_fits_and_one_byte_more_is_refused(pond, monkeypatch):
    srv, base_url, token, _ = pond
    cap = limit(monkeypatch, size(PLAN_TEXT) + size(NOTES_TEXT))
    _posted(base_url, token)
    status, data = _post_text(base_url, token, NOTES_TEXT, "Read the notes")
    assert status == 201, data
    assert srv.service.store.doc_totals() == (2, cap)               # the total equals the limit
    before = snapshot(srv)
    assert _post_text(base_url, token, ONE_BYTE, "One byte more") == FULL
    assert snapshot(srv) == before                                  # no ask, row, event or Telegram call
    assert srv.service.store.get_doc(sha(ONE_BYTE)) is None


def test_a_text_already_stored_does_not_count_again(pond, monkeypatch):
    srv, base_url, token, _ = pond
    limit(monkeypatch, size(PLAN_TEXT))
    first, _ = _posted(base_url, token)
    status, data = _post_text(base_url, token, PLAN_TEXT, "Review the same plan again")
    assert status == 201, data
    store = srv.service.store
    assert store.doc_totals() == (1, size(PLAN_TEXT))
    assert store.ask_docs(data["item"]["id"]) == {sha(PLAN_TEXT)} == store.ask_docs(first)


def test_several_texts_that_do_not_all_fit_are_refused_as_a_whole(pond, monkeypatch):
    srv, base_url, token, _ = pond
    one, two = "# Part one\n\nfirst\n", "# Part two\n\nsecond\n"
    limit(monkeypatch, size(PLAN_TEXT) + max(size(one), size(two)))
    _posted(base_url, token)
    before = snapshot(srv)
    both = ask(read(one, path="/var/tmp/one.md"), read(two, path="/var/tmp/two.md"), title="Read both parts")
    assert _post(base_url, token, both, {sha(one): one, sha(two): two}) == FULL
    assert snapshot(srv) == before
    status, data = _post_text(base_url, token, one, "Read part one")     # either alone fits
    assert status == 201, data


def test_a_deduplicated_post_is_not_refused(pond, monkeypatch):
    srv, base_url, token, _ = pond
    limit(monkeypatch, size(PLAN_TEXT))
    item_id, _ = _posted(base_url, token)
    status, data = _post(base_url, token, ask(read(NOTES_TEXT)), {sha(NOTES_TEXT): NOTES_TEXT})
    assert status == 200 and data["deduped"] is True and data["item"]["id"] == item_id
    assert srv.service.store.get_doc(sha(NOTES_TEXT)) is None


def test_a_correction_and_added_steps_with_a_new_text_are_refused_and_keeping_held_ones_is_not(pond, monkeypatch):
    srv, base_url, token, clock = pond
    limit(monkeypatch, size(PLAN_TEXT))
    item_id, base = _posted(base_url, token)
    clock.advance(60)
    before = snapshot(srv)
    changed = _body(srv, item_id)
    changed["steps"][0]["doc"] = {"sha256": sha(NOTES_TEXT), "bytes": size(NOTES_TEXT)}
    assert _amend(base_url, token, item_id, base, changed, {sha(NOTES_TEXT): NOTES_TEXT}) == FULL
    assert _append(base_url, token, item_id, base, [read(NOTES_TEXT, path=CHECKLIST)],
                   {sha(NOTES_TEXT): NOTES_TEXT}) == FULL
    assert snapshot(srv) == before                                  # no revision, row, event or Telegram call
    kept = _body(srv, item_id)
    kept["steps"][0]["do"] = "Read the whole plan"
    status, data = _amend(base_url, token, item_id, base, kept)     # keeps only what the ask holds
    assert status == 200, data
    clock.advance(15)
    status, data = _append(base_url, token, item_id, data["revision"], [{"do": "Say it is read"}])
    assert status == 200, data


def test_a_release_frees_the_space(pond, monkeypatch):
    srv, base_url, token, _ = pond
    limit(monkeypatch, size(PLAN_TEXT))
    item_id, _ = _posted(base_url, token)
    assert _post_text(base_url, token, NOTES_TEXT, "Read the notes") == FULL
    close(base_url, token, item_id)
    assert release(base_url, item_id, token) == (200, {"released": 1})
    status, data = _post_text(base_url, token, NOTES_TEXT, "Read the notes")
    assert status == 201, data


def test_the_expiry_frees_the_space(synced, monkeypatch):
    srv, base_url, token, clock = synced
    limit(monkeypatch, size(PLAN_TEXT))
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    assert _post_text(base_url, token, NOTES_TEXT, "Read the notes") == FULL
    clock.now += WEEK
    assert srv.service.sweep_docs() == 1
    status, data = _post_text(base_url, token, NOTES_TEXT, "Read the notes")
    assert status == 201, data


def test_a_post_that_loses_the_race_after_its_send_is_refused_and_its_message_withdrawn(pond, monkeypatch):
    """Another request fills the store between this post's early check and its
    transaction (here: while Telegram sends). The transaction's exact check refuses
    it, nothing is stored, and the message already sent loses its buttons and says
    the ask was not stored, without any document text."""
    srv, base_url, token, _ = pond
    limit(monkeypatch, size(PLAN_TEXT))
    store = srv.service.store
    real_send = srv.service.telegram.send_message

    def send_then_rival(*args, **kwargs):
        rival = Item(id=str(uuid4()), source="xo-mailbox", title="Rival", project_id="Demo", lane="personal",
                     payload=json.dumps({"key": uuid4().hex[:16]}))
        store.insert_ask(rival, None, None, docs={sha(NOTES_TEXT): NOTES_TEXT.encode("utf-8")})
        return real_send(*args, **kwargs)

    monkeypatch.setattr(srv.service.telegram, "send_message", send_then_rival)
    assert _post(base_url, token, sent={sha(PLAN_TEXT): PLAN_TEXT}) == FULL
    titles = [row[0] for row in store.conn.execute("SELECT title FROM items")]
    assert titles == ["Rival"]
    assert store.get_doc(sha(PLAN_TEXT)) is None and _counts(srv)["ask_docs"] == 1
    edit = srv.service.telegram.edited_messages[-1]
    assert edit["message_id"] == 1 and edit["reply_markup"] == {"inline_keyboard": []}
    assert "not stored" in edit["text"] and PLAN_TEXT.strip() not in edit["text"]


@pytest.mark.parametrize("failure", [TimeoutError("read timed out"), ValueError("unreadable body")],
                         ids=["timeout", "unreadable"])
def test_a_failed_withdrawal_still_answers_409_and_stores_nothing(pond, monkeypatch, failure):
    """The edit on the race path fails with an error the Telegram client does not wrap:
    the post is still refused with 409 document store full, never a 500."""
    srv, base_url, token, _ = pond
    limit(monkeypatch, size(PLAN_TEXT))
    store = srv.service.store
    telegram = srv.service.telegram
    real_send = telegram.send_message

    def send_then_rival(*args, **kwargs):
        rival = Item(id=str(uuid4()), source="xo-mailbox", title="Rival", project_id="Demo", lane="personal",
                     payload=json.dumps({"key": uuid4().hex[:16]}))
        store.insert_ask(rival, None, None, docs={sha(NOTES_TEXT): NOTES_TEXT.encode("utf-8")})
        return real_send(*args, **kwargs)

    def broken_edit(*args, **kwargs):
        raise failure

    monkeypatch.setattr(telegram, "send_message", send_then_rival)
    monkeypatch.setattr(telegram, "edit_message_text", broken_edit)
    assert _post(base_url, token, sent={sha(PLAN_TEXT): PLAN_TEXT}) == FULL
    assert [row[0] for row in store.conn.execute("SELECT title FROM items")] == ["Rival"]
    assert store.get_doc(sha(PLAN_TEXT)) is None and _counts(srv)["ask_docs"] == 1


# --- the switch off: the limit is never reached ----------------------------------------------

def test_with_the_switch_off_the_limit_is_not_reached(pond, monkeypatch):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    limit(monkeypatch, 0)
    srv.service.store.delete_setting("docs_enabled")

    def never(*args, **kwargs):
        raise AssertionError("the limit was checked")

    monkeypatch.setattr(Store, "check_doc_room", never)
    assert _post_text(base_url, token, NOTES_TEXT, "Read the notes") == (409, {"error": "documents not enabled"})
    status, data = _post(base_url, token, ask({"do": "Plain"}, title="A plain ask"))
    assert status == 201, data
    clock.advance(60)
    kept = _body(srv, item_id)
    kept["steps"][0]["do"] = "Read the whole plan"
    status, data = _amend(base_url, token, item_id, base, kept)
    assert status == 200, data


def test_with_the_switch_on_a_request_without_text_is_never_refused(pond, monkeypatch):
    srv, base_url, token, _ = pond
    limit(monkeypatch, 0)
    status, data = _post(base_url, token, ask({"do": "Plain"}, title="A plain ask"))
    assert status == 201, data


# --- the store: the check inside the transaction ---------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "cap.db")
    s.open()
    yield s
    s.close()


def _new_ask(store, docs):
    item = Item(id=str(uuid4()), source="xo-mailbox", title=f"Ask {uuid4().hex[:6]}", project_id="Demo",
                lane="personal", payload=json.dumps({"key": uuid4().hex[:16]}))
    store.insert_ask(item, None, None, docs=docs)
    return item.id


TEXT_A = b"# First\n\nalpha alpha\n"
TEXT_B = b"# Second\n\nbravo bravo\n"
DOC_A = {sha(TEXT_A.decode()): TEXT_A}
DOC_B = {sha(TEXT_B.decode()): TEXT_B}


def test_insert_ask_refuses_over_the_limit_and_stores_nothing(store, monkeypatch):
    limit(monkeypatch, len(TEXT_A) + len(TEXT_B) - 1)
    _new_ask(store, DOC_A)
    before = store.conn.execute("SELECT (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM events)").fetchone()
    with pytest.raises(StoreFull):
        _new_ask(store, DOC_B)
    assert store.conn.execute("SELECT (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM events)").fetchone() == before
    assert store.doc_totals() == (1, len(TEXT_A))
    assert store.conn.execute("SELECT COUNT(*) FROM ask_docs").fetchone()[0] == 1


def test_two_posts_at_once_never_take_the_total_over_the_limit(store, monkeypatch):
    """Each fits alone, not both. The first is inside its transaction (storing its text)
    when the second starts: the second waits for it, then its own check sees the first
    text and refuses. Exactly one lands."""
    cap = limit(monkeypatch, max(len(TEXT_A), len(TEXT_B)))
    real = store.put_doc
    seen: dict = {}

    def second():
        try:
            _new_ask(store, DOC_B)
            seen["second"] = "stored"
        except StoreFull:
            seen["second"] = "full"

    def racing(sha256, body, at):
        if "rival" not in seen:
            seen["rival"] = threading.Thread(target=second)
            seen["rival"].start()
            seen["rival"].join(timeout=0.5)
            seen["waited"] = seen["rival"].is_alive()
        return real(sha256, body, at)

    monkeypatch.setattr(store, "put_doc", racing)
    _new_ask(store, DOC_A)
    seen["rival"].join(timeout=10)
    assert seen["waited"] is True and seen["second"] == "full"
    count, total = store.doc_totals()
    assert (count, total) == (1, len(TEXT_A)) and total <= cap


def test_many_posts_at_once_land_exactly_as_many_as_fit(store, monkeypatch):
    texts = [f"# Note {i}\n\nsame size\n".encode() for i in range(6)]
    cap = limit(monkeypatch, 2 * len(texts[0]))
    barrier = threading.Barrier(len(texts))
    results: list[str] = []

    def post(body):
        barrier.wait()
        try:
            _new_ask(store, {sha(body.decode()): body})
            results.append("stored")
        except StoreFull:
            results.append("full")

    threads = [threading.Thread(target=post, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == ["full"] * 4 + ["stored"] * 2
    assert store.doc_totals()[1] == cap


def test_amend_tx_refuses_over_the_limit_and_stores_nothing(store, monkeypatch):
    from mailbox.tests.test_store import AT2, _rev
    limit(monkeypatch, len(TEXT_A))
    item = Item(id=str(uuid4()), source="xo-mailbox", title="With a doc", project_id="Demo", lane="personal",
                payload=json.dumps({"key": uuid4().hex[:16]}))
    rev1 = _rev(item.id, 1, "", AT2, "", {"v": 1, "title": "t", "steps": [{"do": "x", "id": "s1"}]})
    store.insert_ask(item, rev1, "ab" * 32, docs=DOC_A)
    new = _rev(item.id, 2, rev1.hash, AT2, "fix", {"v": 1, "title": "t", "steps": [{"do": "y", "id": "s1"}]})
    with pytest.raises(StoreFull):
        store.amend_tx(item.id, base_rev=1, base_hash=rev1.hash, new=new, event_text="fix", docs=DOC_B)
    assert store.current_revision(item.id).rev == 1
    assert store.ask_docs(item.id) == set(DOC_A)
    # The same text again adds nothing: it fits a full store.
    store.amend_tx(item.id, base_rev=1, base_hash=rev1.hash, new=new, event_text="fix", docs=DOC_A)
    assert store.current_revision(item.id).rev == 2


def test_a_released_text_frees_its_bytes(store, monkeypatch):
    limit(monkeypatch, max(len(TEXT_A), len(TEXT_B)))
    first = _new_ask(store, DOC_A)
    with pytest.raises(StoreFull):
        _new_ask(store, DOC_B)
    store.update_item(first, {"status": "done"})
    assert store.release_closed_docs(first) == 1
    _new_ask(store, DOC_B)
    assert store.doc_totals() == (1, len(TEXT_B))

