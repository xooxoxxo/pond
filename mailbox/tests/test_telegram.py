"""Tests for Telegram client."""
import email.message
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from mailbox.telegram import FakeTelegramClient, TelegramClient, TelegramError

DUMMY_TOKEN = "123:TEST"


def _response(payload):
    """A urlopen-compatible success response (BytesIO supports read() + ctx mgr)."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _ok_response(result):
    return _response({"ok": True, "result": result})


def _http_error(code, body, reason="Error", headers=None):
    """A real urllib.error.HTTPError with a JSON body and headers, for fake urlopen."""
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs.add_header(k, v)
    fp = io.BytesIO(json.dumps(body).encode("utf-8"))
    return urllib.error.HTTPError(
        url="https://api.telegram.org/botX/method", code=code, msg=reason, hdrs=hdrs, fp=fp
    )


@pytest.fixture
def client():
    """Fake Telegram client for testing."""
    return FakeTelegramClient()


def test_render_ask_message():
    """Render an ask with all optional parts."""
    msg = TelegramClient.render_ask_message(
        "Orbit", "fix the indexer", "slow startup", "cd /srv && python -c 'fix()'", "https://tracker.example/..."
    )
    assert "🙋" in msg
    assert "<b>Orbit</b>" in msg
    assert "fix the indexer" in msg
    assert "why" in msg
    assert "cd /srv" in msg
    assert "link" in msg


def test_render_ask_message_minimal():
    """Render an ask with only required parts."""
    msg = TelegramClient.render_ask_message("Orbit", "test", None, None, None)
    assert "🙋" in msg
    assert "Orbit" in msg
    assert "test" in msg
    assert "why" not in msg


def test_render_ask_message_escapes_html():
    """HTML is escaped to prevent injection."""
    msg = TelegramClient.render_ask_message(
        "Orbit", "<script>alert('xss')</script>", None, None, None
    )
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg


def test_render_ack_prefix():
    """Ack prefix is the eye emoji."""
    prefix = TelegramClient.render_ack_prefix()
    assert "👀" in prefix


def test_render_resolved_message_done():
    """Render a resolved ask marked as done."""
    msg = TelegramClient.render_resolved_message("fix the bug", "done", "deployed")
    assert "✅" in msg
    assert "done" in msg
    assert "<s>" in msg  # struck through
    assert "deployed" in msg


def test_render_resolved_message_declined():
    """Render a resolved ask marked as declined."""
    msg = TelegramClient.render_resolved_message("fix the bug", "declined", "not needed")
    assert "🚫" in msg
    assert "skipped" in msg
    assert "not needed" in msg


def test_ask_keyboard():
    """Ask keyboard has Done and Skip buttons, and only that row without a base."""
    kbd = TelegramClient("token", 123).ask_keyboard("abc-123")
    assert len(kbd["inline_keyboard"]) == 1
    buttons = kbd["inline_keyboard"][0]
    assert buttons[0]["text"] == "✅ Done"
    assert buttons[0]["callback_data"] == "d:abc-123"
    assert buttons[1]["text"] == "🚫 Skip"
    assert buttons[1]["callback_data"] == "s:abc-123"


def test_send_message(client):
    """Send a message is recorded."""
    client.send_message(1000001, "test")
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == "test"


def test_edit_message_text(client):
    """Edit is recorded."""
    client.edit_message_text(1000001, 42, "updated")
    assert len(client.edited_messages) == 1
    assert client.edited_messages[0]["text"] == "updated"


def test_answer_callback_query(client):
    """Callback answer is recorded."""
    client.answer_callback_query("cb_123", "Done!")
    assert len(client.callback_answers) == 1


def test_get_updates(client):
    """Get updates returns the canned list."""
    client.updates_to_return = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 1000001},
                    "from": {"id": 1000001},
                    "text": "done",
                },
            }
        ]
    ]
    updates = client.get_updates(offset=0, timeout=50)
    assert len(updates) == 1
    assert updates[0]["update_id"] == 1


# --- Fix round 1 ---------------------------------------------------------
# Finding 1: per-call socket timeout, padded for long-polling get_updates.


def test_get_updates_sends_api_timeout_and_pads_socket_timeout(monkeypatch):
    """get_updates(timeout=50) must send Telegram's own timeout=50 form field
    (so it actually long-polls) while using a padded 60s local socket timeout."""
    seen_socket_timeouts = []
    captured = {}

    def fake_urlopen(req, timeout=None):
        seen_socket_timeouts.append(timeout)
        captured["form"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        return _ok_response([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    client.get_updates(offset=5, timeout=50)
    assert seen_socket_timeouts[-1] == 60
    assert captured["form"]["timeout"] == ["50"]
    assert captured["form"]["offset"] == ["5"]
    assert json.loads(captured["form"]["allowed_updates"][0]) == ["message", "callback_query"]


def test_send_message_uses_default_socket_timeout_and_sends_no_timeout_field(monkeypatch):
    """Non-long-poll calls use the 20s socket default and never send a
    Bot API "timeout" form field of their own."""
    seen_socket_timeouts = []
    captured = {}

    def fake_urlopen(req, timeout=None):
        seen_socket_timeouts.append(timeout)
        captured["form"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        return _ok_response({"message_id": 1, "chat": {"id": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    client.send_message(1, "hi")
    assert seen_socket_timeouts[-1] == 20
    assert "timeout" not in captured["form"]


# Finding 2: non-429 HTTP errors carry Telegram's description, never the URL/token.


def test_non_429_error_includes_description_not_url_or_token(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(
            400, {"ok": False, "description": "Bad Request: message can't be edited"}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    with pytest.raises(TelegramError) as exc_info:
        client.edit_message_text(1, 2, "x")
    message = str(exc_info.value)
    assert "can't be edited" in message
    assert DUMMY_TOKEN not in message
    assert "api.telegram.org" not in message


def test_non_429_error_falls_back_to_http_reason_when_body_unparsable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        err = urllib.error.HTTPError(
            url="https://api.telegram.org/botX/method",
            code=500,
            msg="Internal Server Error",
            hdrs=email.message.Message(),
            fp=io.BytesIO(b"not json"),
        )
        raise err

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    with pytest.raises(TelegramError) as exc_info:
        client.send_message(1, "hi")
    message = str(exc_info.value)
    assert "Internal Server Error" in message
    assert DUMMY_TOKEN not in message
    assert "api.telegram.org" not in message


# Finding 3: 429 retries once, sleeping the parsed retry_after.


def test_429_then_200_retries_once_and_sleeps_parsed_retry_after(monkeypatch):
    attempts = {"n": 0}
    sleeps = []

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, {"ok": False, "parameters": {"retry_after": 3}})
        return _ok_response({"message_id": 7, "chat": {"id": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    client = TelegramClient(DUMMY_TOKEN, 1)
    result = client.send_message(1, "hi")
    assert result["message_id"] == 7
    assert attempts["n"] == 2
    assert sleeps == [3]


def test_429_twice_raises_after_one_retry(monkeypatch):
    sleeps = []

    def fake_urlopen(req, timeout=None):
        raise _http_error(429, {"ok": False, "parameters": {"retry_after": 2}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    client = TelegramClient(DUMMY_TOKEN, 1)
    with pytest.raises(TelegramError) as exc_info:
        client.send_message(1, "hi")
    assert "429" in str(exc_info.value)
    assert "retry_after=2" in str(exc_info.value)
    assert sleeps == [2]  # slept once, did not retry a second time


def test_429_retry_after_falls_back_to_header_then_to_one(monkeypatch):
    sleeps = []
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # no `parameters` in body -> must fall back to the Retry-After header
            raise _http_error(429, {"ok": False}, headers={"Retry-After": "4"})
        return _ok_response({"message_id": 1, "chat": {"id": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    client = TelegramClient(DUMMY_TOKEN, 1)
    client.send_message(1, "hi")
    assert sleeps == [4]


# Finding 4: edit_message_text can carry (or omit) reply_markup.


def test_edit_message_text_sends_reply_markup_when_given(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["form"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        return _ok_response({"message_id": 2})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    kb = {"inline_keyboard": []}
    client.edit_message_text(1, 2, "x", reply_markup=kb)
    assert json.loads(captured["form"]["reply_markup"][0]) == kb


def test_edit_message_text_omits_reply_markup_when_absent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["form"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        return _ok_response({"message_id": 2})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    client.edit_message_text(1, 2, "x")
    assert "reply_markup" not in captured["form"]


# Finding 5: FakeTelegramClient records verbatim calls plus typed, richer views.


def test_fake_records_every_call_verbatim():
    client = FakeTelegramClient()
    client.send_message(1, "hi")
    method, params = client.calls[-1]
    assert method == "sendMessage"
    assert params["text"] == "hi"
    assert params["chat_id"] == 1


def test_fake_send_message_records_reply_markup_and_no_reply_id(client):
    kb = {"inline_keyboard": [[{"text": "x", "callback_data": "d:1"}]]}
    client.send_message(1, "hi", reply_markup=kb)
    entry = client.sent_messages[-1]
    assert entry["reply_markup"] == kb
    assert entry["reply_to_message_id"] is None


def test_fake_send_reply_appears_in_replies(client):
    client.send_reply(1, 99, "t")
    entry = client.sent_messages[-1]
    assert entry["reply_to_message_id"] == 99
    assert client.replies[-1]["reply_to_message_id"] == 99


def test_fake_edit_records_reply_markup(client):
    kb = {"inline_keyboard": []}
    client.edit_message_text(1, 2, "x", reply_markup=kb)
    entry = client.edited_messages[-1]
    assert entry["chat_id"] == 1
    assert entry["message_id"] == 2
    assert entry["reply_markup"] == kb


# Finding 6: the real _call, exercised end-to-end through a fake urlopen.


def test_real_call_request_path_and_form_encoding(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["form"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        return _ok_response({"ok_marker": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    client.send_message(1, "hi", reply_markup={"inline_keyboard": []})
    assert captured["url"].endswith(f"/bot{DUMMY_TOKEN}/sendMessage")
    assert captured["form"]["text"] == ["hi"]
    assert json.loads(captured["form"]["reply_markup"][0]) == {"inline_keyboard": []}


def test_real_call_ok_false_raises_telegram_error_with_description(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _response({"ok": False, "description": "Forbidden: bot was blocked"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    with pytest.raises(TelegramError) as exc_info:
        client.send_message(1, "hi")
    assert "bot was blocked" in str(exc_info.value)


def test_real_call_returns_result_on_success(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _ok_response({"message_id": 42})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    result = client.send_message(1, "hi")
    assert result == {"message_id": 42}


# --- Hook ---------------------------------------------------------------


def test_ask_keyboard_with_open_base_adds_the_relay_row():
    kbd = TelegramClient.ask_keyboard("abc-123", "http://127.0.0.1:47823/i")
    assert len(kbd["inline_keyboard"]) == 2
    assert kbd["inline_keyboard"][1] == [
        {"text": "📋 Open in Hook", "url": "http://127.0.0.1:47823/i/abc-123"}
    ]


def test_render_instructable_escapes_counts_and_caps():
    """Review (a): emoji, < and & across 14 steps → escaped, 10 numbered lines,
    "+4 more", no code, at most 3500 characters."""
    ins = {
        "v": 1, "title": "Rotate the key", "urgency": "now",
        "why": "expires <tonight> & then 🔥",
        "steps": [{"do": f"🔑 step {n}: run <cmd> & check", "code": "never on telegram"}
                  for n in range(1, 15)],
    }
    msg = TelegramClient.render_ask_message("Orbit", "Rotate the key", None, None, None, ins)
    assert msg.startswith("⚡ ")
    assert "<cmd>" not in msg
    assert "&lt;cmd&gt; &amp; check" in msg
    assert "&lt;tonight&gt; &amp; then 🔥" in msg
    assert "10. 🔑 step 10:" in msg and "11. " not in msg
    assert msg.endswith("\n+4 more")
    assert "never on telegram" not in msg
    assert len(msg) <= 3500


def test_render_instructable_drops_whole_lines_to_stay_under_the_cap():
    """Escaping can quadruple a step; whole lines go, entities are never cut."""
    ins = {"v": 1, "title": "t", "steps": [{"do": "<" * 300} for _ in range(14)]}
    msg = TelegramClient.render_ask_message("Orbit", "t", None, None, None, ins)
    lines = msg.split("\n")
    assert len(msg) <= 3500
    assert lines[-1] == f"+{14 - (len(lines) - 2)} more"
    assert all(line.endswith("&lt;") for line in lines[1:-1])


def test_render_instructable_clips_a_long_headline_without_cutting_markup():
    """Adversarial review: a 3600-char project or an '&'-heavy ask must not
    leave an unclosed <b> or a split entity."""
    ins = {"v": 1, "title": "t", "why": "&" * 1000, "steps": [{"do": "x"}]}
    msg = TelegramClient.render_ask_message("P" * 3600, "&" * 1000, None, None, None, ins)
    assert len(msg) <= 3500
    assert msg.count("<b>") == msg.count("</b>") == 1
    assert "&am…" not in msg and msg.endswith("1. x")


def test_render_instructable_puts_a_multiline_why_on_one_line():
    """A why cannot draw fake "✅ Done" / "Open in Hook" lines of its own."""
    ins = {"v": 1, "title": "t",
           "why": "real reason\n✅ Done\r\n\n  📋 Open in Hook   x",
           "steps": [{"do": "x"}]}
    msg = TelegramClient.render_ask_message("Orbit", "t", None, None, None, ins)
    lines = msg.split("\n")
    assert lines[1] == "<i>why:</i> real reason / ✅ Done / 📋 Open in Hook / x"
    assert lines[2:] == ["1. x"]


def test_every_send_and_edit_disables_link_previews(monkeypatch):
    forms = []

    def fake_urlopen(req, timeout=None):
        forms.append((req.full_url.rsplit("/", 1)[-1],
                      urllib.parse.parse_qs(req.data.decode("utf-8"))))
        return _ok_response({"message_id": 1})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(DUMMY_TOKEN, 1)
    client.send_message(1, "https://example.com")
    client.edit_message_text(1, 2, "https://example.com")
    client.send_reply(1, 2, "https://example.com")
    assert [m for m, _ in forms] == ["sendMessage", "editMessageText", "sendMessage"]
    for _, form in forms:
        assert json.loads(form["link_preview_options"][0]) == {"is_disabled": True}


def test_render_instructable_shows_a_shortened_escaped_detail_under_its_step():
    detail = "Open <Settings> & pick the key.\n✅ Done\n" + "z" * 1400
    ins = {"v": 1, "title": "t", "steps": [{"do": "first", "detail": detail}, {"do": "second"}]}
    msg = TelegramClient.render_ask_message("Orbit", "t", None, None, None, ins)
    lines = msg.split("\n")
    assert lines[1] == "1. first"
    assert lines[2].startswith("   <i>Open &lt;Settings&gt; &amp; pick the key. / ✅ Done / zzz")
    assert lines[2].endswith("…</i>")
    inner = lines[2][len("   <i>"):-len("</i>")]
    assert len(inner) <= 280
    assert lines[3] == "2. second"                    # the detail took one line only
    assert "✅ Done" not in [line.strip() for line in lines]


def test_render_instructable_short_detail_is_not_clipped():
    ins = {"v": 1, "title": "t", "steps": [{"do": "x", "detail": "short & sweet"}]}
    msg = TelegramClient.render_ask_message("Orbit", "t", None, None, None, ins)
    assert msg.split("\n")[1:] == ["1. x", "   <i>short &amp; sweet</i>"]


def test_render_instructable_long_details_never_push_past_the_cap():
    """Worst case: a long headline and why, 20 '<'-heavy steps each with a
    1500-char '&' detail. The cap holds, markup stays balanced, no entity is cut."""
    ins = {"v": 1, "title": "t", "why": "&" * 300,
           "steps": [{"do": "<" * 300, "detail": "&" * 1500} for _ in range(20)]}
    msg = TelegramClient.render_ask_message("P" * 200, "&" * 120, None, None, None, ins)
    assert len(msg) <= 3500
    assert msg.count("<i>") == msg.count("</i>")
    assert "&am…" not in msg and "&a…" not in msg and "&…" not in msg
    assert msg.split("\n")[-1].startswith("+") and msg.endswith(" more")


# --- notifications (Task 6) -------------------------------------------------


def test_render_notification_reminder_and_brief():
    text = TelegramClient.render_notification("reminder", "call <the> notary", "today 14:00")
    assert text == "⏰ <b>call &lt;the&gt; notary</b>\ntoday 14:00"
    brief = TelegramClient.render_notification("brief", "Saturday 17 October", "Today (1): x\nNext: y", open_asks=2)
    assert brief == "☀️ <b>Saturday 17 October</b>\nToday (1): x\nNext: y\nOpen asks: 2"
    assert TelegramClient.render_notification("brief", "t", None) == "☀️ <b>t</b>"


def test_notification_keyboard_and_done_rendering():
    kbd = TelegramClient.notification_keyboard("n-1", [{"id": "done", "label": "✅ Done"}, {"id": "tomorrow", "label": "⏭ Tomorrow"}])
    assert kbd == {"inline_keyboard": [[{"text": "✅ Done", "callback_data": "r:n-1:done"}, {"text": "⏭ Tomorrow", "callback_data": "r:n-1:tomorrow"}]]}
    assert TelegramClient.render_notification_done("call <x>", "done") == "✅ <s>call &lt;x&gt;</s>"
    assert TelegramClient.render_notification_done("call", "tomorrow") == "⏭ <s>call</s>\n<i>moved to tomorrow</i>"
    assert TelegramClient.render_notification_done("call", "closed") == "✅ <s>call</s>\n<i>already closed in Central Time</i>"
    assert TelegramClient.render_notification_done("call", "gone") == "🚫 <s>call</s>\n<i>gone from Central Time</i>"


def test_notification_callback_data_fits_telegram_limit():
    """A uuid plus the longest verb stays within Telegram's 64 bytes; a longer
    id is refused here rather than by Telegram at send time."""
    import uuid
    kbd = TelegramClient.notification_keyboard(str(uuid.uuid4()), [{"id": "tomorrow", "label": "x"}])
    data = kbd["inline_keyboard"][0][0]["callback_data"]
    assert len(data.encode("utf-8")) <= 64
    with pytest.raises(ValueError):
        TelegramClient.notification_keyboard("n" * 60, [{"id": "done", "label": "x"}])


def test_notification_keyboard_never_uses_the_ask_prefixes():
    """r: is its own namespace: no reminder button can be read as an ask tap."""
    kbd = TelegramClient.notification_keyboard("abc", [{"id": "done", "label": "x"}, {"id": "tomorrow", "label": "y"}])
    for button in kbd["inline_keyboard"][0]:
        assert button["callback_data"].startswith("r:")
        assert not button["callback_data"].startswith(("d:", "s:"))


# --- sub-steps (Relay 0.6.0, design §7.4) -------------------------------------


def _render(ins, project="Acme", ask="t"):
    return TelegramClient.render_ask_message(project, ask, None, None, None, ins)


def test_render_instructable_shows_substeps_indented_with_letters():
    ins = {"v": 1, "title": "t", "steps": [
        {"do": "Create a private channel with this name"},
        {"do": "Create the Slack app for Acme", "substeps": [
            {"do": "Click Create New App, then From scratch", "url": "https://api.slack.com/apps",
             "detail": "sub-step detail is never sent"},
            {"do": "Name it Acme and pick your workspace"},
            {"do": "Under OAuth & Permissions, add the chat:write scope", "code": "chat:write"},
        ]},
        {"do": "Invite the Acme bot to the channel"},
    ]}
    assert _render(ins).split("\n")[1:] == [
        "1. Create a private channel with this name",
        "2. Create the Slack app for Acme",
        "   2a. Click Create New App, then From scratch",
        "   2b. Name it Acme and pick your workspace",
        "   2c. Under OAuth &amp; Permissions, add the chat:write scope",
        "3. Invite the Acme bot to the channel",
    ]
    msg = _render(ins)
    assert "never sent" not in msg and "api.slack.com/apps" not in msg and "chat:write\n" not in msg


def test_html_in_a_substep_do_is_escaped():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "substeps": [{"do": "<b>bold</b> & co"}]}]}
    assert _render(ins).split("\n")[2] == "   1a. &lt;b&gt;bold&lt;/b&gt; &amp; co"


def test_a_step_whose_substeps_do_not_fit_is_left_out_whole():
    big = {"do": "big", "substeps": [{"do": "<" * 300} for _ in range(10)]}   # about 12 000 characters escaped
    ins = {"v": 1, "title": "t", "steps": [{"do": "first"}, big, {"do": "third"}]}
    msg = _render(ins)
    assert msg.split("\n")[1:] == ["1. first", "+2 more"]
    assert "2a." not in msg and len(msg) <= 3500


def test_the_detail_line_goes_first_when_a_block_does_not_fit():
    step = {"do": "x", "detail": "d" * 1000, "substeps": [{"do": "<" * 300}, {"do": "<" * 300}]}
    msg = _render({"v": 1, "title": "t", "steps": [step]}, project="P" * 200, ask="a" * 800)
    lines = msg.split("\n")
    assert lines[1] == "1. x"
    assert [line[:7] for line in lines[2:]] == ["   1a. ", "   1b. "]
    assert "<i>" not in msg and len(msg) <= 3500


def test_max_rendered_steps_still_counts_top_level_steps():
    steps = [{"do": f"s{n}", "substeps": [{"do": "a"}, {"do": "b"}]} for n in range(1, 13)]
    msg = _render({"v": 1, "title": "t", "steps": steps})
    assert "10. s10" in msg and "   10b. b" in msg and "11. " not in msg
    assert msg.endswith("\n+2 more")


def test_the_design_example_renders_as_documented():
    ins = {
        "v": 1, "title": "Add the Slack channel and Product Hunt token", "urgency": "now",
        "why": "Acme's daily brief needs a Slack channel to post into and a token to read Product Hunt launches.",
        "steps": [
            {"do": "Create a private channel with this name", "code": "acme-brief"},
            {"do": "Create the Slack app for Acme", "where": "api.slack.com", "substeps": [
                {"do": "Click Create New App, then From scratch", "url": "https://api.slack.com/apps"},
                {"do": "Name it Acme and pick your workspace"},
                {"do": "Under OAuth & Permissions, add the chat:write scope", "code": "chat:write", "lang": "text"},
            ]},
            {"do": "Invite the Acme bot to the channel", "code": "/invite @acme"},
        ],
    }
    assert _render(ins, ask="Add the Slack channel and Product Hunt token").split("\n") == [
        "⚡ 🙋 <b>Acme</b> — Add the Slack channel and Product Hunt token",
        "<i>why:</i> Acme&#x27;s daily brief needs a Slack channel to post into and a token to read Product Hunt launches.",
        "1. Create a private channel with this name",
        "2. Create the Slack app for Acme",
        "   2a. Click Create New App, then From scratch",
        "   2b. Name it Acme and pick your workspace",
        "   2c. Under OAuth &amp; Permissions, add the chat:write scope",
        "3. Invite the Acme bot to the channel",
    ]


# --- step questions -------------------------------------------------

_ANSWERS_DOC = json.loads(
    (Path(__file__).parent / "answers-vectors.json").read_text(encoding="utf-8")
)
QUESTIONS = _ANSWERS_DOC["instructable"]   # 1 single, 2 multi, 3 free text, 4 single + 4.2 single
ITEM = "9f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"


def test_a_question_shows_one_line_under_its_step_and_sub_step():
    text = TelegramClient.render_ask_message("Atlas", "Ship", None, None, None, QUESTIONS)
    lines = text.split("\n")
    assert lines[lines.index("1. Pick the deploy target") + 1] == "   ❓ Which environment?"
    assert lines[lines.index("   4b. Pick the notes") + 1] == "      ❓ Which notes?"
    assert lines[lines.index("4. Prepare the release") + 1] == "   ❓ Ship today?"
    assert sum("❓" in line for line in lines) == 5     # step 5 has none


def test_a_multiline_prompt_shows_its_first_line_escaped():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "question": {"prompt": "Pick <one>\nmore", "choices": ["x"]}}]}
    text = TelegramClient.render_ask_message("p", "t", None, None, None, ins)
    assert "   ❓ Pick &lt;one&gt;" in text and "more" not in text
    assert not text.endswith("in Hook")              # the one question has buttons


def test_questions_without_buttons_are_counted_at_the_end():
    text = TelegramClient.render_ask_message("Atlas", "Ship", None, None, None, QUESTIONS)
    assert text.endswith("\nAnswer 2 more in Hook")     # 2 (multi) and 3 (free text)
    five = {"v": 1, "title": "t", "steps": [
        {"do": f"s{i}", "question": {"prompt": "?", "choices": ["a", "b"]}} for i in range(6)]}
    assert TelegramClient.render_ask_message("p", "t", None, None, None, five).endswith("Answer 2 more in Hook")


def test_the_keyboard_has_a_row_per_single_pick_question_with_the_current_answer_ticked():
    answers = {"1": {"step": "1", "picked": ["Prod"]}, "4.2": {"step": "4.2", "picked": ["Short"]}}
    rows = TelegramClient.ask_keyboard(ITEM, None, QUESTIONS, answers)["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [
        ["1 · Staging", "✓ Prod"],
        ["4 · Yes", "No"],
        ["4.2 · ✓ Short", "Long"],
        ["✅ Done", "🚫 Skip"],
    ]
    assert [b["callback_data"] for b in rows[2]] == [f"a:{ITEM}:4.2:0", f"a:{ITEM}:4.2:1"]
    assert all(len(b["callback_data"].encode()) <= 64 for row in rows for b in row)


def test_a_ticked_first_choice_keeps_the_path_first():
    rows = TelegramClient.ask_keyboard(ITEM, None, QUESTIONS, {"1": {"picked": ["Staging"]}})["inline_keyboard"]
    assert rows[0][0]["text"] == "1 · ✓ Staging"


def test_only_the_first_four_single_pick_questions_get_rows():
    six = {"v": 1, "title": "t", "steps": [
        {"do": f"s{i}", "question": {"prompt": "?", "choices": ["a", "b"]}} for i in range(6)]}
    rows = TelegramClient.ask_keyboard(ITEM, "http://127.0.0.1:47823/i", six)["inline_keyboard"]
    assert [row[0]["text"] for row in rows] == ["1 · a", "2 · a", "3 · a", "4 · a", "✅ Done", "📋 Open in Hook"]


def test_a_callback_data_over_the_limit_is_refused():
    with pytest.raises(ValueError):
        TelegramClient.ask_keyboard("x" * 60, None, QUESTIONS)


def test_fake_records_a_keyboard_edit(client):
    client.edit_message_reply_markup(1, 2, {"inline_keyboard": []})
    assert client.edited_markups == [{"chat_id": 1, "message_id": 2, "reply_markup": {"inline_keyboard": []}}]


# --- blocking steps and warnings (Hook step cards, design §1) ----------------


def test_a_warning_shows_under_the_step_before_its_detail():
    ins = {"v": 1, "title": "t", "steps": [
        {"do": "Revoke the old key", "detail": "In the console", "blocking": True,
         "warning": "CI jobs fail & stop\nuntil the new key is in", "question": {"prompt": "Done it?"}},
        {"do": "Deploy"},
    ]}
    assert _render(ins).split("\n")[1:] == [
        "1. Revoke the old key",
        "   ⚠️ CI jobs fail &amp; stop",
        "   <i>In the console</i>",
        "   ❓ Done it?",
        "2. Deploy",
        "Answer 1 more in Hook",
    ]


def test_blocking_without_a_warning_says_do_this_first():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "blocking": True}, {"do": "b", "blocking": False}]}
    assert _render(ins).split("\n")[1:] == ["1. a", "   ⚠️ Do this before the next steps", "2. b"]


def test_a_warning_without_blocking_still_shows():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "warning": "\n  careful  \n"}]}
    assert _render(ins).split("\n")[1:] == ["1. a", "   ⚠️ careful"]


def test_a_substep_warning_is_indented_under_the_substep():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "substeps": [
        {"do": "x", "blocking": True, "question": {"prompt": "Which?", "choices": ["1", "2"]}},
        {"do": "y", "warning": "shown once"},
    ]}]}
    assert _render(ins).split("\n")[1:] == [
        "1. a",
        "   1a. x",
        "      ⚠️ Do this before the next steps",
        "      ❓ Which?",
        "   1b. y",
        "      ⚠️ shown once",
    ]


def test_a_long_warning_is_clipped_like_a_detail():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "warning": "&" * 300}]}
    line = _render(ins).split("\n")[2]
    assert line.startswith("   ⚠️ &amp;") and line.endswith("…")
    assert len(line) <= len("   ⚠️ ") + 280


def test_the_detail_is_dropped_but_the_warning_kept_when_a_block_does_not_fit():
    step = {"do": "x", "detail": "d" * 1000, "warning": "keep me",
            "substeps": [{"do": "<" * 300}, {"do": "<" * 300}]}
    msg = _render({"v": 1, "title": "t", "steps": [step]}, project="P" * 200, ask="a" * 800)
    lines = msg.split("\n")
    assert lines[1:3] == ["1. x", "   ⚠️ keep me"]
    assert [line[:7] for line in lines[3:]] == ["   1a. ", "   1b. "]
    assert "<i>" not in msg and len(msg) <= 3500


def test_optional_steps_and_substeps_end_with_optional():
    ins = {"v": 1, "title": "t", "steps": [
        {"do": "a", "optional": True, "substeps": [{"do": "b", "optional": True}, {"do": "c"}]},
        {"do": "d", "optional": False}]}
    text = TelegramClient._render_instructable("P", "t", None, ins)
    assert "1. a (optional)" in text
    assert "   1a. b (optional)" in text
    assert "   1b. c\n" in text + "\n"
    assert "2. d\n" in text + "\n"


def test_a_file_shows_its_name_on_the_mac_under_its_step_and_substep():
    ins = {"v": 1, "title": "t", "steps": [
        {"do": "a", "file": "~/Documents/release-notes.md", "warning": "Read it first",
         "substeps": [{"do": "b", "file": "/var/tmp/x <y>.md"}, {"do": "c"}]},
        {"do": "d"}]}
    lines = TelegramClient._render_instructable("P", "t", None, ins).split("\n")
    i = lines.index("1. a")
    assert lines[i + 1] == "   ⚠️ Read it first"
    assert lines[i + 2] == "   📄 release-notes.md (on the Mac)"
    assert lines[i + 3] == "   1a. b"
    assert lines[i + 4] == "      📄 x &lt;y&gt;.md (on the Mac)"
    assert lines[i + 5] == "   1b. c"
    assert "/var/tmp" not in "\n".join(lines) and "~/Documents" not in "\n".join(lines)


def test_a_path_link_shows_its_name_on_the_mac_and_a_url_link_does_not():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "question": {"prompt": "Which?"}}],
           "links": [{"label": "Spec", "url": "https://example.com/spec"},
                     {"label": "Notes", "path": "~/Documents/notes.md"}]}
    lines = TelegramClient._render_instructable("P", "t", None, ins).split("\n")
    assert "📄 notes.md (on the Mac)" in lines
    assert lines[-1] == "Answer 1 more in Hook"
    assert lines.index("📄 notes.md (on the Mac)") == len(lines) - 2
    assert not any("example.com" in line for line in lines)


def test_path_links_never_push_the_message_past_the_limit():
    from mailbox.telegram import MAX_ASK_TEXT
    ins = {"v": 1, "title": "t",
           "steps": [{"do": "x" * 290, "detail": "y" * 1400, "substeps": [{"do": "z" * 290}] * 10}] * 10,
           "links": [{"label": "l", "path": "/" + "n" * 400 + ".md"}] * 10}
    text = TelegramClient._render_instructable("P", "t", None, ins)
    assert len(text) <= MAX_ASK_TEXT


# --- corrections (Pond 1.1.0) -----------------------------------------------------


def test_render_corrected_escapes_the_reason_and_never_shows_code():
    summary = {"changed": [{"id": "s3", "path": "3", "fields": ["code"]},
                           {"id": "s5", "path": "5", "fields": ["detail"]}],
               "added": [{"id": "r2-1", "path": "6"}], "removed": [], "moved": [], "top": []}
    text = TelegramClient.render_corrected("Orbit", "Rotate <the> key", 2, "fix <script>alert(1)</script>", summary)
    assert text.splitlines() == [
        "✏️ <b>Corrected</b> · version 2",
        "<b>Orbit</b> · Rotate &lt;the&gt; key",
        "The session says: <i>fix &lt;script&gt;alert(1)&lt;/script&gt;</i>",
        "Changed: step 3 (command), step 5 (detail). Added: step 6. Removed: none.",
        "Review it in Hook before you act.",
    ]
    assert "<script>" not in text and "rotate" not in text.lower().replace("rotate &lt;", "")


def test_render_corrected_stays_under_telegrams_limit():
    fields = ["do", "where", "code", "lang", "expect", "detail", "url", "file", "question",
              "blocking", "warning", "optional", "substeps"]
    summary = {"changed": [{"id": f"s{n}", "path": f"{n}.10", "fields": fields} for n in range(1, 61)],
               "added": [{"id": f"r2-{n}", "path": str(n)} for n in range(1, 21)],
               "removed": [{"id": f"x{n}", "path": str(n)} for n in range(1, 21)],
               "moved": [], "top": ["why", "verify", "reply", "links"]}
    text = TelegramClient.render_corrected("P" * 60, "T" * 120, 11, "&" * 200, summary)
    assert len(text) < 4096
    twenty = {**summary, "changed": summary["changed"][:20]}
    assert len(TelegramClient.render_corrected("P", "T", 2, "r", twenty)) < 4096


def test_every_callback_fits_at_rev_11():
    item_id = "9f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    ins = {"v": 1, "title": "t", "steps": [{"do": f"s{n}"} for n in range(19)] + [
        {"do": "last", "substeps": [{"do": f"sub{m}"} for m in range(9)] + [
            {"do": "deep", "question": {"prompt": "?", "choices": ["a", "b", "c", "d", "e", "f"]}}]}]}
    keyboard = TelegramClient.ask_keyboard(item_id, None, ins, rev=11)
    data = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    assert f"a:{item_id}:20.10:5:11" in data and f"d:{item_id}:11" in data
    assert all(len(d.encode()) <= 64 for d in data)
    for d in (f"e:{item_id}:y", f"k:{item_id}:off", f"s:{item_id}"):
        assert len(d.encode()) <= 64


def test_ask_keyboard_binds_done_and_answers_to_the_rev():
    ins = {"v": 1, "title": "t", "steps": [{"do": "x", "question": {"prompt": "?", "choices": ["A", "B"]}}]}
    rows = TelegramClient.ask_keyboard("id", None, ins, rev=2)["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["a:id:1:0:2", "a:id:1:1:2"]
    assert rows[1][0]["callback_data"] == "d:id:2" and rows[1][1]["callback_data"] == "s:id"


def test_a_rev_1_keyboard_is_the_1_0_0_keyboard():
    """No :<rev> on rev 1, so Done and the answers keep working after a rollback to
    1.0.0, which reads d:<id> and a:<id>:<path>:<index> (a missing rev is rev 1)."""
    item_id = "9f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    ins = {"v": 1, "title": "t", "steps": [
        {"do": "x", "question": {"prompt": "?", "choices": ["A", "B"]}},
        {"do": "y", "substeps": [{"do": "z", "question": {"prompt": "?", "choices": ["C"]}}]}]}
    for keyboard in (TelegramClient.ask_keyboard(item_id, None, ins),
                     TelegramClient.ask_keyboard(item_id, None, ins, rev=1)):
        data = [b.get("callback_data") for row in keyboard["inline_keyboard"] for b in row]
        assert data == [f"a:{item_id}:1:0", f"a:{item_id}:1:1", f"a:{item_id}:2.1:0",
                        f"d:{item_id}", f"s:{item_id}"]
    assert [b["callback_data"] for b in TelegramClient.ask_keyboard(item_id)["inline_keyboard"][0]] == [
        f"d:{item_id}", f"s:{item_id}"]


def test_pending_keyboard_is_skip_and_open_only():
    assert TelegramClient.pending_keyboard("id", "http://h/i") == {"inline_keyboard": [
        [{"text": "🚫 Skip", "callback_data": "s:id"}], [{"text": "📋 Open in Hook", "url": "http://h/i/id"}]]}


def test_render_session_note_has_no_markup():
    text = TelegramClient.render_session_note("<a href=x>y</a>")
    assert text == "📝 Session note, not part of the ask:\n&lt;a href=x&gt;y&lt;/a&gt;"
    assert "<a" not in text


def test_render_superseded():
    assert TelegramClient.render_superseded("A <b>", 3) == "<s>A &lt;b&gt;</s>\nReplaced by version 3 below."


def test_send_message_reply_to_and_silent():
    fake = FakeTelegramClient()
    fake.send_message(1, "x", reply_to=5, disable_notification=True)
    method, params = fake.calls[-1]
    assert (params["reply_to_message_id"], params["disable_notification"]) == (5, True)
    fake.send_message(1, "y")
    assert "reply_to_message_id" not in fake.calls[-1][1] and "disable_notification" not in fake.calls[-1][1]


# --- required before Done (Pond 1.3.0) --------------------------------------------

_GOLDEN = Path(__file__).with_name("golden_asks_path.json")
_QUESTIONS = json.loads(Path(__file__).with_name("answers-vectors.json").read_text(encoding="utf-8"))["instructable"]


def _golden_question_keyboard():
    """The first keyboard the 1.2.0 golden recorded for the questions ask, its uuid as <uuid-3>."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    text = json.dumps(golden)
    start = text.index('{"inline_keyboard": [[{"callback_data": "a:<uuid-3>:1:0"')
    keyboard, _ = json.JSONDecoder().raw_decode(text, start)
    return keyboard


def test_a_ready_keyboard_is_the_1_2_0_keyboard():
    golden = _golden_question_keyboard()
    assert TelegramClient.ask_keyboard("<uuid-3>", None, _QUESTIONS, ready=True) == golden
    assert TelegramClient.ask_keyboard("<uuid-3>", None, _QUESTIONS) == golden


def test_a_keyboard_that_isnt_ready_has_no_done():
    ready = TelegramClient.ask_keyboard("id", "http://h/i", _QUESTIONS, rev=2)["inline_keyboard"]
    rows = TelegramClient.ask_keyboard("id", "http://h/i", _QUESTIONS, rev=2, ready=False)["inline_keyboard"]
    assert not any(b.get("callback_data", "").startswith("d:") for row in rows for b in row)
    assert rows[:-2] == ready[:-2]                                   # the question rows stay
    assert rows[-2] == [{"text": "🚫 Skip", "callback_data": "s:id"}]   # Skip alone
    assert rows[-1] == [{"text": "📋 Open in Hook", "url": "http://h/i/id"}]
    plain = TelegramClient.ask_keyboard("id", ready=False)["inline_keyboard"]
    assert plain == [[{"text": "🚫 Skip", "callback_data": "s:id"}]]


def test_the_required_mark():
    assert TelegramClient._required_mark({"do": "x", "required": True}) == " (required)"
    for step in ({"do": "x"}, {"do": "x", "required": False}, {"do": "x", "required": "yes"}, None, "x"):
        assert TelegramClient._required_mark(step) == ""


def test_the_before_done_line():
    line = TelegramClient._before_done_line
    assert line({"done": ["steps"]}) == "Before Done: every step"
    assert line({"done": ["message", "steps", "reply"]}) == "Before Done: every step, a reply, a message from Hook"
    assert line({"done": ["reply"]}) == "Before Done: a reply"
    for ins in ({}, {"done": []}, {"done": "steps"}, {"done": ["later"]}, None):
        assert line(ins) is None


def test_required_steps_and_the_before_done_line_render():
    ins = {"v": 1, "title": "t", "done": ["steps", "message"],
           "steps": [{"do": "a", "required": True, "substeps": [{"do": "b", "required": True}, {"do": "c"}]},
                     {"do": "d", "optional": True}],
           "links": [{"label": "notes", "path": "~/notes.md"}]}
    lines = TelegramClient._render_instructable("P", "t", None, ins).split("\n")
    assert lines[1:] == ["1. a (required)", "   1a. b (required)", "   1b. c", "2. d (optional)",
                         "Before Done: every step, a message from Hook", "📄 notes.md (on the Mac)"]


def test_an_ask_without_the_keys_renders_as_before():
    ins = {"v": 1, "title": "t", "steps": [{"do": "a", "substeps": [{"do": "b"}]}, {"do": "d"}]}
    assert TelegramClient._render_instructable("P", "t", None, ins) == "🙋 <b>P</b> — t\n1. a\n   1a. b\n2. d"
    # The questions ask of the 1.2.0 golden (test_asks_golden.py holds the rest of it).
    golden = json.dumps(json.loads(_GOLDEN.read_text(encoding="utf-8")))
    rendered = TelegramClient.render_ask_message("Atlas", _QUESTIONS["title"], None, None, None, _QUESTIONS)
    assert json.dumps(rendered) in golden


def test_the_before_done_line_counts_in_the_text_budget():
    from mailbox.telegram import MAX_ASK_TEXT
    steps = [{"do": "x" * 300, "detail": "y" * 270, "substeps": [{"do": "z" * 300}]} for _ in range(10)]
    ins = {"v": 1, "title": "t", "done": ["steps", "reply", "message"], "steps": steps,
           "reply": {"prompt": "?", "choices": []}}
    text = TelegramClient._render_instructable("P", "t", None, ins)
    assert len(text) <= MAX_ASK_TEXT
    assert "Before Done: every step, a reply, a message from Hook" in text
    assert "more" in text.split("\n")[-2]                            # "+N more" sits before it


# --- the split past 4096 (Pond 1.4.0) --------------------------------------------

def _units(s):
    return len(s.encode("utf-16-le")) // 2


def test_split_short_text_one_part():
    from mailbox.telegram import TELEGRAM_LIMIT
    assert TELEGRAM_LIMIT == 4096
    text = "a" * 4094 + "😀"
    assert _units(text) == 4096
    assert TelegramClient.split_html(text) == [text]
    assert TelegramClient.split_html("") == [""]


def test_split_at_newline():
    text = "a" * 2999 + "\n" + "b " * 1000
    text = text + "c" * (5000 - len(text))
    parts = TelegramClient.split_html(text)
    assert len(parts) == 2
    assert parts[0] == "a" * 2999
    assert parts[1] == text[3000:]


def test_split_at_space_then_hard():
    text = "word " * 1000                                   # 5000, no newline
    parts = TelegramClient.split_html(text)
    assert all(_units(p) <= 4096 for p in parts) and len(parts) == 2
    assert parts[0].endswith("word") and "".join(p + " " for p in parts).strip() == text.strip()
    hard = "x" * 9000
    parts = TelegramClient.split_html(hard)
    assert [len(p) for p in parts] == [4096, 4096, 808] and "".join(parts) == hard


@pytest.mark.parametrize("piece", ["&amp;", "<i>italics</i>", "😀"])
def test_split_never_cuts_entity_tag_or_surrogate(piece):
    for offset in range(1, len(piece) + 2):
        text = "x" * (4096 - offset) + piece + "y" * 3000
        parts = TelegramClient.split_html(text)
        assert all(_units(p) <= 4096 for p in parts), offset
        joined = "".join(parts)
        if piece.startswith("<i>"):
            # The open <i> is closed at the end of the part and reopened in the next.
            assert all(p.count("<i>") == p.count("</i>") for p in parts)
            assert joined.replace("</i><i>", "") == text
        else:
            assert joined == text and any(piece in p for p in parts)
        for p in parts:
            assert not p.endswith("&am") and not p.startswith("p;")
            assert p.count("<") == p.count(">")
            p.encode("utf-16-le", errors="strict")
            p.encode("utf-8", errors="strict")


def test_split_closes_and_reopens_nested_tags():
    text = "<b>bold:<code>" + "z" * 5000 + "</code> end</b>"
    parts = TelegramClient.split_html(text)
    assert len(parts) == 2 and all(_units(p) <= 4096 for p in parts)
    assert _units(parts[0]) == 4096
    assert parts[0].startswith("<b>bold:<code>") and parts[0].endswith("</code></b>")
    assert parts[1].startswith("<b><code>") and parts[1].endswith("</code> end</b>")


def test_send_reply_sends_parts(client):
    client.send_reply(12345, 77, "r" * 6000)
    sends = [p for m, p in client.calls if m == "sendMessage"]
    assert len(sends) == 2
    assert [p["reply_to_message_id"] for p in sends] == [77, 77]
    assert "".join(p["text"] for p in sends) == "r" * 6000


def test_send_reply_returns_the_first_parts_result(client):
    result = client.send_reply(12345, 77, "r" * 6000)
    assert result["message_id"] == 1


def test_keyless_send_message_sends_parts(client):
    result = client.send_message(12345, "m" * 6000, reply_to=5, disable_notification=True)
    assert result["message_id"] == 1
    sends = [p for m, p in client.calls if m == "sendMessage"]
    assert len(sends) == 2
    assert all(p["reply_to_message_id"] == 5 and p["disable_notification"] is True for p in sends)


def test_keyboard_send_never_split(client):
    keyboard = {"inline_keyboard": [[{"text": "Done", "callback_data": "done:x"}]]}
    client.send_message(12345, "k" * 6000, reply_markup=keyboard)
    assert len([m for m, _ in client.calls if m == "sendMessage"]) == 1


# --- choice objects on Telegram (Pond 1.4.0) ------------------------------------------

def test_telegram_buttons_show_labels():
    ins = {"v": 1, "title": "t", "steps": [{"do": "Pick", "id": "s1", "question": {
        "prompt": "How?", "choices": [{"label": "Mix", "text": "What should change?"}, "All at once"]}}]}
    keyboard = TelegramClient.ask_keyboard("abc", None, ins, answers={"1": {"picked": ["Mix"]}})
    row = keyboard["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["1 · ✓ Mix", "All at once"]
    assert [b["callback_data"] for b in row] == ["a:abc:1:0", "a:abc:1:1"]
    assert "What should change?" not in json.dumps(keyboard)

@pytest.mark.parametrize("urgency", ["now", "today", "later"])
def test_alert_headline_escapes_and_omits_ask_details(urgency):
    text = TelegramClient.render_alert("<Example>", "Test & verify", {"urgency": urgency,
                                      "why": "hidden", "steps": [{"do": "hidden"}]})
    assert "&lt;Example&gt;" in text and "Test &amp; verify" in text
    assert f"Urgency: {urgency}" in text and "Open in Hook" in text
    assert "hidden" not in text
    assert len(TelegramClient.render_alert("<" * 1000, "&" * 4000)) < 4096


def test_alert_keyboard_is_only_a_link():
    item_id = "abcdef12-1234-5678-9abc-123456789abc"
    assert TelegramClient.alert_keyboard(item_id, "https://pond.example.test/i") == {"inline_keyboard": [[{
        "text": "📋 Open in Hook", "url": f"https://pond.example.test/i/{item_id}"}]]}
    assert TelegramClient.alert_keyboard(item_id, None) == {"inline_keyboard": []}


def test_ask_warning_precedes_all_steps():
    text = _render({"v": 1, "title": "t", "warning": "Do not run twice", "steps": [{"do": "Run once"}]})
    assert text.index("⚠️ Do not run twice") < text.index("1. Run once")
