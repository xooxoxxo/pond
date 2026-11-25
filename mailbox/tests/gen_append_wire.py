"""Write append-wire-vectors.json: the wire transcript of steps added to an open ask
(Pond 1.9.0), recorded from this Pond over loopback.

    python3 mailbox/tests/gen_append_wire.py                            # check: exit 1 on a difference
    HOOK_CONFORMANCE_RECORD=1 python3 mailbox/tests/gen_append_wire.py  # record the file again

The cases run in order against one fresh Pond: the Telegram fake, the pinned clock
and the counting ids of gen_phone_vectors, so two runs give the same bytes. A case is

    {name, note, request: {method, path, ownerKey, protocol, body}, response: {status, body}, binds?}

`ownerKey` true sends an approved X-Hook-Owner-Key; `protocol` is the X-Hook-Protocol
sent. `binds` names the item id the response carries ({"ask1": "item.id"}), and later
paths use it as {ask1}. In responses every item id is its {name} and every ISO-8601
timestamp is {time}, except a revision's `at`, kept as recorded because its hash
covers it. `rev`, `hash`, `baseRev` and `baseHash` in a request body are the real
values the Pond accepts; the edit key is a synthetic one. No file holds the Pond token
or an owner key. Hook keeps a copy as conformance/wire/append-wire-vectors.json.
test_append_wire holds the file to what this generator records today.
"""
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):                       # run as a script: the repo root first,
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # before the stdlib `mailbox`

from mailbox import VERSION, owner                  # noqa: E402
from mailbox.tests import gen_phone_vectors as phone  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "append-wire-vectors.json"
SOURCE = "mailbox/tests/gen_append_wire.py, recorded from the Pond over loopback"
RECORD_ENV = "HOOK_CONFORMANCE_RECORD"
EDIT_KEY = phone.EDIT_KEY                            # synthetic, like every key here
APPEND_GAP_S = 16                                    # past the 15 s an append waits after a revision

ASK = {
    "v": 1, "title": "Clean up the staging deploys", "urgency": "today",
    "steps": [
        {"do": "Pick the console", "question": {"prompt": "Which console?", "choices": ["Web", "CLI"]}},
        {"do": "Pick what goes", "question": {"prompt": "What should go?", "choices": ["Logs", "Caches"],
                                             "then": "steps"}},
    ],
}
MORE = [
    {"do": "Delete the old logs", "code": "deploy-logs prune --env staging --older-than 30d"},
    {"do": "Check the disk", "question": {"prompt": "Enough space now?", "choices": ["Yes", "No"]}},
]
NOTE = "Cleanup steps for the logs"


def _format() -> str:
    return __doc__.split("A case is", 1)[1].split("test_append_wire holds", 1)[0].strip()


class _Recorder:
    """Sends each case's request to the Pond and records it with its response."""

    def __init__(self, pond: Any) -> None:
        self.pond = pond
        self.ids: dict[str, str] = {}
        self.cases: list[dict] = []

    def case(self, name: str, note: str, method: str, path: str, body: Any = None, *, owner_key: bool = False,
             expect: int = 200, error: Optional[str] = None, binds: Optional[dict] = None) -> Any:
        request = {"method": method, "path": path, "ownerKey": owner_key, "protocol": 6, "body": body}
        sent = phone._fill(request, self.ids)
        status, answer = self.pond.send(sent["method"], sent["path"], sent["body"],
                                        key=phone.PHONE_KEY if owner_key else None, protocol=6)
        got = answer.get("error") if isinstance(answer, dict) else None
        if status != expect or got != error:
            raise SystemExit(f"gen_append_wire: {name} answered {status} {answer}")
        for bound, dotted in (binds or {}).items():
            self.ids[bound] = phone._at(answer, dotted)
        names = {item_id: bound for bound, item_id in self.ids.items()}
        recorded: dict[str, Any] = {"name": name, "note": note, "request": request,
                                    "response": {"status": status, "body": phone._normal(answer, names)}}
        if binds:
            recorded["binds"] = binds
        self.cases.append(recorded)
        return answer


def _cases() -> list[dict]:
    with phone._pond() as pond:
        pond.approve(phone.PHONE_KEY, "Example Mac")
        t = _Recorder(pond)
        pond.clock.advance(1)
        posted = t.case("append-post", "an editable ask whose step 2 question says more steps follow its answer",
                        "POST", "/asks", {"project": "Example", "context": "personal", "instructable": ASK,
                                          "editKeyHash": owner.key_hash(EDIT_KEY)},
                        expect=201, binds={"ask1": "item.id"})
        rev1 = posted["revision"]
        stamp1 = {"rev": rev1["rev"], "hash": rev1["hash"]}
        pond.clock.advance(5)
        t.case("append-answer", "the owner answers the step 2 question: Done now waits for the next steps",
               "POST", "/asks/{ask1}/answers", {"answers": [{"step": "2", "picked": ["Logs"]}], **stamp1},
               owner_key=True)
        pond.clock.advance(5)
        t.case("append-done-waiting", "Done while the wait is on: 409 waiting_for_steps, with when it ends",
               "POST", "/asks/{ask1}/resolve", {"verdict": "done", "actor": "owner", **stamp1}, owner_key=True,
               expect=409, error="waiting_for_steps")
        t.case("append-detail-waiting", "the detail while the wait is on carries stepWait",
               "GET", "/asks/{ask1}")
        pond.clock.advance(APPEND_GAP_S)
        appended = t.case("append-steps", "the session adds two steps: rev 2, kind append, accepted with it",
                          "POST", "/asks/{ask1}/append", {"editKey": EDIT_KEY, "baseRev": rev1["rev"],
                                                          "baseHash": rev1["hash"], "note": NOTE, "steps": MORE})
        rev2 = appended["revision"]
        t.case("append-detail", "rev 2 with kind append and acceptedRev 2; no stepWait now", "GET", "/asks/{ask1}")
        pond.clock.advance(5)
        t.case("append-done-old-rev", "Done bound to rev 1: 409 steps_added with the current rev",
               "POST", "/asks/{ask1}/resolve", {"verdict": "done", "actor": "owner", **stamp1}, owner_key=True,
               expect=409, error="steps_added")
        t.case("append-answer-old-rev", "an answer bound to rev 1 on step 1, unchanged in rev 2: 200, counted at rev 2",
               "POST", "/asks/{ask1}/answers", {"answers": [{"step": "1", "picked": ["CLI"]}], **stamp1},
               owner_key=True)
        t.case("append-done", "Done bound to rev 2: 200, the ask is done",
               "POST", "/asks/{ask1}/resolve",
               {"verdict": "done", "actor": "owner", "rev": rev2["rev"], "hash": rev2["hash"]}, owner_key=True)
        return t.cases


def build() -> dict:
    return {"version": 1, "pond": VERSION, "source": SOURCE, "format": _format(), "cases": _cases()}


def dump(doc: dict) -> str:
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if any(token in text for token in phone.TOKENS) or any(secret in text for secret in phone.SECRETS):
        raise SystemExit("gen_append_wire: the transcript holds a token or an owner key")
    return text


def main() -> int:
    text = dump(build())
    if os.environ.get(RECORD_ENV) == "1":
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT}")
        return 0
    if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
        print(f"gen_append_wire: {OUT.name} differs from what the Pond answers today; "
              f"record it with {RECORD_ENV}=1", file=sys.stderr)
        return 1
    print(f"{OUT.name}: matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
