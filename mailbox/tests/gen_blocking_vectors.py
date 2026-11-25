"""Write blocking-vectors.json: the blocking-* and warning-* cases of the shared
validator vectors, computed by this mailbox's validator (the reference).

    python3 -m mailbox.tests.gen_blocking_vectors        # from the repo root

The same cases are appended to Hook's Tests/RelayCoreTests/Fixtures/
instructable-vectors.json, so the Swift validator is held to the same answers.
"""
import json
from pathlib import Path

from mailbox.instructable import validate

OUT = Path(__file__).parent / "blocking-vectors.json"


def _ask(step: dict | None = None, *, steps: list | None = None, verify: dict | None = None) -> dict:
    obj = {"v": 1, "title": "Rotate the deploy key",
           "steps": steps if steps is not None else [step or {"do": "Revoke the old key"}, {"do": "Deploy"}]}
    if verify is not None:
        obj["verify"] = verify
    return obj


def _sub(sub: dict) -> dict:
    return _ask(steps=[{"do": "Set up the key", "substeps": [{"do": "Open the console"}, sub]},
                       {"do": "Deploy"}])


INPUTS: list[tuple[str, dict]] = [
    ("blocking-valid-true", _ask({"do": "Revoke the old key", "blocking": True})),
    ("blocking-valid-false", _ask({"do": "Revoke the old key", "blocking": False})),
    ("blocking-valid-null", _ask({"do": "Revoke the old key", "blocking": None})),
    ("blocking-valid-with-warning", _ask({"do": "Revoke the old key", "blocking": True,
                                          "warning": "Deploying first leaves the old key live."})),
    ("blocking-valid-on-substep", _sub({"do": "Create the key", "blocking": True})),
    ("blocking-valid-with-question", _ask({"do": "Pick the region", "blocking": True,
                                           "question": {"prompt": "Which region?", "choices": ["EU", "US"]}})),
    ("blocking-not-bool-string", _ask({"do": "Revoke the old key", "blocking": "true"})),
    ("blocking-not-bool-number", _ask({"do": "Revoke the old key", "blocking": 1})),
    ("blocking-substep-not-bool", _sub({"do": "Create the key", "blocking": "yes"})),
    ("blocking-on-verify", _ask(verify={"do": "Check it", "blocking": True})),
    ("warning-valid", _ask({"do": "Revoke the old key", "warning": "The old key stops working at once."})),
    ("warning-valid-multiline", _ask({"do": "Revoke the old key",
                                      "warning": "The old key stops working at once.\nCI jobs that use it fail."})),
    ("warning-valid-null", _ask({"do": "Revoke the old key", "warning": None})),
    ("warning-valid-300", _ask({"do": "Revoke the old key", "warning": "w" * 300})),
    ("warning-valid-on-substep", _sub({"do": "Create the key", "warning": "Copy it now: it is shown once."})),
    ("warning-301", _ask({"do": "Revoke the old key", "warning": "w" * 301})),
    ("warning-not-string", _ask({"do": "Revoke the old key", "warning": 5})),
    ("warning-hidden", _ask({"do": "Revoke the old key", "warning": "a‮b"})),
    ("warning-tab-ok-zwsp-not", _ask({"do": "Revoke the old key", "warning": "a\tb​c"})),
    ("warning-secret", _ask({"do": "Revoke the old key", "warning": "Do not paste ghp_example here"})),
    ("warning-substep-too-long", _sub({"do": "Create the key", "warning": "w" * 301})),
    ("warning-on-verify", _ask(verify={"do": "Check it", "warning": "Takes a minute"})),
    ("blocking-and-warning-both-bad", _ask({"do": "Revoke the old key", "blocking": [], "warning": ["x"]})),
]


def _py_bytes(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def build() -> list[dict]:
    cases = []
    for name, obj in INPUTS:
        clean, errors = validate(obj)
        cases.append({"name": name, "kind": "instructable", "input": obj, "valid": not errors,
                      "errors": errors, "cleaned": clean or None, "pyBytes": _py_bytes(obj)})
    return cases


def main() -> None:
    doc = {"source": "mailbox/tests/gen_blocking_vectors.py (blocking-* and warning-* cases)",
           "cases": build()}
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
