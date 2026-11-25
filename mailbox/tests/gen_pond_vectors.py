"""Write bug-vectors.json, message-vectors.json and requirements-vectors.json: request
and response pairs for Pond 1.2.0's Hook bugs and messages and Pond 1.3.0's required
before Done, recorded from this Pond over loopback.

    python3 -m mailbox.tests.gen_pond_vectors                      # from the repo root
    python3 -m mailbox.tests.gen_pond_vectors --hook <fixtures dir>  # also into Hook's fixtures

Each file is one scenario: its cases run in order against one fresh Pond (the
Telegram fake, a pinned clock, an approved owner key), so a later case sees what
an earlier one did. A case is

    {name, note, request: {method, path, ownerKey, protocol, body}, response: {status, body}, binds?}

`ownerKey` true sends an approved X-Hook-Owner-Key; `protocol` is the
X-Hook-Protocol sent, or null for none (a 1.1.0 client or older). `binds` names
the item id the response carries ({"bug1": "item.id"}), and later paths and bodies
use it as {bug1}. In responses every item id is its {name} and every ISO-8601
timestamp is {time}; everything else is exactly what the Pond answers.
test_server holds every file to what the generator writes today.
"""
import argparse
import json
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).parent
BUG_OUT = HERE / "bug-vectors.json"
MESSAGE_OUT = HERE / "message-vectors.json"
REQUIREMENTS_OUT = HERE / "requirements-vectors.json"
OUTS = (("bug", BUG_OUT), ("message", MESSAGE_OUT), ("requirements", REQUIREMENTS_OUT))
# The Pond release each file's routes first shipped in.
PONDS = {"bug": "1.2.0", "message": "1.2.0", "requirements": "1.3.0"}

T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
SIG = "a3f9c2e17b04"
CODE = ("Crash signature: hook-crash-a3f9c2e17b04\n"
        "Hook 0.12.0, macOS 15.6, arm64\n"
        "Exception: EXC_BAD_ACCESS (SIGSEGV)\n"
        "0 Hook SettingsWindow.open\n"
        "1 Hook AppDelegate.showSettings")
BUG = {"v": 1, "title": f"Hook crash: EXC_BAD_ACCESS in SettingsWindow.open ({SIG})", "urgency": "whenever",
       "why": "Hook quit unexpectedly. The report holds only the allowed fields.",
       "steps": [{"do": "Read the crash report", "code": CODE, "lang": "text"}]}
ASK = {"v": 1, "title": "Rotate the deploy key", "urgency": "today",
       "steps": [{"do": "Open the deploy settings"}, {"do": "Rotate the key", "code": "deploy-key rotate"}]}


def _req(method: str, path: str, body: Any = None, *, owner_key: bool = False, protocol: Any = 5) -> dict:
    return {"method": method, "path": path, "ownerKey": owner_key, "protocol": protocol, "body": body}


def _bug_body(sig: Any = SIG, **over: Any) -> dict:
    return {"project": "Hook", "context": "personal", "kind": "bug", "signature": sig, "instructable": BUG, **over}


BUG_CASES: list[dict] = [
    {"name": "bug-new", "note": "a new signature files a bug: 201, never sent to Telegram",
     "request": _req("POST", "/asks", _bug_body(), owner_key=True), "binds": {"bug1": "item.id"}},
    {"name": "bug-repeat", "note": "the same signature while the bug is open: an occurrence, no new item",
     "request": _req("POST", "/asks", _bug_body(), owner_key=True)},
    {"name": "bug-plain-ask", "note": "a plain ask beside it, for the kind filter",
     "request": _req("POST", "/asks", {"project": "Example", "context": "personal", "instructable": ASK}),
     "binds": {"ask1": "item.id"}},
    {"name": "bug-list-kind-bug", "note": "GET /asks?kind=bug: bugs only",
     "request": _req("GET", "/asks?kind=bug")},
    {"name": "bug-list-kind-ask", "note": "GET /asks?kind=ask: everything but bugs",
     "request": _req("GET", "/asks?kind=ask")},
    {"name": "bug-detail-protocol-5", "note": "the detail a Hook 0.12 reads",
     "request": _req("GET", "/asks/{bug1}")},
    {"name": "bug-detail-old-client", "note": "a 1.1.0 client (no X-Hook-Protocol): an ordinary ask with extra keys",
     "request": _req("GET", "/asks/{bug1}", protocol=None)},
    {"name": "bug-no-owner-key", "note": "only Hook files bugs: the owner key always",
     "request": _req("POST", "/asks", _bug_body())},
    {"name": "bug-bad-signature", "note": "12 lowercase hex only",
     "request": _req("POST", "/asks", _bug_body("hook-crash-a3f9c2e17b04"), owner_key=True)},
    {"name": "bug-bad-kind", "note": "kind is ask or bug",
     "request": _req("POST", "/asks", _bug_body(kind="crash"), owner_key=True)},
    {"name": "bug-list-bad-kind", "note": "an unknown kind filter",
     "request": _req("GET", "/asks?kind=crash")},
    {"name": "bug-resolve", "note": "a session resolves it with a note; Telegram is not touched",
     "request": _req("POST", "/asks/{bug1}/resolve", {"verdict": "done", "note": "Fixed in 0.12.1"})},
    {"name": "bug-after-close", "note": "the signature of a closed bug files a new bug",
     "request": _req("POST", "/asks", _bug_body(), owner_key=True), "binds": {"bug2": "item.id"}},
]

MESSAGE_CASES: list[dict] = [
    {"name": "message-ask", "note": "the ask the messages go to",
     "request": _req("POST", "/asks", {"project": "Example", "context": "personal", "instructable": ASK}),
     "binds": {"ask1": "item.id"}},
    {"name": "message-send", "note": "a message: kept verbatim, multi-line, never an answer",
     "request": _req("POST", "/asks/{ask1}/reply",
                     {"text": "Use the staging key first.\nThen prod.", "kind": "message"}, owner_key=True)},
    {"name": "message-reply-plain", "note": "an older Hook's reply, no kind: unchanged",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "Go ahead"}, protocol=None)},
    {"name": "message-reply-kind-reply", "note": "kind reply is the same as no kind",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "Yes", "kind": "reply"})},
    {"name": "message-detail", "note": "the message reaches the detail's events",
     "request": _req("GET", "/asks/{ask1}")},
    {"name": "message-events", "note": "and the event stream",
     "request": _req("GET", "/events?after=0&limit=100")},
    {"name": "message-bad-kind", "note": "kind is reply or message",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "x", "kind": "note"})},
    {"name": "message-secret", "note": "a secret-like text: field paths only, never echoed",
     "request": _req("POST", "/asks/{ask1}/reply",
                     {"text": "the token is ghp_example", "kind": "message"})},
    {"name": "message-too-long", "note": "8,000 characters at most (Pond 1.4.0; 1,000 before)",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "m" * 8001, "kind": "message"})},
    {"name": "message-blank", "note": "no text",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "  ", "kind": "message"})},
    {"name": "message-resolve", "note": "the session closes the ask",
     "request": _req("POST", "/asks/{ask1}/resolve", {"verdict": "done"})},
    {"name": "message-closed", "note": "no message to a closed ask",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "One more thing", "kind": "message"})},
]

# Pond 1.3.0: step 2 is required, and the ask also waits for the reply and a message.
REQ_ASK = {"v": 1, "title": "Rotate the staging deploy key", "urgency": "today",
           "steps": [{"do": "Open the deploy settings"}, {"do": "Rotate the key", "required": True},
                     {"do": "Note the rotation time", "optional": True}],
           "reply": {"prompt": "Did the deploy pass?", "choices": ["Yes", "No"]},
           "done": ["reply", "message"]}


def _req_ask(title: str) -> dict:
    return {"project": "Example", "context": "personal", "instructable": {**REQ_ASK, "title": title}}


REQUIREMENTS_CASES: list[dict] = [
    {"name": "requirements-post", "note": "an ask with a required step and done [reply, message]: 201",
     "request": _req("POST", "/asks", _req_ask(REQ_ASK["title"])), "binds": {"ask1": "item.id"}},
    {"name": "requirements-detail-left", "note": "GET /asks/{id} carries requirements: everything left",
     "request": _req("GET", "/asks/{ask1}")},
    {"name": "requirements-list", "note": "GET /asks never carries requirements",
     "request": _req("GET", "/asks")},
    {"name": "requirements-progress-tick", "note": "the owner ticks step 2: one progress event, step 2 done",
     "request": _req("POST", "/asks/{ask1}/progress", {"tick": ["s2"], "untick": [], "rev": 1}, owner_key=True)},
    {"name": "requirements-progress-noop", "note": "the same tick again changes nothing: 200, no event",
     "request": _req("POST", "/asks/{ask1}/progress", {"tick": ["s2"], "rev": 1}, owner_key=True)},
    {"name": "requirements-progress-invalid", "note": "an unknown id, and an id in both lists: field paths only",
     "request": _req("POST", "/asks/{ask1}/progress", {"tick": ["s9", "s1"], "untick": ["s1"]}, owner_key=True)},
    {"name": "requirements-done-unmet", "note": "the owner's Done while the reply and a message are left: 409",
     "request": _req("POST", "/asks/{ask1}/resolve", {"verdict": "done", "actor": "owner", "rev": 1},
                     owner_key=True)},
    {"name": "requirements-reply", "note": "the reply prompt answered: the reply is done",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "Yes"}, owner_key=True)},
    {"name": "requirements-message", "note": "a message to the session: nothing left",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "Rotated, the old key is revoked.", "kind": "message"},
                     owner_key=True)},
    {"name": "requirements-detail-ready", "note": "ready; the progress event is in the detail, ids only",
     "request": _req("GET", "/asks/{ask1}")},
    {"name": "requirements-done-owner", "note": "the owner's Done once nothing is left: 200",
     "request": _req("POST", "/asks/{ask1}/resolve", {"verdict": "done", "actor": "owner", "rev": 1},
                     owner_key=True)},
    {"name": "requirements-progress-closed", "note": "no progress on a closed ask",
     "request": _req("POST", "/asks/{ask1}/progress", {"untick": ["s2"]}, owner_key=True)},
    {"name": "requirements-post-second", "note": "a second ask like it",
     "request": _req("POST", "/asks", _req_ask("Rotate the preview deploy key")), "binds": {"ask2": "item.id"}},
    {"name": "requirements-done-session", "note": "the session's own Done is never gated: 200 with all left",
     "request": _req("POST", "/asks/{ask2}/resolve", {"verdict": "done", "actor": "session"})},
    {"name": "requirements-post-third", "note": "a third ask like it",
     "request": _req("POST", "/asks", _req_ask("Rotate the production deploy key")), "binds": {"ask3": "item.id"}},
    {"name": "requirements-skip-owner", "note": "the owner's Skip is never gated: 200",
     "request": _req("POST", "/asks/{ask3}/resolve", {"verdict": "declined", "actor": "owner"}, owner_key=True)},
]

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)")


def _fill(value: Any, ids: dict[str, str]) -> Any:
    """{name} in a request's strings → the bound id."""
    if isinstance(value, str):
        for name, item_id in ids.items():
            value = value.replace("{" + name + "}", item_id)
        return value
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def _normal(value: Any, names: dict[str, str]) -> Any:
    """A response with item ids as {name} and timestamps as {time}."""
    if isinstance(value, str):
        if value in names:
            return "{" + names[value] + "}"
        if _ISO.fullmatch(value):
            return "{time}"
        return value
    if isinstance(value, list):
        return [_normal(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _normal(v, names) for k, v in value.items()}
    return value


def _at(body: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        body = body[part]
    return body


def _send(base_url: str, token: str, key: str, request: dict) -> tuple[int, Any]:
    from mailbox import owner
    headers = {"Authorization": f"Bearer {token}"}
    if request["ownerKey"]:
        headers[owner.KEY_HEADER] = key
    if request["protocol"] is not None:
        headers["X-Hook-Protocol"] = str(request["protocol"])
    data = None
    if request["body"] is not None:
        data = json.dumps(request["body"], ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + request["path"], data=data, headers=headers, method=request["method"])
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class _AtT0(datetime):
    """datetime whose now() is T0, for the store while a replay runs."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return T0.astimezone(tz) if tz is not None else T0.replace(tzinfo=None)


def replay(cases: list[dict], kind: str = "bug") -> list[dict]:
    """Run the cases in order against a fresh Pond → the cases with their responses."""
    from mailbox.tests.test_server import _approved_key, _running
    out: list[dict] = []
    # The service clock alone is not enough: the store reads datetime.now itself
    # (the 14-day cutoff of GET /asks), so pin that to T0 too, or the file
    # depends on today's date.
    with tempfile.TemporaryDirectory() as tmp, mock.patch("mailbox.store.datetime", _AtT0):
        running = _running(Path(tmp))
        srv, base_url, token = next(running)
        try:
            srv.service.utcnow = lambda: T0
            key = _approved_key(srv, label="Example Mac")
            ids: dict[str, str] = {}
            for case in cases:
                request = case["request"]
                status, body = _send(base_url, token, key, _fill(request, ids))
                for name, dotted in (case.get("binds") or {}).items():
                    ids[name] = _at(body, dotted)
                names = {item_id: name for name, item_id in ids.items()}
                recorded = {"name": case["name"], "note": case["note"], "request": request,
                            "response": {"status": status, "body": _normal(body, names)}}
                if case.get("binds"):
                    recorded["binds"] = case["binds"]
                out.append(recorded)
            if kind in ("bug", "message") and (
                    not srv.telegram.sent_messages[1:]
                    or any("Hook crash" in (m["text"] or "") for m in srv.telegram.sent_messages)):
                raise AssertionError("a bug reached Telegram, or the plain asks never did")
        finally:
            next(running, None)
    return out


def build(kind: str) -> dict:
    cases = {"bug": BUG_CASES, "message": MESSAGE_CASES, "requirements": REQUIREMENTS_CASES}[kind]
    return {"version": 1, "pond": PONDS[kind],
            "source": "mailbox/tests/gen_pond_vectors.py, recorded from the Pond over loopback",
            "format": __doc__.split("A case is", 1)[1].split("test_server holds", 1)[0].strip(),
            "cases": replay(cases, kind)}


def dump(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", type=Path, help="Hook's Tests/RelayCoreTests/Fixtures folder to write too")
    a = parser.parse_args()
    for kind, path in OUTS:
        text = dump(build(kind))
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
        if a.hook:
            (a.hook / path.name).write_text(text, encoding="utf-8")
            print(f"wrote {a.hook / path.name}")


if __name__ == "__main__":
    main()
