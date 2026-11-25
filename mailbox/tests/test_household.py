"""Household mode (MAILBOX_MEMBERS). The asks path is pinned by test_asks_golden.py."""
import base64
import http.client
import io
import json
import logging
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mailbox.__main__ import load_config
from mailbox.ct_client import CentralTimeError, FakeCentralTimeClient
from mailbox.household import PHOTO_MAX, notify_member, parse_members
from mailbox.notification import validate
from mailbox.service import Service
from mailbox.store import Notification, Store
from mailbox.server import MailboxServer
from mailbox.telegram import FakeTelegramClient, TelegramClient
from mailbox.tests.test_server import _running, call, find_free_port, post_ask

AMS = ZoneInfo("Europe/Amsterdam")
OWNER, PARTNER = 12345, 222
MEMBERS = {OWNER: "owner", PARTNER: "partner"}
BASE_ENV = {"TELEGRAM_BOT_TOKEN": "x", "MAILBOX_OWNER_ID": str(OWNER)}


@pytest.fixture
def hh():
    store = Store(":memory:"); store.open()
    tg = FakeTelegramClient(owner_id=OWNER)
    ct = FakeCentralTimeClient()
    svc = Service(store, tg, "household", ct_client=ct, zone=AMS, members=MEMBERS)
    svc.now = lambda: datetime(2026, 10, 26, 10, 0, tzinfo=AMS)
    yield svc, store, tg, ct
    store.close()


def msg(uid, from_id, text=None, **extra):
    m = {"message_id": 500 + uid, "from": {"id": from_id}, "chat": {"id": from_id, "type": "private"}, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": uid, "message": m}


def tap(uid, from_id, data, chat=None):
    cb = {"id": f"cb{uid}", "from": {"id": from_id}, "data": data}
    if chat is not None:
        cb["message"] = {"message_id": 1, "chat": chat}
    return {"update_id": uid, "callback_query": cb}


def test_parse_members():
    assert parse_members("owner:12345, partner:222", OWNER) == MEMBERS
    for bad in ("owner", "owner:x", "Owner:1", "a:1,b:1", "partner:222"):
        with pytest.raises(ValueError):
            parse_members(bad, OWNER)


CT_ENV = {"MAILBOX_CT_URL": "http://ct.test", "MAILBOX_CT_TOKEN": "ct_" + "0" * 48}


def test_load_config_members():
    assert "members" not in load_config(BASE_ENV)
    assert load_config({**BASE_ENV, **CT_ENV, "MAILBOX_MEMBERS": "owner:12345,partner:222"})["members"] == MEMBERS
    with pytest.raises(ValueError, match="MAILBOX_MEMBERS"):
        load_config({**BASE_ENV, **CT_ENV, "MAILBOX_MEMBERS": "partner:222"})


def test_load_config_refuses_members_without_central_time():
    """A household daemon without Central Time would drop every member text in silence."""
    for env in (BASE_ENV, {**BASE_ENV, "MAILBOX_CT_URL": "http://ct.test"}):
        with pytest.raises(ValueError, match="MAILBOX_MEMBERS needs MAILBOX_CT_URL and MAILBOX_CT_TOKEN"):
            load_config({**env, "MAILBOX_MEMBERS": "owner:12345,partner:222"})


def test_a_non_member_is_dropped(hh):
    svc, _, tg, ct = hh
    svc.handle_update(msg(1, 999, "hi"))
    assert (svc.dropped_updates, tg.calls, ct.calls) == (1, [], [])


def test_a_member_text_becomes_a_capture_with_a_stable_command_id(hh):
    svc, _, tg, ct = hh
    svc.handle_update(msg(1, PARTNER, "Reply to the doctor tomorrow 9:00"))
    svc.handle_update(msg(1, PARTNER, "Reply to the doctor tomorrow 9:00"))  # redelivered after a restart
    bodies = [b for (_, path, b) in ct.calls]
    assert [p for (_, p, _) in ct.calls] == ["/api/hh/commands"] * 2
    assert bodies[0] == bodies[1] == {"type": "capture", "commandId": "tg:222:501", "actorTgId": PARTNER,
                                      "text": "Reply to the doctor", "date": "2026-10-27", "time": "09:00"}
    assert tg.replies[0]["text"] == "In Inbox: <b>Reply to the doctor</b> · tomorrow 09:00"


def test_a_captioned_photo_is_downloaded_and_sent_never_logged(hh, caplog):
    svc, _, tg, ct = hh
    tg.files["big"] = b"\xff\xd8" + b"x" * 100
    ct.answer = {"recordId": "rec-1", "version": 1, "suggestion": {"label": "Every 5 days after done"}}
    caplog.set_level(logging.INFO)
    svc.handle_update(msg(2, PARTNER, caption="Water the plants every 5 days", photo=[
        {"file_id": "small", "file_size": 10, "width": 90, "height": 90}, {"file_id": "big", "file_size": 102, "width": 1280, "height": 960}]))
    body = ct.calls[0][2]
    assert body["photo"] == {"mime": "image/jpeg", "data": base64.b64encode(tg.files["big"]).decode()}
    assert tg.replies[0]["text"] == "In Inbox: <b>Water the plants every 5 days</b> · Every 5 days after done · photo"
    assert body["photo"]["data"][:12] not in caplog.text
    assert "test_token" not in caplog.text
    assert "Water the plants" not in caplog.text


def test_photo_refusals(hh):
    svc, _, tg, ct = hh
    svc.handle_update(msg(3, PARTNER, caption="x", photo=[{"file_id": "f", "file_size": PHOTO_MAX + 1, "width": 1, "height": 1}]))
    svc.handle_update(msg(4, PARTNER, caption="x", document={"file_id": "d", "mime_type": "image/heic"}))
    assert ct.calls == []
    assert [r["text"] for r in tg.replies] == ["Not saved: the photo is too large.", "Not saved: send it as a photo, not a file."]


def test_a_photo_that_does_not_download_is_not_saved(hh):
    svc, _, tg, ct = hh
    svc.handle_update(msg(14, PARTNER, caption="x", photo=[{"file_id": "missing", "file_size": 5, "width": 1, "height": 1}]))
    assert ct.calls == []
    assert tg.replies[0]["text"] == "Not saved: the photo did not download. Send it again."


def test_a_nonexistent_time_is_explained(hh):
    svc, _, tg, ct = hh
    ct.fail = CentralTimeError(422, "hh-command", "nonexistent_time")
    svc.now = lambda: datetime(2027, 3, 20, 10, 0, tzinfo=AMS)
    svc.handle_update(msg(5, PARTNER, "Night feed 2027-03-28 2:30"))
    assert tg.replies[0]["text"] == "That time does not exist on 28 Mar (clocks jump). Send another time."


def test_a_command_mismatch_is_a_plain_failure(hh):
    """S138: a reused command id with another body is refused by core; never shown as saved."""
    svc, _, tg, ct = hh
    ct.fail = CentralTimeError(409, "hh-command", "command_mismatch")
    svc.handle_update(msg(15, PARTNER, "Buy plane tickets"))
    assert tg.replies[0]["text"] == "Not saved: Central Time refused it."
    assert "In Inbox" not in tg.replies[0]["text"]


def test_central_time_down_during_a_capture(hh):
    svc, _, tg, ct = hh
    ct.fail = CentralTimeError(0, "hh-command")
    svc.handle_update(msg(12, PARTNER, "Buy plane tickets"))
    assert tg.replies[0]["text"] == "Not saved: Central Time did not answer. Send it again in a minute."


def test_central_time_down_during_a_tap_keeps_the_message_open(hh):
    svc, store, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d3", "Water the plants", "Today 18:00", [{"id": "done", "label": "✅ Done"}], OWNER)
    ct.fail = CentralTimeError(0, "hh-ack")
    svc.handle_update(tap(13, OWNER, f"r:{n.id}:done"))
    assert tg.callback_answers[-1]["text"] == "Central Time did not answer; try again"
    assert tg.edited_messages == []
    assert store.get_notification(n.id).action_taken is None


def test_groups_and_forwards_are_ignored(hh):
    svc, _, tg, ct = hh
    svc.handle_update({"update_id": 6, "message": {"message_id": 1, "from": {"id": PARTNER}, "chat": {"id": -5, "type": "group"}, "text": "x"}})
    svc.handle_update(msg(7, PARTNER, "x", forward_origin={"type": "user"}))
    assert (ct.calls, tg.calls, svc.ignored_messages) == ([], [], 2)


def test_bot_commands_and_secrets_are_not_captured(hh):
    svc, _, tg, ct = hh
    svc.handle_update(msg(16, PARTNER, "/start"))
    svc.handle_update(msg(17, PARTNER, "ghp_" + "a" * 36))
    assert ct.calls == [] and svc.ignored_messages == 1
    assert [r["text"] for r in tg.replies] == ["Not saved: that looks like a secret."]


def test_notify_member_sends_to_that_chat_once_and_never_counts_asks(hh):
    svc, _, tg, _ = hh
    acts = [{"id": "accept", "label": "🤝 Accept"}, {"id": "decline", "label": "↩️ Decline"}]
    n, deduped = notify_member(svc, "reminder", "hh:d1", "Buy plane tickets", "Proposed to you · By Fri 30 Oct", acts, OWNER)
    again, deduped2 = notify_member(svc, "reminder", "hh:d1", "Buy plane tickets", "x", acts, OWNER)
    assert (deduped, deduped2, again.id) == (False, True, n.id)
    assert tg.sent_messages[0]["chat_id"] == OWNER and "Open asks" not in tg.sent_messages[0]["text"]
    assert len(tg.sent_messages) == 1
    with pytest.raises(ValueError):
        notify_member(svc, "reminder", "hh:d2", "t", None, [], 999)


def test_household_validation():
    ok, errs = validate({"kind": "reminder", "ref": "hh:d1", "title": "t", "to": 222, "actions": [{"id": "accept", "label": "A"}]}, household=True)
    assert errs == [] and ok["to"] == 222
    assert validate({"kind": "reminder", "ref": "hh:d1", "title": "t"}, household=True)[1] == ["to: must be a Telegram user id"]
    assert validate({"kind": "brief", "ref": "b", "title": "t", "to": 1}, household=True)[1] == ["kind: a household instance takes reminders only"]
    assert validate({"kind": "reminder", "ref": "hh:d1", "title": "t", "to": 1, "actions": [{"id": "tomorrow", "label": "A"}]}, household=True)[1] == ["actions[0].id: must be one of done, accept, decline"]
    # The asks instance is unchanged: no `to`, done/tomorrow only.
    assert validate({"kind": "reminder", "ref": "r", "title": "t", "to": 1})[1] == ["to: unknown key"]
    assert validate({"kind": "reminder", "ref": "r", "title": "t", "actions": [{"id": "accept", "label": "A"}]})[1] == ["actions[0].id: must be one of done, tomorrow"]


def test_taps_ack_in_central_time_once_and_only_by_the_recipient(hh):
    svc, store, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d1", "Buy plane tickets", "Proposed to you", [{"id": "accept", "label": "🤝 Accept"}, {"id": "decline", "label": "↩️ Decline"}], OWNER)
    svc.handle_update(tap(8, PARTNER, f"r:{n.id}:accept"))
    svc.handle_update(tap(9, OWNER, f"r:{n.id}:accept"))
    svc.handle_update(tap(10, OWNER, f"r:{n.id}:accept"))
    assert [a["text"] for a in tg.callback_answers] == ["Not yours", "Accepted", "Already handled"]
    assert ct.calls == [("POST", "/api/hh/deliveries/d1/ack", {"action": "accept", "actorTgId": OWNER})]
    assert "accepted" in tg.edited_messages[-1]["text"]


def test_a_stale_tap_closes_the_message(hh):
    svc, _, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d2", "Water the plants", "Today 18:00", [{"id": "done", "label": "✅ Done"}], OWNER)
    ct.fail = CentralTimeError(409, "hh-ack", "stale")
    svc.handle_update(tap(11, OWNER, f"r:{n.id}:done"))
    assert tg.callback_answers[-1]["text"] == "Changed in Central Time"
    assert "changed in Central Time" in tg.edited_messages[-1]["text"]


def test_a_tap_on_something_already_changed(hh):
    """S145: core answers 409 closed when the target moved on; the person sees Already changed."""
    svc, store, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d4", "Water the plants", "Today 18:00", [{"id": "done", "label": "✅ Done"}], OWNER)
    ct.fail = CentralTimeError(409, "hh-ack", "closed")
    svc.handle_update(tap(18, OWNER, f"r:{n.id}:done"))
    assert tg.callback_answers[-1]["text"] == "Already changed"
    assert store.get_notification(n.id).action_taken == "closed"
    assert tg.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}


def test_a_tap_already_applied_in_central_time(hh):
    svc, store, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d5", "Water the plants", "Today 18:00", [{"id": "done", "label": "✅ Done"}], OWNER)
    ct.answer = {"applied": False, "outcome": "done"}
    svc.handle_update(tap(19, OWNER, f"r:{n.id}:done"))
    assert tg.callback_answers[-1]["text"] == "Already handled in Central Time"
    assert store.get_notification(n.id).action_taken == "done"


def test_an_asks_reminder_tap_is_unknown_in_household_mode(hh):
    svc, store, tg, ct = hh
    store.insert_notification(Notification(id="n-asks", kind="reminder", ref="fire-1", title="t", body=None,
                                           actions=json.dumps([{"id": "done", "label": "Done"}]), tg_chat_id=OWNER,
                                           tg_message_id=1, action_taken=None, acted_at=None, created="2026-10-01T00:00:00+00:00"))
    svc.handle_update(tap(20, OWNER, "r:n-asks:done"))
    svc.handle_update(tap(21, OWNER, "d:some-ask"))
    assert [a["text"] for a in tg.callback_answers] == ["Unknown reminder", "Ignored"]
    assert ct.calls == []


def test_prune_notifications_drops_old_rows_only():
    store = Store(":memory:"); store.open()
    for i, created in enumerate(("2026-09-01T00:00:00+00:00", "2026-10-25T00:00:00+00:00")):
        store.insert_notification(Notification(id=f"n{i}", kind="reminder", ref=f"hh:{i}", title="t", body=None, actions="[]",
                                               tg_chat_id=OWNER, tg_message_id=1, action_taken=None, acted_at=None, created=created))
    assert store.prune_notifications("2026-09-21T00:00:00+00:00") == 1
    assert store.get_notification("n0") is None and store.get_notification("n1") is not None
    store.close()


def test_the_household_server_takes_no_asks_and_routes_notify(tmp_path):
    for running in _running(tmp_path, members=MEMBERS, notify_token="c" * 64):
        srv, base, token = running
        assert post_ask(base, token)[0] == 403
        status, _ = call(base, "/notify", token="c" * 64, method="POST", body={"kind": "reminder", "ref": "hh:x1", "title": "t", "to": 999})
        assert status == 400
        status, _ = call(base, "/notify", token="c" * 64, method="POST", body={"kind": "reminder", "ref": "hh:x2", "title": "t", "to": PARTNER})
        assert status == 201
        assert srv.telegram.sent_messages[-1]["chat_id"] == PARTNER


def test_notify_stores_a_ref_only_when_it_accepts_and_dedupes_by_ref(tmp_path):
    """The CT sender retries a 400 with the same ref and another title: the refused body must not claim the ref;
    the accepted one must, so a later repost of that ref sends nothing."""
    for running in _running(tmp_path, members=MEMBERS, notify_token="c" * 64):
        srv, base, _ = running
        ref = "hh:d9"
        status, _ = call(base, "/notify", token="c" * 64, method="POST", body={"kind": "reminder", "ref": ref, "title": "", "to": PARTNER})
        assert status == 400 and srv.store.get_notification_by_ref(ref) is None
        body = {"kind": "reminder", "ref": ref, "title": "(reminder)", "to": PARTNER, "actions": [{"id": "done", "label": "✅ Done"}]}
        status, first = call(base, "/notify", token="c" * 64, method="POST", body=body)
        assert status == 201 and first["deduped"] is False
        status, again = call(base, "/notify", token="c" * 64, method="POST", body=body)
        assert (status, again) == (200, {"id": first["id"], "deduped": True})
        assert len(srv.telegram.sent_messages) == 1
        # A tap on it goes to Central Time with the tapping member's Telegram id.
        srv.service.ct_client = FakeCentralTimeClient()
        srv.service.handle_update(tap(22, PARTNER, f"r:{first['id']}:done"))
        assert srv.service.ct_client.calls == [("POST", "/api/hh/deliveries/d9/ack", {"action": "done", "actorTgId": PARTNER})]


def test_the_household_backup_prunes_old_notifications(tmp_path):
    for running in _running(tmp_path, members=MEMBERS):
        srv, _, _ = running
        srv.store.insert_notification(Notification(id="old", kind="reminder", ref="hh:old", title="t", body=None, actions="[]",
                                                   tg_chat_id=OWNER, tg_message_id=1, action_taken="done", acted_at=None,
                                                   created="2026-01-01T00:00:00+00:00"))
        srv._do_backup()
        assert srv.store.get_notification("old") is None


# --- review round 1 ------------------------------------------------------------------------------------------

class _Cut(io.BytesIO):
    """A response whose body breaks off mid-read."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        raise http.client.IncompleteRead(b"\xff\xd8", 100)


def test_a_download_that_breaks_off_is_a_plain_failure_reply(hh, monkeypatch, caplog):
    """IncompleteRead is an HTTPException, not an OSError: the real download_file must still turn it into
    TelegramError so the member is told to send it again and the poll loop is never disturbed."""
    svc, _, tg, ct = hh
    real = TelegramClient("test_token", OWNER)
    monkeypatch.setattr(real, "_open", lambda url, timeout: _Cut())
    tg.download_file = real.download_file
    caplog.set_level(logging.INFO)
    svc.handle_update(msg(30, PARTNER, caption="Water the plants", photo=[{"file_id": "p", "file_size": 102, "width": 9, "height": 9}]))
    assert tg.replies[0]["text"] == "Not saved: the photo did not download. Send it again."
    assert ct.calls == []
    assert "test_token" not in caplog.text and "Water the plants" not in caplog.text


def test_download_file_never_follows_a_redirect():
    real = TelegramClient("test_token", OWNER)
    assert real._open is TelegramClient._open
    from mailbox.telegram import _FILE_OPENER
    handler = next(h for h in _FILE_OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/x") is None


def test_a_photo_without_a_size_is_unknown_not_assumed_to_fit(hh):
    """getFile's size, when present, refuses it before any download; otherwise the streaming cap does."""
    svc, _, tg, ct = hh
    tg.files["unsized"] = b"\xff\xd8" + b"x" * (PHOTO_MAX + 10)
    svc.handle_update(msg(31, PARTNER, caption="x", photo=[{"file_id": "unsized", "width": 9, "height": 9}]))
    tg.file_sizes["sized"] = PHOTO_MAX + 1
    svc.handle_update(msg(32, PARTNER, caption="x", photo=[{"file_id": "sized", "width": 9, "height": 9}]))
    assert [r["text"] for r in tg.replies] == ["Not saved: the photo is too large."] * 2
    assert ct.calls == []
    assert [c for c in tg.calls if c[0] == "download"] == [("download", {"file_path": "photos/unsized.jpg"})]


def test_a_photo_without_a_size_that_fits_is_sent(hh):
    svc, _, tg, ct = hh
    tg.files["u"] = b"\xff\xd8ok"
    svc.handle_update(msg(33, PARTNER, caption="x", photo=[{"file_id": "u", "width": 9, "height": 9}]))
    assert ct.calls[0][2]["photo"]["data"] == base64.b64encode(tg.files["u"]).decode()


@pytest.mark.parametrize("to", [222.0, True, "222", -1, 0, None, [222], {"id": 222}, 999, 2 ** 63])
def test_a_crafted_to_never_reaches_a_non_member(tmp_path, to):
    for running in _running(tmp_path, members=MEMBERS, notify_token="c" * 64):
        srv, base, _ = running
        status, body = call(base, "/notify", token="c" * 64, method="POST",
                            body={"kind": "reminder", "ref": "hh:t1", "title": "t", "to": to})
        assert status == 400, (to, body)
        assert srv.telegram.sent_messages == []
        assert srv.store.get_notification_by_ref("hh:t1") is None


def test_the_same_ref_to_the_other_member_is_deduped_not_resent(hh):
    svc, _, tg, _ = hh
    n, _ = notify_member(svc, "reminder", "hh:d1", "t", None, [], OWNER)
    again, deduped = notify_member(svc, "reminder", "hh:d1", "t", None, [], PARTNER)
    assert deduped and again.id == n.id and [m["chat_id"] for m in tg.sent_messages] == [OWNER]


def test_a_tap_in_a_group_counts_only_for_the_recipient(hh):
    svc, _, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d1", "t", None, [{"id": "done", "label": "Done"}], OWNER)
    group = {"id": -100, "type": "supergroup"}
    svc.handle_update(tap(34, PARTNER, f"r:{n.id}:done", chat=group))   # forwarded keyboard, other member
    svc.handle_update(tap(35, 999, f"r:{n.id}:done", chat=group))       # stranger
    svc.handle_update(tap(36, OWNER, f"r:{n.id}:done", chat=group))     # recipient, from the group copy
    assert [a["text"] for a in tg.callback_answers] == ["Not yours", "Done"]
    assert svc.dropped_updates == 1
    assert ct.calls == [("POST", "/api/hh/deliveries/d1/ack", {"action": "done", "actorTgId": OWNER})]


def test_a_group_message_and_a_message_without_from_never_reach_central_time(hh):
    svc, _, tg, ct = hh
    svc.handle_update({"update_id": 37, "message": {"message_id": 1, "from": {"id": OWNER}, "sender_chat": {"id": -5},
                                                    "chat": {"id": -5, "type": "supergroup"}, "text": "x"}})
    svc.handle_update({"update_id": 38, "message": {"message_id": 2, "chat": {"id": OWNER, "type": "private"}, "text": "x"}})
    assert ct.calls == [] and tg.calls == []
    assert (svc.ignored_messages, svc.dropped_updates) == (1, 1)


def test_the_command_id_is_chat_and_message_only(hh):
    svc, _, _, ct = hh
    u = msg(7, PARTNER, "Buy milk")
    svc.handle_update(u)
    svc.handle_update({"update_id": 99, "message": dict(u["message"])})   # redelivered with another update_id
    assert [b["commandId"] for (_, _, b) in ct.calls] == ["tg:222:507", "tg:222:507"]


def test_two_instances_on_their_own_backups_dirs_keep_their_own_token_mirrors(tmp_path):
    asks = MailboxServer(data_dir=tmp_path / "asks", backup_dir=tmp_path / "backups", telegram_token="a", owner_id=OWNER,
                         context="personal", port=find_free_port(), host="127.0.0.1")
    hh_srv = MailboxServer(data_dir=tmp_path / "hh", backup_dir=tmp_path / "backups-household", telegram_token="b",
                           owner_id=OWNER, context="household", port=find_free_port(), host="127.0.0.1", members=MEMBERS)
    assert (tmp_path / "backups" / "mailbox-token").read_text().strip() == asks.mailbox_token
    assert (tmp_path / "backups-household" / "mailbox-token").read_text().strip() == hh_srv.mailbox_token
    asks._do_backup(); hh_srv._do_backup()
    assert len(list((tmp_path / "backups").glob("mailbox-*.db"))) == len(list((tmp_path / "backups-household").glob("mailbox-*.db"))) == 1
    asks.stop(); hh_srv.stop()


def test_a_long_text_is_cut_to_what_central_time_takes(hh):
    """Core refuses a title above 180 UTF-16 units; the mailbox cuts there, not at its own 200."""
    svc, _, tg, ct = hh
    svc.handle_update(msg(30, PARTNER, "a" * 200))
    svc.handle_update(msg(31, PARTNER, "b" * 179 + "😀" + "c" * 20))
    texts = [b["text"] for (_, _, b) in ct.calls]
    assert len(ct.calls) == 2
    assert texts[0] == "a" * 180
    assert texts[1] == "b" * 179  # the astral emoji would make 181 units; it is not split
    assert tg.replies[0]["text"] == "In Inbox: <b>" + "a" * 180 + "</b>"


def test_a_tap_by_a_member_core_does_not_know_names_the_setup_not_the_token(hh):
    svc, store, tg, ct = hh
    n, _ = notify_member(svc, "reminder", "hh:d6", "Water the plants", "Today 18:00", [{"id": "done", "label": "✅ Done"}], OWNER)
    ct.fail = CentralTimeError(403, "hh-ack", "not_member")
    svc.handle_update(tap(32, OWNER, f"r:{n.id}:done"))
    assert tg.callback_answers[-1]["text"] == "You are not set up in Central Time"
    assert store.get_notification(n.id).action_taken is None


def test_the_asks_env_template_never_turns_on_household_mode():
    """The asks template must not carry MAILBOX_MEMBERS: setting it switches a daemon into household mode."""
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "MAILBOX_MEMBERS" not in example
