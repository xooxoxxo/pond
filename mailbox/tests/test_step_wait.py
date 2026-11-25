"""`then: "steps"` on a question (Pond 1.9.0): after the owner answers it, the
owner's Done waits up to 10 minutes for the session's next steps. Skip never waits."""
import json
from datetime import timedelta

from mailbox import requirements as req
from mailbox.tests.test_append import _append, _post, pond  # noqa: F401  (pond is a fixture)
from mailbox.tests.test_owner import tap
from mailbox.tests.test_server import T0, hcall

WAIT_DOC = {
    "v": 1, "title": "Clean up the old deploys",
    "steps": [
        {"do": "Open the console"},
        {"do": "Pick the cleanup", "question": {"prompt": "What goes?", "choices": ["Logs", "Caches"],
                                               "then": "steps"}},
    ],
}


def _answer(base_url, token, item_id, rev, picked="Logs"):
    body = {"answers": [{"step": "2", "picked": [picked]}], "rev": rev["rev"], "hash": rev["hash"]}
    return hcall(base_url, f"/asks/{item_id}/answers", token, method="POST", body=body)


def _done(base_url, token, item_id, rev):
    body = {"verdict": "done", "actor": "owner", "rev": rev["rev"], "hash": rev["hash"]}
    return hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST", body=body)


def _skip(base_url, token, item_id):
    return hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST",
                 body={"verdict": "declined", "actor": "owner"})


def _until(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _done_buttons(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row
            if b.get("callback_data", "").startswith("d:")]


def test_step_wait_constant():
    assert req.STEP_WAIT_S == 600


def test_wait_after_answer(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    clock.advance(30)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(5)
    assert _done(base_url, token, item_id, rev1) == (
        409, {"error": "waiting_for_steps", "until": _until(630), "path": "2"})
    assert srv.service.store.get_item(item_id).status == "open"
    item = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]["item"]
    assert item["stepWait"] == {"id": "s2", "path": "2", "until": _until(630)}
    assert srv.service._ready(item_id) is False
    # Telegram hides Done while the wait is on, and a Done tap is refused with the copy.
    assert _done_buttons(srv.service._keyboard(srv.service.store.get_item(item_id))) == []
    assert tap(srv.service, f"d:{item_id}") == "Waiting for next steps…"
    assert srv.service.store.get_item(item_id).status == "open"
    status, data = _skip(base_url, token, item_id)
    assert status == 200, data
    assert srv.service.store.get_item(item_id).status == "dismissed"


def test_no_wait_before_the_answer_and_none_on_the_session_resolve(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    item = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]["item"]
    assert "stepWait" not in item
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    status, data = hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST",
                         body={"verdict": "done", "actor": "session"})
    assert status == 200, data
    assert srv.service.store.get_item(item_id).status == "done"


def test_wait_ends_with_append(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    clock.advance(15)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(20)
    status, data = _append(base_url, token, item_id, rev1, [{"do": "Delete the logs"}], note="Cleanup steps")
    assert status == 200, data
    rev2 = data["revision"]
    item = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]["item"]
    assert "stepWait" not in item
    assert srv.service._ready(item_id) is True
    status, data = _done(base_url, token, item_id, rev2)
    assert status == 200, data


def test_wait_ends_with_an_append_in_the_same_second(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    clock.advance(15)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    status, data = _append(base_url, token, item_id, rev1, [{"do": "Delete the logs"}], note="Cleanup steps")
    assert status == 200, data
    assert _done(base_url, token, item_id, data["revision"])[0] == 200


def test_wait_ends_after_10_minutes(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(599)
    assert _done(base_url, token, item_id, rev1)[0] == 409
    clock.advance(2)
    status, data = _done(base_url, token, item_id, rev1)
    assert status == 200, data


def test_new_answer_waits_again(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(300)
    assert _answer(base_url, token, item_id, rev1, picked="Caches")[0] == 200
    clock.advance(400)                                   # 700 s after the first answer
    assert _done(base_url, token, item_id, rev1) == (
        409, {"error": "waiting_for_steps", "until": _until(900), "path": "2"})
    clock.advance(201)
    assert _done(base_url, token, item_id, rev1)[0] == 200


def test_a_question_without_then_never_waits(pond):
    srv, base_url, token, clock = pond
    doc = json.loads(json.dumps(WAIT_DOC))
    del doc["steps"][1]["question"]["then"]
    item_id, rev1 = _post(base_url, token, {**doc, "title": "No wait"})
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    assert _done(base_url, token, item_id, rev1)[0] == 200


def test_sweep_redraws_after_wait(pond):
    srv, base_url, token, clock = pond
    service = srv.service
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(10)
    assert service.sweep_step_waits(clock()) == []
    fake = service.telegram
    fake.edited_markups.clear()
    clock.advance(591)                                   # 601 s after the answer
    assert service.sweep_step_waits(clock()) == [item_id]
    assert len(fake.edited_markups) == 1
    assert _done_buttons(fake.edited_markups[0]["reply_markup"]) == [f"d:{item_id}"]
    clock.advance(60)
    assert service.sweep_step_waits(clock()) == []           # each ended wait is redrawn once
    assert len(fake.edited_markups) == 1


def test_step_wait_pure():
    body = {"steps": [{"do": "a", "id": "s1", "question": {"prompt": "p", "then": "steps"}}]}
    at = T0.isoformat(timespec="seconds")
    answer = {"seq": 5, "kind": "answer", "actor": "owner", "at": at,
              "text": json.dumps({"step": "1", "id": "s1", "text": "x"})}

    class R:
        def __init__(self, at):
            self.at = at

    assert req.step_wait(body, [], [R(at)], T0) is None
    assert req.step_wait(body, [answer], [R(at)], T0 + timedelta(seconds=1)) == {
        "id": "s1", "path": "1", "until": _until(600)}
    assert req.step_wait(body, [answer], [R(at)], T0 + timedelta(seconds=600)) is None
    later = (T0 + timedelta(seconds=3)).isoformat(timespec="seconds")
    assert req.step_wait(body, [answer], [R(at), R(later)], T0 + timedelta(seconds=4)) is None
    session = {**answer, "actor": "session"}
    assert req.step_wait(body, [session], [R(at)], T0) is None


def test_poll_loop_sweeps_once_per_loop(pond, monkeypatch):
    srv, base_url, token, clock = pond
    sweeps = []
    polls = []

    def fake_get_updates(offset=0, timeout=50):
        polls.append(offset)
        if len(polls) == 2:
            srv.running = False
        return []

    monkeypatch.setattr(srv.telegram, "get_updates", fake_get_updates)
    monkeypatch.setattr(srv.service, "sweep_step_waits", lambda now: sweeps.append(now) or [])
    srv.running = True
    srv._poll_loop()
    assert sweeps == [clock(), clock()]


def test_a_failed_sweep_never_stops_the_poll_loop(pond, monkeypatch):
    srv, base_url, token, clock = pond
    polls = []

    def fake_get_updates(offset=0, timeout=50):
        polls.append(offset)
        if len(polls) == 2:
            srv.running = False
        return []

    def boom(now):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv.telegram, "get_updates", fake_get_updates)
    monkeypatch.setattr(srv.service, "sweep_step_waits", boom)
    srv.running = True
    srv._poll_loop()
    assert len(polls) == 2


def test_a_verdict_word_while_waiting_is_kept_as_a_reply(pond):
    srv, base_url, token, clock = pond
    service = srv.service
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    service.handle_update({"message": {
        "message_id": 900, "from": {"id": service.telegram.owner_id}, "chat": {"id": service.telegram.owner_id},
        "text": "done", "reply_to_message": {"message_id": 1}}})
    assert service.store.get_item(item_id).status == "open"
    assert service.store.events_for_item(item_id)[-1].kind == "reply"
    assert service.telegram.replies[-1]["text"] == "Waiting for next steps…"


def test_old_clients_still_get_the_question_with_then(pond):
    srv, base_url, token, clock = pond
    item_id, _ = _post(base_url, token, WAIT_DOC)
    for protocol in (None, 4, 5, 6):
        item = hcall(base_url, f"/asks/{item_id}", token, protocol=protocol)[1]["item"]
        assert item["payload"]["instructable"]["steps"][1]["question"]["then"] == "steps"


def test_an_unchanged_answer_sent_again_keeps_the_wait(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(300)
    assert _answer(base_url, token, item_id, rev1)[0] == 200           # the same pick, resent
    clock.advance(301)                                                 # 601 s after the first answer
    status, data = _done(base_url, token, item_id, rev1)
    assert status == 200, data


def test_the_first_sweep_after_a_restart_looks_back_without_a_bound(pond):
    srv, base_url, token, clock = pond
    service = srv.service
    item_id, rev1 = _post(base_url, token, WAIT_DOC)
    assert _answer(base_url, token, item_id, rev1)[0] == 200
    clock.advance(3600)                                  # the wait ended long before this process swept
    service.telegram.edited_markups.clear()
    assert service.sweep_step_waits(clock()) == [item_id]
    assert len(service.telegram.edited_markups) == 1
    clock.advance(60)
    assert service.sweep_step_waits(clock()) == []       # once per process
    assert len(service.telegram.edited_markups) == 1
