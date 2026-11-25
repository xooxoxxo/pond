"""The fields-* vectors (Pond 1.13.0): a step's settings table, replayed against the
validator, plus what `fields` means for the revision hash, a correction's summary and
the features list."""
import copy
import json
from pathlib import Path

import pytest

from mailbox import FEATURES, VERSION
from mailbox.instructable import FIELD_LABEL_LIMIT, FIELD_VALUE_LIMIT, MAX_FIELDS, validate
from mailbox.revisions import assign_ids, canonical, revision_hash, summarize, validate_amendment

_ALL = json.loads((Path(__file__).parent / "fields-vectors.json").read_text(encoding="utf-8"))["cases"]
_CASES = [c for c in _ALL if c["kind"] == "instructable"]

F = "steps[0].fields"
R = "steps[0].fields[0]"
LIST = f"{F}: must be a list of 1 to 20 rows"
COPY = "copy: must be true or false"

# What each case must answer, written by hand so the generator can't drift with the validator.
EXPECTED: dict[str, list[str]] = {
    "fields-valid-step": [],
    "fields-valid-substep": [],
    "fields-valid-one-row": [],
    "fields-valid-twenty-rows": [],
    "fields-valid-label-40": [],
    "fields-valid-value-500": [],
    "fields-valid-copy-false": [],
    "fields-valid-copy-null": [],
    "fields-valid-null": [],
    "fields-valid-non-ascii": [],
    "fields-valid-same-label-other-step": [],
    "fields-valid-labels-differ-by-case": [],
    "fields-valid-with-code": [],
    "fields-on-verify": ["verify.fields: unknown key"],
    "fields-empty-list": [LIST],
    "fields-too-many-rows": [LIST],
    "fields-not-a-list": [LIST],
    "fields-row-not-object": [f"{F}[1]: must be an object"],
    "fields-row-null": [f"{F}[1]: must be an object"],
    "fields-row-extra-key": [f"{R}.note: unknown key"],
    "fields-label-missing": [f"{R}.label: required"],
    "fields-label-empty": [f"{R}.label: must not be empty"],
    "fields-label-blank": [f"{R}.label: must not be empty"],
    "fields-label-41": [f"{R}.label: longer than 40 characters"],
    "fields-label-number": [f"{R}.label: must be a string"],
    "fields-label-two-lines": [f"{R}.label: must be a single line"],
    "fields-label-hidden": [f"{R}.label: contains hidden or control characters (U+202E)"],
    "fields-label-duplicate": [f"{F}[1].label: duplicate label"],
    "fields-value-missing": [f"{R}.value: required"],
    "fields-value-empty": [f"{R}.value: must not be empty"],
    "fields-value-501": [f"{R}.value: longer than 500 characters"],
    "fields-value-number": [f"{R}.value: must be a string"],
    "fields-value-line-break": [f"{R}.value: must be a single line"],
    "fields-value-carriage-return": [f"{R}.value: contains hidden or control characters (U+000D)"],
    "fields-value-tab": [f"{R}.value: must be a single line"],
    "fields-value-zwj": [f"{R}.value: contains hidden or control characters (U+200D)"],
    "fields-value-variation-selector": [f"{R}.value: contains hidden or control characters (U+FE0F)"],
    "fields-value-bidi": [f"{R}.value: contains hidden or control characters (U+202E)"],
    "fields-value-secret": [f"{R}.value: secret-like value"],
    "fields-value-secret-split": [f"{R}.value: contains hidden or control characters (U+200D)",
                                  f"{R}.value: secret-like value"],
    "fields-label-secret": [f"{R}.label: secret-like value"],
    "fields-copy-string": [f"{R}.{COPY}"],
    "fields-copy-number": [f"{R}.{COPY}"],
    "fields-row-every-problem": [f"{R}.x: unknown key", f"{R}.label: must not be empty",
                                 f"{R}.value: must be a single line", f"{R}.{COPY}"],
    "fields-substep-bad": ["steps[0].substeps[0].fields[1].label: duplicate label"],
}


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_fields_vectors_match_the_reference(case):
    clean, errors = validate(case["input"])
    assert errors == EXPECTED[case["name"]]
    assert errors == case["errors"]
    assert (errors == []) == case["valid"]
    assert (clean or None) == case["cleaned"]


def test_fields_vectors_are_all_present_and_current():
    from mailbox.tests.gen_fields_vectors import build
    assert [c["name"] for c in _CASES] == list(EXPECTED)
    assert build() == _ALL                             # rerun the generator after a rule change
    assert {c["kind"] for c in _ALL} == {"instructable", "canonical", "hash", "amend"}


@pytest.mark.parametrize("case", [c for c in _ALL if c["kind"] == "hash"], ids=lambda c: c["name"])
def test_a_hash_vector(case):
    assert revision_hash(case["rev"], case["prev"], case["at"], case["reason"], case["body"]) == case["hash"]


def test_every_hash_and_body_differs():
    hashes = [c["hash"] for c in _ALL if c["kind"] == "hash"]
    bodies = [c["body"] for c in _ALL if c["kind"] == "hash"]
    assert len(set(hashes)) == len(hashes) == 3 and len(set(bodies)) == 3


@pytest.mark.parametrize("case", [c for c in _ALL if c["kind"] == "canonical"], ids=lambda c: c["name"])
def test_a_canonical_vector(case):
    assert canonical(case["input"]) == case["body"] and json.loads(case["body"]) == case["input"]


AMENDED = {
    "fields-amend-row-added": [],
    "fields-amend-copy-flag": [],
    "fields-amend-rows-reordered": [],
    "fields-amend-table-added": [],
    "fields-amend-table-removed": [],
    "fields-amend-bad-row": ["steps[0].fields[3].label: duplicate label"],
    "fields-amend-on-verify": ["verify.fields: unknown key"],
}


@pytest.mark.parametrize("case", [c for c in _ALL if c["kind"] == "amend"], ids=lambda c: c["name"])
def test_an_amend_vector(case):
    assert case["errors"] == AMENDED[case["name"]]
    if not case["errors"]:
        assert case["summary"]["changed"] == [{"id": "s1", "path": "1", "fields": ["fields"]}]


def test_the_limits():
    assert (MAX_FIELDS, FIELD_LABEL_LIMIT, FIELD_VALUE_LIMIT) == (20, 40, 500)


def test_the_feature_is_listed():
    assert "fields" in FEATURES


def test_copy_is_not_filled_in():
    """`copy` defaults to false for a reader; the Pond stores the row as sent, so the
    hash covers exactly what the session wrote."""
    obj = {"v": 1, "title": "t", "steps": [{"do": "Add it", "fields": [{"label": "Name", "value": "a"}]}]}
    clean, errors = validate(obj)
    assert errors == [] and clean["steps"][0]["fields"] == [{"label": "Name", "value": "a"}]


BASE = {"v": 1, "title": "Add the rule", "urgency": "today", "steps": [
    {"do": "Add the custom rule", "fields": [{"label": "Name", "value": "requests-ip", "copy": True},
                                             {"label": "Method", "value": "POST"}]},
    {"do": "Save"}]}


def test_fields_are_part_of_the_hash():
    rev1, _ = assign_ids(BASE)
    changed = copy.deepcopy(rev1)
    changed["steps"][0]["fields"][1]["value"] = "GET"
    at = "2026-11-21T10:00:00+00:00"
    assert canonical(rev1) != canonical(changed)
    assert revision_hash(1, "", at, "", canonical(rev1)) != revision_hash(1, "", at, "", canonical(changed))
    flipped = copy.deepcopy(rev1)
    flipped["steps"][0]["fields"][1]["copy"] = True
    assert canonical(flipped) != canonical(rev1)


def test_a_changed_row_is_a_changed_step():
    rev1, _ = assign_ids(BASE)
    new = copy.deepcopy(rev1)
    new["steps"][0]["fields"].append({"label": "Action", "value": "Deny (429)"})
    clean, errors = validate(new, new_ids=True)
    assert errors == []
    out, summary, errors = validate_amendment(rev1, clean, {"s1", "s2"}, 2, "The action was missing")
    assert errors == [] and summary["changed"] == [{"id": "s1", "path": "1", "fields": ["fields"]}]
    assert summarize(rev1, rev1)["changed"] == []


def test_telegram_names_a_changed_table_settings_and_never_shows_a_row():
    from mailbox.telegram import TelegramClient
    summary = {"changed": [{"id": "s1", "path": "1", "fields": ["fields"]}], "added": [], "removed": [],
               "moved": [], "top": []}
    text = TelegramClient.render_corrected("Orbit", "Add the rule", 2, "The action was missing", summary)
    assert "step 1 (settings)" in text
    block = "\n".join(TelegramClient._step_block(1, BASE["steps"][0]))
    assert "requests-ip" not in block and "POST" not in block
