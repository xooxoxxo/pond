"""The brief-* vectors (Pond 1.14.0): an ask's background, replayed against the
validator, plus what `brief` means for the revision hash, a correction's summary, step
identities and the features list."""
import copy
import json
from pathlib import Path

import pytest

from mailbox import FEATURES
from mailbox import requirements as req
from mailbox.instructable import BRIEF_LIMIT, validate
from mailbox.revisions import assign_ids, canonical, is_append, revision_hash, summarize, validate_amendment
from mailbox.telegram import TelegramClient
from mailbox.tests import gen_brief_vectors as gen

_ALL = json.loads((Path(__file__).parent / "brief-vectors.json").read_text(encoding="utf-8"))["cases"]
_CASES = [c for c in _ALL if c["kind"] == "instructable"]

LONG = f"brief: longer than {BRIEF_LIMIT} characters"
STRING = "brief: must be a string"
SECRET = "brief: secret-like value"

# What each case must answer, written by hand so the generator can't drift with the validator.
EXPECTED: dict[str, list[str]] = {
    "brief-valid": [],
    "brief-valid-with-why": [],
    "brief-valid-one-line": [],
    "brief-valid-8000": [],
    "brief-valid-null": [],
    "brief-valid-empty": [],
    "brief-valid-tabs-and-lines": [],
    "brief-valid-non-ascii": [],
    "brief-valid-emoji-zwj": [],
    "brief-8001": [LONG],
    "brief-number": [STRING],
    "brief-list": [STRING],
    "brief-object": [STRING],
    "brief-bool": [STRING],
    "brief-carriage-return": ["brief: contains hidden or control characters (U+000D)"],
    "brief-bidi": ["brief: contains hidden or control characters (U+202E)"],
    "brief-nul": ["brief: contains hidden or control characters (U+0000)"],
    "brief-secret": [SECRET],
    "brief-secret-anthropic": [SECRET],
    "brief-secret-split": [SECRET],
    "brief-secret-private-key": [SECRET],
    "brief-and-title": ["title: must not be empty", LONG],
    "brief-on-a-step": ["steps[0].brief: unknown key"],
}


def test_every_case_has_an_expectation():
    assert _CASES and {c["name"] for c in _CASES} == set(EXPECTED)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_brief_vectors_match_the_reference(case):
    clean, errors = validate(case["input"])
    assert errors == EXPECTED[case["name"]]
    assert errors == case["errors"]
    assert (errors == []) == case["valid"]
    assert (clean or None) == case["cleaned"]


def test_the_file_is_what_the_generator_writes():
    path = Path(__file__).parent / "brief-vectors.json"
    want = gen._dump({"source": "mailbox/tests/gen_brief_vectors.py (brief-* cases)", "pond": "1.14.0",
                      "cases": gen.build()})
    assert path.read_text(encoding="utf-8") == want, "run python3 -m mailbox.tests.gen_brief_vectors"


@pytest.mark.parametrize("case", [c for c in _ALL if c["kind"] == "hash"], ids=lambda c: c["name"])
def test_the_revision_hash_covers_the_brief(case):
    assert revision_hash(case["rev"], case["prev"], case["at"], case["reason"], case["body"]) == case["hash"]


@pytest.mark.parametrize("case", [c for c in _ALL if c["kind"] == "canonical"], ids=lambda c: c["name"])
def test_canonical_bodies(case):
    assert canonical(case["input"]) == case["body"]


@pytest.mark.parametrize("case", [c for c in _ALL if c["kind"] == "amend"], ids=lambda c: c["name"])
def test_amend_cases(case):
    out, summary, errors = validate_amendment(case["current"], case["new"], set(case["usedIds"]), case["rev"],
                                              case["reason"])
    assert errors == case["errors"]
    assert summary == case["summary"]


def test_a_changed_brief_is_named_at_the_ask_level_only():
    old = gen.REV1
    new = copy.deepcopy(old)
    new["brief"] = gen.LONGER
    summary = summarize(old, new)
    assert summary["top"] == ["brief"] and summary["changed"] == []
    new["why"] = "Blocked."
    assert summarize(old, new)["top"] == ["why", "brief"]


def test_the_brief_is_not_part_of_any_step_identity():
    """Ticks and answers follow a step's own fields (requirements.identities): adding,
    changing or removing the brief leaves every step identity as it was."""
    with_brief = gen.REV1
    without = {k: v for k, v in with_brief.items() if k != "brief"}
    assert req.identities(with_brief) == req.identities(without)
    changed = {**with_brief, "brief": gen.LONGER}
    assert req.identities(changed) == req.identities(with_brief)


def test_the_brief_changes_the_revision_hash():
    other = {**gen.REV1, "brief": gen.LONGER}
    assert canonical(other) != canonical(gen.REV1)


def test_an_append_keeps_the_brief_and_a_changed_brief_is_no_append():
    base = gen.REV1
    added = copy.deepcopy(base)
    added["steps"].append({"do": "Tell the session it is stored", "id": "r2-1"})
    assert is_append(base, added)
    added["brief"] = gen.LONGER
    assert not is_append(base, added)


def test_the_brief_counts_toward_the_byte_limit():
    big = {"v": 1, "title": "t", "steps": [{"do": "x", "detail": "d" * 1500} for _ in range(20)],
           "brief": "b" * BRIEF_LIMIT}
    _, errors = validate(big)
    assert errors == ["instructable: larger than 32768 bytes"]


def test_assigned_ids_leave_the_brief_alone():
    out, errors = assign_ids({"v": 1, "title": "t", "steps": [{"do": "x"}], "brief": "b"})
    assert errors == [] and out["brief"] == "b"


def test_telegram_calls_a_changed_brief_background():
    text = TelegramClient.render_corrected("Orbit", "t", 2, "why", {
        "changed": [], "added": [], "removed": [], "moved": [], "top": ["why", "brief"]})
    assert "Also: why, background." in text


def test_the_feature_is_listed():
    assert "brief" in FEATURES and "askAbout" not in FEATURES
