"""Tests for the revision-bound owner acts (Pond 1.1.0): answers and Done bound
to the version the owner accepted, accept, problem reports, and the owner
check on every owner route."""
import copy
import json
from datetime import timedelta

import pytest

from mailbox import owner
from mailbox.answers import current as current_answers
from mailbox.revisions import assign_ids
from mailbox.tests.test_owner import Clock, T0, http_approved, req, tap
from mailbox.tests.test_server import _running

EDIT_KEY = "k" * 43
EDIT_HASH = owner.key_hash(EDIT_KEY)
DOC = {
    "v": 1, "title": "Deploy the release",
    "steps": [
        {"do": "Build it", "code": "make build"},
        {"do": "Check the logs"},
        {"do": "Pick the target", "code": "deploy --env X",
         "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
    ],
}


@pytest.fixture
def pond(tmp_path):
    for running in _running(tmp_path):
        srv, base_url, token = running
        clock = Clock()
        srv.service.utcnow = clock
        srv.service.store.set_setting("corrections_enabled", "1")
        yield srv, base_url, token, clock


def post(base_url, token, doc=DOC):
    status, data = req(base_url, "/asks", token, method="POST",
                       body={"project": "Orbit", "context": "personal", "instructable": doc, "editKeyHash": EDIT_HASH})
    assert status == 201, data
    return data["item"]["id"], data["revision"]


def amend(base_url, token, clock, item_id, base, change):
    clock.now = clock.now + timedelta(seconds=61)
    # Read with X-Hook-Protocol: 5, else a corrected ask is the stub.
    doc = json.loads(req_detail(base_url, token, item_id)["item"]["revision"]["body"])
    change(doc)
    status, data = req(base_url, f"/asks/{item_id}/amend", token, method="POST",
                       body={"editKey": EDIT_KEY, "baseRev": base["rev"], "baseHash": base["hash"],
                             "reason": "a fix", "instructable": doc})
    assert status == 200, data
    return data["revision"]


def req_detail(base_url, token, item_id):
    import urllib.request
    request = urllib.request.Request(f"{base_url}/asks/{item_id}",
                                     headers={"Authorization": f"Bearer {token}", "X-Hook-Protocol": "5"})
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read())


def code_change(doc):
    doc["steps"][2]["code"] = "deploy --env Y"


def answer(base_url, token, item_id, *, key=None, **extra):
    return req(base_url, f"/asks/{item_id}/answers", token, method="POST", key=key,
               body={"answers": [{"step": "3", "picked": ["Prod"]}], **extra})


def corrected(rev):
    return {"error": "ask was corrected", "rev": rev["rev"], "hash": rev["hash"], "acceptedRev": 1}


# --- answers ---------------------------------------------------------------------


def test_answers_on_a_rev_1_ask_need_no_rev(pond):
    srv, base_url, token, _ = pond
    item_id, _ = post(base_url, token)
    assert answer(base_url, token, item_id)[0] == 200
    event = srv.service.store.events_for_item(item_id)[-1]
    assert json.loads(event.text) == {"step": "3", "id": "s3", "picked": ["Prod"]}
    assert (event.rev, event.auth) == (1, "token")


def test_an_answer_may_name_the_step_by_id(pond):
    srv, base_url, token, _ = pond
    item_id, _ = post(base_url, token)
    status, _ = req(base_url, f"/asks/{item_id}/answers", token, method="POST",
                    body={"answers": [{"step": "s3", "picked": ["Staging"]}]})
    assert status == 200
    assert json.loads(srv.service.store.events_for_item(item_id)[-1].text)["step"] == "3"


TWO_QUESTIONS = {
    "v": 1, "title": "Pick two",
    "steps": [
        {"do": "First", "id": "s3", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
        {"do": "Middle", "id": "s2"},
        {"do": "Last", "id": "s1", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
    ],
}


def test_a_step_id_that_reads_as_a_step_number_is_refused_at_post_and_amend(pond):
    srv, base_url, token, clock = pond
    doc = {**DOC, "steps": [{**step, "id": sid} for step, sid in zip(DOC["steps"], ("2", "1", "s3"))]}
    status, data = req(base_url, "/asks", token, method="POST",
                       body={"project": "Orbit", "context": "personal", "instructable": doc})
    assert status == 400
    assert data["details"] == ["steps[0].id: must not look like a step number",
                               "steps[1].id: must not look like a step number"]
    item_id, rev1 = post(base_url, token)
    clock.now = clock.now + timedelta(seconds=61)
    body = json.loads(req_detail(base_url, token, item_id)["item"]["revision"]["body"])
    body["steps"].insert(0, {"do": "Inserted", "id": "1"})
    status, data = req(base_url, f"/asks/{item_id}/amend", token, method="POST",
                       body={"editKey": EDIT_KEY, "baseRev": rev1["rev"], "baseHash": rev1["hash"],
                             "reason": "a fix", "instructable": body})
    assert (status, data["error"]) == (400, "invalid instructable")
    assert "steps[0].id: must not look like a step number" in data["details"]


def test_an_answer_or_a_problem_naming_an_id_lands_on_that_step(pond):
    """Ids that look like s<n> but name another position: the id wins over nothing
    else, and a path stays a path."""
    srv, base_url, token, _ = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token, TWO_QUESTIONS)
    events = srv.service.store.events_for_item
    for step, landed in (("s1", {"step": "3", "id": "s1"}), ("1", {"step": "1", "id": "s3"}),
                         ("s3", {"step": "1", "id": "s3"}), ("3", {"step": "3", "id": "s1"})):
        status, _ = req(base_url, f"/asks/{item_id}/answers", token, method="POST",
                        body={"answers": [{"step": step, "picked": ["Prod"]}]})
        assert status == 200
        assert json.loads(events(item_id)[-1].text) == {**landed, "picked": ["Prod"]}, step
    for step, landed in (("s1", {"id": "s1", "step": "3"}), ("1", {"id": "s3", "step": "1"}),
                         ("s2", {"id": "s2", "step": "2"}), ("2", {"id": "s2", "step": "2"})):
        body = {"step": step, "rev": 1, "hash": rev1["hash"], "note": "fails"}
        assert req(base_url, f"/asks/{item_id}/problems", token, method="POST", body=body, key=secret)[0] == 200
        assert json.loads(events(item_id)[-1].text) == {**landed, "note": "fails"}, step


def test_answers_on_a_corrected_ask_are_bound_to_the_accepted_version(pond):
    srv, base_url, token, clock = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token)
    rev2 = amend(base_url, token, clock, item_id, rev1, code_change)
    assert answer(base_url, token, item_id) == (409, corrected(rev2))                  # the old app
    assert answer(base_url, token, item_id, rev=2) == (409, corrected(rev2))           # not accepted yet
    assert req(base_url, f"/asks/{item_id}/accept", token, method="POST",
               body={"rev": 2, "hash": rev2["hash"]}, key=secret)[0] == 200
    assert answer(base_url, token, item_id, rev=2, hash="0" * 64)[0] == 409
    assert answer(base_url, token, item_id, rev=2, hash=rev2["hash"], key=secret)[0] == 200
    event = srv.service.store.events_for_item(item_id)[-1]
    assert (event.kind, event.rev, event.auth) == ("answer", 2, "owner-key")


def test_owner_done_is_bound_too(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = post(base_url, token)
    assert req(base_url, f"/asks/{item_id}/resolve", token, method="POST",
               body={"verdict": "done", "actor": "owner", "rev": 1, "hash": "0" * 64})[0] == 409
    rev2 = amend(base_url, token, clock, item_id, rev1, code_change)
    path = f"/asks/{item_id}/resolve"
    assert req(base_url, path, token, method="POST", body={"verdict": "done", "actor": "owner"}) == (
        409, corrected(rev2))
    assert req(base_url, path, token, method="POST", body={"verdict": "done", "actor": "session"})[0] == 200
    other, rev1b = post(base_url, token, {**DOC, "title": "Second"})
    amend(base_url, token, clock, other, rev1b, code_change)
    assert req(base_url, f"/asks/{other}/resolve", token, method="POST",
               body={"verdict": "declined", "actor": "owner"})[0] == 200


# --- accept ---------------------------------------------------------------------------


def test_accept_rules(pond):
    srv, base_url, token, clock = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token)
    rev2 = amend(base_url, token, clock, item_id, rev1, code_change)
    path = f"/asks/{item_id}/accept"
    assert req(base_url, path, token, method="POST", body={"rev": 2, "hash": rev2["hash"]}, key=secret)[0] == 200
    assert req(base_url, path, token, method="POST", body={"rev": 2, "hash": rev2["hash"]}, key=secret)[0] == 200
    assert req(base_url, path, token, method="POST", body={"rev": 1, "hash": rev1["hash"]}, key=secret) == (
        409, {"error": "stale revision", "rev": 2, "hash": rev2["hash"]})
    assert req(base_url, path, token, method="POST", body={"rev": 2, "hash": "0" * 64}, key=secret)[0] == 409
    assert req(base_url, path, token, method="POST", body={"rev": 2}, key=secret) == (400, {"error": "missing fields"})
    assert req(base_url, "/asks/nope/accept", token, method="POST", body={"rev": 2, "hash": "x"}, key=secret)[0] == 404
    assert req_detail(base_url, token, item_id)["item"]["revision"]["acceptedRev"] == 2
    kinds = [(e.kind, e.rev, e.auth) for e in srv.service.store.events_for_item(item_id) if e.kind == "accepted"]
    assert kinds == [("accepted", 2, "owner-key")] * 2


def test_a_token_only_accept_is_refused_and_changes_nothing(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = post(base_url, token)
    rev2 = amend(base_url, token, clock, item_id, rev1, code_change)
    edits = len(srv.telegram.edited_messages)
    assert req(base_url, f"/asks/{item_id}/accept", token, method="POST",
               body={"rev": 2, "hash": rev2["hash"]}) == (403, {"error": "owner key required"})
    assert req_detail(base_url, token, item_id)["item"]["revision"]["acceptedRev"] == 1
    assert len(srv.telegram.edited_messages) == edits
    keyboard = srv.telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for row in keyboard for b in row if "callback_data" in b] == [f"s:{item_id}"]
    assert tap(srv.service, f"d:{item_id}:2") == "This ask changed. Use the newest message."


# --- the answer rule --------------------------------------------------------------------


def _event(seq, text, rev):
    return {"seq": seq, "kind": "answer", "actor": "owner", "text": json.dumps(text), "at": "t", "rev": rev}


def test_an_answer_to_a_changed_question_is_earlier_not_counted():
    rev1 = assign_ids(DOC)[0]
    rev2 = copy.deepcopy(rev1)
    rev2["steps"][2]["question"]["choices"] = ["Staging", "Prod", "Dev"]
    events = [_event(1, {"step": "3", "picked": ["Prod"]}, None)]
    counted, earlier = current_answers(events, {1: rev1, 2: rev2}, rev2)
    assert counted == {} and earlier["s3"]["picked"] == ["Prod"]
    only_code = copy.deepcopy(rev1)
    only_code["steps"][2]["code"] = "deploy --env Z"
    counted, earlier = current_answers(events, {1: rev1, 2: only_code}, only_code)
    assert counted["s3"]["picked"] == ["Prod"] and earlier == {}


def test_the_answer_rule_follows_a_moved_step():
    rev1 = assign_ids(DOC)[0]
    rev2 = copy.deepcopy(rev1)
    rev2["steps"].insert(0, {"do": "New first step", "id": "r2-1"})
    events = [_event(1, {"step": "3", "id": "s3", "picked": ["Prod"]}, 1)]
    counted, _ = current_answers(events, {1: rev1, 2: rev2}, rev2)
    assert counted["s3"]["picked"] == ["Prod"]


# --- problems ------------------------------------------------------------------------------


def test_problems_need_the_owner_key_and_are_checked(pond):
    srv, base_url, token, clock = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token)
    path = f"/asks/{item_id}/problems"
    body = {"step": "2", "rev": 1, "hash": rev1["hash"], "note": "permission denied on /var/lib/grafana"}
    assert req(base_url, path, token, method="POST", body=body) == (403, {"error": "owner key required"})
    assert req(base_url, path, token, method="POST", body=body, key=secret)[0] == 200
    event = srv.service.store.events_for_item(item_id)[-1]
    assert (event.kind, event.actor, event.rev, event.auth) == ("problem", "owner", 1, "owner-key")
    assert json.loads(event.text) == {"id": "s2", "step": "2", "note": body["note"]}
    assert req(base_url, path, token, method="POST", body={**body, "step": "s3"}, key=secret)[0] == 200
    assert req(base_url, path, token, method="POST", body={**body, "note": "bell\x07here"}, key=secret) == (
        400, {"error": "invalid problem", "details": ["note: contains hidden or control characters (U+0007)"]})
    assert req(base_url, path, token, method="POST", body={**body, "step": "9"}, key=secret) == (
        400, {"error": "invalid problem", "details": ["step: unknown"]})
    assert req(base_url, path, token, method="POST", body={**body, "hash": "0" * 64}, key=secret)[1]["error"] == (
        "stale revision")
    for _ in range(18):
        assert req(base_url, path, token, method="POST", body=body, key=secret)[0] == 200
    assert req(base_url, path, token, method="POST", body=body, key=secret) == (409, {"error": "too many reports"})


TRACEBACK = "\n".join(
    ["Traceback (most recent call last):"]
    + [f'  File "/srv/app/module_{n}.py", line {10 * n}, in handler_{n}\n    result = step_{n}(payload)'
       for n in range(1, 7)]
    + ["PermissionError: [Errno 13] Permission denied: '/var/lib/grafana/grafana.db'"])


def test_problem_note_up_to_8000_multiline(pond):
    srv, base_url, token, clock = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token)
    path = f"/asks/{item_id}/problems"
    note = TRACEBACK + " " * (937 - len(TRACEBACK))
    assert len(note) == 937 and note.count("\n") == 13
    body = {"step": "2", "rev": 1, "hash": rev1["hash"], "note": note}
    assert req(base_url, path, token, method="POST", body=body, key=secret)[0] == 200
    event = srv.service.store.events_for_item(item_id)[-1]
    assert json.loads(event.text)["note"] == note
    assert req(base_url, path, token, method="POST", body={**body, "note": "n" * 8000}, key=secret)[0] == 200
    assert req(base_url, path, token, method="POST", body={**body, "note": "n" * 8001}, key=secret) == (
        400, {"error": "invalid problem", "details": ["note: too long"]})


def test_problem_note_secret_refused(pond):
    srv, base_url, token, clock = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token)
    leak = "ghp_" + "a" * 36
    body = {"step": "2", "rev": 1, "hash": rev1["hash"], "note": TRACEBACK + "\ntoken " + leak}
    status, data = req(base_url, f"/asks/{item_id}/problems", token, method="POST", body=body, key=secret)
    assert (status, data) == (400, {"error": "invalid problem", "details": ["note: secret-like value"]})
    assert leak not in json.dumps(data)


def test_a_problem_on_the_accepted_or_the_current_version(pond):
    srv, base_url, token, clock = pond
    secret, _ = http_approved(srv, base_url, token)
    item_id, rev1 = post(base_url, token)
    rev2 = amend(base_url, token, clock, item_id, rev1, code_change)
    path = f"/asks/{item_id}/problems"
    for rev in (rev1, rev2):
        body = {"step": "3", "rev": rev["rev"], "hash": rev["hash"], "note": "fails"}
        assert req(base_url, path, token, method="POST", body=body, key=secret)[0] == 200
    req(base_url, f"/asks/{item_id}/resolve", token, method="POST", body={"verdict": "declined", "actor": "owner"})
    assert req(base_url, path, token, method="POST",
               body={"step": "3", "rev": 2, "hash": rev2["hash"], "note": "x"}, key=secret) == (
        409, {"error": "ask is closed"})


# --- the owner check with enforcement on ----------------------------------------------------


def test_owner_routes_with_enforcement_on(pond):
    srv, base_url, token, _ = pond
    secret, _ = http_approved(srv, base_url, token)
    req(base_url, "/owner/require", token, method="POST", body={"required": True}, key=secret)
    item_id, _ = post(base_url, token)
    assert req(base_url, f"/asks/{item_id}/reply", token, method="POST", body={"text": "x"}) == (
        403, {"error": "owner key required"})
    assert req(base_url, "/asks/nope/reply", token, method="POST", body={"text": "x"}) == (
        403, {"error": "owner key required"})
    assert answer(base_url, token, item_id)[0] == 403
    assert req(base_url, f"/asks/{item_id}/resolve", token, method="POST",
               body={"verdict": "done", "actor": "owner"})[0] == 403
    assert req(base_url, f"/asks/{item_id}/resolve", token, method="POST",
               body={"verdict": "done", "actor": "session"})[0] == 200
    assert req(base_url, f"/asks/{item_id}/reopen", token, method="POST", body={}) == (
        403, {"error": "owner key required"})
    assert req(base_url, f"/asks/{item_id}/reopen", token, method="POST", body={"actor": "session"})[0] == 200
    assert tap(srv.service, f"d:{item_id}") == "Done"
    done = [e for e in srv.service.store.events_for_item(item_id) if e.kind == "resolved"][-1]
    assert (done.actor, done.auth) == ("owner", "telegram")
    assert req(base_url, f"/asks/{item_id}/reopen", token, method="POST", body={}, key=secret)[0] == 200
    reopened = [e for e in srv.service.store.events_for_item(item_id) if e.kind == "reopened"][-1]
    assert (reopened.actor, reopened.auth) == ("owner", "owner-key")


# --- the requirements gate on every owner Done (Pond 1.3.0) -----------------------

REQUIRED_DOC = {**DOC, "steps": [DOC["steps"][0], {**DOC["steps"][1], "required": True}, DOC["steps"][2]]}


def _text_done(service, item_id, text="done, all of it"):
    message_id = json.loads(service.store.get_item(item_id).payload)["tg_message_id"]
    service.handle_update({"message": {
        "message_id": 700, "from": {"id": service.telegram.owner_id},
        "chat": {"id": service.telegram.owner_id, "type": "private"},
        "text": text, "reply_to_message": {"message_id": message_id}}})


def test_every_owner_done_entry_point_is_gated(pond):
    srv, base_url, token, _ = pond
    item_id, _ = post(base_url, token, REQUIRED_DOC)
    status, data = req(base_url, f"/asks/{item_id}/resolve", token, method="POST",
                       body={"verdict": "done", "actor": "owner"})
    assert (status, data["error"], data["details"]) == (409, "requirements_unmet", ["step 2"])
    assert tap(srv.service, f"d:{item_id}") == "Finish it in Hook first"
    _text_done(srv.service, item_id)
    events = srv.service.store.events_for_item(item_id)
    assert srv.service.store.get_item(item_id).status == "open"
    assert "resolved" not in [e.kind for e in events]
    assert (events[-1].kind, events[-1].actor) == ("reply", "owner")
    # The session closing its own ask, and the owner's Skip, are never gated.
    assert req(base_url, f"/asks/{item_id}/resolve", token, method="POST",
               body={"verdict": "done", "actor": "session"})[0] == 200
    other, _ = post(base_url, token, {**REQUIRED_DOC, "title": "Deploy the hotfix"})
    assert tap(srv.service, f"s:{other}") == "Skipped"


def test_the_gate_opens_for_every_entry_point_once_finished(pond):
    srv, base_url, token, _ = pond
    ids = [post(base_url, token, {**REQUIRED_DOC, "title": f"Deploy {n}"})[0] for n in ("one", "two", "three")]
    for item_id in ids:
        assert req(base_url, f"/asks/{item_id}/progress", token, method="POST", body={"tick": ["s2"]})[0] == 200
    assert req(base_url, f"/asks/{ids[0]}/resolve", token, method="POST",
               body={"verdict": "done", "actor": "owner"})[0] == 200
    assert tap(srv.service, f"d:{ids[1]}") == "Done"
    _text_done(srv.service, ids[2])
    assert [srv.service.store.get_item(i).status for i in ids] == ["done", "done", "done"]


# Shared progress (Pond 1.5): the same endpoint for every ask.
def _shared_progress(service, item_id, tick=(), untick=(), rev=1, hash=None):
    return service.progress(item_id, list(tick), list(untick), rev=rev, hash=hash, auth="token")


def test_progress_idempotent(pond):
    srv, base_url, token, _ = pond
    item_id, _ = post(base_url, token)
    service = srv.service
    _shared_progress(service, item_id, ["s1"])
    count = len(service.store.events_for_item(item_id))
    _shared_progress(service, item_id, ["s1", "s1"], ["s2"])
    assert len(service.store.events_for_item(item_id)) == count
    assert service._ticked(item_id, service.in_force(item_id)[1], service._event_dicts(item_id)) == {"s1"}
    assert service.requirements_left(item_id) is None


def test_interleaved_tick_untick(pond):
    srv, base_url, token, _ = pond
    item_id, _ = post(base_url, token)
    service = srv.service
    for tick, untick in [(["s1"], []), (["s2"], []), ([], ["s1"]), (["s1"], []), ([], ["s2"])]:
        _shared_progress(service, item_id, tick, untick)
    assert service._ticked(item_id, service.in_force(item_id)[1], service._event_dicts(item_id)) == {"s1"}


def test_progress_revision_binding(pond):
    from mailbox.service import AskClosed, Corrected, InvalidProgress
    srv, base_url, token, clock = pond
    item_id, first = post(base_url, token)
    service = srv.service
    count = len(service.store.events_for_item(item_id))
    with pytest.raises(InvalidProgress):
        _shared_progress(service, item_id, ["unknown"])
    with pytest.raises(Corrected):
        _shared_progress(service, item_id, ["s1"], hash="0" * 64)
    assert len(service.store.events_for_item(item_id)) == count
    amend(base_url, token, clock, item_id, first, code_change)
    count = len(service.store.events_for_item(item_id))
    with pytest.raises(Corrected):
        _shared_progress(service, item_id, ["s1"])
    assert len(service.store.events_for_item(item_id)) == count
    service.resolve(item_id, "declined", actor="owner")
    count = len(service.store.events_for_item(item_id))
    with pytest.raises(AskClosed):
        _shared_progress(service, item_id, ["s1"])
    assert len(service.store.events_for_item(item_id)) == count


def test_progress_concurrent_delta(pond, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    srv, base_url, token, _ = pond
    item_id, _ = post(base_url, token)
    service = srv.service
    # Start tick -> untick while the tick has read its snapshot but not appended.
    # Atomic processing makes the second writer observe the committed tick.
    read = Event()
    release = Event()
    second_started = Event()
    second_finished = Event()
    original = service._ticked
    first = True
    def paused(*args):
        nonlocal first
        result = original(*args)
        if first:
            first = False
            read.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(service, "_ticked", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tick = pool.submit(_shared_progress, service, item_id, ["s1"])
        assert read.wait(5)
        def untick_after_read():
            second_started.set()
            try:
                return _shared_progress(service, item_id, [], ["s1"])
            finally:
                second_finished.set()
        untick = pool.submit(untick_after_read)
        assert second_started.wait(5)
        try:
            assert not second_finished.wait(0.05)  # validation cannot finish against an uncommitted read
        finally:
            release.set()
        tick.result(timeout=5)
        untick.result(timeout=5)
    assert original(item_id, service.in_force(item_id)[1], service._event_dicts(item_id)) == set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda step: _shared_progress(service, item_id, [step]), ["s1", "s2"]))
    assert original(item_id, service.in_force(item_id)[1], service._event_dicts(item_id)) == {"s1", "s2"}


@pytest.mark.parametrize("mutation", ["amend", "close"])
@pytest.mark.parametrize("progress_first", [False, True])
def test_progress_serializes_with_amendment_and_close(pond, monkeypatch, mutation, progress_first):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from mailbox.service import AskClosed, Corrected
    srv, base_url, token, clock = pond
    item_id, first = post(base_url, token)
    service, store = srv.service, srv.service.store
    body = json.loads(store.current_revision(item_id).body)
    code_change(body)
    clock.now += timedelta(seconds=61)
    attempted, validated, release = Event(), Event(), Event()
    def mutate():
        attempted.set()
        if mutation == "amend":
            service.amend(item_id, EDIT_KEY, first["rev"], first["hash"], "Fix command", body)
        else:
            service.resolve(item_id, "declined", actor="owner")
    if progress_first:
        original = service._bind
        def held_bind(*args, **kwargs):
            bound = original(*args, **kwargs)
            validated.set()
            assert release.wait(5)
            return bound
        monkeypatch.setattr(service, "_bind", held_bind)
        with ThreadPoolExecutor(max_workers=2) as pool:
            progress = pool.submit(_shared_progress, service, item_id, ["s1"])
            assert validated.wait(5)
            write = pool.submit(mutate)
            assert attempted.wait(5)
            release.set()
            progress.result(timeout=5)
            write.result(timeout=5)
        kinds = [e.kind for e in store.events_for_item(item_id)]
        assert kinds.index("progress") < kinds.index("revised" if mutation == "amend" else "declined")
    else:
        # The competing writer owns the connection lock before progress starts.
        def progress():
            attempted.set()
            return _shared_progress(service, item_id, ["s1"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store._lock:
                write = pool.submit(progress)
                assert attempted.wait(5)
                mutate()
            with pytest.raises(Corrected if mutation == "amend" else AskClosed):
                write.result(timeout=5)
        assert not any(e.kind == "progress" for e in store.events_for_item(item_id))


def test_progress_redraw_runs_after_transaction_commit(pond, monkeypatch):
    srv, base_url, token, _ = pond
    doc = copy.deepcopy(DOC)
    doc["steps"][0]["required"] = True
    item_id, _ = post(base_url, token, doc)
    service = srv.service
    original = service.telegram.edit_message_reply_markup
    called = []
    def checked(*args, **kwargs):
        assert not service.store.conn.in_transaction
        called.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.telegram, "edit_message_reply_markup", checked)
    _shared_progress(service, item_id, ["s1"])
    assert called == [True]
    assert service.requirements_left(item_id) == []
