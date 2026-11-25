"""Write doc-vectors.json: the doc-* cases of the shared validator vectors, computed
by this mailbox's validator (the reference).

    python3 -m mailbox.tests.gen_doc_vectors                   # from the repo root

A `doc` is the fingerprint of the document a step's (or sub-step's) `file`, or a
link's `path`, points at: {"sha256": <64 lowercase hex>, "bytes": <1 to 262144>}.
Every value here is synthetic: the file is copied as it is into other repositories.
"""
import hashlib
import json
from pathlib import Path

from mailbox.instructable import MAX_DOC_BYTES, validate

OUT = Path(__file__).parent / "doc-vectors.json"
PREFIXES = ("doc-",)

TITLE = "Review the release plan"
READ = "Read the plan"
SHIP = {"do": "Ship it"}
PLAN = "~/notes/plan.md"
CHECKLIST = "/var/tmp/release-checklist.md"
PLAN_TEXT = b"# Release plan\n\nShip on Friday.\n"
CHECKLIST_TEXT = b"# Checklist\n\n- tests pass\n"
SHA = hashlib.sha256(PLAN_TEXT).hexdigest()
SHA_2 = hashlib.sha256(CHECKLIST_TEXT).hexdigest()
DOC = {"sha256": SHA, "bytes": len(PLAN_TEXT)}


def _ask(step: dict | None = None, *, steps: list | None = None, verify: dict | None = None,
         links: list | None = None) -> dict:
    obj = {"v": 1, "title": TITLE,
           "steps": steps if steps is not None else [step or {"do": READ}, SHIP]}
    if verify is not None:
        obj["verify"] = verify
    if links is not None:
        obj["links"] = links
    return obj


def _doc(**keys) -> dict:
    return {**DOC, **keys}


def _step_doc(doc) -> dict:
    return _ask({"do": READ, "file": PLAN, "doc": doc})


def _link_doc(doc) -> dict:
    return _ask(links=[{"label": "Checklist", "path": CHECKLIST, "doc": doc}])


INPUTS: list[tuple[str, dict]] = [
    # valid
    ("doc-valid-step", _step_doc(DOC)),
    ("doc-valid-substep", _ask(steps=[{"do": "Go through the plan", "substeps": [
        {"do": READ, "file": PLAN, "doc": DOC}, {"do": "Note questions"}]}, SHIP])),
    ("doc-valid-link", _link_doc({"sha256": SHA_2, "bytes": len(CHECKLIST_TEXT)})),
    ("doc-valid-max-bytes", _step_doc(_doc(bytes=MAX_DOC_BYTES))),
    ("doc-valid-one-byte", _step_doc(_doc(bytes=1))),
    ("doc-valid-null", _step_doc(None)),
    # where a doc may stand
    ("doc-on-verify", _ask(verify={"do": "Check the plan", "doc": DOC})),
    ("doc-on-verify-with-file", _ask(verify={"do": "Check the plan", "file": PLAN, "doc": DOC})),
    ("doc-without-file", _ask({"do": READ, "doc": DOC})),
    ("doc-without-file-substep", _ask(steps=[{"do": "Go through the plan", "substeps": [
        {"do": READ, "doc": DOC}]}, SHIP])),
    ("doc-file-null", _ask({"do": READ, "file": None, "doc": DOC})),
    ("doc-link-without-path", _ask(links=[{"label": "Site", "url": "https://example.com/plan",
                                           "doc": DOC}])),
    # its shape
    ("doc-not-object-string", _step_doc(SHA)),
    ("doc-not-object-list", _step_doc([SHA, len(PLAN_TEXT)])),
    ("doc-extra-key", _step_doc(_doc(note="draft"))),
    ("doc-sha256-uppercase", _step_doc(_doc(sha256=SHA.upper()))),
    ("doc-sha256-63", _step_doc(_doc(sha256=SHA[:63]))),
    ("doc-sha256-65", _step_doc(_doc(sha256=SHA + "0"))),
    ("doc-sha256-not-hex", _step_doc(_doc(sha256="g" * 64))),
    ("doc-sha256-number", _step_doc(_doc(sha256=12345))),
    ("doc-sha256-missing", _step_doc({"bytes": len(PLAN_TEXT)})),
    ("doc-bytes-zero", _step_doc(_doc(bytes=0))),
    ("doc-bytes-too-many", _step_doc(_doc(bytes=MAX_DOC_BYTES + 1))),
    ("doc-bytes-negative", _step_doc(_doc(bytes=-1))),
    ("doc-bytes-fraction", _step_doc(_doc(bytes=1.5))),
    ("doc-bytes-string", _step_doc(_doc(bytes=str(len(PLAN_TEXT))))),
    ("doc-bytes-true", _step_doc(_doc(bytes=True))),
    ("doc-bytes-missing", _step_doc({"sha256": SHA})),
    ("doc-empty", _step_doc({})),
    ("doc-link-bad", _link_doc({"sha256": SHA.upper(), "bytes": 0})),
    # a good doc beside a bad anchor: only the anchor is named
    ("doc-bad-file-good-doc", _ask({"do": READ, "file": "notes/plan.md", "doc": DOC})),
    ("doc-link-url-path-and-doc", _ask(links=[{"label": "Checklist", "url": "https://pond.example.test/checklist",
                                               "path": CHECKLIST, "doc": {"sha256": SHA_2,
                                                                          "bytes": len(CHECKLIST_TEXT)}}])),
    # null is "no document" wherever it stands, also with nothing to anchor it
    ("doc-null-without-file", _ask({"do": READ, "doc": None})),
    ("doc-null-link-url", _ask(links=[{"label": "Site", "url": "https://example.com/plan", "doc": None}])),
]


def _py_bytes(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def build() -> list[dict]:
    names = [name for name, _ in INPUTS]
    if not names or len(set(names)) != len(names):
        raise SystemExit("gen_doc_vectors: no cases, or a duplicate case name")
    cases = []
    for name, obj in INPUTS:
        assert name.startswith(PREFIXES), name
        clean, errors = validate(obj)
        cases.append({"name": name, "kind": "instructable", "input": obj, "valid": not errors,
                      "errors": errors, "cleaned": clean or None, "pyBytes": _py_bytes(obj)})
    return cases


def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    cases = build()
    OUT.write_text(_dump({"source": "mailbox/tests/gen_doc_vectors.py (doc-* cases)",
                          "cases": cases}), encoding="utf-8")


if __name__ == "__main__":
    main()
