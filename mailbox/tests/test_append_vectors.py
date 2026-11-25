"""The shared append vectors (Pond 1.9.0): append-vectors.json is Hook's canonical
copy, byte for byte (test_conformance_sync compares it). The Pond's rule, its
advance and its body builder must give every answer the file records."""
import json
from pathlib import Path

import pytest

from mailbox.instructable import no_duplicates
from mailbox.revisions import (KIND_APPEND, MAX_APPEND_NOTE, MAX_APPEND_STEPS, MAX_APPENDS, MAX_REV,
                               MIN_APPEND_GAP_S, build_append, canonical, is_append, json_equal, parse_exact)

_DOC = json.loads((Path(__file__).parent / "append-vectors.json").read_text(encoding="utf-8"))


def _cases(kind: str) -> list:
    return [c for c in _DOC["cases"] if c["kind"] == kind]


def _strict(text: str) -> dict:
    """A served body text, parsed as Hook parses it: a duplicate key is refused, and
    numbers keep their source text."""
    return parse_exact(text, object_pairs_hook=no_duplicates)


def test_the_constants():
    assert (MAX_APPENDS, MAX_APPEND_STEPS, MAX_APPEND_NOTE, MIN_APPEND_GAP_S, MAX_REV) == (10, 10, 200, 15, 21)
    assert KIND_APPEND == "append"


def test_the_file_is_whole():
    assert _DOC["version"] == 1
    names = [c["name"] for c in _DOC["cases"]]
    assert len(set(names)) == len(names)
    assert all(_cases(k) for k in ("check", "chain", "append"))


@pytest.mark.parametrize("case", _cases("check"), ids=lambda c: c["name"])
def test_check_cases(case):
    assert is_append(_strict(case["base"]), _strict(case["next"])) is case["expect"]["append"]


@pytest.mark.parametrize("case", _cases("chain"), ids=lambda c: c["name"])
def test_chain_cases(case):
    # The Pond's own store is untampered: a tampered hash changes Hook's state, never
    # the Pond's call. Stepwise: revision k moves the accepted one to k when it was
    # k - 1 and k is an append of k - 1.
    bodies = {r["rev"]: _strict(r["body"]) for r in case["revisions"]}
    accepted = case["accepted"]
    for k in sorted(bodies):
        if k > accepted and accepted == k - 1 and is_append(bodies[k - 1], bodies[k]):
            accepted = k
    assert (accepted == max(bodies)) is case["expect"]["pondAdvances"]


@pytest.mark.parametrize("case", _cases("append"), ids=lambda c: c["name"])
def test_append_cases(case):
    request = case["request"]
    new, added, errors = build_append(case["current"], request["steps"], set(case["used"]), case["rev"],
                                      request["note"])
    expect = case["expect"]
    if expect["ok"]:
        assert errors == []
        assert canonical(new) == expect["body"]
        assert added == expect["added"]
        assert is_append(case["current"], new)
    else:
        assert (new, added) == ({}, [])
        assert expect["status"] == 400 and expect["error"] == "invalid steps"
        assert errors == expect["details"]


def test_json_value_equality_is_strict_about_types():
    base = {"v": 1, "title": "t", "steps": [{"id": "s1", "do": "a", "optional": True}]}
    one = {"v": 1, "title": "t", "steps": [{"id": "s1", "do": "a", "optional": 1}, {"id": "s2", "do": "b"}]}
    assert not is_append(base, one)                     # true is not 1 as a JSON value
    one["steps"][0]["optional"] = True
    assert is_append(base, one)
    base["n"], one["n"] = 1, 1.0
    assert not is_append(base, one)                     # numbers compare by their text: 1 is not 1.0


def test_json_equal_is_exact():
    assert not json_equal(True, 1) and not json_equal(1, True) and not json_equal(False, 0)
    assert not json_equal(1, 1.0)
    assert json_equal(parse_exact("1"), 1) and not json_equal(parse_exact("1.0"), 1)
    assert not json_equal(parse_exact("0.10000000000000000001"), parse_exact("0.1"))
    assert not json_equal(parse_exact("9007199254740993"), parse_exact("9007199254740992"))
    assert json_equal(parse_exact("1e400"), parse_exact("1e400"))
    assert not json_equal(parse_exact("1"), "1")                 # a number is never a string
    assert not json_equal("caf\u00e9", "cafe\u0301")            # code points, no normalisation
    assert not json_equal({"caf\u00e9": 1}, {"cafe\u0301": 1})
    with pytest.raises(ValueError):
        parse_exact("NaN")


def test_build_append_refuses_a_bad_step_count():
    current = {"v": 1, "title": "t", "urgency": "today", "steps": [{"id": "s1", "do": "a"}]}
    for steps in ([], [{"do": "x"}] * 11, None, "x"):
        assert build_append(current, steps, {"s1"}, 2, "a note") == ({}, [], ["steps: 1 to 10"])


def test_build_append_never_mutates_its_inputs():
    current = {"v": 1, "title": "t", "urgency": "today", "steps": [{"id": "s1", "do": "a"}]}
    steps = [{"do": "b"}]
    before = json.dumps([current, steps])
    new, added, errors = build_append(current, steps, {"s1"}, 2, "a note")
    assert errors == [] and added == [{"id": "r2-1", "path": "2"}]
    assert json.dumps([current, steps]) == before
