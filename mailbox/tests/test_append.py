"""Steps added to an open ask (Pond 1.9.0): the revision kind in its side table,
the append transaction and the Pond's advance of the version in force."""
import json

import pytest

from mailbox.store import AskNotOpen, StaleRevision, Store
from mailbox.tests.test_store import AT1, AT2, _ask, _rev


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "append.db")
    s.open()
    yield s
    s.close()


def _appended(item, base, at=AT2):
    body = json.loads(base.body)
    body["steps"].append({"do": "one more", "id": f"r{base.rev + 1}-1"})
    return _rev(item.id, base.rev + 1, base.hash, at, "more steps", body)


def _tx(store, item, base, *, advance, kind="append"):
    new = _appended(item, base)
    return store.append_tx(item.id, base_rev=base.rev, base_hash=base.hash, new=new, kind=kind,
                           advance=advance, event_text='{"note":"more steps"}')


def _counts(store, item_id):
    tables = ("revisions", "revision_meta", "accepts", "event_meta")
    out = {t: store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    out["events"] = len(store.events_for_item(item_id))
    out["updated"] = store.get_item(item_id).updated
    return out


# --- the store (Task 4) --------------------------------------------------------

def test_append_tx_advances(store):
    item, rev1 = _ask(store)
    new = _tx(store, item, rev1, advance=True)
    assert new.rev == 2 and store.current_revision(item.id) == new
    assert store.accepted_rev(item.id) == 2
    assert store.revision_kinds(item.id) == {2: "append"}
    auth = store.conn.execute("SELECT auth FROM accepts WHERE item_id = ? AND rev = 2", (item.id,)).fetchone()
    assert auth == ("append",)
    events = store.events_for_item(item.id)
    assert [(e.kind, e.actor, e.rev) for e in events[-2:]] == [("revised", "session", 2), ("accepted", "daemon", 2)]
    assert events[-1].auth == "append"
    assert store.get_item(item.id).updated == AT2


def test_append_tx_without_advance_leaves_the_version_in_force(store):
    item, rev1 = _ask(store)
    _tx(store, item, rev1, advance=False)
    assert store.accepted_rev(item.id) == 1
    assert [e.kind for e in store.events_for_item(item.id)][-1] == "revised"


def test_append_tx_closed(store):
    item, rev1 = _ask(store)
    store.update_item(item.id, {"status": "done"})
    before = _counts(store, item.id)
    with pytest.raises(AskNotOpen):
        _tx(store, item, rev1, advance=True)
    assert _counts(store, item.id) == before


def test_append_tx_stale(store):
    item, rev1 = _ask(store)
    rev2 = _tx(store, item, rev1, advance=True)
    before = _counts(store, item.id)
    with pytest.raises(StaleRevision) as info:
        _tx(store, item, rev1, advance=True)
    assert info.value.current == rev2
    assert _counts(store, item.id) == before


def test_amend_tx_records_its_kind(store):
    item, rev1 = _ask(store)
    body = json.loads(rev1.body)
    body["steps"][0]["do"] = "changed"
    rev2 = _rev(item.id, 2, rev1.hash, AT2, "fix", body)
    store.amend_tx(item.id, base_rev=1, base_hash=rev1.hash, new=rev2, event_text="fix")
    rev3 = _appended(item, rev2, at="2026-10-29T10:10:00+00:00")
    store.amend_tx(item.id, base_rev=2, base_hash=rev2.hash, new=rev3, event_text="more", kind="append",
                   advance=False)
    assert store.revision_kinds(item.id) == {2: "correction", 3: "append"}
    assert store.accepted_rev(item.id) == 1


def test_revision_kinds_of_an_ask_without_rows(store):
    item, _ = _ask(store)
    assert store.revision_kinds(item.id) == {}


# --- POST /asks/{id}/append and the switch (Task 5) ----------------------------

import copy  # noqa: E402

from mailbox.revisions import canonical as _canonical, is_append, revision_hash  # noqa: E402
from mailbox.tests.test_server import EDIT_HASH, EDIT_KEY, _Clock, _running, hcall, post_ask  # noqa: E402

DOC = {
    "v": 1, "title": "Rotate the staging key",
    "steps": [
        {"do": "Open the settings", "where": "Console"},
        {"do": "Pick the target", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
    ],
    "verify": {"do": "A deploy goes through", "code": "curl -sI https://pond.example.test/health"},
}
MORE = [{"do": "Delete the old key"}]


@pytest.fixture
def pond(tmp_path):
    """A running Pond with a pinned clock and corrections OFF: appends need no switch."""
    for srv, base_url, token in _running(tmp_path):
        clock = _Clock()
        srv.service.utcnow = clock
        yield srv, base_url, token, clock


def _post(base_url, token, doc=DOC):
    status, data = post_ask(base_url, token, ask=None, instructable=doc, editKeyHash=EDIT_HASH)
    assert status == 201, data
    return data["item"]["id"], data["revision"]


def _append(base_url, token, item_id, base, steps=MORE, *, key=EDIT_KEY, note="Two more checks", **extra):
    body = {"editKey": key, "baseRev": base["rev"], "baseHash": base["hash"], "note": note, "steps": steps,
            **extra}
    return hcall(base_url, f"/asks/{item_id}/append", token, method="POST", body=body, protocol=5)


def _amend(base_url, token, item_id, base, change, reason="a fix"):
    detail = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    doc = json.loads(detail["item"]["revision"]["body"])
    change(doc)
    body = {"editKey": EDIT_KEY, "baseRev": base["rev"], "baseHash": base["hash"], "reason": reason,
            "instructable": doc}
    return hcall(base_url, f"/asks/{item_id}/amend", token, method="POST", body=body, protocol=5)


def _stored(srv, item_id):
    return len(srv.service.store.revisions(item_id))


def test_append_happy(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, base)
    assert status == 200, data
    revision = data["revision"]
    assert (revision["rev"], revision["kind"], revision["added"]) == (2, "append", [{"id": "r2-1", "path": "3"}])
    store = srv.service.store
    assert store.accepted_rev(item_id) == 2
    assert store.revision_kinds(item_id) == {2: "append"}
    rev1, rev2 = store.revisions(item_id)
    assert revision["hash"] == rev2.hash == revision_hash(2, rev1.hash, rev2.at, "Two more checks", rev2.body)
    assert rev2.reason == "Two more checks"
    body = json.loads(rev2.body)
    assert is_append(json.loads(rev1.body), body)
    assert list(body)[-1] == "verify" and body["verify"] == DOC["verify"]     # canonical: verify sorts last
    assert body["steps"][-1] == {"do": "Delete the old key", "id": "r2-1"}
    detail = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    assert detail["item"]["revision"]["kind"] == "append"
    assert detail["item"]["revision"]["acceptedRev"] == 2
    assert [r.get("kind") for r in detail["revisions"]] == [None, "append"]
    assert data["item"]["id"] == item_id


def test_append_check_order(pond):
    srv, base_url, token, clock = pond
    store = srv.service.store
    item_id, base = _post(base_url, token)
    clock.advance(15)
    path = f"/asks/{item_id}/append"
    ok = {"editKey": EDIT_KEY, "baseRev": 1, "baseHash": base["hash"], "note": "n", "steps": MORE}
    assert hcall(base_url, path, None, method="POST", body=ok) == (401, {"error": "unauthorized"})
    assert hcall(base_url, "/asks/nope/append", token, method="POST", body=ok) == (404, {"error": "not found"})
    store.set_setting("appends_disabled", "1")
    assert hcall(base_url, path, token, method="POST", body=ok) == (409, {"error": "appends not enabled"})
    store.delete_setting("appends_disabled")
    assert hcall(base_url, path, token, method="POST", raw=b"{") == (400, {"error": "invalid json"})
    assert hcall(base_url, path, token, method="POST", body={"editKey": EDIT_KEY}) == (400, {"error": "missing fields"})
    plain = post_ask(base_url, token, ask=None, instructable={**DOC, "title": "No key"})[1]
    assert hcall(base_url, f"/asks/{plain['item']['id']}/append", token, method="POST",
                 body={**ok, "baseHash": plain["revision"]["hash"]}) == (403, {"error": "not editable"})
    assert hcall(base_url, path, token, method="POST", body={**ok, "editKey": "x" * 43}) == (
        403, {"error": "wrong edit key"})
    assert hcall(base_url, path, token, method="POST", body={**ok, "baseHash": "0" * 64}) == (
        409, {"error": "stale revision", "rev": 1, "hash": base["hash"]})
    status, data = hcall(base_url, path, token, method="POST", body={**ok, "note": None, "steps": []})
    assert (status, data) == (400, {"error": "invalid steps", "details": ["note: missing", "steps: 1 to 10"]})
    assert _stored(srv, item_id) == 1


def test_append_to_closed_ask(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST", body={"verdict": "done", "actor": "session"})
    clock.advance(15)
    assert _append(base_url, token, item_id, base) == (409, {"error": "ask is closed"})
    assert _stored(srv, item_id) == 1


def test_append_over_step_limit(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, base, [{"do": f"x{n}"} for n in range(11)])
    assert (status, data) == (400, {"error": "invalid steps", "details": ["steps: 1 to 10"]})
    doc = {**DOC, "steps": [{"do": f"Step {n}"} for n in range(1, 20)]}
    item_id, base = _post(base_url, token, {**doc, "title": "Nineteen"})
    clock.advance(15)
    status, data = _append(base_url, token, item_id, base, [{"do": "20"}, {"do": "21"}])
    assert (status, data) == (400, {"error": "invalid steps",
                                    "details": ["steps: must be a list of 1 to 20 steps"]})
    assert _stored(srv, item_id) == 1


def test_append_names_the_request_in_its_details(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, base,
                           [{"do": "fine"}, {"do": "Set it", "code": "export DEPLOY_KEY=ghp_example"}, {"id": "s1", "do": "dup"}])
    assert (status, data) == (400, {"error": "invalid steps",
                                    "details": ["steps[2].id: used before", "steps[1].code: secret-like value"]})
    assert _stored(srv, item_id) == 1


def test_append_limit_reached(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    for n in range(10):
        clock.advance(15)
        status, data = _append(base_url, token, item_id, base, [{"do": f"more {n}"}])
        assert status == 200, data
        base = data["revision"]
    clock.advance(15)
    assert _append(base_url, token, item_id, base) == (409, {"error": "append limit reached"})
    assert _stored(srv, item_id) == 11


def test_append_not_gated_by_corrections(pond):
    srv, base_url, token, clock = pond
    assert not srv.service.corrections_enabled()
    item_id, base = _post(base_url, token)
    clock.advance(15)
    assert _append(base_url, token, item_id, base)[0] == 200


def test_appends_switch_off(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    clock.advance(15)
    srv.service.store.set_setting("appends_disabled", "1")
    assert _append(base_url, token, item_id, base) == (409, {"error": "appends not enabled"})
    assert _stored(srv, item_id) == 1
    srv.service.store.delete_setting("appends_disabled")
    assert _append(base_url, token, item_id, base)[0] == 200


def test_append_waits_15s_after_amend(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    clock.advance(14)
    assert _append(base_url, token, item_id, base) == (429, {"error": "too soon", "retryAfter": 1})
    clock.advance(47)
    status, data = _amend(base_url, token, item_id, base, lambda d: d["steps"][0].update(do="Open the console"))
    assert status == 200, data
    base = data["revision"]
    clock.advance(10)
    assert _append(base_url, token, item_id, base) == (429, {"error": "too soon", "retryAfter": 5})
    assert _stored(srv, item_id) == 2
    clock.advance(5)
    assert _append(base_url, token, item_id, base)[0] == 200


def test_amend_limit_counts_corrections_only(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    for n in range(10):
        clock.advance(15)
        status, data = _append(base_url, token, item_id, base, [{"do": f"more {n}"}])
        assert status == 200, data
        base = data["revision"]
    for n in range(10):
        clock.advance(60)
        status, data = _amend(base_url, token, item_id, base, lambda d, n=n: d["steps"][0].update(do=f"try {n}"))
        assert status == 200, data
        assert "kind" not in data["revision"] or data["revision"]["kind"] == "correction"
        base = data["revision"]
    assert base["rev"] == 21
    clock.advance(60)
    assert _amend(base_url, token, item_id, base, lambda d: d["steps"][0].update(do="one too many")) == (
        409, {"error": "revision limit reached"})


def test_an_amend_that_only_adds_steps_is_an_append(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    clock.advance(60)
    status, data = _amend(base_url, token, item_id, base, lambda d: d["steps"].append({"do": "one more"}))
    assert status == 200, data
    store = srv.service.store
    assert store.revision_kinds(item_id) == {2: "append"}
    assert store.accepted_rev(item_id) == 2


def test_append_does_not_advance_past_unaccepted_correction(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    clock.advance(60)
    status, data = _amend(base_url, token, item_id, base, lambda d: d["steps"][0].update(do="Open the console"))
    assert status == 200, data
    clock.advance(15)
    status, data = _append(base_url, token, item_id, data["revision"])
    assert status == 200, data
    store = srv.service.store
    assert data["revision"]["kind"] == "append"
    assert store.revision_kinds(item_id) == {2: "correction", 3: "append"}
    assert store.accepted_rev(item_id) == 1


def test_old_client_gets_stub(pond):
    srv, base_url, token, clock = pond
    item_id, base = _post(base_url, token)
    clock.advance(15)
    assert _append(base_url, token, item_id, base)[0] == 200
    item = hcall(base_url, f"/asks/{item_id}", token, protocol=4)[1]["item"]
    instructable = item["payload"]["instructable"]
    assert instructable["steps"] == [{"do": "Update Hook: menu bar › Check for Updates…", "id": "s1"}]
    assert "revision" not in item


def test_a_correction_keeps_todays_wire_bytes(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    clock.advance(60)
    assert _amend(base_url, token, item_id, base, lambda d: d["steps"][0].update(do="Open the console"))[0] == 200
    detail = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    assert set(detail["item"]["revision"]) == {"rev", "hash", "prev", "at", "reason", "body", "acceptedRev"}
    assert all(set(r) == {"rev", "hash", "prev", "at", "reason", "body"} for r in detail["revisions"])


# --- acts on an older version (Task 6) -----------------------------------------

from mailbox.service import Corrected, StepsAdded  # noqa: E402
from mailbox.tests.test_owner import tap  # noqa: E402


def _appended_ask(srv, base_url, token, clock, doc=DOC):
    item_id, rev1 = _post(base_url, token, doc)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, rev1)
    assert status == 200, data
    return item_id, rev1, data["revision"]


def _answer(base_url, token, item_id, rev, step="2", picked="Prod"):
    body = {"answers": [{"step": step, "picked": [picked]}], "rev": rev["rev"], "hash": rev["hash"]}
    return hcall(base_url, f"/asks/{item_id}/answers", token, method="POST", body=body)


def _resolve(base_url, token, item_id, verdict, rev):
    body = {"verdict": verdict, "actor": "owner", "rev": rev["rev"], "hash": rev["hash"]}
    return hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST", body=body)


def test_old_rev_answer_counts_and_done_refused(pond):
    srv, base_url, token, clock = pond
    item_id, rev1, rev2 = _appended_ask(srv, base_url, token, clock)
    status, data = _answer(base_url, token, item_id, rev1)
    assert status == 200, data
    event = srv.service.store.events_for_item(item_id)[-1]
    assert (event.kind, event.rev) == ("answer", 2)
    assert json.loads(event.text) == {"step": "2", "id": "s2", "picked": ["Prod"]}
    assert _resolve(base_url, token, item_id, "done", rev1) == (
        409, {"error": "steps_added", "rev": 2, "hash": rev2["hash"]})
    assert srv.service.store.get_item(item_id).status == "open"
    status, data = _resolve(base_url, token, item_id, "declined", rev1)
    assert status == 200, data
    assert srv.service.store.get_item(item_id).status == "dismissed"


def test_done_on_the_current_rev_after_an_append_closes(pond):
    srv, base_url, token, clock = pond
    item_id, _, rev2 = _appended_ask(srv, base_url, token, clock)
    assert _answer(base_url, token, item_id, rev2)[0] == 200
    assert _resolve(base_url, token, item_id, "done", rev2)[0] == 200
    assert srv.service.store.get_item(item_id).status == "done"


def test_an_old_rev_act_with_the_wrong_hash_is_refused(pond):
    srv, base_url, token, clock = pond
    item_id, rev1, rev2 = _appended_ask(srv, base_url, token, clock)
    count = len(srv.service.store.events_for_item(item_id))
    for digest in ("0" * 64, rev2["hash"]):           # rev 1 with any hash but rev 1's
        status, data = _answer(base_url, token, item_id, {"rev": 1, "hash": digest})
        assert (status, data["error"]) == (409, "ask was corrected")
    assert len(srv.service.store.events_for_item(item_id)) == count


def test_an_old_rev_answer_on_a_step_it_does_not_have_is_refused(pond):
    srv, base_url, token, clock = pond
    doc = {**DOC, "title": "Ask more", "steps": DOC["steps"][:1]}
    item_id, rev1 = _post(base_url, token, doc)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, rev1,
                           [{"do": "Pick", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}}])
    assert status == 200, data
    status, data = _answer(base_url, token, item_id, rev1)
    assert (status, data["error"]) == (409, "ask was corrected")
    assert _answer(base_url, token, item_id, {"rev": data["rev"], "hash": data["hash"]})[0] == 200


def test_old_rev_answer_on_changed_step_refused(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    item_id, rev1 = _post(base_url, token)
    clock.advance(60)
    status, data = _amend(base_url, token, item_id, rev1,
                          lambda d: d["steps"][1]["question"].update(prompt="Which env?"))
    assert status == 200, data
    rev2 = data["revision"]
    srv.service.accept(item_id, 2, rev2["hash"], "owner-key")
    clock.advance(15)
    status, data = _append(base_url, token, item_id, rev2)
    assert status == 200, data
    rev3 = data["revision"]
    assert srv.service.store.accepted_rev(item_id) == 3
    count = len(srv.service.store.events_for_item(item_id))
    assert _answer(base_url, token, item_id, rev1) == (
        409, {"error": "ask was corrected", "rev": 3, "hash": rev3["hash"], "acceptedRev": 3})
    assert len(srv.service.store.events_for_item(item_id)) == count
    status, data = _answer(base_url, token, item_id, rev2)                 # rev 2 → 3 is an append
    assert status == 200, data
    assert srv.service.store.events_for_item(item_id)[-1].rev == 3


def test_old_telegram_tap_counts(pond):
    srv, base_url, token, clock = pond
    item_id, _, _ = _appended_ask(srv, base_url, token, clock)
    assert tap(srv.service, f"a:{item_id}:2:0") == "2: Staging"
    event = srv.service.store.events_for_item(item_id)[-1]
    assert (event.kind, event.rev, event.auth) == ("answer", 2, "telegram")
    assert json.loads(event.text)["picked"] == ["Staging"]
    assert tap(srv.service, f"d:{item_id}") == "New steps were added. Open the ask to see them."
    assert srv.service.store.get_item(item_id).status == "open"


def test_progress_rev1_after_append(pond):
    srv, base_url, token, clock = pond
    doc = {**DOC, "title": "With ticks", "done": ["steps"]}
    item_id, rev1, _ = _appended_ask(srv, base_url, token, clock, doc)
    service = srv.service
    service.progress(item_id, ["s1"], None, rev=1, hash=rev1["hash"], auth="token")
    event = service.store.events_for_item(item_id)[-1]
    assert (event.kind, event.rev) == ("progress", 2)
    assert json.loads(event.text) == {"tick": ["s1"], "untick": []}
    with pytest.raises(Corrected):
        service.progress(item_id, ["r2-1"], None, rev=1, hash=rev1["hash"], auth="token")   # not in rev 1


def test_steps_added_is_a_corrected():
    assert issubclass(StepsAdded, Corrected)       # every old `except Corrected` still refuses it


# --- an amend that only adds steps keeps the append gates (security fix) -------
# A Hook seals an append-shaped revision and accepts it as the owner, so an amend that
# only adds steps but fails a gate is refused with the append route's error: stored as
# a correction it would get past the switch and the limits through that seal.

def _add_one(d):
    d["steps"].append({"do": "one more"})


def _nothing_stored(store, item_id, rev):
    assert [r.rev for r in store.revisions(item_id)] == list(range(1, rev + 1))
    assert not any(e.kind == "revised" and e.rev and e.rev > rev for e in store.events_for_item(item_id))


def test_a_pure_append_amend_is_refused_when_appends_are_off(pond):
    srv, base_url, token, clock = pond
    store = srv.service.store
    store.set_setting("corrections_enabled", "1")
    store.set_setting("appends_disabled", "1")
    item_id, base = _post(base_url, token)
    clock.advance(60)
    assert _amend(base_url, token, item_id, base, _add_one) == (409, {"error": "appends not enabled"})
    _nothing_stored(store, item_id, 1)
    assert store.accepted_rev(item_id) == 1
    assert not any(e.kind == "accepted" for e in store.events_for_item(item_id))
    # A real correction is still a correction.
    status, data = _amend(base_url, token, item_id, base, lambda d: d["steps"][0].update(do="Open the console"))
    assert status == 200, data
    assert store.revision_kinds(item_id) == {2: "correction"}


def test_a_pure_append_amend_is_refused_once_appends_are_used_up(pond):
    srv, base_url, token, clock = pond
    store = srv.service.store
    store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    for n in range(10):
        clock.advance(15)
        status, data = _append(base_url, token, item_id, base, [{"do": f"more {n}"}])
        assert status == 200, data
        base = data["revision"]
    assert store.accepted_rev(item_id) == 11
    clock.advance(60)
    assert _amend(base_url, token, item_id, base, _add_one) == (409, {"error": "append limit reached"})
    _nothing_stored(store, item_id, 11)
    assert store.accepted_rev(item_id) == 11
    # Corrections keep their own count: 10 of them still go through, then the limit.
    for n in range(10):
        clock.advance(60)
        status, data = _amend(base_url, token, item_id, base, lambda d, n=n: d["steps"][0].update(do=f"try {n}"))
        assert status == 200, data
        base = data["revision"]
    assert base["rev"] == 21
    clock.advance(60)
    assert _amend(base_url, token, item_id, base, lambda d: d["steps"][0].update(do="one too many")) == (
        409, {"error": "revision limit reached"})


def test_a_pure_append_amend_over_the_per_call_cap_is_refused(pond):
    srv, base_url, token, clock = pond
    store = srv.service.store
    store.set_setting("corrections_enabled", "1")
    item_id, base = _post(base_url, token)
    clock.advance(60)
    assert _amend(base_url, token, item_id, base,
                  lambda d: d["steps"].extend({"do": f"more {n}"} for n in range(11))) == (
        400, {"error": "invalid steps", "details": ["steps: 1 to 10"]})
    _nothing_stored(store, item_id, 1)
    assert store.accepted_rev(item_id) == 1
    item_id, base = _post(base_url, token, {**DOC, "title": "Ten more"})
    clock.advance(60)
    status, data = _amend(base_url, token, item_id, base,
                          lambda d: d["steps"].extend({"do": f"ten {n}"} for n in range(10)))
    assert status == 200, data
    assert store.revision_kinds(item_id) == {2: "append"}         # 10 is within the cap
    assert store.accepted_rev(item_id) == 2
