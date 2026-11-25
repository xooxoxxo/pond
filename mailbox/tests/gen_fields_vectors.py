"""Write fields-vectors.json: the fields-* cases of the shared validator vectors (Pond
1.13.0), computed by this mailbox's validator (the reference).

    python3 -m mailbox.tests.gen_fields_vectors                   # from the repo root

`fields` is a settings table on a step or sub-step, never on verify: 1 to 20 rows
{"label", "value", "copy"?}. A label is 1 to 40 characters on one line and unique within
its step; a value is 1 to 500 characters on one line, held to the hidden-character rule
of `code`; `copy` is true or false. Every value here is synthetic: the file is copied as
it is into other repositories.

Beside the validator cases (kind "instructable"), the file holds the revision cases a
table touches, in the shapes of revision-vectors.json (which Hook copies into its Swift
fixtures, so they live here): "canonical" {input, body}, "hash" {rev, prev, at, reason,
body, hash} and "amend" {current, new, usedIds, rev, reason, output, summary, errors}.
"""
import json
from pathlib import Path

import copy
from typing import Any, Callable

from mailbox.instructable import FIELD_LABEL_LIMIT, FIELD_VALUE_LIMIT, MAX_FIELDS, _is_hidden, validate
from mailbox.revisions import assign_ids, canonical, revision_hash, step_ids
from mailbox.tests.gen_revision_vectors import AT1, AT2, _amend_case

OUT = Path(__file__).parent / "fields-vectors.json"
PREFIXES = ("fields-",)

TITLE = "Add the rate limit rule"
ADD = "Add the custom rule"
SAVE = {"do": "Save and deploy"}
RULE = [
    {"label": "Name", "value": "requests-ip", "copy": True},
    {"label": "Path", "value": "/api/v1/requests", "copy": True},
    {"label": "Method", "value": "POST"},
    {"label": "Rate limit", "value": "5 per 10 minutes per IP"},
    {"label": "Action", "value": "Deny (429)"},
]


def _ask(step: dict | None = None, *, steps: list | None = None, verify: dict | None = None) -> dict:
    obj = {"v": 1, "title": TITLE,
           "steps": steps if steps is not None else [step or {"do": ADD}, SAVE]}
    if verify is not None:
        obj["verify"] = verify
    return obj


def _rows(*rows) -> dict:
    return _ask({"do": ADD, "fields": list(rows)})


def _row(**keys) -> dict:
    return {"label": "Name", "value": "requests-ip", **keys}


INPUTS: list[tuple[str, dict]] = [
    # valid
    ("fields-valid-step", _ask({"do": ADD, "fields": RULE, "detail": "Same shape as the press-account rule."})),
    ("fields-valid-substep", _ask(steps=[{"do": "Set up the firewall", "substeps": [
        {"do": ADD, "fields": RULE[:3]}, {"do": "Check the rule list"}]}, SAVE])),
    ("fields-valid-one-row", _rows(_row())),
    ("fields-valid-twenty-rows", _rows(*[_row(label=f"Setting {n}") for n in range(1, MAX_FIELDS + 1)])),
    ("fields-valid-label-40", _rows(_row(label="L" * FIELD_LABEL_LIMIT))),
    ("fields-valid-value-500", _rows(_row(value="v" * FIELD_VALUE_LIMIT))),
    ("fields-valid-copy-false", _rows(_row(copy=False))),
    ("fields-valid-copy-null", _rows(_row(copy=None))),
    ("fields-valid-null", _ask({"do": ADD, "fields": None})),
    ("fields-valid-non-ascii", _rows(_row(label="Açıklama", value="Hız sınırı: 5 / 10 dk"))),
    ("fields-valid-same-label-other-step", _ask(steps=[{"do": ADD, "fields": [_row()]},
                                                       {"do": "Add the second rule", "fields": [_row(value="b")]}])),
    ("fields-valid-labels-differ-by-case", _rows(_row(), _row(label="name"))),
    ("fields-valid-with-code", _ask({"do": ADD, "fields": [_row()], "code": "fw rules list", "lang": "sh"})),
    # where fields may stand
    ("fields-on-verify", _ask(verify={"do": "Check the rule", "fields": [_row()]})),
    # the list
    ("fields-empty-list", _rows()),
    ("fields-too-many-rows", _rows(*[_row(label=f"Setting {n}") for n in range(1, MAX_FIELDS + 2)])),
    ("fields-not-a-list", _ask({"do": ADD, "fields": {"Name": "requests-ip"}})),
    ("fields-row-not-object", _rows(_row(), "Method: POST")),
    ("fields-row-null", _rows(_row(), None)),
    ("fields-row-extra-key", _rows(_row(note="the main one"))),
    # label
    ("fields-label-missing", _rows({"value": "requests-ip"})),
    ("fields-label-empty", _rows(_row(label=""))),
    ("fields-label-blank", _rows(_row(label="   "))),
    ("fields-label-41", _rows(_row(label="L" * (FIELD_LABEL_LIMIT + 1)))),
    ("fields-label-number", _rows(_row(label=7))),
    ("fields-label-two-lines", _rows(_row(label="Rate\nlimit"))),
    ("fields-label-hidden", _rows(_row(label="Na‮me"))),
    ("fields-label-duplicate", _rows(_row(), _row(value="other"), _row(label="Path"))),
    # value
    ("fields-value-missing", _rows({"label": "Name"})),
    ("fields-value-empty", _rows(_row(value=""))),
    ("fields-value-501", _rows(_row(value="v" * (FIELD_VALUE_LIMIT + 1)))),
    ("fields-value-number", _rows(_row(value=5))),
    ("fields-value-line-break", _rows(_row(value="requests\nip"))),
    ("fields-value-carriage-return", _rows(_row(value="requests\rip"))),
    ("fields-value-tab", _rows(_row(value="requests\tip"))),
    ("fields-value-zwj", _rows(_row(value="requests‍ip"))),
    ("fields-value-variation-selector", _rows(_row(value="requests️ip"))),
    ("fields-value-bidi", _rows(_row(value="requests‮ip"))),
    ("fields-value-secret", _rows(_row(label="Token", value="ghp_example-not-a-token"))),
    ("fields-value-secret-split", _rows(_row(label="Key", value="sk-‍ant-example"))),
    ("fields-label-secret", _rows(_row(label="xoxb-example"))),
    # copy
    ("fields-copy-string", _rows(_row(copy="yes"))),
    ("fields-copy-number", _rows(_row(copy=1))),
    # several problems in one row, in the reference order
    ("fields-row-every-problem", _rows({"label": "", "value": "a\nb", "copy": "no", "x": 1})),
    ("fields-substep-bad", _ask(steps=[{"do": "Set up the firewall", "substeps": [
        {"do": ADD, "fields": [_row(), _row()]}]}, SAVE])),
]


# --- the revision cases: rows are part of the body, its hash and a correction's summary ---

TABLE: dict = {
    "v": 1, "title": TITLE, "urgency": "today",
    "steps": [
        {"do": ADD, "detail": "Same shape as the press-account rule.", "fields": [RULE[0], RULE[2], RULE[3]]},
        SAVE,
    ],
}
PLAIN: dict = {"v": 1, "title": TITLE, "urgency": "today", "steps": [{"do": ADD}, SAVE]}


def _ids(obj: dict) -> dict:
    out, errors = assign_ids(obj)
    assert errors == [], errors
    return out


def _change(fn: Callable[[dict], Any], base: dict) -> dict:
    out = copy.deepcopy(base)
    fn(out)
    return out


REV1 = _ids(TABLE)
PLAIN1 = _ids(PLAIN)
BODY1 = canonical(REV1)
CANONICAL: list[tuple[str, Any]] = [
    ("fields-canonical-rows", REV1),
    ("fields-canonical-row-keys-sorted", {"fields": [{"value": "POST", "label": "Method", "copy": False}]}),
]
HASHES: list[tuple[str, int, str, str, str, str]] = [
    ("fields-hash-body", 1, "", AT1, "", BODY1),
    ("fields-hash-row-changed", 2, revision_hash(1, "", AT1, "", BODY1), AT2, "The method was wrong",
     canonical(_change(lambda d: d["steps"][0]["fields"][1].update(value="GET"), REV1))),
    ("fields-hash-copy-flag", 2, revision_hash(1, "", AT1, "", BODY1), AT2, "Copy the method too",
     canonical(_change(lambda d: d["steps"][0]["fields"][1].update(copy=True), REV1))),
]
AMEND: list[tuple[str, dict, dict, list[str], int, Any]] = [
    ("fields-amend-row-added", REV1, _change(lambda d: d["steps"][0]["fields"].append(
        {"label": "Action", "value": "Deny (429)"}), REV1), step_ids(REV1), 2, "The action was missing"),
    ("fields-amend-copy-flag", REV1, _change(lambda d: d["steps"][0]["fields"][1].update(copy=True), REV1),
     step_ids(REV1), 2, "Copy the method too"),
    ("fields-amend-rows-reordered", REV1, _change(lambda d: d["steps"][0]["fields"].reverse(), REV1),
     step_ids(REV1), 2, "Name first is wrong"),
    ("fields-amend-table-added", PLAIN1, _change(lambda d: d["steps"][0].update(fields=[RULE[0]]), PLAIN1),
     step_ids(PLAIN1), 2, "Name the rule"),
    ("fields-amend-table-removed", REV1, _change(lambda d: d["steps"][0].pop("fields"), REV1),
     step_ids(REV1), 2, "The rule exists"),
    ("fields-amend-bad-row", REV1, _change(lambda d: d["steps"][0]["fields"].append(
        {"label": "Name", "value": "dup"}), REV1), step_ids(REV1), 2, "A second name"),
    ("fields-amend-on-verify", REV1, _change(lambda d: d.update(verify={"do": "Check it", "fields": [RULE[0]]}),
                                             REV1), step_ids(REV1), 2, "Check the rule"),
]


def _py_bytes(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def build() -> list[dict]:
    names = [name for name, _ in INPUTS]
    if not names or len(set(names)) != len(names):
        raise SystemExit("gen_fields_vectors: no cases, or a duplicate case name")
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
        raise SystemExit("gen_fields_vectors: a duplicate case name, or one without the fields- prefix")
    return cases


def _dump(doc: dict) -> str:
    """Indented JSON, non-ASCII kept, every hidden character (and ZWJ or a variation
    selector) written as a \\u escape so a reader sees it."""
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    return "".join(f"\\u{ord(ch):04x}" if _is_hidden(ch) or ord(ch) in (0x200D, 0xFE0E, 0xFE0F) else ch
                   for ch in text) + "\n"


def main() -> None:
    cases = build()
    OUT.write_text(_dump({"source": "mailbox/tests/gen_fields_vectors.py (fields-* cases)",
                          "cases": cases}), encoding="utf-8")


if __name__ == "__main__":
    main()
