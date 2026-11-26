"""The asks path, recorded on the code before household mode. With
MAILBOX_MEMBERS and MAILBOX_BRIEF_ASKS unset it must stay byte-for-byte the same.
MAILBOX_GOLDEN_WRITE=1 rewrites the golden; only ever on untouched code.
The reviewed atomic-resolution change commits resolved before edit_failed;
that single expected event-order change is recorded in the golden."""
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mailbox.__main__ import load_config
from mailbox.ct_client import CentralTimeError, FakeCentralTimeClient
from mailbox.notification import validate as validate_notification
from mailbox.service import Service
from mailbox.store import Store
from mailbox.server import MailboxServer
from mailbox.telegram import FakeTelegramClient, TelegramError
from mailbox.tests.test_server import OWNER_ID, _running, answers_call, call, find_free_port, legacy_database, post_ask, post_questions

GOLDEN = Path(__file__).with_name("golden_asks_path.json")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
NOTIFY = "c" * 64
AMS = ZoneInfo("Europe/Amsterdam")
OWNER = 12345
QUESTIONS = json.loads((Path(__file__).parent / "answers-vectors.json").read_text(encoding="utf-8"))["instructable"]


def _norm(obj):
    """Stamps and ISO times are blanked; each distinct uuid becomes <uuid-N> in order of first appearance, so a
    button wired to the wrong ask or notification changes the golden."""
    seen = {}

    def number(m):
        return f"<uuid-{seen.setdefault(m.group(0), len(seen) + 1)}>"

    text = json.dumps(obj, sort_keys=True, default=str)
    return json.loads(STAMP.sub("<stamp>", ISO.sub("<iso>", UUID.sub(number, text))))


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


@contextmanager
def logged():
    """The mailbox log lines (level + message), kept off stderr while recording."""
    log = logging.getLogger("mailbox")
    handler, old_level, old_propagate = _Lines(), log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        yield handler.lines
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        log.propagate = old_propagate


def startup_section(tmp_path):
    """The startup log lines from a real start(), and /health once a poll has succeeded."""
    out = {}
    legacy_database(tmp_path / "start-data")
    with logged() as lines:
        srv = MailboxServer(
            data_dir=tmp_path / "start-data", backup_dir=tmp_path / "start-backups", telegram_token="test_token",
            owner_id=OWNER_ID, context="personal", port=find_free_port(), host="127.0.0.1",
            ct_url="http://127.0.0.1:1", ct_token="ct_" + "a" * 48, notify_token=NOTIFY,
        )
        srv.zone = AMS
        srv.telegram = srv.service.telegram = FakeTelegramClient(owner_id=OWNER_ID)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{srv.port}"
        deadline = time.time() + 10
        while time.time() < deadline and not any("listening" in x for x in lines):
            time.sleep(0.02)
        time.sleep(0.2)
        status, health = call(base, "/health")
        out["health"] = (status, {k: v for k, v in sorted(health.items()) if k != "last_poll_age_s"})
        srv.stop()
        thread.join(timeout=10)
        out["lines"] = [re.sub(r"\(tz ([^ ]+) [+-]\d{4} \w+\)", r"(tz \1 <offset>)", re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", x)) for x in lines]
    return out


def http_section(tmp_path):
    """The HTTP layer Task 9 edits (do_post_ask, do_notify, the auth split), recorded through a real server."""
    out = []
    for srv, base, token in _running(tmp_path, notify_token=NOTIFY):
        rem = {"kind": "reminder", "ref": "fire-9", "title": "Call the bank", "body": "today 18:00", "actions": [{"id": "done", "label": "\u2705 Done"}]}
        out.append(("ask", post_ask(base, token)))
        out.append(("ask-again", post_ask(base, token)))
        out.append(("asks", call(base, "/asks?status=open&days=14", token=token)))
        out.append(("notify", call(base, "/notify", token=NOTIFY, method="POST", body=rem)))
        out.append(("notify-again", call(base, "/notify", token=NOTIFY, method="POST", body=rem)))
        out.append(("notify-to", call(base, "/notify", token=NOTIFY, method="POST", body={**rem, "ref": "fire-10", "to": 222})))
        out.append(("ask-with-notify-token", post_ask(base, NOTIFY)))
        status, health = call(base, "/health")
        out.append(("health", (status, sorted(health))))
        # Step questions and the answers route.
        qid = post_questions(base, token)
        out.append(("answers", answers_call(base, token, qid, [{"step": "1", "picked": ["Prod"]}, {"step": "3", "text": "EU"}])))
        out.append(("answers-invalid", answers_call(base, token, qid, [{"step": "1", "picked": ["Prod"]}, {"step": "5", "text": "x"}])))
        out.append(("answers-no-list", call(base, f"/asks/{qid}/answers", token=token, method="POST", body={})))
        out.append(("answers-unknown", answers_call(base, token, "nope", [{"step": "1", "picked": ["Prod"]}])))
        out.append(("answers-no-token", call(base, f"/asks/{qid}/answers", method="POST", body={"answers": []})))
        out.append(("answers-notify-token", answers_call(base, NOTIFY, qid, [{"step": "1", "picked": ["Prod"]}])))
        out.append(("question-ask", call(base, f"/asks/{qid}", token=token)))
        out.append(("events", call(base, "/events", token=token)))
        out.append(("resolve", call(base, f"/asks/{qid}/resolve", token=token, method="POST", body={"verdict": "done"})))
        out.append(("answers-closed", answers_call(base, token, qid, [{"step": "1", "picked": ["Prod"]}])))
        out.append(("reopen", call(base, f"/asks/{qid}/reopen", token=token, method="POST", body={})))
        out.append(("answers-reopened", answers_call(base, token, qid, [{"step": "1", "picked": ["Staging"]}])))
        out.append(("telegram", srv.telegram.calls))
        out.append(("edited_markups", srv.telegram.edited_markups))
    for srv, base, token in _running(tmp_path / "relay", relay_open_base="http://127.0.0.1:47823/i"):
        out.append(("ask-relay", post_ask(base, token)))
        out.append(("telegram-relay", srv.telegram.calls))
    return out


def _msg(update_id, from_id, text=None, reply_to=None, chat_type="private", **extra):
    m = {"message_id": 100 + update_id, "from": {"id": from_id}, "chat": {"id": from_id, "type": chat_type}}
    if text is not None:
        m["text"] = text
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    m.update(extra)
    return {"update_id": update_id, "message": m}


def _tap(update_id, from_id, data):
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "from": {"id": from_id}, "data": data}}


def scenario(tmp_path):
    with logged() as lines:
        out = _service_section(tmp_path, lines)
    out["startup"] = startup_section(tmp_path)
    out["http"] = http_section(tmp_path)
    return _norm(out)


def _service_section(tmp_path, lines):
    store = Store(":memory:")
    store.open()
    tg = FakeTelegramClient(owner_id=OWNER)
    ct = FakeCentralTimeClient()
    svc = Service(store, tg, "personal", ct_client=ct, zone=AMS)
    svc.now = lambda: datetime(2026, 10, 26, 10, 0, tzinfo=AMS)
    out = {}
    a, _ = svc.post("Orbit", "fix the bug", why="slow", cmd="pytest")
    svc.post("Orbit", "fix the bug")
    b, _ = svc.post("Orbit", "second ask")
    c, _ = svc.post("Orbit", "third ask")
    svc.handle_update(_tap(1, OWNER, f"d:{a.id}"))
    svc.handle_update(_tap(2, OWNER, f"s:{b.id}"))
    svc.handle_update(_msg(3, OWNER, "done, fixed it", reply_to=3))  # ask c is the third message sent
    svc.handle_update(_msg(4, 999, "hello from a stranger"))
    svc.handle_update(_msg(5, OWNER, "dentist fri 10:00"))
    svc.handle_update(_msg(6, OWNER, caption="Water the plants every 5 days", photo=[{"file_id": "f1", "file_size": 1000, "width": 10, "height": 10}]))
    svc.handle_update(_msg(7, OWNER, "/start"))
    svc.handle_update(_msg(8, OWNER, "group text", chat_type="group"))
    svc.post("Orbit", "still open when the brief goes out")
    n1, d1 = svc.notify("reminder", "fire-1", "Call the bank", "today 18:00", [{"id": "done", "label": "\u2705 Done"}, {"id": "tomorrow", "label": "\u23ed Tomorrow"}])
    svc.notify("reminder", "fire-1", "Call the bank", "today 18:00", [])
    svc.notify("brief", "brief:2026-10-26", "Mon 26 Oct", "Today (1): Call the bank")
    n2, _ = svc.notify("reminder", "fire-2", "Pay rent", "today 19:00", [{"id": "done", "label": "\u2705 Done"}])
    svc.handle_update(_tap(9, OWNER, f"r:{n1.id}:done"))
    svc.handle_update(_tap(10, OWNER, f"r:{n1.id}:done"))
    ct.fail = CentralTimeError(409, "ack", "closed")
    svc.handle_update(_tap(11, OWNER, f"r:{n2.id}:done"))
    svc.handle_update(_tap(12, OWNER, "x:unknown"))
    # Step questions, question taps, answers.
    q, _ = svc.post("Atlas", "Ship the Atlas release", instructable=QUESTIONS)
    svc.handle_update(_tap(13, OWNER, f"a:{q.id}:1:1"))      # pick Prod
    svc.handle_update(_tap(14, OWNER, f"a:{q.id}:1:1"))      # same pick again: stores nothing
    svc.handle_update(_tap(15, OWNER, f"a:{q.id}:1:0"))      # change to Staging
    svc.handle_update(_tap(16, OWNER, f"a:{q.id}:1:9"))      # no such choice
    svc.handle_update(_tap(17, 999, f"a:{q.id}:1:1"))        # a stranger
    svc.answer(q.id, [{"step": "3", "text": "EU"}])
    svc.handle_update(_tap(18, OWNER, "a:nope:1:0"))         # unknown ask
    svc.handle_update(_tap(19, OWNER, f"d:{q.id}"))
    svc.handle_update(_tap(20, OWNER, f"a:{q.id}:1:1"))      # closed
    # Owner-only: a stranger can neither resolve, skip, ack nor reply; nothing changes.
    ct.fail = None
    g, _ = svc.post("Orbit", "guarded ask")
    gmsg = json.loads(store.get_item(g.id).payload)["tg_message_id"]
    n3, _ = svc.notify("reminder", "fire-3", "Water plants", "today 20:00", [{"id": "done", "label": "Done"}, {"id": "tomorrow", "label": "Tomorrow"}])
    before = (store.get_item(g.id).status, [e.kind for e in store.events_for_item(g.id)], store.get_notification(n3.id).action_taken, len(ct.calls))
    svc.handle_update(_tap(21, 999, f"d:{g.id}"))
    svc.handle_update(_tap(22, 999, f"s:{g.id}"))
    svc.handle_update(_tap(23, 999, f"r:{n3.id}:done"))
    svc.handle_update(_msg(24, 999, "done", reply_to=gmsg))
    svc.handle_update(_msg(25, 999, "plain text from a stranger"))
    out["stranger"] = {"before": before, "after": (store.get_item(g.id).status, [e.kind for e in store.events_for_item(g.id)], store.get_notification(n3.id).action_taken, len(ct.calls))}
    # A reply to an ask, a reply to a non-ask, a reply to a closed ask.
    svc.handle_update(_msg(26, OWNER, "still thinking", reply_to=gmsg))
    svc.handle_update(_msg(27, OWNER, "reply to a reminder", reply_to=999))
    svc.handle_update(_tap(28, OWNER, f"r:{n3.id}:tomorrow"))
    svc.handle_update(_msg(29, OWNER, "skip this one", reply_to=gmsg))
    svc.handle_update(_msg(30, OWNER, "late reply", reply_to=gmsg))
    # Capture with Central Time failing, and a Telegram error on send and on edit.
    ct.fail = CentralTimeError(500, "create", "down")
    svc.handle_update(_msg(31, OWNER, "buy milk tomorrow"))
    ct.fail = None
    real_call = tg._call
    def failing(method, **params):
        if method in ("sendMessage", "editMessageText"):
            raise TelegramError("boom")
        return real_call(method, **params)
    tg._call = failing
    try:
        svc.post("Orbit", "send fails")
        out["send-error"] = "no error"
    except TelegramError as exc:
        out["send-error"] = type(exc).__name__
    tg._call = real_call
    ed, _ = svc.post("Orbit", "edit fails")
    tg._call = failing
    svc.handle_update(_tap(32, OWNER, f"d:{ed.id}"))  # resolves; the message edit fails
    tg._call = real_call
    out["edit-error-events"] = [(e.kind, e.actor) for e in store.events_for_item(ed.id)]
    out["log"] = list(lines)
    out["telegram"] = tg.calls
    out["ct"] = ct.calls
    out["counters"] = {"dropped": svc.dropped_updates, "ignored": svc.ignored_messages}
    out["asks"] = sorted((i.title, i.status) for i in store.list_items(days=1))
    out["question-events"] = [(e.kind, e.actor, e.text) for e in store.events_for_item(q.id)]
    out["notifications"] = [(store.get_notification(n).action_taken if store.get_notification(n) else None) for n in (n1.id, n2.id)]
    out["validate"] = [
        validate_notification({"kind": "reminder", "ref": "r1", "title": "t", "to": 1}),
        validate_notification({"kind": "reminder", "ref": "r1", "title": "t", "actions": [{"id": "accept", "label": "A"}]}),
        validate_notification({"kind": "brief", "ref": "b1", "title": "t", "body": "b"}),
    ]
    out["config"] = sorted(load_config({"TELEGRAM_BOT_TOKEN": "x", "MAILBOX_OWNER_ID": "1"}).items())
    store.close()
    return out


def test_asks_path_is_unchanged(tmp_path):
    got = scenario(tmp_path)
    if os.environ.get("MAILBOX_GOLDEN_WRITE") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))
