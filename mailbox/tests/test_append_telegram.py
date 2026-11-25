"""Telegram for added steps (Pond 1.9.0): the old message keeps its question
buttons and loses Done and Skip; a reply shows the new steps with Done bound to
the new version, silently when the owner acted on the ask in the last 2 minutes."""
import json

from mailbox.telegram import TelegramClient, TelegramError
from mailbox.tests.test_append import _amend, _append, _post, pond  # noqa: F401  (pond is a fixture)
from mailbox.tests.test_server import hcall

DOC = {
    "v": 1, "title": "Rotate the staging key",
    "steps": [
        {"do": "Pick the console", "question": {"prompt": "Which?", "choices": ["Web", "CLI"]}},
        {"do": "Pick the target", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
    ],
}
MORE = [
    {"do": "Delete the old key", "question": {"prompt": "Gone?", "choices": ["Yes", "No"]}},
    {"do": "Check the logs", "code": "tail -n 50 /var/log/deploy.log"},
]


def _callbacks(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]


def _appended(srv, base_url, token, clock, doc=DOC, **post):
    item_id, rev1 = _post(base_url, token, doc)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")
    assert status == 200, data
    return item_id, rev1, data["revision"]


def test_append_edits_old_and_replies(pond):
    srv, base_url, token, clock = pond
    fake = srv.service.telegram
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    old = [m for m in fake.edited_messages if m["message_id"] == 1][-1]
    assert old["text"].endswith("\nSteps added: see below")
    assert old["text"].startswith("🙋 <b>Orbit</b> — Rotate the staging key")
    calls = _callbacks(old["reply_markup"])
    assert calls == [f"a:{item_id}:1:0", f"a:{item_id}:1:1", f"a:{item_id}:2:0", f"a:{item_id}:2:1"]
    assert not [c for c in calls if c.startswith(("d:", "s:"))]
    new = fake.sent_messages[-1]
    assert new["reply_to_message_id"] == 1
    assert new["text"].startswith("➕ 2 steps added · Cleanup steps")
    lines = new["text"].split("\n")
    assert any(line.startswith("3. Delete the old key") for line in lines)
    assert any(line.startswith("4. Check the logs") for line in lines)
    assert not any(line.startswith(("1. ", "2. ")) for line in lines)
    assert "tail -n 50" not in new["text"]                     # code stays in Hook
    calls = _callbacks(new["reply_markup"])
    assert f"d:{item_id}:2" in calls and f"s:{item_id}" in calls
    assert f"a:{item_id}:3:0:2" in calls
    assert not [c for c in calls if c.startswith((f"a:{item_id}:1:", f"a:{item_id}:2:"))]
    new_id = len(fake.sent_messages)
    assert srv.service.store.tg_message(new_id) == (item_id, 2)
    assert new["disable_notification"] is False


def test_the_newest_append_message_keeps_its_first_step_on_redraw(pond):
    srv, base_url, token, clock = pond
    service = srv.service
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    body = {"answers": [{"step": "3", "picked": ["Yes"]}], "rev": 2, "hash": rev2["hash"]}
    assert hcall(base_url, f"/asks/{item_id}/answers", token, method="POST", body=body)[0] == 200
    markup = service.telegram.edited_markups[-1]
    assert markup["message_id"] == len(service.telegram.sent_messages)
    calls = _callbacks(markup["reply_markup"])
    assert calls[0] == f"a:{item_id}:3:0:2" and f"d:{item_id}:2" in calls
    assert not [c for c in calls if c.startswith((f"a:{item_id}:1:", f"a:{item_id}:2:"))]
    # The ack redraws the appended message, not the whole ask.
    service.ack(item_id)
    acked = service.telegram.edited_messages[-1]
    assert acked["text"].startswith("👀 ➕ 2 steps added · Cleanup steps")


def test_a_second_append_edits_the_first_reply(pond):
    srv, base_url, token, clock = pond
    fake = srv.service.telegram
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    first_reply = len(fake.sent_messages)
    clock.advance(15)
    status, data = _append(base_url, token, item_id, rev2, [{"do": "Tell the team"}], note="One more")
    assert status == 200, data
    old = [m for m in fake.edited_messages if m["message_id"] == first_reply][-1]
    assert old["text"].startswith("➕ 2 steps added · Cleanup steps")
    assert old["text"].endswith("\nSteps added: see below")
    assert _callbacks(old["reply_markup"]) == [f"a:{item_id}:3:0:2", f"a:{item_id}:3:1:2"]
    new = fake.sent_messages[-1]
    assert new["reply_to_message_id"] == first_reply
    assert new["text"].startswith("➕ 1 step added · One more")
    assert "\n5. Tell the team" in new["text"]


def test_append_alert_mode(pond):
    srv, base_url, token, clock = pond
    service = srv.service
    service.telegram_mode = "alert"
    service.open_base = "https://pond.example.test/i"
    fake = service.telegram
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    assert [m for m in fake.edited_markups if m["message_id"] == 1][-1]["reply_markup"] == {"inline_keyboard": []}
    assert not [m for m in fake.edited_messages if m["message_id"] == 1]
    new = fake.sent_messages[-1]
    body = json.loads(service.store.current_revision(item_id).body)
    assert new["text"] == TelegramClient.render_alert("Orbit", DOC["title"], body) + "\nSteps added"
    assert new["reply_markup"] == TelegramClient.alert_keyboard(item_id, service.open_base)
    assert new["reply_to_message_id"] == 1


def test_append_quiet_after_recent_act(pond):
    srv, base_url, token, clock = pond
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(30)
    body = {"answers": [{"step": "1", "picked": ["Web"]}], "rev": 1, "hash": rev1["hash"]}
    assert hcall(base_url, f"/asks/{item_id}/answers", token, method="POST", body=body)[0] == 200
    clock.advance(119)
    assert _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")[0] == 200
    assert srv.service.telegram.sent_messages[-1]["disable_notification"] is True


def test_append_rings_otherwise(pond):
    srv, base_url, token, clock = pond
    fake = srv.service.telegram
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(30)
    body = {"answers": [{"step": "1", "picked": ["Web"]}], "rev": 1, "hash": rev1["hash"]}
    assert hcall(base_url, f"/asks/{item_id}/answers", token, method="POST", body=body)[0] == 200
    clock.advance(120)
    status, data = _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")
    assert status == 200, data
    assert fake.sent_messages[-1]["disable_notification"] is False
    # The Pond's own accepted/daemon event of that append is not the owner acting.
    clock.advance(15)
    assert _append(base_url, token, item_id, data["revision"], [{"do": "More"}], note="More")[0] == 200
    assert fake.sent_messages[-1]["disable_notification"] is False


def test_append_telegram_failure_kept(pond, monkeypatch):
    srv, base_url, token, clock = pond
    fake = srv.service.telegram
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(15)

    def broken(method, **params):
        raise TelegramError(f"{method} 500: down")

    monkeypatch.setattr(fake, "_call", broken)
    status, data = _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")
    assert status == 200, data
    assert data["revision"]["rev"] == 2
    kinds = [e.kind for e in srv.service.store.events_for_item(item_id)]
    assert "edit_failed" in kinds


def test_an_amend_stored_as_an_append_is_announced_as_one(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    fake = srv.service.telegram
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(60)
    status, data = _amend(base_url, token, item_id, rev1, lambda d: d["steps"].append({"do": "One more"}),
                          reason="Cleanup steps")
    assert status == 200, data
    assert srv.service.store.revision_kinds(item_id) == {2: "append"}
    new = fake.sent_messages[-1]
    assert new["text"].startswith("➕ 1 step added · Cleanup steps")
    assert "Corrected" not in new["text"]
    assert f"d:{item_id}:2" in _callbacks(new["reply_markup"])


def test_an_append_on_an_unreviewed_correction_waits_for_the_review(pond):
    srv, base_url, token, clock = pond
    srv.service.store.set_setting("corrections_enabled", "1")
    fake = srv.service.telegram
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(60)
    status, data = _amend(base_url, token, item_id, rev1, lambda d: d["steps"][0].update(do="Open the console"))
    assert status == 200, data
    edits = len(fake.edited_messages)
    clock.advance(15)
    assert _append(base_url, token, item_id, data["revision"], MORE, note="Cleanup steps")[0] == 200
    assert len(fake.edited_messages) == edits                 # the Corrected message is left as it is
    new = fake.sent_messages[-1]
    assert new["text"].startswith("➕ 2 steps added · Cleanup steps")
    assert new["reply_markup"] == TelegramClient.pending_keyboard(item_id, srv.service.open_base)


def test_render_appended_escapes_and_counts():
    text = TelegramClient.render_appended("P<1>", "T&T", 1, "a <b> note", [(7, {"do": "x < y"})])
    lines = text.split("\n")
    assert lines[0] == "➕ 1 step added · a &lt;b&gt; note"
    assert lines[1] == "<b>P&lt;1&gt;</b> · T&amp;T"
    assert lines[2] == "7. x &lt; y"
    assert TelegramClient.steps_added_line() == "Steps added: see below"


def test_render_appended_stays_under_the_cap():
    steps = [(n, {"do": "d" * 300, "detail": "e" * 1500}) for n in range(3, 13)]
    text = TelegramClient.render_appended("P", "T", 10, "n" * 200, steps)
    assert len(text) <= 3500


def test_question_keyboard_and_first_path():
    ins = {"steps": [{"do": "a", "question": {"prompt": "?", "choices": ["A", "B"]}},
                     {"do": "b", "question": {"prompt": "?", "choices": ["C", "D"]}}]}
    rows = TelegramClient.question_keyboard("id", ins, {}, rev=2)["inline_keyboard"]
    assert [b["callback_data"] for row in rows for b in row] == ["a:id:1:0:2", "a:id:1:1:2", "a:id:2:0:2", "a:id:2:1:2"]
    full = TelegramClient.ask_keyboard("id", None, ins, {}, rev=2, first_path=2)
    assert _callbacks(full) == ["a:id:2:0:2", "a:id:2:1:2", "d:id:2", "s:id"]


def test_a_reopen_sent_afresh_shows_the_whole_ask(pond, monkeypatch):
    srv, base_url, token, clock = pond
    service = srv.service
    fake = service.telegram
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    service.resolve(item_id, "declined", actor="owner")
    real = fake._call

    def no_edits(method, **params):
        if method == "editMessageText":
            raise TelegramError("editMessageText 400: Bad Request: message can't be edited")
        return real(method, **params)

    monkeypatch.setattr(fake, "_call", no_edits)
    service.reopen(item_id)
    fresh = fake.sent_messages[-1]
    assert fresh["text"].startswith("🙋 <b>Orbit</b> — Rotate the staging key")
    calls = _callbacks(fresh["reply_markup"])
    assert calls[0] == f"a:{item_id}:1:0:2" and f"d:{item_id}:2" in calls


def test_a_tap_on_the_old_message_refreshes_its_own_buttons(pond):
    from mailbox.tests.test_owner import tap
    srv, base_url, token, clock = pond
    service = srv.service
    fake = service.telegram
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    fake.edited_markups.clear()
    assert tap(service, f"a:{item_id}:1:1", message_id=1) == "1: CLI"
    old = [m for m in fake.edited_markups if m["message_id"] == 1]
    assert len(old) == 1
    rows = old[0]["reply_markup"]["inline_keyboard"]
    assert rows[0][1] == {"text": "✓ CLI", "callback_data": f"a:{item_id}:1:1"}
    assert not [c for c in _callbacks(old[0]["reply_markup"]) if c.startswith(("d:", "s:"))]
    newest = len(fake.sent_messages)
    assert [m for m in fake.edited_markups if m["message_id"] == newest]      # the reply is redrawn too


# --- a Done on the version before an append names the new steps ---------------

STEPS_ADDED_TEXT = "New steps were added. Open the ask to see them."


def test_a_done_tap_on_the_old_message_says_steps_were_added(pond):
    from mailbox.tests.test_owner import tap
    srv, base_url, token, clock = pond
    service = srv.service
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    assert tap(service, f"d:{item_id}:1", message_id=1) == STEPS_ADDED_TEXT
    assert service.store.get_item(item_id).status == "open"


def test_a_done_reply_on_the_old_message_says_steps_were_added(pond):
    from mailbox.tests.test_owner import OWNER_ID
    srv, base_url, token, clock = pond
    service = srv.service
    item_id, rev1, rev2 = _appended(srv, base_url, token, clock)
    service.handle_update({"update_id": 1, "message": {
        "message_id": 90, "from": {"id": OWNER_ID}, "chat": {"id": OWNER_ID}, "text": "done, all of it",
        "reply_to_message": {"message_id": 1}}})
    assert service.store.get_item(item_id).status == "open"
    assert service.telegram.replies[-1]["text"] == STEPS_ADDED_TEXT
    assert service.telegram.replies[-1]["reply_to_message_id"] == 90
    replies = [e for e in service.store.events_for_item(item_id) if e.kind == "reply"]
    assert replies and replies[-1].text == "done, all of it"


def test_an_owner_accept_is_not_fresh_attention(pond):
    # Hook's own seal accept and Use Version N are `accepted` by the owner, but they
    # aren't the owner looking at new steps: the Mac's rule counts only answer, reply,
    # message, problem, progress, reopened and resolved.
    srv, base_url, token, clock = pond
    store = srv.service.store
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(30)
    now = lambda: srv.service._stamp(srv.service._utc())  # noqa: E731  (the Pond's clock)
    store.append_events(item_id, "accepted", "owner", [json.dumps({"rev": 1, "hash": rev1["hash"]})], rev=1, at=now())
    clock.advance(15)
    assert _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")[0] == 200
    assert srv.service.telegram.sent_messages[-1]["disable_notification"] is False
    # A problem report is attention: the next append is quiet.
    store.append_events(item_id, "problem", "owner", [json.dumps({"step": "1", "note": "stuck"})], rev=2, at=now())
    clock.advance(15)
    rev2 = {"rev": 2, "hash": store.current_revision(item_id).hash}
    assert _append(base_url, token, item_id, rev2, [{"do": "More"}], note="More")[0] == 200
    assert srv.service.telegram.sent_messages[-1]["disable_notification"] is True


def test_a_failed_append_reply_gives_the_old_message_its_done_and_skip_back(pond, monkeypatch):
    srv, base_url, token, clock = pond
    service = srv.service
    fake = service.telegram
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(15)

    def down(*args, **kwargs):
        raise TelegramError("sendMessage 500: down")

    monkeypatch.setattr(fake, "send_message", down)
    status, data = _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")
    assert status == 200, data
    assert "edit_failed" in [e.kind for e in service.store.events_for_item(item_id)]
    old = [m for m in fake.edited_messages if m["message_id"] == 1][-1]
    assert not old["text"].endswith("Steps added: see below")       # nothing is below
    calls = _callbacks(old["reply_markup"])
    assert f"d:{item_id}:2" in calls and f"s:{item_id}" in calls     # the current rev's Done, and Skip
    assert old["reply_markup"] == service._keyboard(service.store.get_item(item_id))


def test_a_failed_append_reply_in_alert_mode_restores_the_alert_buttons(pond, monkeypatch):
    srv, base_url, token, clock = pond
    service = srv.service
    service.telegram_mode = "alert"
    service.open_base = "https://pond.example.test/i"
    fake = service.telegram
    item_id, rev1 = _post(base_url, token, DOC)
    clock.advance(15)

    def down(*args, **kwargs):
        raise TelegramError("sendMessage 500: down")

    monkeypatch.setattr(fake, "send_message", down)
    assert _append(base_url, token, item_id, rev1, MORE, note="Cleanup steps")[0] == 200
    assert [m for m in fake.edited_markups if m["message_id"] == 1][-1]["reply_markup"] == \
        TelegramClient.alert_keyboard(item_id, service.open_base)
