"""Write choice-vectors.json and limits-vectors.json: request and response pairs for
Pond 1.4.0's choice objects {label, text} and its 8000-character messages and problem
reports, recorded from this Pond over loopback.

    python3 -m mailbox.tests.gen_choice_vectors                      # from the repo root
    python3 -m mailbox.tests.gen_choice_vectors --hook <fixtures dir>  # also into Hook's fixtures

Each file is one scenario: its cases run in order against one fresh Pond (the
Telegram fake, a pinned clock, an approved owner key), so a later case sees what
an earlier one did. A case is

    {name, note, request: {method, path, ownerKey, protocol, body}, response: {status, body}, binds?}

`ownerKey` true sends an approved X-Hook-Owner-Key; `protocol` is the
X-Hook-Protocol sent, or null for none. `binds` names a string the response carries
({"ask1": "item.id", "hash1": "revision.hash"}), and later paths and bodies use it
as {ask1} or {hash1}. In responses every bound string is its {name} and every
ISO-8601 timestamp is {time}; everything else is exactly what the Pond answers.
test_server holds both files to what the generator writes today.
"""
import argparse
from pathlib import Path
from typing import Any

from mailbox.tests.gen_pond_vectors import dump, replay

HERE = Path(__file__).parent
CHOICE_OUT = HERE / "choice-vectors.json"
LIMITS_OUT = HERE / "limits-vectors.json"
OUTS = (("choice", CHOICE_OUT), ("limits", LIMITS_OUT))

MIX = {"label": "Mix", "text": "What should change?"}
CHOICE_ASK = {"v": 1, "title": "Roll out the new schedule", "urgency": "today",
              "steps": [{"do": "Read the schedule"},
                        {"do": "Pick how it rolls out",
                         "question": {"prompt": "How should it roll out?",
                                      "choices": ["All at once", MIX, "Staff first"]}}],
              "reply": {"prompt": "Anything else before it goes out?",
                        "choices": ["No", {"label": "Yes", "text": "What else?"}]}}
PLAIN_ASK = {"v": 1, "title": "Rotate the deploy key", "urgency": "today",
             "steps": [{"do": "Open the deploy settings"}, {"do": "Rotate the key", "code": "deploy-key rotate"}]}
TRACEBACK = "\n".join(
    ["Traceback (most recent call last):"]
    + [f'  File "/srv/app/module_{n}.py", line {10 * n}, in handler_{n}\n    result = step_{n}(payload)'
       for n in range(1, 7)]
    + ["PermissionError: [Errno 13] Permission denied: '/var/lib/app/app.db'"])
TRACEBACK = TRACEBACK + " " * (937 - len(TRACEBACK))          # the spec's 937-character, 14-line paste


def _req(method: str, path: str, body: Any = None, *, owner_key: bool = False, protocol: Any = 6) -> dict:
    return {"method": method, "path": path, "ownerKey": owner_key, "protocol": protocol, "body": body}


def _post(instructable: dict) -> dict:
    return _req("POST", "/asks", {"project": "Example", "context": "personal", "instructable": instructable})


def _with_choices(choices: list, *, multi: Any = None) -> dict:
    question: dict = {"prompt": "How should it roll out?", "choices": choices}
    if multi is not None:
        question["multi"] = multi
    return {**CHOICE_ASK, "title": "A refused ask", "steps": [{"do": "Pick", "question": question}]}


INVALID = [
    ("choice-invalid-number", "a choice is a string or {label, text}", ["All at once", 5]),
    ("choice-invalid-unknown-key", "only label and text", ["All at once", {**MIX, "hint": "x"}]),
    ("choice-invalid-no-label", "label is required", ["All at once", {"text": "What should change?"}]),
    ("choice-invalid-long-label", "label: 1 to 60 characters", ["All at once", {**MIX, "label": "M" * 61}]),
    ("choice-invalid-no-text", "text is required: a choice without words is a plain string",
     ["All at once", {"label": "Mix"}]),
    ("choice-invalid-long-text", "text: 1 to 100 characters", ["All at once", {**MIX, "text": "w" * 101}]),
    ("choice-invalid-text-lines", "text is one line", ["All at once", {**MIX, "text": "two\nlines"}]),
    ("choice-invalid-duplicate", "labels are distinct across strings and objects", ["Mix", MIX]),
    ("choice-invalid-secret", "a secret-like text: field paths only, never echoed",
     ["All at once", {**MIX, "text": "use ghp_example"}]),
]

CHOICE_CASES: list[dict] = [
    {"name": "choice-post", "note": "a question and the reply with choice objects: 201",
     "request": _post(CHOICE_ASK), "binds": {"ask1": "item.id", "hash1": "revision.hash"}},
    {"name": "choice-detail-protocol-6", "note": "Hook 0.14 (protocol 6) reads the objects and the revision",
     "request": _req("GET", "/asks/{ask1}")},
    {"name": "choice-detail-protocol-5", "note": "Hook 0.13 (protocol 5): labels only, no revision or revisions",
     "request": _req("GET", "/asks/{ask1}", protocol=5)},
    {"name": "choice-detail-old-client", "note": "no X-Hook-Protocol: labels only too",
     "request": _req("GET", "/asks/{ask1}", protocol=None)},
    {"name": "choice-list-protocol-5", "note": "GET /asks for protocol 5: labels only",
     "request": _req("GET", "/asks", protocol=5)},
    *[{"name": name, "note": note, "request": _post(_with_choices(choices))} for name, note, choices in INVALID],
    {"name": "choice-invalid-multi", "note": "a choice with text can't be in a multi question",
     "request": _post(_with_choices(["All at once", MIX], multi=True))},
    {"name": "choice-invalid-reply", "note": "the reply's choices follow the same rules",
     "request": _post({**CHOICE_ASK, "title": "A refused reply", "reply": {
         "prompt": "Anything else?", "choices": ["No", {"label": "Yes"}]}})},
    {"name": "choice-answer-words", "note": "a pick of a word choice with the owner's words: picked plus text",
     "request": _req("POST", "/asks/{ask1}/answers",
                     {"answers": [{"step": "2", "picked": ["Mix"], "text": "Staff on Monday, the rest on Wednesday"}]},
                     owner_key=True)},
    {"name": "choice-answer-words-on-plain", "note": "words on a choice that takes none",
     "request": _req("POST", "/asks/{ask1}/answers",
                     {"answers": [{"step": "2", "picked": ["Staff first"], "text": "Monday"}]}, owner_key=True)},
    {"name": "choice-answer-words-too-long", "note": "the words: 8,000 characters at most (Pond 1.15.0; 1,000 before)",
     "request": _req("POST", "/asks/{ask1}/answers",
                     {"answers": [{"step": "2", "picked": ["Mix"], "text": "w" * 8001}]}, owner_key=True)},
    {"name": "choice-answer-no-words", "note": "a word choice picked with no words: picked only",
     "request": _req("POST", "/asks/{ask1}/answers", {"answers": [{"step": "2", "picked": ["Mix"]}]},
                     owner_key=True)},
    {"name": "choice-reply-words", "note": "a reply choice with words: the text is \"choice: <label> · <words>\"",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "choice: Yes · Tell the night shift first"},
                     owner_key=True)},
    {"name": "choice-reply-plain", "note": "a reply choice without words: \"choice: <label>\"",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "choice: No"}, owner_key=True)},
    {"name": "choice-events", "note": "the answer and reply events as a session reads them",
     "request": _req("GET", "/events?after=0&limit=100")},
    {"name": "choice-protocol-5-answer", "note": "Hook 0.13 answers by label: 200, labels only in the item",
     "request": _req("POST", "/asks/{ask1}/answers", {"answers": [{"step": "2", "picked": ["Staff first"]}]},
                     owner_key=True, protocol=5)},
    {"name": "choice-plain-post", "note": "an ask of plain strings beside it",
     "request": _post(PLAIN_ASK), "binds": {"ask2": "item.id"}},
    {"name": "choice-plain-protocol-5", "note": "unchanged for protocol 5: the revision is there",
     "request": _req("GET", "/asks/{ask2}", protocol=5)},
]

LIMITS_CASES: list[dict] = [
    {"name": "limits-ask", "note": "the ask the messages and reports go to",
     "request": _post(PLAIN_ASK), "binds": {"ask1": "item.id", "hash1": "revision.hash"}},
    {"name": "limits-message-8000", "note": "a message of 8,000 characters: 200",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "m" * 8000, "kind": "message"}, owner_key=True)},
    {"name": "limits-message-8001", "note": "8,001 characters: 400 text: too long",
     "request": _req("POST", "/asks/{ask1}/reply", {"text": "m" * 8001, "kind": "message"}, owner_key=True)},
    {"name": "limits-problem-multiline", "note": "a pasted traceback (937 characters, 14 lines), kept verbatim",
     "request": _req("POST", "/asks/{ask1}/problems", {"step": "2", "rev": 1, "hash": "{hash1}", "note": TRACEBACK},
                     owner_key=True)},
    {"name": "limits-problem-8000", "note": "a problem report of 8,000 characters: 200",
     "request": _req("POST", "/asks/{ask1}/problems", {"step": "2", "rev": 1, "hash": "{hash1}", "note": "n" * 8000},
                     owner_key=True)},
    {"name": "limits-problem-8001", "note": "8,001 characters: 400 note: too long",
     "request": _req("POST", "/asks/{ask1}/problems", {"step": "2", "rev": 1, "hash": "{hash1}", "note": "n" * 8001},
                     owner_key=True)},
    {"name": "limits-problem-hidden", "note": "a control character is still refused",
     "request": _req("POST", "/asks/{ask1}/problems",
                     {"step": "2", "rev": 1, "hash": "{hash1}", "note": "bell\u0007here"}, owner_key=True)},
    {"name": "limits-detail", "note": "the message and the reports, whole, in the detail's events",
     "request": _req("GET", "/asks/{ask1}")},
]

PONDS = {"choice": "1.4.0", "limits": "1.4.0"}


def build(kind: str) -> dict:
    cases = {"choice": CHOICE_CASES, "limits": LIMITS_CASES}[kind]
    return {"version": 1, "pond": PONDS[kind],
            "source": "mailbox/tests/gen_choice_vectors.py, recorded from the Pond over loopback",
            "format": __doc__.split("A case is", 1)[1].split("test_server holds", 1)[0].strip(),
            "cases": replay(cases, kind)}


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
