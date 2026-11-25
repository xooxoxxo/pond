"""The doc-* vectors: a step's or link's `doc`, the fingerprint of the document its
`file` or `path` points at, replayed against the validator, plus `doc_refs`."""
import hashlib
import json
from pathlib import Path

import pytest

from mailbox.instructable import MAX_DOC_BYTES, doc_keys, doc_refs, validate

_CASES = json.loads((Path(__file__).parent / "doc-vectors.json").read_text(encoding="utf-8"))["cases"]

S = "steps[0].doc"
L = "links[0].doc"
HEX = "sha256: must be 64 lowercase hex characters"
NUM = "bytes: must be a whole number from 1 to 262144"

# What each case must answer, written by hand so the generator can't drift with the validator.
EXPECTED: dict[str, list[str]] = {
    "doc-valid-step": [],
    "doc-valid-substep": [],
    "doc-valid-link": [],
    "doc-valid-max-bytes": [],
    "doc-valid-one-byte": [],
    "doc-valid-null": [],
    "doc-on-verify": ["verify.doc: unknown key"],
    "doc-on-verify-with-file": ["verify.file: unknown key", "verify.doc: unknown key"],
    "doc-without-file": [f"{S}: needs file"],
    "doc-without-file-substep": ["steps[0].substeps[0].doc: needs file"],
    "doc-file-null": [f"{S}: needs file"],
    "doc-link-without-path": [f"{L}: needs path"],
    "doc-not-object-string": [f"{S}: must be an object"],
    "doc-not-object-list": [f"{S}: must be an object"],
    "doc-extra-key": [f"{S}.note: unknown key"],
    "doc-sha256-uppercase": [f"{S}.{HEX}"],
    "doc-sha256-63": [f"{S}.{HEX}"],
    "doc-sha256-65": [f"{S}.{HEX}"],
    "doc-sha256-not-hex": [f"{S}.{HEX}"],
    "doc-sha256-number": [f"{S}.{HEX}"],
    "doc-sha256-missing": [f"{S}.{HEX}"],
    "doc-bytes-zero": [f"{S}.{NUM}"],
    "doc-bytes-too-many": [f"{S}.{NUM}"],
    "doc-bytes-negative": [f"{S}.{NUM}"],
    "doc-bytes-fraction": [f"{S}.{NUM}"],
    "doc-bytes-string": [f"{S}.{NUM}"],
    "doc-bytes-true": [f"{S}.{NUM}"],
    "doc-bytes-missing": [f"{S}.{NUM}"],
    "doc-empty": [f"{S}.{HEX}", f"{S}.{NUM}"],
    "doc-link-bad": [f"{L}.{HEX}", f"{L}.{NUM}"],
    "doc-bad-file-good-doc": ["steps[0].file: must be an absolute path"],
    "doc-link-url-path-and-doc": ["links[0]: needs url or path, not both"],
    # null is "no document" wherever it stands, also with nothing to anchor it
    "doc-null-without-file": [],
    "doc-null-link-url": [],
}


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_doc_vectors_match_the_reference(case):
    clean, errors = validate(case["input"])
    assert errors == EXPECTED[case["name"]]
    assert errors == case["errors"]
    assert (errors == []) == case["valid"]
    assert (clean or None) == case["cleaned"]


def test_doc_vectors_are_all_present_and_current():
    from mailbox.tests.gen_doc_vectors import build
    assert [c["name"] for c in _CASES] == list(EXPECTED)
    assert build() == _CASES                           # rerun the generator after a rule change


def test_max_doc_bytes():
    assert MAX_DOC_BYTES == 262144


def _with_doc(sha: str) -> dict:
    return {"v": 1, "title": "Read it", "steps": [
        {"do": "Read the plan", "file": "~/notes/plan.md", "doc": {"sha256": sha, "bytes": 10}}]}


@pytest.mark.parametrize("seed", [b"", b"a", b"plan", b"# Notes\n", b"sk-ant-", b"AKIA", b"ghp_x"])
def test_a_valid_sha256_is_never_secret_like(seed):
    sha = hashlib.sha256(seed).hexdigest()
    assert validate(_with_doc(sha))[1] == []


@pytest.mark.parametrize("sha", ["0" * 64, "f" * 64, "0123456789abcdef" * 4, "deadbeef" * 8])
def test_edge_hashes_are_never_secret_like(sha):
    assert validate(_with_doc(sha))[1] == []


def test_doc_refs_in_document_order():
    a, b, c, d = (hashlib.sha256(x).hexdigest() for x in (b"a", b"b", b"c", b"d"))
    obj = {"v": 1, "title": "Read them", "steps": [
        {"do": "No document"},
        {"do": "First", "file": "~/notes/a.md", "doc": {"sha256": a, "bytes": 1},
         "substeps": [{"do": "Inner none"},
                      {"do": "Inner", "file": "~/notes/b.md", "doc": {"sha256": b, "bytes": 2}}]},
        {"do": "File without doc", "file": "~/notes/x.md"},
        {"do": "Null doc", "file": "~/notes/y.md", "doc": None},
        {"do": "Last", "file": "/var/tmp/c.md", "doc": {"sha256": c, "bytes": 3}},
    ], "links": [
        {"label": "Site", "url": "https://example.com"},
        {"label": "Doc", "path": "~/notes/d.md", "doc": {"sha256": d, "bytes": 4}},
    ]}
    assert validate(obj)[1] == []
    assert doc_refs(obj) == [
        ("steps[1]", a, 1),
        ("steps[1].substeps[1]", b, 2),
        ("steps[4]", c, 3),
        ("links[1]", d, 4),
    ]


def test_doc_refs_empty():
    assert doc_refs({"v": 1, "title": "t", "steps": [{"do": "x"}]}) == []


def test_doc_keys_finds_every_doc_key_null_included():
    a = hashlib.sha256(b"a").hexdigest()
    obj = {"v": 1, "title": "t", "steps": [
        {"do": "Plain"},
        {"do": "Null", "doc": None, "substeps": [{"do": "Inner", "file": "~/a.md", "doc": {"sha256": a, "bytes": 1}}]},
    ], "links": [{"label": "Site", "url": "https://example.com", "doc": None}]}
    assert doc_keys(obj) == [("steps[1]", None), ("steps[1].substeps[0]", {"sha256": a, "bytes": 1}),
                             ("links[0]", None)]
    assert doc_keys(None) == [] and doc_keys({"v": 1, "title": "t", "steps": [{"do": "x"}]}) == []
