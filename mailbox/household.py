"""Household mode (MAILBOX_MEMBERS set): a separate bot for a household's members.

Members' private chats only. A text or a captioned photo becomes a Central Time
household capture (command id tg:<chat>:<message>, so a redelivered update is
a replay, not a duplicate). A tap on an hh: message acks the delivery in
Central Time as the tapping member. No asks, no brief, no open-ask counts.
Nothing of a text, a photo or a token reaches a log line."""
import base64
import dataclasses
import html
import json
import logging
import re
from datetime import date
from typing import Any, Optional

from mailbox import capture
from mailbox.ct_client import CentralTimeError
from mailbox.instructable import looks_secret
from mailbox.notification import HH_ACTION_IDS
from mailbox.store import Notification
from mailbox.telegram import FileTooLarge, TelegramError

logger = logging.getLogger("mailbox")
PHOTO_MAX = 5 * 1024 * 1024
PRUNE_DAYS = 35
HH_MAX_TITLE = 180  # Central Time core refuses a household command text above 180
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_FORWARD_KEYS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name")
_TAP_ANSWERS = {"done": "Done", "accept": "Accepted", "decline": "Declined"}
# Core's verdicts that end the message: 404 gone, 409 closed (the target already changed, S145), 409 stale
# (the delivery itself was cancelled or expired). Recorded as the outcome and struck through.
_ENDED = {"gone": "Gone from Central Time", "closed": "Already changed", "stale": "Changed in Central Time"}


def parse_members(raw: str, owner_id: int) -> dict[int, str]:
    """'owner:12345,partner:222' -> {12345: 'owner', 222: 'partner'}; must include the owner."""
    out: dict[int, str] = {}
    for part in raw.split(","):
        name, sep, tg = part.strip().partition(":")
        if not sep or not _NAME.match(name) or not tg.isdigit() or int(tg) <= 0 or int(tg) in out:
            raise ValueError("MAILBOX_MEMBERS must be name:telegram-id pairs, comma separated")
        out[int(tg)] = name
    if owner_id not in out:
        raise ValueError("MAILBOX_MEMBERS must include MAILBOX_OWNER_ID")
    return out


def handle_update(svc, update: dict[str, Any]) -> None:
    """Members only; every callback is answered exactly once (S26)."""
    if "callback_query" in update:
        cb = update["callback_query"]
        from_id = (cb.get("from") or {}).get("id")
        if from_id not in svc.members:
            svc.dropped_updates += 1
            return
        data = cb.get("data", "")
        answer = _tap(svc, data[2:], from_id) if data.startswith("r:") else "Ignored"
        try:
            svc.telegram.answer_callback_query(cb.get("id"), answer)
        except TelegramError:
            pass
        return
    msg = update.get("message") or {}
    from_id = (msg.get("from") or {}).get("id")
    if from_id not in svc.members:
        svc.dropped_updates += 1
        return
    _capture(svc, msg, from_id)


def _capture(svc, msg: dict[str, Any], from_id: int) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if svc.ct_client is None or chat.get("type", "private") != "private" or chat_id != from_id or any(msg.get(k) for k in _FORWARD_KEYS):
        svc.ignored_messages += 1
        return
    message_id = msg.get("message_id")
    text = " ".join((msg.get("text") or msg.get("caption") or "").split())
    photos = msg.get("photo") or []
    if msg.get("document"):
        svc._reply(chat_id, message_id, "Not saved: send it as a photo, not a file.")
        return
    if (not text and not photos) or text.startswith("/"):
        svc.ignored_messages += 1
        return
    if text and looks_secret(text):
        svc._reply(chat_id, message_id, "Not saved: that looks like a secret.")
        return
    now = svc.now()
    cap = capture.parse(text, now) if text else capture.Capture(title="Photo", date=None, time=None)
    # Core refuses a title above HH_MAX_TITLE UTF-16 units (commands.ts); cut there, not at the asks 200.
    cap = dataclasses.replace(cap, title=capture._cut(cap.title, HH_MAX_TITLE))
    if not cap.title:
        svc.ignored_messages += 1
        return
    body: dict[str, Any] = {"type": "capture", "commandId": f"tg:{chat_id}:{message_id}", "actorTgId": from_id, "text": cap.title}
    if cap.date:
        body["date"] = cap.date
    if cap.time:
        body["time"] = cap.time
    if photos:
        photo = _photo(svc, photos, chat_id, message_id)
        if photo is None:
            return
        body["photo"] = photo
    try:
        out = svc.ct_client.hh_command(body)
    except CentralTimeError as exc:
        # The reason is one of HH_REASONS or None: a fixed word, never response text.
        logger.warning("[mailbox] household capture failed: %s %s %s", exc.what, exc.status, exc.reason)
        svc._reply(chat_id, message_id, _refusal(exc, cap))
        return
    logger.info("[mailbox] household capture: recorded")
    svc._reply(chat_id, message_id, _added(cap, out.get("suggestion"), now.date(), bool(photos)))


def _photo(svc, photos: list[dict[str, Any]], chat_id: int, message_id: Optional[int]) -> Optional[dict[str, str]]:
    """The largest size within PHOTO_MAX, downloaded; None after replying why not. A size Telegram did not
    report is unknown, not assumed to fit: getFile's size is checked, then the download reads with the cap."""
    fitting = [p for p in photos if p.get("file_size") is None or p["file_size"] <= PHOTO_MAX]
    if not fitting:
        svc._reply(chat_id, message_id, "Not saved: the photo is too large.")
        return None
    best = max(fitting, key=lambda p: (p.get("width") or 0) * (p.get("height") or 0))
    try:
        info = svc.telegram.get_file(best["file_id"])
        if (info.get("file_size") or 0) > PHOTO_MAX:
            raise FileTooLarge("file too large")
        raw = svc.telegram.download_file(info["file_path"], PHOTO_MAX)
    except FileTooLarge:
        svc._reply(chat_id, message_id, "Not saved: the photo is too large.")
        return None
    except (TelegramError, KeyError) as exc:
        logger.warning("[mailbox] household photo download failed: %s", type(exc).__name__)
        svc._reply(chat_id, message_id, "Not saved: the photo did not download. Send it again.")
        return None
    return {"mime": "image/jpeg", "data": base64.b64encode(raw).decode("ascii")}


def _added(cap: capture.Capture, suggestion: Any, today: date, photo: bool) -> str:
    parts = [f"In Inbox: <b>{html.escape(cap.title)}</b>"]
    if cap.date:
        parts.append(capture.describe(cap, today))
    label = suggestion.get("label") if isinstance(suggestion, dict) else None
    if isinstance(label, str) and label:
        parts.append(html.escape(label))
    if photo:
        parts.append("photo")
    return " · ".join(parts)


def _refusal(exc: CentralTimeError, cap: capture.Capture) -> str:
    if exc.reason == "nonexistent_time" and cap.date:
        d = date.fromisoformat(cap.date)
        return f"That time does not exist on {d.day} {capture.MONTHS[d.month - 1]} (clocks jump). Send another time."
    if exc.reason in ("photo_too_large", "too_large"):
        return "Not saved: the photo is too large."
    if exc.reason == "storage_full":
        return "Not saved: photo storage is full."
    if exc.reason == "not_member" or exc.status in (401, 403):
        return "Not saved: you are not set up in Central Time."
    if 400 <= exc.status <= 499:
        # Includes 409 command_mismatch (S138): a refused command is never shown as saved.
        return "Not saved: Central Time refused it."
    return "Not saved: Central Time did not answer. Send it again in a minute."


def _tap(svc, rest: str, from_id: int) -> str:
    """A tap on a household message: the toast text. Central Time decides, as the tapping member;
    the message is edited only after it answered. No title reaches a log."""
    nid, _, action = rest.partition(":")
    n = svc.store.get_notification(nid) if nid and action in HH_ACTION_IDS else None
    if n is None or not n.ref.startswith("hh:") or action not in {a.get("id") for a in json.loads(n.actions)}:
        return "Unknown reminder"
    if n.tg_chat_id != from_id:
        return "Not yours"
    if n.action_taken:
        return "Already handled"
    if svc.ct_client is None:
        return "Central Time is not configured"
    try:
        result = svc.ct_client.hh_ack(n.ref[3:], action, from_id)
    except CentralTimeError as exc:
        logger.warning("[mailbox] household tap failed: %s %s %s", exc.what, exc.status, exc.reason)
        if exc.reason == "not_yours":
            return "Not yours"
        outcome = "gone" if exc.status == 404 else exc.reason if exc.status == 409 and exc.reason in ("closed", "stale") else None
        if outcome:
            svc._end_notification(n, outcome)
            return _ENDED[outcome]
        if exc.reason == "not_member":
            return "You are not set up in Central Time"
        if exc.status in (401, 403):
            return "Central Time refused the token"
        return "Central Time did not answer; try again"
    svc._end_notification(n, action)
    return "Already handled in Central Time" if result.get("applied") is False else _TAP_ANSWERS[action]


def notify_member(svc, kind: str, ref: str, title: str, body: Optional[str] = None,
                  actions: Optional[list[dict[str, str]]] = None, to: Optional[int] = None) -> tuple[Notification, bool]:
    """The household /notify: to a member's chat, once per ref, no ask count. Service._send_once gives it the
    per-ref lock and the store-on-success rule: the ref is claimed only after Telegram accepted, so a refused
    or failed post never blocks the sender's retry with the same ref. Raises ValueError for a non-member."""
    if to not in svc.members:
        raise ValueError("to: not a member")
    return svc._send_once(ref, to, kind, title, body, actions)
