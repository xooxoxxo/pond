"""Write required-vectors.json: the required-* and done-* cases of the shared
validator vectors (Pond 1.3.0), computed by this mailbox's validator (the reference).

    python3 -m mailbox.tests.gen_required_vectors                   # from the repo root
    python3 -m mailbox.tests.gen_required_vectors --hook <fixture>  # also into Hook's fixture

With --hook, the same cases replace every required-* and done-* case in Hook's
Tests/RelayCoreTests/Fixtures/instructable-vectors.json (at the place the first
one stood, or at the end), leaving every other case as it was, so the Swift
validator is held to the same answers.
"""
import argparse
import json
from pathlib import Path

from mailbox.instructable import validate

OUT = Path(__file__).parent / "required-vectors.json"
PREFIXES = ("required-", "done-")

TITLE = "Rotate the staging deploy key"
OPEN = {"do": "Open the deploy settings"}
ROTATE = "Rotate the key"
SHIP = {"do": "Redeploy staging"}
REPLY = {"prompt": "Did the deploy pass?", "choices": ["Yes", "No"]}
FREE_REPLY = {"prompt": "What did the deploy print?", "choices": []}
QUESTION = {"prompt": "Which environment?", "choices": ["Staging", "Preview"]}
LONG_WARNING = "w" * 301


def _ask(step: dict | None = None, *, steps: list | None = None, verify: dict | None = None,
         reply: dict | None = None, done=None) -> dict:
    obj = {"v": 1, "title": TITLE,
           "steps": steps if steps is not None else [step or {"do": ROTATE}, SHIP]}
    if verify is not None:
        obj["verify"] = verify
    if reply is not None:
        obj["reply"] = reply
    if done is not None:
        obj["done"] = done
    return obj


def _sub(*subs: dict, **parent) -> dict:
    """A first step "Open the deploy settings" (with the `parent` keys) holding these sub-steps."""
    return _ask(steps=[{**OPEN, **parent, "substeps": list(subs)}, SHIP])


def _done(done, **keys) -> dict:
    return _ask(done=done, **keys)


INPUTS: list[tuple[str, dict]] = [
    # `required` on a step or sub-step
    ("required-valid-leaf", _ask({"do": ROTATE, "required": True})),
    ("required-valid-false", _ask({"do": ROTATE, "required": False})),
    ("required-valid-null", _ask({"do": ROTATE, "required": None})),
    ("required-valid-parent", _sub({"do": ROTATE}, required=True)),
    ("required-valid-substep", _sub({"do": ROTATE, "required": True})),
    ("required-valid-question", _ask({"do": ROTATE, "required": True, "question": QUESTION})),
    ("required-valid-with-blocking", _ask({"do": ROTATE, "required": True, "blocking": True})),
    ("required-valid-false-with-optional", _ask({"do": ROTATE, "required": False, "optional": True})),
    ("required-valid-parent-some-optional", _sub({"do": ROTATE}, {"do": "Copy it", "optional": True},
                                                 required=True)),
    ("required-valid-parent-all-optional-own-question", _sub({"do": ROTATE, "optional": True},
                                                             required=True, question=QUESTION)),
    ("required-valid-substep-of-required-parent", _sub({"do": ROTATE, "required": True}, required=True)),
    ("required-and-optional", _ask({"do": ROTATE, "required": True, "optional": True})),
    ("required-and-optional-substep", _sub({"do": ROTATE, "required": True, "optional": True})),
    ("required-inside-optional-parent", _sub({"do": ROTATE, "required": True}, optional=True)),
    ("required-inside-optional-parent-second", _sub({"do": ROTATE}, {"do": "Copy it", "required": True},
                                                    optional=True)),
    ("required-on-verify", _ask(verify={"do": "Check the deploy", "required": True})),
    ("required-not-bool-string", _ask({"do": ROTATE, "required": "true"})),
    ("required-not-bool-number", _ask({"do": ROTATE, "required": 1})),
    ("required-not-bool-list", _ask({"do": ROTATE, "required": []})),
    ("required-substep-not-bool", _sub({"do": ROTATE, "required": "yes"})),
    ("required-parent-all-optional", _sub({"do": ROTATE, "optional": True},
                                          {"do": "Copy it", "optional": True}, required=True)),
    # the reference order: blocking, optional, required, then warning
    ("required-not-bool-after-optional", _ask({"do": ROTATE, "optional": 1, "required": "x",
                                               "warning": LONG_WARNING})),
    ("required-and-optional-blocking", _ask({"do": ROTATE, "required": True, "optional": True,
                                             "blocking": True})),
    # the ask's `done` list
    ("done-valid-steps", _done(["steps"])),
    ("done-valid-reply", _done(["reply"], reply=REPLY)),
    ("done-valid-reply-free-text", _done(["reply"], reply=FREE_REPLY)),
    ("done-valid-message", _done(["message"])),
    ("done-valid-all", _done(["message", "reply", "steps"], reply=REPLY)),
    ("done-valid-null", {**_ask(), "done": None}),
    ("done-valid-steps-with-required", _done(["steps"], step={"do": ROTATE, "required": True})),
    ("done-valid-steps-some-optional", _done(["steps"], steps=[{"do": ROTATE, "optional": True}, SHIP])),
    ("done-reply-without-prompt", _done(["reply"])),
    ("done-empty", _done([])),
    ("done-string", _done("steps")),
    ("done-object", _done({"steps": True})),
    ("done-unknown", _done(["ticks"])),
    ("done-not-string", _done(["steps", 3])),
    ("done-duplicate", _done(["steps", "steps"])),
    ("done-four", _done(["steps", "reply", "message", "steps"], reply=REPLY)),
    ("done-steps-all-optional", _done(["steps"], steps=[{"do": ROTATE, "optional": True},
                                                        {**SHIP, "optional": True}])),
    ("done-unknown-before-bad-reply", _done(["ticks", "reply"])),
]


def _py_bytes(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def build() -> list[dict]:
    names = [name for name, _ in INPUTS]
    if not names or len(set(names)) != len(names):
        raise SystemExit("gen_required_vectors: no cases, or a duplicate case name")
    cases = []
    for name, obj in INPUTS:
        assert name.startswith(PREFIXES), name
        clean, errors = validate(obj)
        cases.append({"name": name, "kind": "instructable", "input": obj, "valid": not errors,
                      "errors": errors, "cleaned": clean or None, "pyBytes": _py_bytes(obj)})
    return cases


def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def _ours(case: dict) -> bool:
    return str(case.get("name", "")).startswith(PREFIXES)


def write_hook(fixture: Path, cases: list[dict]) -> None:
    """Replace the required-* and done-* cases in Hook's fixture with `cases`, at the
    place the first of them stood (or at the end); every other case stays as it was."""
    if not cases:
        raise SystemExit("gen_required_vectors: no cases to write")
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    old = doc.get("cases")
    if not isinstance(old, list) or not old:
        raise SystemExit(f"gen_required_vectors: {fixture} holds no cases")
    first = next((i for i, c in enumerate(old) if _ours(c)), len(old))
    kept_before = [c for c in old[:first] if not _ours(c)]
    kept_after = [c for c in old[first:] if not _ours(c)]
    doc["cases"] = kept_before + cases + kept_after
    fixture.write_text(_dump(doc), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", type=Path, help="Hook's instructable-vectors.json to update too")
    args = parser.parse_args()
    cases = build()
    OUT.write_text(_dump({"source": "mailbox/tests/gen_required_vectors.py (required-* and done-* cases)",
                          "cases": cases}), encoding="utf-8")
    if args.hook:
        write_hook(args.hook, cases)


if __name__ == "__main__":
    main()
