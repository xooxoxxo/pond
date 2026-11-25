"""Write brief-vectors.json: the brief-* cases of the shared validator vectors (Pond
1.14.0), computed by this mailbox's validator (the reference).

    python3 -m mailbox.tests.gen_brief_vectors                    # from the repo root

`brief` is a top-level key: the background a session would have told the owner in chat
(the why, the context, the links it used), Markdown, multi-line, at most 8000
characters, under the prose hidden-character rule and the secret scan. Every value here
is synthetic: the file is copied as it is into other repositories.

Beside the validator cases (kind "instructable"), the file holds the revision cases a
brief touches, in the shapes of revision-vectors.json: "canonical" {input, body},
"hash" {rev, prev, at, reason, body, hash} and "amend" {current, new, usedIds, rev,
reason, output, summary, errors}. A changed brief is named in the summary's `top`, never
in a step's `changed` fields.
"""
import copy
import json
from pathlib import Path
from typing import Any, Callable

from mailbox.instructable import BRIEF_LIMIT, _is_hidden, validate
from mailbox.revisions import assign_ids, canonical, revision_hash, step_ids
from mailbox.tests.gen_revision_vectors import AT1, AT2, _amend_case

OUT = Path(__file__).parent / "brief-vectors.json"
PREFIXES = ("brief-",)

TITLE = "Create a restricted payments key"
BRIEF = (
    "## Why\n"
    "The import job needs read access to charges, nothing else.\n\n"
    "## Context\n"
    "- The dashboard calls them *restricted keys*.\n"
    "- Leave every other permission at **None**.\n\n"
    "## Links I used\n"
    "- [Restricted keys](https://payments.example.test/docs/keys#restricted)\n"
)
STEPS = [
    {"do": "Open the API keys page", "url": "https://payments.example.test/keys"},
    {"do": "Create a restricted key named import-reader"},
    {"do": "Store it in the Keychain", "code": "security add-generic-password -s import-reader -a me -w",
     "lang": "sh"},
]


def _ask(**keys) -> dict:
    return {"v": 1, "title": TITLE, "steps": copy.deepcopy(STEPS), **keys}


INPUTS: list[tuple[str, dict]] = [
    # valid
    ("brief-valid", _ask(brief=BRIEF)),
    ("brief-valid-with-why", _ask(why="The import job is blocked.", brief=BRIEF)),
    ("brief-valid-one-line", _ask(brief="The import job reads charges only.")),
    ("brief-valid-8000", _ask(brief="b" * BRIEF_LIMIT)),
    ("brief-valid-null", _ask(brief=None)),
    ("brief-valid-empty", _ask(brief="")),
    ("brief-valid-tabs-and-lines", _ask(brief="Context\n\tindented\n\nend")),
    ("brief-valid-non-ascii", _ask(brief="Açıklama: yalnızca okuma izni.")),
    ("brief-valid-emoji-zwj", _ask(brief="Done by the team \U0001F468\u200d\U0001F469\u200d\U0001F467")),
    # the value
    ("brief-8001", _ask(brief="b" * (BRIEF_LIMIT + 1))),
    ("brief-number", _ask(brief=7)),
    ("brief-list", _ask(brief=["Why", "Context"])),
    ("brief-object", _ask(brief={"why": "Context"})),
    ("brief-bool", _ask(brief=True)),
    ("brief-carriage-return", _ask(brief="Context\r\nmore")),
    ("brief-bidi", _ask(brief="Context \u202e reversed")),
    ("brief-nul", _ask(brief="Context\x00")),
    # secrets
    ("brief-secret", _ask(brief="Use the old key ghp_example-not-a-token if it fails.")),
    ("brief-secret-anthropic", _ask(brief="The key starts sk-ant-example, keep it.")),
    ("brief-secret-split", _ask(brief="sk-\u200dant-example")),
    ("brief-secret-private-key", _ask(brief="-----BEGIN RSA PRIVATE KEY-----\nexample\n")),
    # beside other problems, in the reference order
    ("brief-and-title", {"v": 1, "title": "", "steps": copy.deepcopy(STEPS), "brief": "b" * (BRIEF_LIMIT + 1)}),
    ("brief-on-a-step", {"v": 1, "title": TITLE, "steps": [{"do": "Open the page", "brief": BRIEF}]}),
]


# --- the revision cases: the brief is part of the body and its hash, never of a step ---

WITH: dict = {"v": 1, "title": TITLE, "urgency": "today", "steps": copy.deepcopy(STEPS), "brief": BRIEF}
WITHOUT: dict = {"v": 1, "title": TITLE, "urgency": "today", "steps": copy.deepcopy(STEPS)}


def _ids(obj: dict) -> dict:
    out, errors = assign_ids(obj)
    assert errors == [], errors
    return out


def _change(fn: Callable[[dict], Any], base: dict) -> dict:
    out = copy.deepcopy(base)
    fn(out)
    return out


REV1 = _ids(WITH)
PLAIN1 = _ids(WITHOUT)
BODY1 = canonical(REV1)
LONGER = BRIEF + "- The key expires in 90 days.\n"
CANONICAL: list[tuple[str, Any]] = [
    ("brief-canonical", REV1),
    ("brief-canonical-escapes", {"brief": "line one\nline \"two\"\ttab \\ slash"}),
]
HASHES: list[tuple[str, int, str, str, str, str]] = [
    ("brief-hash-body", 1, "", AT1, "", BODY1),
    ("brief-hash-brief-changed", 2, revision_hash(1, "", AT1, "", BODY1), AT2, "Say when it expires",
     canonical(_change(lambda d: d.update(brief=LONGER), REV1))),
]
AMEND: list[tuple[str, dict, dict, list[str], int, Any]] = [
    ("brief-amend-changed", REV1, _change(lambda d: d.update(brief=LONGER), REV1), step_ids(REV1), 2,
     "Say when it expires"),
    ("brief-amend-added", PLAIN1, _change(lambda d: d.update(brief=BRIEF), PLAIN1), step_ids(PLAIN1), 2,
     "Add the background"),
    ("brief-amend-removed", REV1, _change(lambda d: d.pop("brief"), REV1), step_ids(REV1), 2,
     "The background was wrong"),
    ("brief-amend-with-why-and-step", REV1,
     _change(lambda d: (d.update(why="The import job is blocked.", brief=LONGER),
                        d["steps"][1].update(do="Create a restricted key named import-read")), REV1),
     step_ids(REV1), 2, "Shorter key name"),
]


def _py_bytes(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def build() -> list[dict]:
    names = [name for name, _ in INPUTS]
    if not names or len(set(names)) != len(names):
        raise SystemExit("gen_brief_vectors: no cases, or a duplicate case name")
    cases = []
    for name, obj in INPUTS:
        assert name.startswith(PREFIXES), name
        clean, errors = validate(obj)
        cases.append({"name": name, "kind": "instructable", "input": obj, "valid": not errors,
                      "errors": errors, "cleaned": clean or None, "pyBytes": _py_bytes(obj)})
    for name, obj in CANONICAL:
        cases.append({"name": name, "kind": "canonical", "input": obj, "body": canonical(obj)})
    for name, rev, prev, at, reason, body in HASHES:
        cases.append({"name": name, "kind": "hash", "rev": rev, "prev": prev, "at": at, "reason": reason,
                      "body": body, "hash": revision_hash(rev, prev, at, reason, body)})
    for args in AMEND:
        cases.append(_amend_case(*args))
    names = [c["name"] for c in cases]
    if len(set(names)) != len(names) or not all(n.startswith(PREFIXES) for n in names):
        raise SystemExit("gen_brief_vectors: a duplicate case name, or one without the brief- prefix")
    return cases


def _dump(doc: dict) -> str:
    """Indented JSON, non-ASCII kept, every hidden character (and ZWJ or a variation
    selector) written as a \\u escape so a reader sees it."""
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    return "".join(f"\\u{ord(ch):04x}" if _is_hidden(ch) or ord(ch) in (0x200D, 0xFE0E, 0xFE0F) else ch
                   for ch in text) + "\n"


def main() -> None:
    cases = build()
    OUT.write_text(_dump({"source": "mailbox/tests/gen_brief_vectors.py (brief-* cases)",
                          "pond": "1.14.0", "cases": cases}), encoding="utf-8")


if __name__ == "__main__":
    main()
