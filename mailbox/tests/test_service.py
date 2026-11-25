"""Tests for the service layer."""
import json
import time
from datetime import datetime

import pytest

from mailbox.ct_client import CentralTimeError, FakeCentralTimeClient
from mailbox.service import Service
from mailbox.store import Store
from mailbox.telegram import FakeTelegramClient, TelegramError


@pytest.fixture
def setup():
    """Service with fake Telegram and test store."""
    store = Store(":memory:")
    store.open()
    telegram = FakeTelegramClient()
    service = Service(store, telegram, "personal")
    yield service, store, telegram
    store.close()


def test_post_new_ask(setup):
    """Post a new ask to Telegram."""
    service, store, telegram = setup
    item, resp = service.post("Orbit", "fix the bug", why="slow", cmd="pytest", link="https://...")

    assert item.id
    assert item.title == "fix the bug"
    assert item.status == "open"
    assert "deduped" not in resp or not resp["deduped"]
    assert len(telegram.sent_messages) == 1


def test_post_deduped_ask(setup):
    """Same ask within 24h is deduped, and no second Telegram call is made (S14)."""
    service, store, telegram = setup
    item1, resp1 = service.post("Orbit", "fix the bug")
    item2, resp2 = service.post("Orbit", "fix the bug")

    assert item1.id == item2.id
    assert resp2.get("deduped") is True
    assert len(telegram.sent_messages) == 1  # only sent once
    assert len(telegram.calls) == 1  # no Telegram call at all on dedup


def test_ack_ask(setup):
    """Ack prefixes the message with 👀."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    service.ack(item.id)

    assert len(telegram.edited_messages) == 1
    edited_text = telegram.edited_messages[0]["text"]
    assert "👀" in edited_text


def test_ack_keeps_the_ask_keyboard(setup):
    """S9: ack must re-send the Done/Skip keyboard, not drop it."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    service.ack(item.id)

    edited = telegram.edited_messages[-1]
    assert edited["reply_markup"] == telegram.ask_keyboard(item.id)


def test_steps_reply(setup):
    """Steps sends a reply to the ask message."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    service.steps(item.id, "1. Do X\n2. Do Y")

    # Check the event was recorded
    _, events = service.get_item_with_events(item.id)
    step_events = [e for e in events if e["kind"] == "steps"]
    assert len(step_events) == 1
    assert "Do X" in step_events[0]["text"]


def test_resolve_done(setup):
    """Resolve an ask as done."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    resolved = service.resolve(item.id, "done", note="deployed")

    assert resolved.status == "done"
    assert resolved.completed_at is not None
    assert len(telegram.edited_messages) == 1
    edited_text = telegram.edited_messages[0]["text"]
    assert "✅" in edited_text
    assert "<s>" in edited_text


def test_resolve_declined(setup):
    """Resolve an ask as declined."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    resolved = service.resolve(item.id, "declined", note="not needed")

    assert resolved.status == "dismissed"
    assert "🚫" in telegram.edited_messages[0]["text"]


def test_resolve_removes_the_inline_keyboard(setup):
    """S9: resolve must clear the Done/Skip keyboard on the edited message."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    service.resolve(item.id, "done")

    edited = telegram.edited_messages[-1]
    assert edited["reply_markup"] == {"inline_keyboard": []}


def test_resolve_direct_call_records_session_actor(setup):
    """A resolve() call made directly (e.g. from the HTTP API) is actor=session."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    service.resolve(item.id, "done")

    _, events = service.get_item_with_events(item.id)
    resolved_events = [e for e in events if e["kind"] == "resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0]["actor"] == "session"


def test_resolve_invalid_verdict_raises_value_error(setup):
    """verdict must be 'done' or 'declined'."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    with pytest.raises(ValueError):
        service.resolve(item.id, "maybe")


def test_resolve_unknown_item_raises_value_error(setup):
    """resolve on an unknown item id raises."""
    service, store, telegram = setup
    with pytest.raises(ValueError):
        service.resolve("does-not-exist", "done")


# --- Ruling S12: the resolve fallback, precisely -------------------------


@pytest.mark.parametrize(
    "error_message",
    [
        "editMessageText 400: Bad Request: message can't be edited",
        "editMessageText 400: Bad Request: message to edit not found",
        "editMessageText 400: Bad Request: message is too old",
        "editMessageText 400: Bad Request: MESSAGE_ID_INVALID",
        "editMessageText 500: Internal Server Error",  # "any other" TelegramError
    ],
)
def test_resolve_falls_back_to_a_new_message_on_edit_failure(setup, error_message):
    """When editing the original message fails (whitelisted reason or any other
    TelegramError), resolve sends a fresh message and records edit_failed."""
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")

    def raising_edit(*args, **kwargs):
        raise TelegramError(error_message)

    telegram.edit_message_text = raising_edit

    resolved = service.resolve(item.id, "done", note="deployed")
    assert resolved.status == "done"

    # No edited_messages recorded (the raising stub bypassed the fake's
    # bookkeeping), but a fresh message must have been sent instead.
    assert len(telegram.sent_messages) == 2  # original post + fallback
    fallback_text = telegram.sent_messages[-1]["text"]
    assert fallback_text == "✅ resolved: rotate the key — deployed"

    _, events = service.get_item_with_events(item.id)
    edit_failed = [e for e in events if e["kind"] == "edit_failed"]
    assert len(edit_failed) == 1
    assert edit_failed[0]["actor"] == "daemon"
    assert error_message in edit_failed[0]["text"]


def test_resolve_declined_fallback_message_text(setup):
    """The declined fallback uses the 🚫 skipped template."""
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")

    def raising_edit(*args, **kwargs):
        raise TelegramError("editMessageText 400: Bad Request: message can't be edited")

    telegram.edit_message_text = raising_edit

    service.resolve(item.id, "declined", note="not needed")
    fallback_text = telegram.sent_messages[-1]["text"]
    assert fallback_text == "🚫 skipped: rotate the key — not needed"


def test_resolve_fallback_omits_note_when_none(setup):
    """No note -> no em-dash suffix in the fallback text."""
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")

    def raising_edit(*args, **kwargs):
        raise TelegramError("editMessageText 400: Bad Request: message can't be edited")

    telegram.edit_message_text = raising_edit

    service.resolve(item.id, "done")
    fallback_text = telegram.sent_messages[-1]["text"]
    assert fallback_text == "✅ resolved: rotate the key"


def test_resolve_message_not_modified_is_treated_as_success(setup):
    """S12: 'message is not modified' is SUCCESS — no fallback, no edit_failed."""
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")

    def raising_edit(*args, **kwargs):
        raise TelegramError("editMessageText 400: Bad Request: message is not modified")

    telegram.edit_message_text = raising_edit

    resolved = service.resolve(item.id, "done", note="deployed")
    assert resolved.status == "done"

    # No fallback message sent (only the original post).
    assert len(telegram.sent_messages) == 1

    _, events = service.get_item_with_events(item.id)
    assert not [e for e in events if e["kind"] == "edit_failed"]


def test_resolve_fallback_escapes_html(setup):
    """The fallback text is HTML-escaped, matching the client's renderer conventions."""
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "<script>alert(1)</script>")

    def raising_edit(*args, **kwargs):
        raise TelegramError("editMessageText 400: Bad Request: message can't be edited")

    telegram.edit_message_text = raising_edit

    service.resolve(item.id, "done", note="<b>done</b>")
    fallback_text = telegram.sent_messages[-1]["text"]
    assert "<script>" not in fallback_text
    assert "&lt;script&gt;" in fallback_text
    assert "&lt;b&gt;done&lt;/b&gt;" in fallback_text


def test_list_open(setup):
    """List open asks."""
    service, store, telegram = setup
    item1, _ = service.post("Orbit", "ask1")
    item2, _ = service.post("Birch", "ask2")
    service.resolve(item1.id, "done")

    open_asks = service.list_open()
    assert len(open_asks) == 1
    assert open_asks[0].id == item2.id


def test_get_item_with_events(setup):
    """Fetch item and its event log."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")
    service.ack(item.id)
    service.steps(item.id, "steps")

    fetched, events = service.get_item_with_events(item.id)
    assert fetched.id == item.id
    assert len(events) >= 3  # posted, acked, steps
    assert events[0]["kind"] == "posted"


def test_get_item_with_events_survives_a_busy_store(setup):
    """S25 reproduction: 1005 filler events belonging to OTHER items must not
    push a new item's own events out of get_item_with_events' view."""
    service, store, telegram = setup

    filler_id = "filler-item"
    for _ in range(1005):
        store.append_event(filler_id, "reply", "owner", "noise")

    item, _ = service.post("Orbit", "test after the flood")

    fetched, events = service.get_item_with_events(item.id)
    assert fetched.id == item.id
    kinds = [e["kind"] for e in events]
    assert "posted" in kinds


def test_handle_update_done_tap(setup):
    """Handle a Done button tap."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": f"d:{item.id}",
        }
    }
    service.handle_update(update)

    updated = service.store.get_item(item.id)
    assert updated.status == "done"
    assert len(telegram.callback_answers) == 1


def test_handle_update_done_tap_records_owner_actor(setup):
    """S13: a button tap resolves with actor=owner, not session."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": f"d:{item.id}",
        }
    }
    service.handle_update(update)

    _, events = service.get_item_with_events(item.id)
    resolved_events = [e for e in events if e["kind"] == "resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0]["actor"] == "owner"


def test_text_reply_with_a_verdict_word_resolves_the_ask(setup):
    """The owner answers an ask by replying to its Telegram message with 'done …'."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    payload = json.loads(item.payload)
    assert store.get_by_tg_message_id(payload["tg_message_id"]).id == item.id
    service.handle_update({
        "message": {
            "from": {"id": service.telegram.owner_id},
            "text": "done, rotated this morning",
            "reply_to_message": {"message_id": payload["tg_message_id"]},
        }
    })
    assert store.get_item(item.id).status == "done"


def test_text_reply_with_a_verdict_word_records_owner_actor(setup):
    """S13: a verdict-word reply resolves with actor=owner."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    payload = json.loads(item.payload)
    service.handle_update({
        "message": {
            "from": {"id": service.telegram.owner_id},
            "text": "done, rotated this morning",
            "reply_to_message": {"message_id": payload["tg_message_id"]},
        }
    })
    _, events = service.get_item_with_events(item.id)
    resolved_events = [e for e in events if e["kind"] == "resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0]["actor"] == "owner"


def test_text_reply_without_a_verdict_word_is_recorded_not_resolved(setup):
    """A question in the thread stays a question: recorded as an event, ask still open."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    payload = json.loads(item.payload)
    service.handle_update({
        "message": {
            "from": {"id": service.telegram.owner_id},
            "text": "which key do you mean?",
            "reply_to_message": {"message_id": payload["tg_message_id"]},
        }
    })
    assert store.get_item(item.id).status == "open"
    events, _ = store.read_events(0, 100)
    assert any(
        e.kind == "reply" and e.text == "which key do you mean?" and e.actor == "owner"
        for e in events
    )


def test_handle_update_skip_tap(setup):
    """Handle a Skip button tap."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": f"s:{item.id}",
        }
    }
    service.handle_update(update)

    updated = service.store.get_item(item.id)
    assert updated.status == "dismissed"


def test_handle_update_wrong_owner(setup):
    """S13: update from non-owner is dropped, not answered, and counted."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": 999999},  # different owner
            "data": f"d:{item.id}",
        }
    }
    service.handle_update(update)

    # Item should still be open
    updated = service.store.get_item(item.id)
    assert updated.status == "open"
    # No callback answer sent
    assert len(telegram.callback_answers) == 0
    # Dropped-update counter incremented
    assert service.dropped_updates == 1


def test_handle_update_wrong_owner_text_message_also_counted(setup):
    """A non-owner text message is dropped and counted too, not just callbacks."""
    service, store, telegram = setup
    service.handle_update({
        "message": {
            "from": {"id": 999999},
            "text": "done",
            "reply_to_message": {"message_id": 1},
        }
    })
    assert service.dropped_updates == 1


# --- Ruling S26: every owner tap is answered exactly once -----------------


def test_handle_update_unknown_item_tap_answers_unknown_ask(setup):
    """A Done tap on an item id that doesn't exist must still be answered
    (never leave the owner's phone with a spinning button), and must not
    create or change anything."""
    service, store, telegram = setup

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": "d:does-not-exist",
        }
    }
    service.handle_update(update)

    assert len(telegram.callback_answers) == 1
    assert telegram.callback_answers[0]["text"] == "Unknown ask"
    assert store.get_item("does-not-exist") is None


def test_handle_update_unknown_item_skip_tap_answers_unknown_ask(setup):
    """Same as above but for the Skip button."""
    service, store, telegram = setup

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": "s:does-not-exist",
        }
    }
    service.handle_update(update)

    assert len(telegram.callback_answers) == 1
    assert telegram.callback_answers[0]["text"] == "Unknown ask"


def test_handle_update_garbage_callback_data_answers_ignored(setup):
    """callback_data matching neither d: nor s: is answered 'Ignored', not silently dropped."""
    service, store, telegram = setup

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": "garbage",
        }
    }
    service.handle_update(update)

    assert len(telegram.callback_answers) == 1
    assert telegram.callback_answers[0]["text"] == "Ignored"


def test_handle_update_successful_tap_answers_exactly_once(setup):
    """A successful Done tap answers exactly once, with 'Done'."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": f"d:{item.id}",
        }
    }
    service.handle_update(update)

    assert len(telegram.callback_answers) == 1
    assert telegram.callback_answers[0]["text"] == "Done"


def test_handle_update_successful_skip_tap_answers_exactly_once(setup):
    """A successful Skip tap answers exactly once, with 'Skipped'."""
    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": f"s:{item.id}",
        }
    }
    service.handle_update(update)

    assert len(telegram.callback_answers) == 1
    assert telegram.callback_answers[0]["text"] == "Skipped"


def test_handle_update_tap_swallows_answer_callback_query_error(setup):
    """If answer_callback_query itself fails, the resolution the tap already
    performed must stand — the error is swallowed, not propagated."""
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "test")

    def raising_answer(*args, **kwargs):
        raise TelegramError("answerCallbackQuery 400: Bad Request: query is too old")

    telegram.answer_callback_query = raising_answer

    update = {
        "callback_query": {
            "id": "cb_123",
            "from": {"id": service.telegram.owner_id},
            "data": f"d:{item.id}",
        }
    }
    service.handle_update(update)  # must not raise

    assert store.get_item(item.id).status == "done"


def test_ack_keeps_the_relay_row_and_the_instructable_text(setup):
    """With a base, post returns openUrl and ack re-sends both keyboard rows
    and the instructable's rendering, not the legacy one."""
    service, store, telegram = setup
    service.open_base = "http://127.0.0.1:47823/i"
    ins = {"v": 1, "title": "test", "urgency": "today", "steps": [{"do": "Click Save"}]}
    item, resp = service.post("Orbit", "test", instructable=ins)
    assert resp["openUrl"] == f"http://127.0.0.1:47823/i/{item.id}"
    assert resp["editable"] is False and resp["revision"]["rev"] == 1

    service.ack(item.id)
    edited = telegram.edited_messages[-1]
    assert edited["reply_markup"] == telegram.ask_keyboard(item.id, "http://127.0.0.1:47823/i")
    assert "1. Click Save" in edited["text"]


# --- reopen -------------------------------------------------------------


def test_reopen_edits_back_the_instructable_with_its_keyboard(setup):
    service, store, telegram = setup
    service.open_base = "http://127.0.0.1:47823/i"
    ins = {"v": 1, "title": "Rotate the key", "why": "it leaked\nyesterday",
           "steps": [{"do": "open the vault"}]}
    item, _ = service.post("Orbit", "Rotate the key", instructable=ins)
    original = telegram.sent_messages[0]
    service.resolve(item.id, "declined", actor="owner")

    reopened = service.reopen(item.id)
    assert reopened.status == "open"
    assert reopened.completed_at is None
    edit = telegram.edited_messages[-1]
    assert edit["text"] == original["text"]
    assert "it leaked / yesterday" in edit["text"]
    assert edit["reply_markup"] == original["reply_markup"]
    assert len(edit["reply_markup"]["inline_keyboard"]) == 2


def test_reopen_sends_afresh_when_the_edit_fails_and_stores_the_new_id(setup):
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    service.resolve(item.id, "done")
    first_id = json.loads(store.get_item(item.id).payload)["tg_message_id"]

    def raising_edit(*args, **kwargs):
        raise TelegramError("editMessageText 400: Bad Request: message to edit not found")

    telegram.edit_message_text = raising_edit
    service.reopen(item.id)

    fresh = telegram.sent_messages[-1]
    assert fresh["text"] == telegram.sent_messages[0]["text"]
    assert fresh["reply_markup"] == telegram.ask_keyboard(item.id)
    new_id = json.loads(store.get_item(item.id).payload)["tg_message_id"]
    assert new_id == len(telegram.sent_messages) and new_id != first_id
    assert store.get_by_tg_message_id(new_id).id == item.id
    _, events = service.get_item_with_events(item.id)
    assert [e["kind"] for e in events] == ["posted", "resolved", "reopened"]


def test_reopen_message_not_modified_sends_nothing(setup):
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    service.resolve(item.id, "done")

    def raising_edit(*args, **kwargs):
        raise TelegramError("editMessageText 400: Bad Request: message is not modified")

    telegram.edit_message_text = raising_edit
    service.reopen(item.id)
    assert len(telegram.sent_messages) == 1


def test_reopen_logs_and_survives_when_telegram_is_down(setup, caplog):
    from mailbox.telegram import TelegramError

    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    service.resolve(item.id, "done")

    def boom(*args, **kwargs):
        raise TelegramError("network error")

    telegram.edit_message_text = boom
    telegram.send_message = boom
    with caplog.at_level("WARNING", logger="mailbox"):
        reopened = service.reopen(item.id)
    assert reopened.status == "open"
    assert "reopen" in caplog.text


def test_reopen_unknown_item_raises_value_error(setup):
    service, _, _ = setup
    with pytest.raises(ValueError):
        service.reopen("nope")


# --- capture (Task 2) ---------------------------------------------------

NOW = datetime(2026, 10, 17, 13, 0)   # a Saturday, as in test_capture


@pytest.fixture
def capture_setup():
    """Service with the Telegram fake and a Central Time fake; the clock pinned."""
    store = Store(":memory:")
    store.open()
    telegram = FakeTelegramClient()
    ct = FakeCentralTimeClient()
    service = Service(store, telegram, "personal", ct_client=ct)
    service.now = lambda: NOW
    yield service, store, telegram, ct
    store.close()


def _text_update(text, from_id=12345, message_id=77, reply_to=None):
    msg = {"message_id": message_id, "from": {"id": from_id}, "chat": {"id": from_id}, "text": text}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": msg}


def test_capture_creates_an_item_and_replies(capture_setup):
    service, store, telegram, ct = capture_setup
    service.handle_update(_text_update("call mum tomorrow 9:00"))
    assert ct.calls == [("POST", "/api/items", {"title": "call mum", "anchor": {"date": "2026-10-18", "allDay": False, "time": "09:00"}})]
    assert len(telegram.replies) == 1
    assert telegram.replies[0]["reply_to_message_id"] == 77
    assert telegram.replies[0]["text"] == "Added: <b>call mum</b> · tomorrow 09:00"


def test_capture_without_a_date_lands_in_the_inbox(capture_setup):
    service, _, telegram, ct = capture_setup
    service.handle_update(_text_update("buy milk"))
    assert ct.calls[0][2] == {"title": "buy milk", "anchor": None}
    assert telegram.replies[0]["text"] == "Added: <b>buy milk</b> · inbox"


def test_capture_escapes_html_in_the_reply(capture_setup):
    service, _, telegram, _ = capture_setup
    service.handle_update(_text_update("<b>bold</b> idea"))
    assert telegram.replies[0]["text"] == "Added: <b>&lt;b&gt;bold&lt;/b&gt; idea</b> · inbox"


def test_capture_is_silent_and_counted_without_a_client(setup):
    service, _, telegram = setup
    service.handle_update(_text_update("buy milk"))
    assert service.ignored_messages == 1
    assert telegram.sent_messages == []


def test_capture_ignores_a_group_chat_even_from_the_owner(capture_setup):
    service, _, telegram, ct = capture_setup
    update = _text_update("buy milk")
    update["message"]["chat"] = {"id": -100123, "type": "supergroup"}
    service.handle_update(update)
    assert ct.calls == []
    assert telegram.replies == [] and telegram.sent_messages == []
    assert service.ignored_messages == 1


@pytest.mark.parametrize("key,val", [
    ("forward_origin", {"type": "user"}),
    ("forward_from", {"id": 9}),
    ("forward_from_chat", {"id": -5}),
    ("forward_sender_name", "Hidden"),
])
def test_capture_ignores_a_forwarded_message(capture_setup, key, val):
    service, _, telegram, ct = capture_setup
    update = _text_update("buy milk")
    update["message"][key] = val
    service.handle_update(update)
    assert ct.calls == []
    assert telegram.replies == [] and telegram.sent_messages == []
    assert service.ignored_messages == 1


def test_capture_still_works_in_the_private_owner_chat(capture_setup):
    service, _, telegram, ct = capture_setup
    update = _text_update("buy milk")
    update["message"]["chat"]["type"] = "private"
    service.handle_update(update)
    assert len(ct.calls) == 1 and len(telegram.replies) == 1


def test_capture_ignores_bot_commands(capture_setup):
    service, _, telegram, ct = capture_setup
    service.handle_update(_text_update("/start"))
    assert ct.calls == [] and telegram.sent_messages == []
    assert service.ignored_messages == 1


def test_capture_refuses_secret_like_text(capture_setup):
    service, _, telegram, ct = capture_setup
    service.handle_update(_text_update("token ghp_" + "a" * 40 + " tomorrow"))
    assert ct.calls == []
    assert telegram.replies[0]["text"] == "Not saved: that looks like a secret."


def test_capture_when_central_time_is_down_replies_and_logs_no_text(capture_setup, caplog):
    service, _, telegram, ct = capture_setup
    ct.fail = CentralTimeError(502, "create")
    with caplog.at_level("WARNING", logger="mailbox"):
        service.handle_update(_text_update("call mum tomorrow 9:00"))
    assert telegram.replies[0]["text"] == "Not saved: Central Time did not answer. Send it again in a minute."
    assert any("capture failed: create 502" in r.getMessage() for r in caplog.records)
    assert not any("call mum" in r.getMessage() for r in caplog.records)


def test_capture_refused_by_central_time_says_so(capture_setup, caplog):
    service, _, telegram, ct = capture_setup
    ct.fail = CentralTimeError(400, "create")
    with caplog.at_level("WARNING", logger="mailbox"):
        service.handle_update(_text_update("call mum tomorrow 9:00"))
    assert telegram.replies[0]["text"] == "Not saved: Central Time refused it."
    assert any("capture failed: create 400" in r.getMessage() for r in caplog.records)


def test_capture_of_hidden_characters_only_is_ignored(capture_setup):
    """Only hidden code points: the cleaned title is empty, nothing is sent, no reply."""
    service, _, telegram, ct = capture_setup
    service.handle_update(_text_update("​​"))
    assert ct.calls == [] and telegram.sent_messages == [] and telegram.replies == []
    assert service.ignored_messages == 1


def test_capture_drops_a_wrong_owner_before_anything(capture_setup):
    service, _, telegram, ct = capture_setup
    service.handle_update(_text_update("buy milk", from_id=999))
    assert service.dropped_updates == 1 and ct.calls == [] and telegram.sent_messages == []


def test_a_reply_to_an_ask_still_resolves_and_never_captures(capture_setup):
    service, store, telegram, ct = capture_setup
    item, _ = service.post("Orbit", "rotate the key")
    tg_id = json.loads(item.payload)["tg_message_id"]
    service.handle_update(_text_update("done, rotated", reply_to=tg_id))
    assert store.get_item(item.id).status == "done"
    assert ct.calls == []


# --- asks flow unchanged with capture on (regression) --------------------


def test_a_non_verdict_reply_to_an_ask_is_recorded_not_captured(capture_setup):
    service, store, telegram, ct = capture_setup
    item, _ = service.post("Orbit", "pick a colour")
    tg_id = json.loads(item.payload)["tg_message_id"]
    service.handle_update(_text_update("go with blue", reply_to=tg_id))
    assert store.get_item(item.id).status == "open"
    assert any(e.kind == "reply" and e.text == "go with blue" for e in store.events_for_item(item.id))
    assert ct.calls == [] and telegram.replies == []


def test_a_skip_reply_to_an_ask_declines_it_and_never_captures(capture_setup):
    service, store, telegram, ct = capture_setup
    item, _ = service.post("Orbit", "rotate the key")
    tg_id = json.loads(item.payload)["tg_message_id"]
    service.handle_update(_text_update("skip this one", reply_to=tg_id))
    assert store.get_item(item.id).status == "dismissed"
    assert ct.calls == []


def test_a_reply_to_a_non_ask_message_is_never_captured(capture_setup):
    service, _, telegram, ct = capture_setup
    service.handle_update(_text_update("buy milk", reply_to=424242))
    assert ct.calls == [] and telegram.sent_messages == []
    assert service.ignored_messages == 1


def test_button_taps_still_resolve_with_capture_on(capture_setup):
    service, store, telegram, ct = capture_setup
    done, _ = service.post("Orbit", "ask one")
    skip, _ = service.post("Orbit", "ask two")
    for data in (f"d:{done.id}", f"s:{skip.id}", "x:junk"):
        service.handle_update({"update_id": 2, "callback_query": {"id": "cb", "from": {"id": 12345}, "data": data}})
    assert store.get_item(done.id).status == "done"
    assert store.get_item(skip.id).status == "dismissed"
    assert [a["text"] for a in telegram.callback_answers] == ["Done", "Skipped", "Ignored"]
    assert ct.calls == []


def test_a_message_without_text_is_not_captured(capture_setup):
    service, _, telegram, ct = capture_setup
    service.handle_update({"update_id": 3, "message": {"message_id": 5, "from": {"id": 12345}, "chat": {"id": 12345}}})
    assert ct.calls == [] and telegram.sent_messages == []


# --- notifications (Task 6) -------------------------------------------------


def _tap(nid, action, from_id=12345):
    return {"update_id": 2, "callback_query": {"id": "cb-1", "from": {"id": from_id}, "data": f"r:{nid}:{action}"}}


ACTIONS = [{"id": "done", "label": "✅ Done"}, {"id": "tomorrow", "label": "⏭ Tomorrow"}]


def test_notify_stores_the_message_id_and_dedupes(capture_setup):
    service, store, telegram, _ = capture_setup
    n, deduped = service.notify("reminder", "fire-1", "call the notary", "today 14:00", ACTIONS)
    assert deduped is False
    assert (n.tg_chat_id, n.tg_message_id) == (telegram.owner_id, 1)
    assert json.loads(n.actions) == ACTIONS
    again, deduped = service.notify("reminder", "fire-1", "other", None, [])
    assert deduped is True and again.id == n.id
    assert len(telegram.sent_messages) == 1


def test_brief_carries_the_open_ask_count(capture_setup):
    service, _, telegram, _ = capture_setup
    service.post("Orbit", "rotate the key")
    service.post("Orbit", "approve the PR")
    service.notify("brief", "brief:2026-10-17", "Saturday 17 October", "Today (1): x")
    assert telegram.sent_messages[-1]["text"] == "☀️ <b>Saturday 17 October</b>\nToday (1): x\nOpen asks: 2"
    assert telegram.sent_messages[-1]["reply_markup"] is None


def test_two_concurrent_notifies_with_one_ref_send_exactly_once(capture_setup, monkeypatch):
    """Core re-posts a ref after its 5 s timeout while the first send may still be inside
    Telegram (a 429 sleep): the second request must wait for the first, then dedupe."""
    import threading

    service, _, telegram, _ = capture_setup
    gate = threading.Event()
    real_send = telegram.send_message

    def slow_send(*args, **kwargs):
        gate.wait(timeout=5)
        return real_send(*args, **kwargs)

    monkeypatch.setattr(telegram, "send_message", slow_send)
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.notify("reminder", "fire-1", "x", None, ACTIONS))) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.1)                       # both threads are inside notify(); one holds the ref lock, one waits
    gate.set()
    for t in threads:
        t.join(timeout=5)
    assert len(telegram.sent_messages) == 1
    assert sorted(deduped for _, deduped in results) == [False, True]
    assert {n.id for n, _ in results} == {results[0][0].id}
    assert service._ref_locks == {}       # nothing leaks once both are through


def test_a_text_reply_to_a_reminder_is_ignored_and_counted(capture_setup):
    service, _, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "call the notary", "today 14:00", ACTIONS)
    service.handle_update(_text_update("done", reply_to=n.tg_message_id))
    assert service.ignored_messages == 1
    assert ct.calls == [] and telegram.replies == []


def test_done_tap_acks_in_central_time_edits_the_message_and_answers_once(capture_setup):
    service, store, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "call the notary", "today 14:00", ACTIONS)
    service.handle_update(_tap(n.id, "done"))
    assert ct.calls == [("POST", "/api/reminders/fire-1/ack", {"action": "done"})]
    assert store.get_notification(n.id).action_taken == "done"
    assert telegram.edited_messages[-1] == {"chat_id": telegram.owner_id, "message_id": n.tg_message_id,
                                            "text": "✅ <s>call the notary</s>", "reply_markup": {"inline_keyboard": []}}
    assert [a["text"] for a in telegram.callback_answers] == ["Done"]


def test_tomorrow_tap(capture_setup):
    service, _, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "call the notary", "today 14:00", ACTIONS)
    service.handle_update(_tap(n.id, "tomorrow"))
    assert ct.calls[0][2] == {"action": "tomorrow"}
    assert telegram.edited_messages[-1]["text"] == "⏭ <s>call the notary</s>\n<i>moved to tomorrow</i>"
    assert telegram.callback_answers[-1]["text"] == "Moved to tomorrow"


def test_second_tap_unknown_id_and_unoffered_action(capture_setup):
    service, _, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "x", None, ACTIONS[:1])
    service.handle_update(_tap(n.id, "tomorrow"))          # not offered
    service.handle_update(_tap("nope", "done"))            # unknown
    service.handle_update(_tap(n.id, "done"))
    service.handle_update(_tap(n.id, "done"))              # already handled
    assert len(ct.calls) == 1
    assert [a["text"] for a in telegram.callback_answers] == ["Unknown reminder", "Unknown reminder", "Done", "Already handled"]
    assert len(telegram.edited_messages) == 1


@pytest.mark.parametrize("data", ["r:", "r:x", "r::done", "r:done", "r:nope:done:extra", "r:" + "x" * 80, "r:ü:done"])
def test_forged_or_malformed_reminder_data_is_answered_and_ignored(capture_setup, data):
    service, store, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "x", None, ACTIONS)
    service.handle_update({"update_id": 3, "callback_query": {"id": "cb-9", "from": {"id": 12345}, "data": data}})
    assert [a["text"] for a in telegram.callback_answers] == ["Unknown reminder"]
    assert ct.calls == [] and telegram.edited_messages == []
    assert store.get_notification(n.id).action_taken is None


def test_reminder_and_ask_taps_never_cross(capture_setup):
    """r: cannot shadow d:/s: and the ask prefixes cannot reach a reminder: a
    d: tap carrying a notification id is an unknown ask, an r: tap carrying an
    ask id is an unknown reminder, and the ask flow still works beside it."""
    service, store, telegram, ct = capture_setup
    item, _ = service.post("Orbit", "rotate the key")
    n, _ = service.notify("reminder", "fire-1", "call the notary", None, ACTIONS)
    cb = lambda data: {"update_id": 4, "callback_query": {"id": "cb-2", "from": {"id": 12345}, "data": data}}
    service.handle_update(cb(f"d:{n.id}"))
    service.handle_update(cb(f"s:{n.id}"))
    service.handle_update(cb(f"r:{item.id}:done"))
    service.handle_update(cb(f"rr:{n.id}:done"))
    service.handle_update(cb(f"r{n.id}:done"))
    assert [a["text"] for a in telegram.callback_answers] == ["Unknown ask", "Unknown ask", "Unknown reminder", "Ignored", "Ignored"]
    assert ct.calls == [] and telegram.edited_messages == []
    assert store.get_item(item.id).status == "open"
    assert store.get_notification(n.id).action_taken is None
    service.handle_update(cb(f"d:{item.id}"))
    service.handle_update(cb(f"r:{n.id}:done"))
    assert store.get_item(item.id).status == "done"
    assert store.get_notification(n.id).action_taken == "done"
    assert ct.calls == [("POST", "/api/reminders/fire-1/ack", {"action": "done"})]
    assert [a["text"] for a in telegram.callback_answers][-2:] == ["Done", "Done"]


def test_a_repeat_ack_in_central_time_still_closes_the_message(capture_setup):
    """applied:false: Central Time had already recorded an ack for this fire
    (its row outlived a mailbox restart). The message closes with the action
    Central Time holds, and the owner is told."""
    service, store, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "call the notary", None, ACTIONS)
    ct.answer = {"fire": {"id": "fire-1", "ack": "tomorrow"}, "item": {"id": "item-1"}, "applied": False}
    service.handle_update(_tap(n.id, "done"))
    assert store.get_notification(n.id).action_taken == "tomorrow"
    assert telegram.edited_messages[-1]["text"] == "⏭ <s>call the notary</s>\n<i>moved to tomorrow</i>"
    assert telegram.callback_answers[-1]["text"] == "Already handled in Central Time"


@pytest.mark.parametrize("status, reason, answer", [
    (409, "recurring", "Not possible: it repeats"),
    (409, "stale", "Not possible: it was already moved"),
    (409, "untimed", "Not possible: it has no time now"),
    (409, "unanchored", "Not possible: it has no time now"),
    (409, None, "Not possible now"),
    (0, None, "Central Time did not answer; try again"),
    (502, None, "Central Time did not answer; try again"),
    (401, None, "Central Time refused the token"),
    (403, None, "Central Time refused the token"),
])
def test_tap_when_central_time_refuses_keeps_the_keyboard(capture_setup, status, reason, answer, caplog):
    """A refusal of THIS action leaves the message as it is: Done may still apply."""
    service, store, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "call the notary", None, ACTIONS)
    ct.fail = CentralTimeError(status, "ack", reason)
    with caplog.at_level("WARNING", logger="mailbox"):
        service.handle_update(_tap(n.id, "tomorrow"))
    assert telegram.callback_answers[-1]["text"] == answer
    assert telegram.edited_messages == []
    assert store.get_notification(n.id).action_taken is None
    assert any(f"reminder ack failed: ack {status}" in r.getMessage() for r in caplog.records)
    assert not any("call the notary" in r.getMessage() for r in caplog.records)
    ct.fail = None
    service.handle_update(_tap(n.id, "done"))              # the keyboard is still live
    assert store.get_notification(n.id).action_taken == "done"


@pytest.mark.parametrize("status, reason, outcome, text, answer", [
    (409, "closed", "closed", "✅ <s>call the notary</s>\n<i>already closed in Central Time</i>", "Already closed in Central Time"),
    (404, None, "gone", "🚫 <s>call the notary</s>\n<i>gone from Central Time</i>", "Gone from Central Time"),
])
def test_tap_on_a_finished_item_closes_the_message(capture_setup, status, reason, outcome, text, answer):
    """closed and gone end the reminder for both actions: the message is struck
    through, the keyboard goes, and a later tap is already handled."""
    service, store, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "call the notary", None, ACTIONS)
    ct.fail = CentralTimeError(status, "ack", reason)
    service.handle_update(_tap(n.id, "done"))
    assert telegram.callback_answers[-1]["text"] == answer
    assert telegram.edited_messages[-1]["text"] == text
    assert telegram.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}
    assert store.get_notification(n.id).action_taken == outcome
    service.handle_update(_tap(n.id, "tomorrow"))
    assert telegram.callback_answers[-1]["text"] == "Already handled"
    assert len(ct.calls) == 1


def test_tap_without_a_central_time_client(setup):
    service, store, telegram = setup
    n, _ = service.notify("reminder", "fire-1", "x", None, ACTIONS)
    service.handle_update(_tap(n.id, "done"))
    assert telegram.callback_answers[-1]["text"] == "Central Time is not configured"
    assert store.get_notification(n.id).action_taken is None


def test_tap_from_a_wrong_owner_is_dropped(capture_setup):
    service, _, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "x", None, ACTIONS)
    service.handle_update(_tap(n.id, "done", from_id=999))
    assert service.dropped_updates == 1 and ct.calls == [] and telegram.callback_answers == []


def test_tap_edit_failure_keeps_the_ack(capture_setup, monkeypatch):
    """The ack is in Central Time before the edit: a Telegram failure on the
    edit is logged by type and the tap still answers Done."""
    service, store, telegram, ct = capture_setup
    n, _ = service.notify("reminder", "fire-1", "x", None, ACTIONS)

    def refuse(*args, **kwargs):
        raise TelegramError("editMessageText: nope")

    monkeypatch.setattr(telegram, "edit_message_text", refuse)
    service.handle_update(_tap(n.id, "done"))
    assert store.get_notification(n.id).action_taken == "done"
    assert telegram.callback_answers[-1]["text"] == "Done"


def test_capture_dates_a_bare_time_by_the_household_wall_clock(capture_setup):
    """15:00 Amsterdam is 13:00 UTC: `call 14:00` typed then is tomorrow by the
    household clock, today by a UTC one (final-review C1). The clock is aware."""
    from zoneinfo import ZoneInfo
    service, store, telegram, ct = capture_setup
    service.now = lambda: datetime(2026, 10, 17, 13, 0, tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Amsterdam"))
    service.handle_update(_text_update("call the notary 14:00"))
    assert ct.calls[-1][2]["anchor"] == {"date": "2026-10-18", "allDay": False, "time": "14:00"}
    assert "tomorrow 14:00" in telegram.sent_messages[-1]["text"]


# --- step answers ---------------------------------------------------

from pathlib import Path  # noqa: E402

from mailbox.service import AskClosed, InvalidAnswers  # noqa: E402

QUESTIONS = json.loads(
    (Path(__file__).parent / "answers-vectors.json").read_text(encoding="utf-8")
)["instructable"]


def _question_ask(service):
    item, _ = service.post("Atlas", QUESTIONS["title"], instructable=QUESTIONS)
    return item


def _pick(service, data, cb_id="cb_a"):
    service.handle_update({"callback_query": {
        "id": cb_id, "from": {"id": service.telegram.owner_id}, "data": data}})


def _answer_texts(store, item_id):
    return [e.text for e in store.events_for_item(item_id) if e.kind == "answer"]


def test_a_question_ask_is_posted_with_its_button_rows(setup):
    service, _, telegram = setup
    item = _question_ask(service)
    rows = telegram.sent_messages[0]["reply_markup"]["inline_keyboard"]
    assert [row[0]["callback_data"] for row in rows[:3]] == [
        f"a:{item.id}:1:0", f"a:{item.id}:4:0", f"a:{item.id}:4.2:0"]
    assert rows[3][0]["callback_data"] == f"d:{item.id}"


def test_a_tap_records_the_answer_and_ticks_the_button(setup):
    service, store, telegram = setup
    item = _question_ask(service)
    _pick(service, f"a:{item.id}:1:0")
    assert _answer_texts(store, item.id) == ['{"step":"1","id":"s1","picked":["Staging"]}']
    assert telegram.callback_answers[-1]["text"] == "1: Staging"
    assert telegram.edited_markups[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "1 · ✓ Staging"
    _pick(service, f"a:{item.id}:1:1")            # another tap replaces the answer
    assert telegram.callback_answers[-1]["text"] == "1: Prod"
    assert telegram.edited_markups[-1]["reply_markup"]["inline_keyboard"][0][1]["text"] == "✓ Prod"
    assert len(_answer_texts(store, item.id)) == 2


def test_re_tapping_the_current_pick_stores_nothing(setup):
    service, store, telegram = setup
    item = _question_ask(service)
    _pick(service, f"a:{item.id}:4.2:1")
    edits = len(telegram.edited_markups)
    _pick(service, f"a:{item.id}:4.2:1")
    assert _answer_texts(store, item.id) == ['{"step":"4.2","id":"s4.2","picked":["Long"]}']
    assert telegram.callback_answers[-1]["text"] == "4.2: Long"
    assert len(telegram.edited_markups) == edits


def test_a_tap_on_a_closed_or_unknown_ask_is_refused(setup):
    service, store, telegram = setup
    item = _question_ask(service)
    service.resolve(item.id, "done")
    _pick(service, f"a:{item.id}:1:0")
    _pick(service, "a:nope:1:0")
    assert [a["text"] for a in telegram.callback_answers] == ["This ask is closed"] * 2
    assert _answer_texts(store, item.id) == []


def test_a_tap_on_a_choice_that_is_gone(setup):
    service, store, telegram = setup
    item = _question_ask(service)
    for data in (f"a:{item.id}:1:2", f"a:{item.id}:5:0", f"a:{item.id}:1:x"):
        _pick(service, data)
    assert [a["text"] for a in telegram.callback_answers] == ["That choice is gone"] * 3
    assert _answer_texts(store, item.id) == []


def test_a_tap_from_someone_else_is_dropped(setup):
    service, store, telegram = setup
    item = _question_ask(service)
    service.handle_update({"callback_query": {"id": "x", "from": {"id": 999}, "data": f"a:{item.id}:1:0"}})
    assert telegram.callback_answers == [] and _answer_texts(store, item.id) == []


def test_a_failed_keyboard_edit_never_fails_the_answers(setup):
    service, store, telegram = setup
    item = _question_ask(service)

    def raising(*args, **kwargs):
        raise TelegramError("editMessageReplyMarkup 400: Bad Request: message to edit not found")

    telegram.edit_message_reply_markup = raising
    service.answer(item.id, [{"step": "3", "text": "EU"}])
    assert _answer_texts(store, item.id) == ['{"step":"3","id":"s3","text":"EU"}']


def test_answer_refuses_closed_asks_and_invalid_batches(setup):
    service, store, _ = setup
    item = _question_ask(service)
    with pytest.raises(InvalidAnswers) as excinfo:
        service.answer(item.id, [{"step": "1", "picked": ["Nope"]}])
    assert excinfo.value.details == ["answers[0].picked[0]: not one of the choices"]
    service.resolve(item.id, "declined")
    with pytest.raises(AskClosed):
        service.answer(item.id, [{"step": "1", "picked": ["Prod"]}])
    assert _answer_texts(store, item.id) == []


def test_reopen_brings_the_question_rows_back_with_the_current_tick(setup):
    service, _, telegram = setup
    item = _question_ask(service)
    _pick(service, f"a:{item.id}:4:1")
    service.resolve(item.id, "done")
    assert telegram.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}
    service.reopen(item.id)
    rows = telegram.edited_messages[-1]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[1]] == ["4 · Yes", "✓ No"]


def test_ack_keeps_the_question_rows(setup):
    service, _, telegram = setup
    item = _question_ask(service)
    _pick(service, f"a:{item.id}:1:1")
    service.ack(item.id)
    assert telegram.edited_messages[-1]["reply_markup"]["inline_keyboard"][0][1]["text"] == "✓ Prod"


# --- amend and Telegram (Pond 1.1.0) -------------------------------------------

import hashlib as _hashlib  # noqa: E402
from datetime import timedelta as _td, timezone as _tz  # noqa: E402

from mailbox.instructable import validate as _validate_ins  # noqa: E402

_KEY = "e" * 43
_DOC = {"v": 1, "title": "Rotate the key",
        "steps": [{"do": "Open the console"}, {"do": "Rotate", "code": "rotate --now"}]}


class _PinnedClock:
    def __init__(self):
        self.now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=_tz.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def pond(setup):
    service, store, telegram = setup
    clock = _PinnedClock()
    service.utcnow = clock
    store.set_setting("corrections_enabled", "1")
    return service, store, telegram, clock


def _post_doc(service, doc=_DOC):
    clean, errors = _validate_ins(doc)
    assert errors == []
    return service.post("Orbit", clean["title"], instructable=clean,
                        edit_key_hash=_hashlib.sha256(_KEY.encode()).hexdigest())


def _amend_doc(service, item, resp, code="rotate --later", reason="wrong flag"):
    new = json.loads(service.store.current_revision(item.id).body)
    new["steps"][1]["code"] = code
    current = service.store.current_revision(item.id)
    return service.amend(item.id, _KEY, current.rev, current.hash, reason, new)


def test_a_telegram_failure_during_amend_still_stores_the_revision(pond):
    service, store, telegram, clock = pond
    item, resp = _post_doc(service)
    clock.now += _td(seconds=61)

    def boom(method, **params):
        raise TelegramError(f"{method}: Bad Request: message can't be edited")

    telegram._call = boom
    _, rev2, _ = _amend_doc(service, item, resp)
    assert rev2.rev == 2
    assert store.current_revision(item.id).rev == 2
    kinds = [e.kind for e in store.events_for_item(item.id)]
    assert kinds[-2:] == ["revised", "edit_failed"]


def _cb(service, data, update_id=900):
    service.handle_update({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": service.telegram.owner_id}, "data": data}})
    return service.telegram.callback_answers[-1]["text"]


def _text_reply(service, text, reply_to):
    service.handle_update({"update_id": 901, "message": {
        "message_id": 500, "from": {"id": service.telegram.owner_id},
        "chat": {"id": service.telegram.owner_id, "type": "private"},
        "text": text, "reply_to_message": {"message_id": reply_to}}})


_Q_DOC = {"v": 1, "title": "Deploy it",
          "steps": [{"do": "Build", "code": "make"},
                    {"do": "Pick", "code": "deploy", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}}]}


def _accept_current(service, item):
    current = service.store.current_revision(item.id)
    return service.accept(item.id, current.rev, current.hash, "owner-key")


def test_the_amend_flow_on_telegram(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service)
    first = json.loads(item.payload)["tg_message_id"]
    clock.now += _td(seconds=61)
    sent_before = len(telegram.sent_messages)
    _amend_doc(service, item, None)
    assert telegram.edited_messages[-1]["message_id"] == first
    assert telegram.edited_messages[-1]["text"].startswith("<s>Rotate the key</s>")
    assert telegram.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}
    corrected = telegram.sent_messages[sent_before]
    assert corrected["reply_to_message_id"] == first
    assert corrected["text"].startswith("✏️ <b>Corrected</b> · version 2")
    assert "rotate --later" not in corrected["text"]
    assert corrected["reply_markup"] == telegram.pending_keyboard(item.id, None)
    assert corrected["disable_notification"] is True             # rev 1 is 61 s old: under 10 minutes
    second_id = len(telegram.sent_messages)
    assert store.newest_tg_message(item.id) == (second_id, 2)
    # A second correction 5 minutes later: silent, as a reply to the rev-2 message.
    clock.now += _td(minutes=5)
    _amend_doc(service, item, None, code="rotate --much-later")
    third = telegram.sent_messages[-1]
    assert third["reply_to_message_id"] == second_id and third["disable_notification"] is True


def test_a_correction_after_ten_minutes_notifies(pond):
    service, _, telegram, clock = pond
    item, _ = _post_doc(service)
    clock.now += _td(minutes=11)
    _amend_doc(service, item, None)
    assert telegram.sent_messages[-1]["disable_notification"] is False


def test_taps_after_a_correction(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service)
    clock.now += _td(seconds=61)
    _amend_doc(service, item, None)
    assert _cb(service, f"d:{item.id}:1") == "This ask changed. Use the newest message."
    assert _cb(service, f"d:{item.id}:2") == "This ask changed. Use the newest message."
    assert store.get_item(item.id).status == "open"
    _accept_current(service, item)
    newest = store.newest_tg_message(item.id)[0]
    edit = telegram.edited_messages[-1]
    assert edit["message_id"] == newest and "rotate" not in edit["text"]
    assert [b["callback_data"] for b in edit["reply_markup"]["inline_keyboard"][0]] == [f"d:{item.id}:2", f"s:{item.id}"]
    assert _cb(service, f"d:{item.id}:2") == "Done"
    assert store.get_item(item.id).status == "done"
    done_edit = telegram.edited_messages[-1]
    assert done_edit["message_id"] == newest and done_edit["reply_markup"] == {"inline_keyboard": []}


def test_skip_always_works_after_a_correction(pond):
    service, store, _, clock = pond
    item, _ = _post_doc(service)
    clock.now += _td(seconds=61)
    _amend_doc(service, item, None)
    assert _cb(service, f"s:{item.id}") == "Skipped"
    assert store.get_item(item.id).status == "dismissed"


def test_an_answer_after_accepting_redraws_the_corrected_message(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service, _Q_DOC)
    first = json.loads(item.payload)["tg_message_id"]
    clock.now += _td(seconds=61)
    _amend_doc(service, item, None)
    _accept_current(service, item)
    newest = store.newest_tg_message(item.id)[0]
    assert _cb(service, f"a:{item.id}:2:1:1") == "This ask changed. Use the newest message."
    assert _cb(service, f"a:{item.id}:2:1:2") == "2: Prod"
    markup = telegram.edited_markups[-1]
    assert markup["message_id"] == newest != first
    data = [b["callback_data"] for row in markup["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert all(d.endswith(":2") for d in data if not d.startswith("s:"))
    assert "✓ Prod" in markup["reply_markup"]["inline_keyboard"][0][1]["text"]
    assert all(m["message_id"] != first for m in telegram.edited_markups)


def test_a_done_reply_to_the_superseded_message(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service)
    first = json.loads(item.payload)["tg_message_id"]
    clock.now += _td(seconds=61)
    _amend_doc(service, item, None)
    _accept_current(service, item)
    _text_reply(service, "done, rotated", first)
    assert store.get_item(item.id).status == "open"
    assert telegram.sent_messages[-1]["text"] == "This message is out of date. Reply to the newest one."
    assert telegram.sent_messages[-1]["reply_to_message_id"] == 500
    reply = store.events_for_item(item.id)[-1]
    assert (reply.kind, reply.rev, reply.auth) == ("reply", 1, "telegram")


def test_a_done_reply_to_an_unaccepted_correction(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service)
    clock.now += _td(seconds=61)
    _amend_doc(service, item, None)
    newest = store.newest_tg_message(item.id)[0]
    _text_reply(service, "done", newest)                 # raises nothing
    assert store.get_item(item.id).status == "open"
    assert telegram.sent_messages[-1]["text"] == "This message is out of date. Reply to the newest one."
    _accept_current(service, item)
    _text_reply(service, "done", newest)
    assert store.get_item(item.id).status == "done"


def test_ack_on_a_pending_correction_renders_the_version_in_force(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service)
    clock.now += _td(seconds=61)
    _amend_doc(service, item, None)
    service.ack(item.id)
    edit = telegram.edited_messages[-1]
    assert edit["message_id"] == store.newest_tg_message(item.id)[0]
    assert edit["text"].endswith("\nCorrected: review in Hook")
    assert "1. Open the console" in edit["text"]
    assert edit["reply_markup"] == telegram.pending_keyboard(item.id, None)


def test_show_open_again_records_a_fresh_message(pond):
    service, store, telegram, clock = pond
    item, _ = _post_doc(service)
    service.resolve(item.id, "done", actor="session")
    real = telegram._call

    def no_edits(method, **params):
        if method == "editMessageText":
            raise TelegramError("editMessageText: Bad Request: message to edit not found")
        return real(method, **params)

    telegram._call = no_edits
    service.reopen(item.id, actor="session")
    telegram._call = real
    fresh = len(telegram.sent_messages)
    assert store.tg_message(fresh) == (item.id, 1)
    assert store.newest_tg_message(item.id) == (fresh, 1)


def test_steps_are_a_session_note_and_refuse_secrets(pond):
    from mailbox.service import AmendError
    service, store, telegram, _ = pond
    item, _ = _post_doc(service)
    service.steps(item.id, "1. <b>do</b> this")
    sent = telegram.sent_messages[-1]
    assert sent["text"] == "📝 Session note, not part of the ask:\n1. &lt;b&gt;do&lt;/b&gt; this"
    assert sent["reply_to_message_id"] == store.newest_tg_message(item.id)[0]
    with pytest.raises(AmendError) as caught:
        service.steps(item.id, "use sk-ant-" + "x" * 10)
    assert caught.value.status == 400 and caught.value.body == {
        "error": "invalid steps", "details": ["text: secret-like value"]}


# --- Hook bugs (Pond 1.2.0) ----------------------------------------------------

BUG_SIG = "a3f9c2e17b04"


def bug_doc(sig=BUG_SIG, code="Crash signature: hook-crash-a3f9c2e17b04\nException: EXC_BAD_ACCESS"):
    return {"v": 1, "title": f"Hook crash: EXC_BAD_ACCESS in SettingsWindow.open ({sig})", "urgency": "whenever",
            "why": "Hook quit unexpectedly.", "steps": [{"do": "Read the crash report", "code": code, "lang": "text"}]}


def test_post_bug_files_an_ask_without_telegram(setup):
    service, store, telegram = setup
    item, resp = service.post_bug("Hook", bug_doc(), BUG_SIG)
    assert telegram.calls == []
    assert json.loads(item.payload)["tg_message_id"] is None
    assert resp["occurrences"] == 1 and not resp.get("deduped")
    assert resp["revision"]["rev"] == 1 and resp["editable"] is False
    meta = store.bug_meta(item.id)
    assert (meta["signature"], meta["count"]) == (BUG_SIG, 1)
    assert meta["excerpt"].startswith("Crash signature: hook-crash-")
    assert item.title == bug_doc()["title"] and item.project_id == "Hook" and item.lane == "personal"


def test_a_repeated_signature_adds_an_occurrence_and_no_item(setup):
    service, store, telegram = setup
    first, _ = service.post_bug("Hook", bug_doc(), BUG_SIG)
    again, resp = service.post_bug("Hook", bug_doc(code="Crash signature: hook-crash-a3f9c2e17b04\nsecond run"),
                                   BUG_SIG)
    assert again.id == first.id
    assert resp["deduped"] is True and resp["occurrences"] == 2
    assert len(store.list_items(kind="bug")) == 1
    assert store.bug_meta(first.id)["excerpt"].endswith("second run")
    events = [(e.kind, e.actor) for e in store.events_for_item(first.id)]
    assert events == [("posted", "hook"), ("occurrence", "hook")]
    assert telegram.calls == []


def test_a_closed_bugs_signature_opens_a_new_item(setup):
    service, store, telegram = setup
    first, _ = service.post_bug("Hook", bug_doc(), BUG_SIG)
    service.resolve(first.id, "done", note="fixed in 0.12.1", actor="session")
    second, resp = service.post_bug("Hook", bug_doc(), BUG_SIG)
    assert second.id != first.id and resp["occurrences"] == 1
    assert telegram.calls == []


def test_resolve_reopen_and_answers_on_a_bug_never_touch_telegram(setup):
    service, store, telegram = setup
    item, _ = service.post_bug("Hook", bug_doc(), BUG_SIG)
    service.ack(item.id)
    service.resolve(item.id, "done", note="fixed", actor="owner")
    service.reopen(item.id)
    service._redraw_keyboard(item.id)
    service.resolve(item.id, "declined", note="not ours", actor="session")
    assert telegram.calls == []
    assert store.get_item(item.id).status == "dismissed"


def test_a_plain_ask_with_a_bugs_title_is_not_deduped_into_it(setup):
    service, store, telegram = setup
    bug, _ = service.post_bug("Hook", bug_doc(), BUG_SIG)
    ask, resp = service.post("Hook", bug_doc()["title"], instructable=bug_doc())
    assert ask.id != bug.id and not resp.get("deduped")
    again, resp = service.post("Hook", bug_doc()["title"], instructable=bug_doc())
    assert again.id == ask.id and resp["deduped"] is True          # the 24-hour title dedupe is untouched


def test_a_bugs_wire_carries_kind_and_bug_and_a_plain_ask_does_not(setup):
    service, store, telegram = setup
    bug, _ = service.post_bug("Hook", bug_doc(), BUG_SIG)
    service.post_bug("Hook", bug_doc(), BUG_SIG)
    ask, _ = service.post("Orbit", "rotate the key")
    for revisions in (True, False):
        wire = service.wire_item(store.get_item(bug.id), revisions=revisions)
        assert wire["kind"] == "bug"
        assert set(wire["bug"]) == {"signature", "count", "firstSeen", "lastSeen"}
        assert (wire["bug"]["signature"], wire["bug"]["count"]) == (BUG_SIG, 2)
        plain = service.wire_item(store.get_item(ask.id), revisions=revisions)
        assert "kind" not in plain and "bug" not in plain


# --- messages: a reply kind (Pond 1.2.0) ---------------------------------------

from mailbox.service import AmendError  # noqa: E402


def test_a_message_is_stored_as_a_message_event_verbatim_and_never_as_a_reply(setup):
    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key", instructable={
        "v": 1, "title": "rotate the key", "steps": [{"do": "Rotate it", "code": "rotate --now"}]})
    calls = len(telegram.calls)
    text = "  Use the staging key first.\nThen prod.  "
    service.reply(item.id, text, kind="message", auth="owner-key")
    events = store.events_for_item(item.id)
    assert [(e.kind, e.actor, e.text, e.rev, e.auth) for e in events if e.kind != "posted"] == [
        ("message", "owner", text, 1, "owner-key")]
    assert len(telegram.calls) == calls
    assert store.get_item(item.id).status == "open"


def test_a_plain_reply_is_unchanged(setup):
    service, store, telegram = setup
    item, _ = service.post("Orbit", "rotate the key")
    service.reply(item.id, "  yes  ")
    last = store.events_for_item(item.id)[-1]
    assert (last.kind, last.actor, last.text, last.rev, last.auth) == ("reply", "owner", "yes", None, "token")


@pytest.mark.parametrize("text,detail", [
    ("x" * 8001, "text: too long"),
    ("token ghp_" + "a" * 36, "text: secret-like value"),
])
def test_a_refused_message_names_the_field_and_never_echoes(setup, text, detail):
    service, store, _ = setup
    item, _ = service.post("Orbit", "rotate the key")
    with pytest.raises(AmendError) as exc:
        service.reply(item.id, text, kind="message")
    assert exc.value.status == 400 and exc.value.body == {"error": "invalid message", "details": [detail]}
    assert "ghp_" not in json.dumps(exc.value.body)
    assert [e.kind for e in store.events_for_item(item.id)] == ["posted"]


def test_message_up_to_8000(setup):
    service, store, _ = setup
    item, _ = service.post("Orbit", "rotate the key")
    service.reply(item.id, "m" * 8000, kind="message")
    assert store.events_for_item(item.id)[-1].text == "m" * 8000
    with pytest.raises(AmendError) as exc:
        service.reply(item.id, "m" * 8001, kind="message")
    assert (exc.value.status, exc.value.body) == (400, {"error": "invalid message", "details": ["text: too long"]})


def test_a_message_on_a_closed_ask_is_409(setup):
    service, store, _ = setup
    item, _ = service.post("Orbit", "rotate the key")
    service.resolve(item.id, "done")
    with pytest.raises(AmendError) as exc:
        service.reply(item.id, "one more thing", kind="message")
    assert (exc.value.status, exc.value.body) == (409, {"error": "ask is closed"})


def test_the_51st_message_is_409(setup):
    service, store, _ = setup
    item, _ = service.post("Orbit", "rotate the key")
    for n in range(50):
        service.reply(item.id, f"note {n}", kind="message")
    with pytest.raises(AmendError) as exc:
        service.reply(item.id, "one too many", kind="message")
    assert (exc.value.status, exc.value.body) == (409, {"error": "too many messages"})
    service.reply(item.id, "a plain reply still goes")
    assert sum(1 for e in store.events_for_item(item.id) if e.kind == "message") == 50


def test_an_unknown_reply_kind_raises(setup):
    service, _, _ = setup
    item, _ = service.post("Orbit", "rotate the key")
    with pytest.raises(ValueError):
        service.reply(item.id, "x", kind="note")


# --- requirements and progress (Pond 1.3.0) -------------------------------------

from mailbox.service import Corrected, InvalidProgress  # noqa: E402

_REQ_DOC = {"v": 1, "title": "Rotate the staging deploy key",
            "steps": [{"do": "Open the deploy settings"},
                      {"do": "Rotate the key", "required": True},
                      {"do": "Pick", "question": {"prompt": "Env?", "choices": ["Staging", "Preview"]}}],
            "reply": {"prompt": "Did the deploy pass?", "choices": []}}


def _req_ask(service, doc=_REQ_DOC, **top):
    clean, errors = _validate_ins({**doc, **top})
    assert errors == []
    return service.post("Orbit", clean["title"], instructable=clean,
                        edit_key_hash=_hashlib.sha256(_KEY.encode()).hexdigest())[0]


def _kinds(store, item_id):
    return [e.kind for e in store.events_for_item(item_id)]


def test_requirements_left_is_none_without_requirements(setup):
    service, _, _ = setup
    plain = service.post("Orbit", "plain ask")[0]
    assert service.requirements_left(plain.id) is None
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]})
    assert service.requirements_left(item.id) is None


def test_requirements_left_follows_ticks_answers_replies_and_messages(setup):
    service, _, _ = setup
    item = _req_ask(service, done=["steps", "reply", "message"])
    assert service.requirements_left(item.id) == [
        {"kind": "step", "id": "s1", "path": "1"}, {"kind": "step", "id": "s2", "path": "2"},
        {"kind": "step", "id": "s3", "path": "3"}, {"kind": "reply"}, {"kind": "message"}]
    service.progress(item.id, ["s1", "s2", "s3"], None, rev=1, hash=None, auth="token")
    service.answer(item.id, [{"step": "3", "picked": ["Staging"]}], rev=1)
    service.reply(item.id, "it passed")
    assert service.requirements_left(item.id) == [{"kind": "message"}]
    service.reply(item.id, "thanks", kind="message")
    assert service.requirements_left(item.id) == []


def test_a_progress_stores_one_canonical_event_with_ids_only(setup):
    service, store, _ = setup
    item = _req_ask(service)
    service.progress(item.id, ["s2", "s1", "s2"], [], rev=1, hash=None, auth="owner-key")
    event = store.events_for_item(item.id)[-1]
    assert (event.kind, event.actor, event.rev, event.auth) == ("progress", "owner", 1, "owner-key")
    assert event.text == '{"tick":["s1","s2"],"untick":[]}'
    service.progress(item.id, None, ["s1"], rev=1, hash=None, auth="token")
    assert store.events_for_item(item.id)[-1].text == '{"tick":[],"untick":["s1"]}'
    assert service.requirements_left(item.id) == []


def test_a_progress_that_changes_nothing_stores_nothing(setup):
    service, store, _ = setup
    item = _req_ask(service)
    service.progress(item.id, ["s2"], None, rev=1, hash=None, auth="token")
    before = _kinds(store, item.id)
    service.progress(item.id, ["s2"], ["s1"], rev=1, hash=None, auth="token")      # s2 ticked, s1 never was
    service.progress(item.id, [], [], rev=1, hash=None, auth="token")
    assert _kinds(store, item.id) == before
    # Only the effective change is recorded.
    service.progress(item.id, ["s1", "s2"], None, rev=1, hash=None, auth="token")
    assert store.events_for_item(item.id)[-1].text == '{"tick":["s1"],"untick":[]}'


def test_a_progress_on_an_ask_without_requirements_is_shared(setup):
    service, store, _ = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]})
    got = service.progress(item.id, ["s1"], None, rev=1, hash=None, auth="token")
    assert got.id == item.id
    assert _kinds(store, item.id)[-1] == "progress"


def test_progress_refusals(setup):
    service, _, _ = setup
    item = _req_ask(service)
    with pytest.raises(ValueError):
        service.progress("nope", ["s1"], None, rev=1, hash=None, auth="token")
    cases = [
        (("s1", None), ["tick: must be a list of step ids"]),
        ((None, {"s1": True}), ["untick: must be a list of step ids"]),
        ((["s1", "s9", 3], None), ["tick[1]: unknown step", "tick[2]: unknown step"]),
        ((["s1"], ["s2", "s1"]), ["untick[1]: also in tick"]),
        ((["s1"] * 62, None), ["tick: more than 61 ids"]),
        ((["2"], None), ["tick[0]: unknown step"]),                 # a path is not an id
    ]
    for (tick, untick), details in cases:
        with pytest.raises(InvalidProgress) as exc:
            service.progress(item.id, tick, untick, rev=1, hash=None, auth="token")
        assert exc.value.details == details, (tick, untick)


def test_progress_refusals_never_echo_a_value(setup):
    service, _, _ = setup
    item = _req_ask(service)
    with pytest.raises(InvalidProgress) as exc:
        service.progress(item.id, ["sk-ant-abc"], None, rev=1, hash=None, auth="token")
    assert "sk-ant" not in json.dumps(exc.value.details)


def test_progress_on_a_closed_ask(setup):
    service, _, _ = setup
    item = _req_ask(service)
    service.resolve(item.id, "declined", actor="owner")
    with pytest.raises(AskClosed):
        service.progress(item.id, ["s1"], None, rev=1, hash=None, auth="token")


def test_progress_is_bound_to_the_accepted_current_revision(pond):
    service, store, _, clock = pond
    item = _req_ask(service)
    with pytest.raises(Corrected):
        service.progress(item.id, ["s1"], None, rev=1, hash="0" * 64, auth="token")
    clock.now += _td(seconds=61)
    new = json.loads(store.current_revision(item.id).body)
    new["steps"][0]["code"] = "open settings"
    current = store.current_revision(item.id)
    service.amend(item.id, _KEY, current.rev, current.hash, "a code to copy", new)
    for rev in (1, 2):                                          # 2 isn't accepted yet
        with pytest.raises(Corrected):
            service.progress(item.id, ["s2"], None, rev=rev, hash=None, auth="token")
    assert "progress" not in _kinds(store, item.id)


def test_a_tick_survives_a_correction_that_only_toggles_required(pond):
    service, store, _, clock = pond
    item = _req_ask(service)
    service.progress(item.id, ["s1", "s2"], None, rev=1, hash=None, auth="token")
    clock.now += _td(seconds=61)
    new = json.loads(store.current_revision(item.id).body)
    new["steps"][0]["required"] = True
    new["steps"][1]["do"] = "Rotate the key now"
    current = store.current_revision(item.id)
    service.amend(item.id, _KEY, current.rev, current.hash, "step 1 matters too", new)
    _accept_current(service, item)
    assert service.requirements_left(item.id) == [{"kind": "step", "id": "s2", "path": "2"}]


def test_done_reply_reads_ready_after_a_telegram_text_reply(setup):
    service, _, telegram = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]}, done=["reply"])
    assert service.requirements_left(item.id) == [{"kind": "reply"}]
    _text_reply(service, "the deploy passed", json.loads(item.payload)["tg_message_id"])
    assert service.requirements_left(item.id) == []


def test_wire_item_carries_requirements_only_when_asked_and_present(setup):
    service, store, _ = setup
    item = _req_ask(service)
    plain = service.post("Orbit", "plain ask")[0]
    assert "requirements" not in service.wire_item(store.get_item(item.id), revisions=True)
    wired = service.wire_item(store.get_item(item.id), revisions=True, requirements=True)
    assert wired["requirements"] == {"left": [{"kind": "step", "id": "s2", "path": "2"}], "ready": False}
    assert "requirements" not in service.wire_item(store.get_item(plain.id), revisions=True, requirements=True)


# --- the owner's Done waits for the requirements (Pond 1.3.0) --------------------

from mailbox.service import CHANGED_TOAST, NOT_YET_REPLY, NOT_YET_TOAST, REPLY_KEPT, RequirementsUnmet  # noqa: E402

_STEP_2 = [{"kind": "step", "id": "s2", "path": "2"}]


def _resolved_kinds(store, item_id):
    return [k for k in _kinds(store, item_id) if k in ("resolved", "declined")]


def test_an_owner_done_with_something_left_is_refused_and_writes_nothing(setup):
    service, store, telegram = setup
    item = _req_ask(service, done=["reply"])
    before = (_kinds(store, item.id), len(telegram.calls))
    with pytest.raises(RequirementsUnmet) as exc:
        service.resolve(item.id, "done", actor="owner", rev=1, auth="token")
    assert exc.value.left == _STEP_2 + [{"kind": "reply"}]
    assert exc.value.body() == {"error": "requirements_unmet", "left": _STEP_2 + [{"kind": "reply"}],
                                "details": ["step 2", "the reply"]}
    assert store.get_item(item.id).status == "open"
    assert (_kinds(store, item.id), len(telegram.calls)) == before          # no event, no Telegram edit


def test_a_session_done_and_an_owner_skip_are_never_gated(setup):
    service, store, _ = setup
    first = _req_ask(service)
    assert service.resolve(first.id, "done", actor="session").status == "done"
    second = _req_ask(service, {**_REQ_DOC, "title": "Rotate the preview deploy key"})
    assert service.resolve(second.id, "declined", actor="owner", auth="token").status == "dismissed"
    assert _resolved_kinds(store, first.id) == ["resolved"] and _resolved_kinds(store, second.id) == ["declined"]


def test_a_stale_owner_done_is_corrected_before_requirements(setup):
    service, _, _ = setup
    item = _req_ask(service)
    with pytest.raises(Corrected):
        service.resolve(item.id, "done", actor="owner", rev=1, hash="0" * 64)
    with pytest.raises(Corrected):
        service.resolve(item.id, "done", actor="owner", rev=2)


def test_an_owner_done_closes_once_the_last_item_is_finished(setup):
    service, store, _ = setup
    item = _req_ask(service, done=["steps"])
    service.progress(item.id, ["s1", "s2"], None, rev=1, hash=None, auth="token")
    with pytest.raises(RequirementsUnmet) as exc:
        service.resolve(item.id, "done", actor="owner", rev=1)
    assert exc.value.left == [{"kind": "step", "id": "s3", "path": "3"}]     # a tick isn't an answer
    service.answer(item.id, [{"step": "3", "picked": ["Staging"]}], rev=1)
    assert service.resolve(item.id, "done", actor="owner", rev=1, auth="token").status == "done"


def test_an_ask_without_requirements_closes_as_before(setup):
    service, _, _ = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]})
    assert service.resolve(item.id, "done", actor="owner", rev=1).status == "done"
    plain = service.post("Orbit", "plain ask")[0]
    assert service.resolve(plain.id, "done", actor="owner").status == "done"


def test_a_reopened_ask_is_gated_the_same_way(setup):
    service, store, _ = setup
    item = _req_ask(service)
    service.resolve(item.id, "declined", actor="owner")
    service.reopen(item.id)
    with pytest.raises(RequirementsUnmet):
        service.resolve(item.id, "done", actor="owner", rev=1)
    assert store.get_item(item.id).status == "open"


def test_an_owner_done_on_a_skipped_ask_is_gated_too(setup):
    """Skip then Done never turns a skipped ask into a done one past its requirements."""
    service, store, _ = setup
    item = _req_ask(service)
    service.resolve(item.id, "declined", actor="owner")
    with pytest.raises(RequirementsUnmet):
        service.resolve(item.id, "done", actor="owner", rev=1)
    assert store.get_item(item.id).status == "dismissed"


def test_the_telegram_done_tap_toasts_and_redraws(setup):
    service, store, telegram = setup
    item = _req_ask(service)
    message_id = json.loads(item.payload)["tg_message_id"]
    assert _cb(service, f"d:{item.id}") == NOT_YET_TOAST
    assert store.get_item(item.id).status == "open"
    assert "resolved" not in _kinds(store, item.id)
    methods = [m for m, _ in telegram.calls]
    assert methods[-2:] == ["answerCallbackQuery", "editMessageReplyMarkup"]      # the toast first
    assert telegram.edited_markups[-1]["message_id"] == message_id
    assert sum(1 for m in methods if m == "answerCallbackQuery") == 1


def test_a_stale_telegram_done_tap_still_reads_changed(pond):
    service, store, _, clock = pond
    item = _req_ask(service)
    assert _cb(service, f"d:{item.id}:2") == CHANGED_TOAST


def test_a_verdict_word_reply_with_something_left_is_kept_as_a_reply(setup):
    service, store, telegram = setup
    item = _req_ask(service)
    message_id = json.loads(item.payload)["tg_message_id"]
    _text_reply(service, "done, rotated it", message_id)
    assert store.get_item(item.id).status == "open"
    reply = store.events_for_item(item.id)[-1]
    assert (reply.kind, reply.actor, reply.text, reply.auth) == ("reply", "owner", "done, rotated it", "telegram")
    assert telegram.replies[-1]["text"] == NOT_YET_REPLY
    assert telegram.replies[-1]["reply_to_message_id"] == 500
    assert telegram.edited_markups and telegram.edited_markups[-1]["message_id"] == message_id


def test_a_verdict_word_reply_that_finishes_done_reply_still_never_closes(setup):
    service, store, telegram = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]}, done=["reply"])
    _text_reply(service, "done: it passed", json.loads(item.payload)["tg_message_id"])
    assert store.get_item(item.id).status == "open"
    assert _kinds(store, item.id)[-1] == "reply"
    assert service.requirements_left(item.id) == []
    assert telegram.replies[-1]["text"] == REPLY_KEPT


def test_a_verdict_word_reply_without_requirements_resolves_as_before(setup):
    service, store, _ = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]})
    _text_reply(service, "done, rotated it", json.loads(item.payload)["tg_message_id"])
    assert store.get_item(item.id).status == "done"
    assert _kinds(store, item.id)[-1] == "resolved"


def test_a_skip_word_reply_with_something_left_closes(setup):
    service, store, _ = setup
    item = _req_ask(service)
    _text_reply(service, "skip, not today", json.loads(item.payload)["tg_message_id"])
    assert store.get_item(item.id).status == "dismissed"


def test_a_refused_done_tap_on_a_closed_ask_puts_no_keyboard_back(setup):
    service, store, telegram = setup
    item = _req_ask(service)
    service.resolve(item.id, "declined", actor="owner")
    markups = len(telegram.edited_markups)
    assert _cb(service, f"d:{item.id}") == NOT_YET_TOAST
    assert len(telegram.edited_markups) == markups
    assert store.get_item(item.id).status == "dismissed"


# --- Telegram hides Done until nothing is left (Pond 1.3.0) -----------------------


def _has_done(keyboard):
    return any(b.get("callback_data", "").startswith("d:") for row in keyboard["inline_keyboard"] for b in row)


def test_an_ask_with_requirements_is_posted_without_done(setup):
    service, _, telegram = setup
    _req_ask(service, done=["message"])
    sent = telegram.sent_messages[-1]
    assert not _has_done(sent["reply_markup"])
    assert "2. Rotate the key (required)" in sent["text"]
    assert sent["text"].endswith("Before Done: a message from Hook")
    _req_ask(service, {**_REQ_DOC, "title": "Plain", "steps": [{"do": "Open the deploy settings"}]})
    assert _has_done(telegram.sent_messages[-1]["reply_markup"])


def test_a_progress_that_finishes_the_last_item_redraws_once_with_done(setup):
    service, _, telegram = setup
    item = _req_ask(service)
    message_id = json.loads(item.payload)["tg_message_id"]
    service.progress(item.id, ["s1"], None, rev=1, hash=None, auth="token")     # stored, still not ready
    assert telegram.edited_markups == []
    service.progress(item.id, ["s2"], None, rev=1, hash=None, auth="token")
    assert len(telegram.edited_markups) == 1
    assert telegram.edited_markups[0]["message_id"] == message_id
    assert _has_done(telegram.edited_markups[0]["reply_markup"])
    service.progress(item.id, ["s2"], None, rev=1, hash=None, auth="token")     # changes nothing
    assert len(telegram.edited_markups) == 1


def test_an_untick_that_leaves_something_again_drops_done(setup):
    service, _, telegram = setup
    item = _req_ask(service)
    service.progress(item.id, ["s2"], None, rev=1, hash=None, auth="token")
    service.progress(item.id, None, ["s2"], rev=1, hash=None, auth="token")
    assert len(telegram.edited_markups) == 2
    assert not _has_done(telegram.edited_markups[-1]["reply_markup"])


def test_a_message_that_finishes_done_message_redraws(setup):
    service, _, telegram = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]}, done=["message"])
    service.reply(item.id, "Rotated it.", kind="message")
    assert len(telegram.edited_markups) == 1 and _has_done(telegram.edited_markups[0]["reply_markup"])
    service.reply(item.id, "And the preview one.", kind="message")              # still ready: no redraw
    assert len(telegram.edited_markups) == 1


def test_a_reply_that_finishes_done_reply_redraws_from_hook_and_telegram(setup):
    service, _, telegram = setup
    doc = {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]}
    first = _req_ask(service, doc, done=["reply"])
    service.reply(first.id, "it passed")
    assert len(telegram.edited_markups) == 1 and _has_done(telegram.edited_markups[0]["reply_markup"])
    second = _req_ask(service, {**doc, "title": "Rotate the preview deploy key"}, done=["reply"])
    _text_reply(service, "the deploy passed", json.loads(second.payload)["tg_message_id"])
    assert len(telegram.edited_markups) == 2 and _has_done(telegram.edited_markups[1]["reply_markup"])
    _text_reply(service, "and again", json.loads(second.payload)["tg_message_id"])
    assert len(telegram.edited_markups) == 2


def test_a_reply_on_an_ask_without_requirements_redraws_nothing(setup):
    service, _, telegram = setup
    item = service.post("Orbit", "plain ask")[0]
    service.reply(item.id, "ok")
    _text_reply(service, "noted", json.loads(item.payload)["tg_message_id"])
    assert telegram.edited_markups == []


def test_a_redraw_after_the_refused_done_tap_has_no_done(setup):
    service, _, telegram = setup
    item = _req_ask(service)
    _cb(service, f"d:{item.id}")
    assert not _has_done(telegram.edited_markups[-1]["reply_markup"])


def test_the_verdict_word_reply_that_finishes_done_reply_brings_done_back(setup):
    service, _, telegram = setup
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]}, done=["reply"])
    _text_reply(service, "done: it passed", json.loads(item.payload)["tg_message_id"])
    assert _has_done(telegram.edited_markups[-1]["reply_markup"])


def test_a_corrected_ask_keeps_the_pending_keyboard(pond):
    service, store, telegram, clock = pond
    item = _req_ask(service, {**_REQ_DOC, "steps": [{"do": "Open the deploy settings"}]}, done=["reply"])
    clock.now += _td(seconds=61)
    new = json.loads(store.current_revision(item.id).body)
    new["steps"][0]["code"] = "open settings"
    current = store.current_revision(item.id)
    service.amend(item.id, _KEY, current.rev, current.hash, "a code to copy", new)
    service.reply(item.id, "it passed")
    assert all(not _has_done(m["reply_markup"]) for m in telegram.edited_markups)


# --- a word choice tapped on Telegram (Pond 1.4.0) -------------------------------------

WORD_CHOICE_ASK = {"v": 1, "title": "Roll out the change", "steps": [
    {"do": "Pick the rollout", "question": {"prompt": "How?", "choices": [
        "All at once", {"label": "Mix", "text": "What should change?"}]}}]}


def test_tap_on_word_choice_records_label(setup):
    service, store, telegram = setup
    item, _ = service.post("Atlas", WORD_CHOICE_ASK["title"], instructable=WORD_CHOICE_ASK)
    row = telegram.sent_messages[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["1 · All at once", "Mix"]
    _pick(service, f"a:{item.id}:1:1")
    assert telegram.callback_answers[-1]["text"] == "1: Mix"
    assert [json.loads(t) for t in _answer_texts(store, item.id)] == [{"step": "1", "id": "s1", "picked": ["Mix"]}]
    _pick(service, f"a:{item.id}:1:1", cb_id="again")                     # the current pick: nothing new
    assert len(_answer_texts(store, item.id)) == 1


def test_an_answer_with_words_through_the_service(setup):
    service, store, _ = setup
    item, _ = service.post("Atlas", WORD_CHOICE_ASK["title"], instructable=WORD_CHOICE_ASK)
    service.answer(item.id, [{"step": "1", "picked": ["Mix"], "text": "Staff on Monday"}])
    assert _answer_texts(store, item.id) == ['{"step":"1","id":"s1","picked":["Mix"],"text":"Staff on Monday"}']

# --- long answers (Pond 1.15.0, feature `longAnswers`) -------------------------

LONG_ANSWER_ASK = {"v": 1, "title": "Report the test run", "steps": [
    {"do": "Pick the rollout", "question": {"prompt": "How?", "choices": [
        "All at once", {"label": "Mix", "text": "What should change?"}]}},
    {"do": "Paste the summary", "question": {"prompt": "What did the run say?"}}]}


@pytest.mark.parametrize("mode", ["full", "alert"])
def test_a_long_answer_is_stored_whole_and_never_sent_to_telegram(setup, mode):
    """An 8000-character answer (free text, and words on a pick) is stored verbatim. Telegram
    shows picks only, never an answer's text, so no edit it causes can pass Telegram's 4096
    limit; the whole text is in Hook."""
    from mailbox.telegram import TELEGRAM_LIMIT
    service, store, telegram = setup
    if mode == "alert":
        _alert(service)
    item, _ = service.post("Atlas", LONG_ANSWER_ASK["title"], instructable=LONG_ANSWER_ASK)
    words = "w" * 7999 + "W"
    summary = ("PASS test_case\n" * 600)[:7999] + "S"
    assert len(words) == len(summary) == 8000
    before = len(telegram.calls)
    service.answer(item.id, [{"step": "1", "picked": ["Mix"], "text": words},
                             {"step": "2", "text": summary}])
    stored = [json.loads(t) for t in _answer_texts(store, item.id)]
    assert [{k: v for k, v in a.items() if k != "id"} for a in stored] == [
        {"step": "1", "picked": ["Mix"], "text": words}, {"step": "2", "text": summary}]
    calls = telegram.calls[before:]
    assert calls, "the keyboard (or the alert text) is redrawn"
    for _, params in calls:
        dumped = json.dumps(params, ensure_ascii=False)
        assert "wwwwwwwwww" not in dumped and "PASS test_case" not in dumped
        if isinstance(params.get("text"), str):
            assert len(params["text"].encode("utf-16-le")) // 2 <= TELEGRAM_LIMIT


def test_an_answer_one_past_the_cap_is_refused_and_nothing_is_stored(setup):
    from mailbox.service import InvalidAnswers
    service, store, _ = setup
    item, _ = service.post("Atlas", LONG_ANSWER_ASK["title"], instructable=LONG_ANSWER_ASK)
    with pytest.raises(InvalidAnswers) as caught:
        service.answer(item.id, [{"step": "2", "text": "t" * 8001}])
    assert caught.value.details == ["answers[0].text: longer than 8000 characters"]
    assert _answer_texts(store, item.id) == []

# --- Telegram alert-only ask interactions (Pond 1.5.0) -------------------------

def _alert(service):
    service.telegram_mode = "alert"
    service.open_base = "https://pond.example.test/i"


def _assert_alert(message, item_id):
    assert message["reply_markup"] == {"inline_keyboard": [[{
        "text": "📋 Open in Hook", "url": f"https://pond.example.test/i/{item_id}"}]]}
    assert "Open in Hook" in message["text"]
    assert "Urgency:" in message["text"]
    for secret in ("private why", "private command", "private detail", "private note", "wrong flag"):
        assert secret not in message["text"]


def test_alert_post_ack_reopen_and_resolution_keep_only_headline(setup):
    service, store, telegram = setup
    _alert(service)
    item, _ = service.post("Example", "Test alert", why="private why", cmd="private command")
    _assert_alert(telegram.sent_messages[-1], item.id)
    service.ack(item.id)
    _assert_alert(telegram.edited_messages[-1], item.id)
    before = len(telegram.calls)
    service.steps(item.id, "private detail")
    assert len(telegram.calls) == before
    assert store.events_for_item(item.id)[-1].text == "private detail"
    service.resolve(item.id, "done", note="private note")
    closed = telegram.edited_messages[-1]
    assert "private note" not in closed["text"] and "Urgency: today" in closed["text"]
    assert closed["reply_markup"] == {"inline_keyboard": []}
    assert store.events_for_item(item.id)[-1].text == "private note"
    service.reopen(item.id)
    _assert_alert(telegram.edited_messages[-1], item.id)


def test_alert_corrections_accept_and_requirements_redraw(pond):
    service, store, telegram, clock = pond
    _alert(service)
    item, response = _post_doc(service)
    _assert_alert(telegram.sent_messages[-1], item.id)
    clock.now += _td(seconds=61)
    _amend_doc(service, item, response)
    _assert_alert(telegram.sent_messages[-1], item.id)
    service.ack(item.id)
    _assert_alert(telegram.edited_messages[-1], item.id)
    _accept_current(service, item)
    _assert_alert(telegram.edited_messages[-1], item.id)
    service._redraw_keyboard(item.id)
    _assert_alert(telegram.edited_messages[-1], item.id)


@pytest.mark.parametrize("prefix", ["d:", "s:", "a:"])
def test_alert_refuses_old_ask_callbacks_without_events(setup, prefix):
    service, store, telegram = setup
    item, _ = service.post("Example", "Old full message")
    before = store.events_for_item(item.id)
    _alert(service)
    service.handle_update({"callback_query": {"id": "tap", "from": {"id": telegram.owner_id},
                                               "data": prefix + item.id + (":1:0" if prefix == "a:" else "")}})
    assert store.events_for_item(item.id) == before
    assert store.get_item(item.id).status == "open"
    assert len(telegram.callback_answers) == 1
    assert "Open in Hook" in telegram.callback_answers[0]["text"]


@pytest.mark.parametrize("text", ["done", "skip", "An ordinary reply"])
def test_alert_refuses_old_text_replies_without_events(setup, text):
    service, store, telegram = setup
    item, _ = service.post("Example", "Old full message")
    before = store.events_for_item(item.id)
    _alert(service)
    service.handle_update({"message": {"message_id": 200, "from": {"id": telegram.owner_id},
                                        "reply_to_message": {"message_id": 1}, "text": text}})
    assert store.events_for_item(item.id) == before
    assert store.get_item(item.id).status == "open"
    assert "Open in Hook" in telegram.replies[-1]["text"]


def test_alert_preserves_enrollment_and_recovery_callbacks(setup):
    from mailbox import owner
    service, store, telegram = setup
    _alert(service)
    key = service.enroll(owner.key_hash("synthetic-key"), "Test phone")
    def tap(data):
        service.handle_update({"callback_query": {"id": "tap", "from": {"id": telegram.owner_id}, "data": data}})
    tap(f"e:{key.id}:y")
    key = store.get_owner_key(key.id)
    assert key.status == "approved"
    service.request_enforcement(True, key)
    assert service.request_enforcement(False, key) == "pending"
    request = store.get_setting(owner.SETTING_OFF_REQUEST)
    tap(f"k:{request}:off")
    assert not service.owner_key_required()


def test_alert_preserves_nonask_workflows_and_household_dispatch(setup, monkeypatch):
    service, _, telegram = setup
    _alert(service)
    seen = []
    monkeypatch.setattr(service, "_notification_tap", lambda data: seen.append(("reminder", data)) or "Done")
    monkeypatch.setattr(service, "_capture", lambda msg, text: seen.append(("capture", text)))
    service.handle_update({"callback_query": {"id": "tap", "from": {"id": telegram.owner_id}, "data": "r:test:done"}})
    service.handle_update({"message": {"from": {"id": telegram.owner_id}, "text": "Buy milk"}})
    service.members = {telegram.owner_id: "Owner"}
    monkeypatch.setattr("mailbox.service.household.handle_update", lambda svc, update: seen.append(("household", update)))
    service.handle_update({})
    assert seen == [("reminder", "test:done"), ("capture", "Buy milk"), ("household", {})]


def test_alert_resolve_fallback_omits_note(setup, monkeypatch):
    service, _, telegram = setup
    _alert(service)
    item, _ = service.post("Example", "Test fallback")
    def fail(*args, **kwargs):
        raise TelegramError("message cannot be edited")
    monkeypatch.setattr(telegram, "edit_message_text", fail)
    service.resolve(item.id, "declined", note="private note")
    assert "private note" not in telegram.sent_messages[-1]["text"]
    assert "Urgency: today" in telegram.sent_messages[-1]["text"]


def test_alert_requirement_transition_has_no_action_buttons(setup):
    service, _, telegram = setup
    _alert(service)
    doc = {"v": 1, "title": "Check test", "steps": [{"do": "Check", "required": True}], "urgency": "now"}
    item, _ = service.post("Example", doc["title"], instructable=doc)
    assert "Urgency: now" in telegram.sent_messages[-1]["text"]
    service.progress(item.id, ["s1"], [], rev=1, hash=None, auth="owner-key")
    _assert_alert(telegram.edited_messages[-1], item.id)


def test_alert_without_open_base_has_guidance_and_no_callbacks(setup):
    service, _, telegram = setup
    service.telegram_mode = "alert"
    item, _ = service.post("Example", "No URL configured")
    assert telegram.sent_messages[-1]["reply_markup"] == {"inline_keyboard": []}
    assert "Open in Hook" in telegram.sent_messages[-1]["text"]

@pytest.mark.parametrize("trigger", ["answer", "requirements"])
@pytest.mark.parametrize("acked", [False, True])
def test_full_to_alert_redraw_replaces_text_and_preserves_ack(setup, trigger, acked):
    service, store, telegram = setup
    doc = {"v": 1, "title": "Check the test deployment", "urgency": "now", "why": "private why",
           "steps": [{"do": "Check the deployment", "required": True, "detail": "private detail"},
                     {"do": "Pick an environment", "question": {"prompt": "Which?", "choices": ["Test", "Preview"]}}]}
    item, _ = service.post("Example", doc["title"], instructable=doc)
    original = telegram.sent_messages[-1]
    assert "private why" in original["text"] and "private detail" in original["text"]
    assert any("callback_data" in button for row in original["reply_markup"]["inline_keyboard"] for button in row)
    if acked:
        service.ack(item.id)
    _alert(service)
    if trigger == "answer":
        service.answer(item.id, [{"step": "2", "picked": ["Test"]}], rev=1)
    else:
        service.progress(item.id, ["s1"], [], rev=1, hash=None, auth="owner-key")
    message = telegram.edited_messages[-1]
    _assert_alert(message, item.id)
    assert message["text"].startswith("👀 ") is acked
    assert "Example" in message["text"] and doc["title"] in message["text"]
    assert "Urgency: now" in message["text"]
    assert message["message_id"] == 1


@pytest.mark.parametrize("ack_when", ["before_correction", "after_correction", "after_correction_and_old_accept"])
def test_full_to_alert_pending_redraw_preserves_current_correction_and_ack(pond, ack_when):
    service, store, telegram, clock = pond
    item, response = _post_doc(service)
    if ack_when == "before_correction":
        service.ack(item.id)
    clock.now += _td(seconds=61)
    _amend_doc(service, item, response)
    corrected_id = service._current_message(item)[1]
    if ack_when.startswith("after_correction"):
        service.ack(item.id)
    if ack_when == "after_correction_and_old_accept":
        original = store.revisions(item.id)[0]
        service.accept(item.id, original.rev, original.hash, "owner-key")
    _alert(service)
    service._redraw_keyboard(item.id)
    message = telegram.edited_messages[-1]
    _assert_alert(message, item.id)
    assert "Corrected: review in Hook" in message["text"]
    assert message["text"].startswith("👀 ") is ack_when.startswith("after_correction")
    assert message["message_id"] == corrected_id


def test_shared_verify_identity_does_not_alias_a_real_verify_step(setup):
    service, store, _ = setup
    item, _ = service.post("Demo", "Check shared Verify", instructable={
        "v": 1, "title": "Check shared Verify", "urgency": "today",
        "steps": [{"id": "verify", "do": "Check the build"}], "verify": {"do": "Check final response"}})
    revision = store.current_revision(item.id)
    service.progress(item.id, ["verify", "@verify"], [], rev=1, hash=revision.hash, auth="owner-key")
    service.progress(item.id, [], ["@verify"], rev=1, hash=revision.hash, auth="owner-key")
    _, body = service.in_force(item.id)
    assert service._ticked(item.id, body, service._event_dicts(item.id)) == {"verify"}


def test_shared_legacy_paths_can_be_ticked_without_fabricating_step_ids(setup):
    from mailbox.store import Item
    service, store, _ = setup
    item = Item(id="synthetic-legacy", source="demo", title="Legacy ask", payload=json.dumps({"instructable": {
        "v": 1, "title": "Legacy ask", "steps": [{"do": "Check", "required": True}], "verify": {"do": "Verify"}}}))
    store.insert_item(item)
    service.progress(item.id, ["@legacy:1", "@verify"], [], rev=None, hash=None, auth="owner-key")
    assert service.requirements_left(item.id) == []
    _, body = service.in_force(item.id)
    assert service._ticked(item.id, body, service._event_dicts(item.id)) == {"@legacy:1", "@verify"}
