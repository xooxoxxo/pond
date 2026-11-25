"""The shared revision vectors (Pond 1.1.0): revision-vectors.json is what this
Pond's reference code computes today, byte for byte, so a rule change without a
regenerated fixture fails here before Hook is held to stale answers."""
import json
from collections import Counter

import pytest

from mailbox.instructable import no_duplicates
from mailbox.tests.gen_revision_vectors import KINDS, OUT, build, dump

_TEXT = OUT.read_text(encoding="utf-8")
_DOC = json.loads(_TEXT)


def test_the_file_is_what_the_generator_writes():
    assert dump(build()) == _TEXT                      # rerun the generator after a rule change


def test_the_file_is_whole():
    assert _DOC["version"] == 1
    cases = _DOC["cases"]
    assert len(cases) >= 60
    names = [c["name"] for c in cases]
    assert len(set(names)) == len(names)
    kinds = Counter(c["kind"] for c in cases)
    assert set(kinds) == set(KINDS)
    assert all(kinds[k] >= 3 for k in KINDS), kinds


@pytest.mark.parametrize("case", [c for c in _DOC["cases"] if c["kind"] == "duplicateKey"],
                         ids=lambda c: c["name"])
def test_a_duplicate_key_vector_is_refused_by_the_strict_parser(case):
    assert case["valid"] is False
    with pytest.raises(ValueError):
        json.loads(case["raw"], object_pairs_hook=no_duplicates)
    json.loads(case["raw"])                            # the lenient parser would read it


def test_a_canonical_vector_is_the_documented_form():
    for case in _DOC["cases"]:
        if case["kind"] == "canonical":
            assert case["body"] == json.dumps(case["input"], sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False)
