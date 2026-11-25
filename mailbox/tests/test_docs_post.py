"""Posting with documents (Pond 1.10.0): the `docs` member of POST /asks, amend and
append, docs.check's errors, storage in the ask's own transaction, and the switch.

Every text here is synthetic. A secret-like value is built at run time from parts."""
import dataclasses
import hashlib
import json
import logging
import urllib.request
from pathlib import Path

import pytest

from mailbox import FEATURES
from mailbox.docs import MAX_DOCS_BYTES, check, digest
from mailbox.instructable import MAX_DOC_BYTES, validate
from mailbox.revisions import assign_ids, canonical, revision_hash
from mailbox.telegram import TelegramError
from mailbox.tests.test_server import (
    EDIT_HASH, EDIT_KEY, T0, _approved_key, _bug_doc, _Clock, _running, hcall, post_ask, post_bug, raw_exchange,
)

PLAN = "~/notes/plan.md"
CHECKLIST = "/var/tmp/release-checklist.md"
PLAN_TEXT = "# Release plan\n\nShip on Friday.\n"
NOTES_TEXT = "# Notes\n\ncafé, naïve\n"
SHAPE = "docs: must be an object of SHA-256 hash to text"


def ref(text: str) -> dict:
    sha, size = digest(text)
    return {"sha256": sha, "bytes": size}


def sha(text: str) -> str:
    return digest(text)[0]


def ask(*steps, links=None, title="Review the release plan") -> dict:
    obj = {"v": 1, "title": title, "steps": list(steps) or [{"do": "Read the plan", "file": PLAN,
                                                             "doc": ref(PLAN_TEXT)}]}
    if links is not None:
        obj["links"] = links
    return obj


def read(text=PLAN_TEXT, path=PLAN, do="Read the plan") -> dict:
    return {"do": do, "file": path, "doc": ref(text)}


# --- docs.digest and docs.check ------------------------------------------------------

def test_digest_is_the_sha256_of_the_utf8_bytes():
    assert digest(NOTES_TEXT) == (hashlib.sha256(NOTES_TEXT.encode("utf-8")).hexdigest(),
                                  len(NOTES_TEXT.encode("utf-8")))
    assert digest(NOTES_TEXT)[1] == len(NOTES_TEXT) + 2      # é and ï are two bytes each


def test_the_limits():
    assert MAX_DOCS_BYTES == 786432 and MAX_DOC_BYTES == 262144


def test_a_sent_and_referenced_document_passes():
    assert check(ask(), {sha(PLAN_TEXT): PLAN_TEXT}, set()) == []
    assert check(ask({"do": "x"}), None, set()) == []
    assert check(ask({"do": "x"}), {}, set()) == []


@pytest.mark.parametrize("sent", [
    [], "text", 5,
    {"A" * 64: "text"},                                 # uppercase
    {"0" * 63: "text"},                                 # short
    {"g" * 64: "text"},                                 # not hex
    {"0" * 64: 5},                                      # not a string
    {"0" * 64: None},
    {"0" * 64: "fine", "not-a-hash-key": "x"},
], ids=["list", "string", "number", "uppercase", "short", "not-hex", "number-value", "null-value", "mixed"])
def test_a_malformed_docs_member_is_one_error_that_echoes_nothing(sent):
    assert check(ask(), sent, set()) == [SHAPE]


def test_a_text_that_does_not_match_its_key():
    key = sha(NOTES_TEXT)
    assert check(ask(read(NOTES_TEXT)), {key: PLAN_TEXT}, set()) == [f"docs.{key}: does not match its hash"]


def test_a_document_larger_than_256_kb():
    text = "x" * (MAX_DOC_BYTES + 1)
    obj = ask({"do": "Read it", "file": PLAN, "doc": {"sha256": sha(text), "bytes": MAX_DOC_BYTES}})
    assert check(obj, {sha(text): text}, set()) == [f"docs.{sha(text)}: larger than 262144 bytes"]
    largest = "y" * MAX_DOC_BYTES
    assert check(ask(read(largest)), {sha(largest): largest}, set()) == []


def test_more_than_768_kb_in_all():
    texts = [c * MAX_DOC_BYTES for c in "abc"]          # exactly 786432 bytes: still fine
    obj = ask(*[read(t, path=f"/var/tmp/{i}.md") for i, t in enumerate(texts)])
    assert check(obj, {sha(t): t for t in texts}, set()) == []
    texts.append("d")
    obj = ask(*[read(t, path=f"/var/tmp/{i}.md") for i, t in enumerate(texts)])
    assert check(obj, {sha(t): t for t in texts}, set()) == ["docs: more than 786432 bytes in all"]


def test_a_sent_document_no_step_or_link_uses():
    sent = {sha(PLAN_TEXT): PLAN_TEXT, sha(NOTES_TEXT): NOTES_TEXT}
    assert check(ask(), sent, set()) == [f"docs.{sha(NOTES_TEXT)}: not used by any step or link"]


def test_a_doc_with_no_document_sent_or_held():
    assert check(ask(), None, set()) == ["steps[0].doc: no document with this hash"]
    assert check(ask(), {}, {sha(NOTES_TEXT)}) == ["steps[0].doc: no document with this hash"]


def test_a_held_document_needs_no_text():
    assert check(ask(), None, {sha(PLAN_TEXT)}) == []
    assert check(ask(), None, {sha(PLAN_TEXT)}, held_bytes={sha(PLAN_TEXT): len(PLAN_TEXT)}) == []


def test_a_doc_whose_bytes_do_not_match_the_document():
    wrong = ask({"do": "Read the plan", "file": PLAN, "doc": {"sha256": sha(PLAN_TEXT), "bytes": 3}})
    assert check(wrong, {sha(PLAN_TEXT): PLAN_TEXT}, set()) == ["steps[0].doc.bytes: does not match the document"]
    assert check(wrong, None, {sha(PLAN_TEXT)}, held_bytes={sha(PLAN_TEXT): len(PLAN_TEXT)}) == [
        "steps[0].doc.bytes: does not match the document"]


def test_a_secret_like_document_is_named_by_its_hash_only():
    secret = "sk-" + "ant-" + "api03-" + "Q" * 32       # built at runtime: no literal key in the repo
    text = f"# Setup\n\nexport KEY={secret}\n"
    errors = check(ask(read(text)), {sha(text): text}, set())
    assert errors == [f"docs.{sha(text)}: secret-like value"]
    assert all(secret not in e and "Setup" not in e for e in errors)


@pytest.mark.parametrize("text", ["# Plan\x00\n", "# Plan \ud800\n"], ids=["nul", "lone-surrogate"])
def test_a_document_that_is_not_text(text):
    key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    obj = ask({"do": "Read it", "file": PLAN, "doc": {"sha256": key, "bytes": 8}})
    assert check(obj, {key: text}, set()) == [f"docs.{key}: not text"]


def test_errors_come_in_reference_order_then_hash_order_each_once():
    a, b, c = "# A\n", "# B\n", "# C\n"
    stray_1, stray_2 = "# stray one\n", "# stray two\n"
    obj = ask(
        {"do": "Read A", "file": PLAN, "doc": {"sha256": sha(a), "bytes": 99}},
        {"do": "Group", "substeps": [{"do": "Read B", "file": PLAN, "doc": ref(b)}]},
        links=[{"label": "C", "path": CHECKLIST, "doc": ref(c)},
               {"label": "A again", "path": CHECKLIST, "doc": {"sha256": sha(a), "bytes": 98}}],
    )
    sent = {sha(a): a, sha(stray_2): stray_2, sha(stray_1): stray_1, sha(c): b}
    strays = sorted([sha(stray_1), sha(stray_2), sha(c)])
    expected = [
        "steps[0].doc.bytes: does not match the document",
        "steps[1].substeps[0].doc: no document with this hash",
        "links[1].doc.bytes: does not match the document",
    ] + [f"docs.{h}: does not match its hash" if h == sha(c) else f"docs.{h}: not used by any step or link"
         for h in strays]
    assert check(obj, sent, set()) == expected


# --- over HTTP: the Pond ---------------------------------------------------------------

@pytest.fixture
def pond(tmp_path):
    """A running Pond with a pinned clock, corrections and documents on."""
    for srv, base_url, token in _running(tmp_path):
        clock = _Clock()
        srv.service.utcnow = clock
        srv.service.store.set_setting("corrections_enabled", "1")
        srv.service.store.set_setting("docs_enabled", "1")
        yield srv, base_url, token, clock


def _counts(srv):
    conn = srv.service.store.conn
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("items", "revisions", "docs", "ask_docs")}


def _post(base_url, token, instructable=None, sent=None, **extra):
    body = {"instructable": instructable or ask(), "editKeyHash": EDIT_HASH, **extra}
    if sent is not None:
        body["docs"] = sent
    return post_ask(base_url, token, ask=None, **body)


def _posted(base_url, token, instructable=None, sent=None):
    status, data = _post(base_url, token, instructable, sent if sent is not None else {sha(PLAN_TEXT): PLAN_TEXT})
    assert status == 201, data
    return data["item"]["id"], data["revision"]


def _body(srv, item_id):
    return json.loads(srv.service.store.current_revision(item_id).body)


def _amend(base_url, token, item_id, base, instructable, sent=None):
    body = {"editKey": EDIT_KEY, "baseRev": base["rev"], "baseHash": base["hash"], "reason": "a fix",
            "instructable": instructable}
    if sent is not None:
        body["docs"] = sent
    return hcall(base_url, f"/asks/{item_id}/amend", token, method="POST", body=body, protocol=5)


def _append(base_url, token, item_id, base, steps, sent=None):
    body = {"editKey": EDIT_KEY, "baseRev": base["rev"], "baseHash": base["hash"], "note": "More reading",
            "steps": steps}
    if sent is not None:
        body["docs"] = sent
    return hcall(base_url, f"/asks/{item_id}/append", token, method="POST", body=body, protocol=5)


def test_a_post_stores_its_documents_with_the_ask(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    store = srv.service.store
    assert store.ask_docs(item_id) == {sha(PLAN_TEXT)}
    assert store.get_doc(sha(PLAN_TEXT)) == PLAN_TEXT.encode("utf-8")
    assert store.doc_status(item_id, sha(PLAN_TEXT)) == "kept"
    assert _body(srv, item_id)["steps"][0]["doc"] == ref(PLAN_TEXT)       # the revision hash covers it


def test_a_post_with_document_errors_answers_them_all_and_stores_nothing(pond):
    srv, base_url, token, _ = pond
    stray = "# Unused\n"
    obj = ask(read(), read(NOTES_TEXT, path=CHECKLIST, do="Read the notes"))
    status, data = _post(base_url, token, obj, {sha(PLAN_TEXT): PLAN_TEXT, sha(stray): stray})
    assert (status, data) == (400, {"error": "invalid documents", "details": [
        "steps[1].doc: no document with this hash", f"docs.{sha(stray)}: not used by any step or link"]})
    assert _counts(srv) == {"items": 0, "revisions": 0, "docs": 0, "ask_docs": 0}
    assert srv.service.telegram.sent_messages == []


def test_a_malformed_docs_member_is_one_400(pond):
    srv, base_url, token, _ = pond
    status, data = _post(base_url, token, sent={"NOT-A-HASH": PLAN_TEXT})
    assert (status, data) == (400, {"error": "invalid documents", "details": [SHAPE]})
    assert "NOT-A-HASH" not in json.dumps(data)
    assert _counts(srv)["items"] == 0


def test_a_telegram_failure_stores_no_document(pond, monkeypatch):
    srv, base_url, token, _ = pond

    def boom(*args, **kwargs):
        raise TelegramError("sendMessage 403: bot was blocked by the user")

    monkeypatch.setattr(srv.telegram, "send_message", boom)
    assert _post(base_url, token, sent={sha(PLAN_TEXT): PLAN_TEXT})[0] == 502
    assert _counts(srv) == {"items": 0, "revisions": 0, "docs": 0, "ask_docs": 0}


def test_a_deduplicated_post_stores_and_links_nothing(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    again = ask(read(NOTES_TEXT))
    status, data = _post(base_url, token, again, {sha(NOTES_TEXT): NOTES_TEXT})
    assert status == 200 and data["deduped"] is True and data["item"]["id"] == item_id
    assert _counts(srv) == {"items": 1, "revisions": 1, "docs": 1, "ask_docs": 1}
    assert srv.service.store.get_doc(sha(NOTES_TEXT)) is None


def test_the_instructables_own_errors_win(pond):
    srv, base_url, token, _ = pond
    srv.service.store.delete_setting("docs_enabled")
    bad = ask({"do": "Read the plan", "file": "notes/plan.md", "doc": ref(PLAN_TEXT)})
    status, data = _post(base_url, token, bad, {sha(PLAN_TEXT): PLAN_TEXT})
    assert (status, data) == (400, {"error": "invalid instructable",
                                    "details": ["steps[0].file: must be an absolute path"]})


def test_an_amend_reuses_a_held_document_without_resending_it(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Read the whole plan"
    status, data = _amend(base_url, token, item_id, base, new)
    assert status == 200, data
    assert data["revision"]["changed"] == [{"id": "s1", "path": "1", "fields": ["do"]}]
    assert _counts(srv)["docs"] == 1 and _counts(srv)["ask_docs"] == 1


def test_an_amend_that_changes_only_a_document_names_that_step(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = ref(NOTES_TEXT)
    status, data = _amend(base_url, token, item_id, base, new, {sha(NOTES_TEXT): NOTES_TEXT})
    assert status == 200, data
    assert data["revision"]["rev"] == 2
    assert data["revision"]["changed"] == [{"id": "s1", "path": "1", "fields": ["doc"]}]
    store = srv.service.store
    assert store.ask_docs(item_id) == {sha(PLAN_TEXT), sha(NOTES_TEXT)}    # the old one stays linked
    assert store.doc_status(item_id, sha(PLAN_TEXT)) == "kept"


def test_an_amend_with_a_document_neither_sent_nor_held(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = ref(NOTES_TEXT)
    before = _counts(srv)
    assert _amend(base_url, token, item_id, base, new) == (
        400, {"error": "invalid documents", "details": ["steps[0].doc: no document with this hash"]})
    new["steps"][0]["doc"] = {**ref(PLAN_TEXT), "bytes": 4}
    assert _amend(base_url, token, item_id, base, new) == (
        400, {"error": "invalid documents", "details": ["steps[0].doc.bytes: does not match the document"]})
    assert _counts(srv) == before


def test_a_correction_refused_after_its_documents_were_given_leaves_nothing(pond):
    """A correction that loses the compare-and-swap inside its transaction (another
    revision landed first) stores and links none of the documents it carried."""
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    clock.advance(60)
    store = srv.service.store
    real = store.amend_tx

    def racing(item, **kwargs):
        rival = dataclasses.replace(kwargs["new"], hash="f" * 64, reason="rival")
        real(item, base_rev=kwargs["base_rev"], base_hash=kwargs["base_hash"], new=rival, event_text="rival")
        return real(item, **kwargs)

    store.amend_tx = racing
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = ref(NOTES_TEXT)
    status, data = _amend(base_url, token, item_id, base, new, {sha(NOTES_TEXT): NOTES_TEXT})
    assert status == 409 and data["error"] == "stale revision"
    assert store.ask_docs(item_id) == {sha(PLAN_TEXT)}
    assert store.get_doc(sha(NOTES_TEXT)) is None
    assert _counts(srv)["docs"] == 1 and _counts(srv)["ask_docs"] == 1


def test_an_append_adds_a_document(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, base, [read(NOTES_TEXT, path=CHECKLIST)],
                           {sha(NOTES_TEXT): NOTES_TEXT})
    assert status == 200, data
    store = srv.service.store
    assert store.ask_docs(item_id) == {sha(PLAN_TEXT), sha(NOTES_TEXT)}
    assert store.get_doc(sha(NOTES_TEXT)) == NOTES_TEXT.encode("utf-8")
    assert _body(srv, item_id)["steps"][-1]["doc"] == ref(NOTES_TEXT)


def test_append_errors_name_the_requests_steps(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    clock.advance(15)
    before = _counts(srv)
    status, data = _append(base_url, token, item_id, base, [{"do": "Plain"}, read(NOTES_TEXT, path=CHECKLIST)])
    assert (status, data) == (400, {"error": "invalid documents",
                                    "details": ["steps[1].doc: no document with this hash"]})
    assert _counts(srv) == before


def test_the_switch_off_refuses_any_document(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    srv.service.store.delete_setting("docs_enabled")
    off = (409, {"error": "documents not enabled"})
    before = _counts(srv)
    plain = ask({"do": "Plain"}, title="Something else")
    assert _post(base_url, token, plain, {}) == off                      # a docs member, even empty
    assert _post(base_url, token, ask(title="Other")) == off             # a doc, no docs member
    assert _post(base_url, token, ask(title="Third"), {sha(PLAN_TEXT): PLAN_TEXT}) == off
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = ref(NOTES_TEXT)
    assert _amend(base_url, token, item_id, base, new, {sha(NOTES_TEXT): NOTES_TEXT}) == off
    assert _append(base_url, token, item_id, base, [read(NOTES_TEXT)], {sha(NOTES_TEXT): NOTES_TEXT}) == off
    assert _counts(srv) == before
    assert _post(base_url, token, plain, None, docs=None)[0] == 201      # null is none


# --- with the switch off, an ask that holds documents can still be added to (W5) -----------

OFF = (409, {"error": "documents not enabled"})


@pytest.fixture
def held_off(pond):
    """An ask posted with PLAN_TEXT while the switch was on; then the switch is off."""
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token)
    srv.service.store.delete_setting("docs_enabled")
    clock.advance(60)
    return srv, base_url, token, clock, item_id, base


def test_switch_off_an_append_of_a_plain_step_to_an_ask_with_a_document(held_off):
    srv, base_url, token, _, item_id, base = held_off
    status, data = _append(base_url, token, item_id, base, [{"do": "Plain"}])
    assert status == 200, data
    assert _body(srv, item_id)["steps"][0]["doc"] == ref(PLAN_TEXT)


def test_switch_off_a_correction_that_keeps_the_document(held_off):
    srv, base_url, token, _, item_id, base = held_off
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Read the whole plan"
    status, data = _amend(base_url, token, item_id, base, new)
    assert status == 200, data
    assert _counts(srv)["docs"] == 1 and _counts(srv)["ask_docs"] == 1


def test_switch_off_a_correction_that_keeps_the_document_is_still_checked(held_off):
    srv, base_url, token, _, item_id, base = held_off
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = {**ref(PLAN_TEXT), "bytes": 4}
    assert _amend(base_url, token, item_id, base, new) == (
        400, {"error": "invalid documents", "details": ["steps[0].doc.bytes: does not match the document"]})


def test_switch_off_a_correction_that_changes_the_document(held_off):
    srv, base_url, token, _, item_id, base = held_off
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = ref(NOTES_TEXT)
    before = _counts(srv)
    assert _amend(base_url, token, item_id, base, new) == OFF
    assert _amend(base_url, token, item_id, base, new, {sha(NOTES_TEXT): NOTES_TEXT}) == OFF
    assert _counts(srv) == before


def test_switch_off_a_correction_that_adds_a_second_document(held_off):
    srv, base_url, token, _, item_id, base = held_off
    new = _body(srv, item_id)
    new["steps"].append(read(NOTES_TEXT, path=CHECKLIST, do="Read the notes"))
    assert _amend(base_url, token, item_id, base, new, {sha(NOTES_TEXT): NOTES_TEXT}) == OFF
    assert _append(base_url, token, item_id, base, [read(NOTES_TEXT, path=CHECKLIST)],
                   {sha(NOTES_TEXT): NOTES_TEXT}) == OFF


def test_switch_off_a_docs_member_is_refused_even_with_the_held_text(held_off):
    srv, base_url, token, _, item_id, base = held_off
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Read the whole plan"
    assert _amend(base_url, token, item_id, base, new, {sha(PLAN_TEXT): PLAN_TEXT}) == OFF
    assert _amend(base_url, token, item_id, base, new, {}) == OFF
    assert _append(base_url, token, item_id, base, [{"do": "Plain"}], {}) == OFF


def test_switch_off_a_null_doc_only_where_the_ask_already_has_one(pond):
    srv, base_url, token, clock = pond
    item_id, base = _posted(base_url, token, ask(read(), {"do": "Plain", "doc": None}))
    srv.service.store.delete_setting("docs_enabled")
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][1]["do"] = "Plainer"                      # keeps its doc: null in place
    status, data = _amend(base_url, token, item_id, base, new)
    assert status == 200, data
    base = data["revision"]
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"].append({"do": "New", "doc": None})        # a null doc where there was none
    assert _amend(base_url, token, item_id, base, new) == OFF
    assert _append(base_url, token, item_id, base, [{"do": "New", "doc": None}]) == OFF


def test_with_the_switch_off_an_ask_without_documents_amends_and_appends_as_before(pond):
    srv, base_url, token, clock = pond
    srv.service.store.delete_setting("docs_enabled")
    status, data = _post(base_url, token, ask({"do": "Plain"}))
    assert status == 201, data
    item_id, base = data["item"]["id"], data["revision"]
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Plainer"
    status, data = _amend(base_url, token, item_id, base, new)
    assert status == 200, data
    clock.advance(15)
    assert _append(base_url, token, item_id, data["revision"], [{"do": "More"}])[0] == 200


NULL_DOCS = {
    "step-with-file": ask({"do": "Read the plan", "file": PLAN, "doc": None}, title="Null on a file"),
    "step-without-file": ask({"do": "Plain", "doc": None}, title="Null on a plain step"),
    "substep": ask({"do": "Group", "substeps": [{"do": "Inner", "doc": None}]}, title="Null on a sub-step"),
    "url-link": ask({"do": "Plain"}, links=[{"label": "Site", "url": "https://example.com/plan", "doc": None}],
                    title="Null on a link"),
}


@pytest.mark.parametrize("name", list(NULL_DOCS))
def test_with_the_switch_off_a_null_doc_counts_as_a_doc(pond, name):
    """1.9.0 answered 400 `doc: unknown key`: no ask with a `doc` key may reach a device
    that does not know the key while the switch is off, not even a null one."""
    srv, base_url, token, _ = pond
    srv.service.store.delete_setting("docs_enabled")
    assert _post(base_url, token, NULL_DOCS[name]) == (409, {"error": "documents not enabled"})
    assert _counts(srv) == {"items": 0, "revisions": 0, "docs": 0, "ask_docs": 0}


@pytest.mark.parametrize("name", list(NULL_DOCS))
def test_with_the_switch_on_a_null_doc_is_no_document(pond, name):
    srv, base_url, token, _ = pond
    status, data = _post(base_url, token, NULL_DOCS[name])
    assert status == 201, data
    assert _counts(srv)["docs"] == 0 and _counts(srv)["ask_docs"] == 0


def test_with_the_switch_off_a_bug_with_a_null_doc_is_refused(bug_pond):
    srv, base_url, token, _, key = bug_pond
    srv.service.store.delete_setting("docs_enabled")
    doc = _bug_doc()
    doc["steps"].append({"do": "Plain", "doc": None})
    assert post_bug(base_url, token, key, doc=doc) == (409, {"error": "documents not enabled"})
    assert _counts(srv)["items"] == 0


_VECTORS = json.loads((Path(__file__).parent / "revision-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("enabled", [False, True], ids=["switch-off", "switch-on"])
def test_an_ask_without_documents_posts_exactly_as_before(pond, enabled):
    """The revision-vectors.json hash-rev1 case, posted as is: the same body bytes and hash."""
    srv, base_url, token, clock = pond
    if not enabled:
        srv.service.store.delete_setting("docs_enabled")
    case = next(c for c in _VECTORS if c["name"] == "hash-rev1")
    assert clock.now.isoformat(timespec="seconds") == case["at"] == T0.isoformat(timespec="seconds")
    status, data = post_ask(base_url, token, ask=None, instructable=json.loads(case["body"]), editKeyHash=EDIT_HASH)
    assert status == 201, data
    rev1 = srv.service.store.current_revision(data["item"]["id"])
    assert (rev1.body, rev1.hash) == (case["body"], case["hash"])
    assert data["revision"]["hash"] == case["hash"]
    assert _counts(srv)["docs"] == 0 and _counts(srv)["ask_docs"] == 0


def test_version_lists_docs_only_when_the_switch_is_on(pond):
    srv, base_url, token, _ = pond
    assert hcall(base_url, "/version", token)[1]["features"] == [*FEATURES, "docs"]
    srv.service.store.delete_setting("docs_enabled")
    assert hcall(base_url, "/version", token)[1]["features"] == list(FEATURES)
    assert "docs" not in FEATURES


def test_a_body_over_the_cap_is_refused_and_stores_nothing(pond):
    srv, base_url, token, _ = pond
    texts = [c * MAX_DOC_BYTES for c in "abcd"]          # over 1 MiB as JSON
    obj = ask(*[read(t, path=f"/var/tmp/{i}.md") for i, t in enumerate(texts)])
    raw = json.dumps({"project": "Orbit", "context": "personal", "instructable": obj,
                      "docs": {sha(t): t for t in texts}}).encode("utf-8")
    assert len(raw) > 1 << 20
    request = (f"POST /asks HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
               ).encode("ascii") + raw[:4096]
    assert raw_exchange(srv.port, request, timeout=5) == (413, {"error": "body too large"})
    assert _counts(srv) == {"items": 0, "revisions": 0, "docs": 0, "ask_docs": 0}


def test_no_document_text_is_ever_logged_or_served(pond, caplog):
    srv, base_url, token, clock = pond
    marker = "Synthetic marker text for the leak check"
    text = f"# Plan\n\n{marker}\n"
    caplog.set_level(logging.DEBUG)
    item_id, base = _posted(base_url, token, ask(read(text)), {sha(text): text})
    bad = _post(base_url, token, ask(read(text), title="Bad"), {sha(text): text + "x"})[1]
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Read it again"
    refused = _amend(base_url, token, item_id, {"rev": 1, "hash": "0" * 64}, new, {sha(text): text})[1]
    seen = [json.dumps(bad), json.dumps(refused)]
    for path in ("/asks", f"/asks/{item_id}", "/events"):
        for protocol in (None, 5, 6):
            seen.append(json.dumps(hcall(base_url, path, token, protocol=protocol)[1]))
    seen.append(json.dumps(hcall(base_url, "/health", None)[1]))
    with urllib.request.urlopen(f"{base_url}/i/{item_id}", timeout=10) as resp:
        seen.append(resp.read().decode("utf-8"))
    seen.append(json.dumps(srv.service.telegram.calls, default=str))
    seen += [r.getMessage() for r in caplog.records]
    assert not [s for s in seen if marker in s]
    assert srv.service.store.get_doc(sha(text)) == text.encode("utf-8")      # it is stored, just never shown


# --- a Hook bug takes no documents (fix round 1) -----------------------------------------

@pytest.fixture
def bug_pond(pond):
    srv, base_url, token, clock = pond
    return srv, base_url, token, clock, _approved_key(srv)


def _bug_with_doc(text=PLAN_TEXT):
    doc = _bug_doc()
    doc["steps"].append(read(text))
    return doc


def test_a_bug_with_documents_is_refused_while_the_switch_is_off(bug_pond):
    srv, base_url, token, _, key = bug_pond
    srv.service.store.delete_setting("docs_enabled")
    off = (409, {"error": "documents not enabled"})
    assert post_bug(base_url, token, key, doc=_bug_with_doc()) == off
    assert post_bug(base_url, token, key, docs={}) == off
    assert post_bug(base_url, token, key, doc=_bug_with_doc(), docs={sha(PLAN_TEXT): PLAN_TEXT}) == off
    assert _counts(srv) == {"items": 0, "revisions": 0, "docs": 0, "ask_docs": 0}


def test_a_bug_never_holds_a_document_while_the_switch_is_on(bug_pond):
    srv, base_url, token, _, key = bug_pond
    invalid = "invalid documents"
    assert post_bug(base_url, token, key, doc=_bug_with_doc()) == (
        400, {"error": invalid, "details": ["steps[1].doc: no document with this hash"]})
    assert post_bug(base_url, token, key, doc=_bug_with_doc(), docs={sha(PLAN_TEXT): PLAN_TEXT}) == (
        400, {"error": invalid, "details": ["steps[1].doc: no document with this hash",
                                            f"docs.{sha(PLAN_TEXT)}: not used by any step or link"]})
    assert post_bug(base_url, token, key, docs={sha(PLAN_TEXT): PLAN_TEXT}) == (
        400, {"error": invalid, "details": [f"docs.{sha(PLAN_TEXT)}: not used by any step or link"]})
    assert post_bug(base_url, token, key, docs={"NOT-A-HASH": PLAN_TEXT}) == (400, {"error": invalid,
                                                                                   "details": [SHAPE]})
    # A malformed member is the shape error alone, as for an ask, even beside a `doc`.
    assert post_bug(base_url, token, key, doc=_bug_with_doc(), docs={"NOT-A-HASH": PLAN_TEXT}) == (
        400, {"error": invalid, "details": [SHAPE]})
    # A well-formed text is never kept, so it is never checked: not its hash, not a secret.
    secret = "sk-" + "ant-" + "api03-" + "Q" * 32       # built at runtime: no literal key in the repo
    for text in (f"# Setup\n\nexport KEY={secret}\n", "\x00", ""):
        assert post_bug(base_url, token, key, docs={sha(PLAN_TEXT): text}) == (
            400, {"error": invalid, "details": [f"docs.{sha(PLAN_TEXT)}: not used by any step or link"]})
    big = "x" * (MAX_DOCS_BYTES // 2)
    assert post_bug(base_url, token, key, docs={sha(PLAN_TEXT): big, sha(NOTES_TEXT): big + "y"}) == (
        400, {"error": invalid, "details": sorted(f"docs.{h}: not used by any step or link"
                                                  for h in (sha(PLAN_TEXT), sha(NOTES_TEXT)))})
    assert _counts(srv) == {"items": 0, "revisions": 0, "docs": 0, "ask_docs": 0}
    status, data = post_bug(base_url, token, key, docs={})                 # an empty member: nothing to store
    assert status == 201, data
    assert _counts(srv)["docs"] == 0 and _counts(srv)["ask_docs"] == 0


def test_a_repeated_bug_with_a_document_is_refused_before_its_occurrence(bug_pond):
    srv, base_url, token, _, key = bug_pond
    status, first = post_bug(base_url, token, key)
    assert status == 201, first
    assert post_bug(base_url, token, key, doc=_bug_with_doc())[0] == 400
    assert srv.service.store.bug_meta(first["item"]["id"])["count"] == 1


@pytest.mark.parametrize("enabled", [False, True], ids=["switch-off", "switch-on"])
def test_a_plain_bug_is_stored_exactly_as_before(bug_pond, enabled):
    srv, base_url, token, clock, key = bug_pond
    if not enabled:
        srv.service.store.delete_setting("docs_enabled")
    status, data = post_bug(base_url, token, key, docs=None)
    assert status == 201, data
    rev1 = srv.service.store.current_revision(data["item"]["id"])
    clean, problems = validate(_bug_doc())
    assert problems == []
    body = canonical(assign_ids(clean)[0])
    at = clock.now.isoformat(timespec="seconds")
    assert (rev1.body, rev1.hash) == (body, revision_hash(1, "", at, "", body))
    assert data["revision"]["hash"] == rev1.hash


# --- Telegram's change summary names a document (W6) --------------------------------------

def test_a_correction_of_only_a_document_reads_document_on_telegram(pond):
    srv, base_url, token, clock = pond
    marker = "Synthetic marker for the summary check"
    text = f"# Notes\n\n{marker}\n"
    item_id, base = _posted(base_url, token)
    clock.advance(60)
    new = _body(srv, item_id)
    new["steps"][0]["doc"] = ref(text)
    status, data = _amend(base_url, token, item_id, base, new, {sha(text): text})
    assert status == 200, data
    calls = json.dumps(srv.service.telegram.calls, default=str, ensure_ascii=False)
    assert "Changed: step 1 (document)." in calls
    assert "(doc)" not in calls
    assert marker not in calls and sha(text) not in calls
