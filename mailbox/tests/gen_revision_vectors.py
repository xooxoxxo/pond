"""Write revision-vectors.json: the shared revision vectors (Pond 1.1.0), computed
by this Pond's reference code (revisions.py, answers.py, owner.py).

    python3 -m mailbox.tests.gen_revision_vectors                      # from the repo root
    python3 -m mailbox.tests.gen_revision_vectors --hook <fixtures dir>  # also into Hook's fixtures

With --hook, the same file is written as revision-vectors.json in Hook's
Tests/RelayCoreTests/Fixtures folder, so Hook's hash check, strict body parser,
answer rule and enrollment code are held to the same answers. Kinds:

    hash          {rev, prev, at, reason, body, hash}
    canonical     {input, body}: the exact body string
    assign        {input, output, errors}
    amend         {current, new, usedIds, rev, reason, output, summary, errors}
    answerRule    {revisions: [{rev, body}], inForce, events, counted, earlier}
    enrollCode    {id, keyHash, code}
    keyHash       {secret, hash}
    stub          {title, urgency, from, output}
    duplicateKey  {raw, valid: false}
"""
import argparse
import copy
import json
from pathlib import Path
from typing import Any, Callable, Optional

from mailbox import owner
from mailbox.answers import current as current_answers
from mailbox.instructable import no_duplicates, validate
from mailbox.revisions import assign_ids, canonical, revision_hash, step_ids, stub, validate_amendment

OUT = Path(__file__).parent / "revision-vectors.json"
NAME = "revision-vectors.json"
KINDS = ("hash", "canonical", "assign", "amend", "answerRule", "enrollCode", "keyHash", "stub", "duplicateKey")

AT1 = "2026-10-29T10:00:00+00:00"
AT2 = "2026-10-29T10:05:00+00:00"
ORIGIN = {"session": "0c1d2e3f-4a5b-4c6d-8e7f-001122334455", "host": "studio", "cwd": "~/code/app"}

DOC: dict = {
    "v": 1, "title": "Deploy the release", "urgency": "today",
    "why": "The release is tagged and waiting.",
    "steps": [
        {"do": "Build it", "code": "make build"},
        {"do": "Check the logs", "substeps": [{"do": "Open the dashboard"}, {"do": "Look for errors"}]},
        {"do": "Pick the target", "code": "deploy --env X",
         "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
    ],
    "verify": {"do": "The site answers", "code": "curl -sI https://example.com"},
}


def _ids(doc: dict = DOC) -> dict:
    out, errors = assign_ids(doc)
    assert errors == [], errors
    return out


def _change(fn: Callable[[dict], None], doc: Optional[dict] = None) -> dict:
    out = copy.deepcopy(doc if doc is not None else _ids())
    fn(out)
    return out


# --- hash ----------------------------------------------------------------------------

BODY1 = canonical(_ids())
HASH1 = revision_hash(1, "", AT1, "", BODY1)
HASHES: list[tuple[str, int, str, str, str, str]] = [
    ("hash-rev1", 1, "", AT1, "", BODY1),
    ("hash-rev2", 2, HASH1, AT2, "The deploy flag was wrong", canonical(_change(
        lambda d: d["steps"][2].update(code="deploy --env Y")))),
    ("hash-minimal-body", 1, "", AT1, "", '{"a":1}'),
    ("hash-non-ascii-reason", 2, HASH1, AT2, "Düzeltme: port 8443 → 9443", '{"title":"Gör"}'),
    ("hash-non-ascii-body", 1, "", AT1, "", canonical({"v": 1, "title": "Café ☕ 日本", "steps": [{"do": "é"}]})),
    ("hash-slash-and-escapes", 1, "", AT1, "", canonical({"code": "a/b \"q\" \\ \n\t"})),
    ("hash-line-separator", 1, "", AT1, "", canonical({"do": "a b c"})),
    ("hash-emoji-body", 3, "f" * 64, "2026-10-29T23:59:59+00:00", "Emoji 👍🏽 fix", canonical({"do": "👩‍💻"})),
    ("hash-rev11", 11, "0" * 64, AT2, "The last correction", '{"v":1}'),
    ("hash-empty-body", 1, "", AT1, "", ""),
]

# --- canonical -------------------------------------------------------------------------

CANONICAL: list[tuple[str, Any]] = [
    ("canonical-sorted-keys", {"b": 1, "a": 2, "c": 3}),
    ("canonical-nested-key-order", {"z": {"b": [1, {"d": 1, "c": 2}], "a": None}, "a": {"y": True, "x": False}}),
    ("canonical-array-order-kept", {"list": [3, 1, 2, "b", "a"]}),
    ("canonical-non-ascii-kept", {"title": "Gör ölçü", "why": "Café 日本"}),
    ("canonical-slash-not-escaped", {"url": "https://example.com/a/b?c=d&e=f"}),
    ("canonical-quotes-and-backslashes", {"code": "echo \"$HOME\" \\ done"}),
    ("canonical-control-escapes", {"code": "a\nb\tc\r\u0001\u001f"}),
    ("canonical-line-separators-raw", {"do": "a b c"}),
    ("canonical-emoji-and-joiners", {"do": "👩‍💻 ✔️ 🇹🇷"}),
    ("canonical-numbers-and-literals", {"n": 0, "m": -12, "big": 12345678901234, "t": True, "f": False, "z": None}),
    ("canonical-empty-containers", {"a": {}, "b": [], "c": ""}),
    ("canonical-instructable-with-ids", _ids()),
]

# --- assign ----------------------------------------------------------------------------

TWENTY = {"v": 1, "title": "Twenty steps", "steps": [{"do": f"Step {n}"} for n in range(1, 21)]}
ASSIGN: list[tuple[str, dict]] = [
    ("assign-none-given", DOC),
    ("assign-full-set-kept", _change(lambda d: [s.update(id=f"x{n}") for n, s in enumerate(d["steps"])]
                                     or [sub.update(id=f"x1.{m}") for m, sub in enumerate(d["steps"][1]["substeps"])],
                                     copy.deepcopy(DOC))),
    ("assign-partial-refused", _change(lambda d: d["steps"][0].update(id="first"), copy.deepcopy(DOC))),
    ("assign-substep-missing-refused", _change(
        lambda d: [s.update(id=f"x{n}") for n, s in enumerate(d["steps"])], copy.deepcopy(DOC))),
    ("assign-twenty-steps", TWENTY),
    ("assign-verify-gets-none", {"v": 1, "title": "One", "steps": [{"do": "a"}], "verify": {"do": "check"}}),
    ("assign-null-ids-are-absent", {"v": 1, "title": "Nulls", "steps": [{"do": "a", "id": None}, {"do": "b"}]}),
]

# --- amend -----------------------------------------------------------------------------

REV1 = _ids()
USED1 = step_ids(REV1)


def _without_from(d: dict) -> None:
    d.pop("from", None)


AMEND: list[tuple[str, dict, dict, list[str], int, Any]] = [
    ("amend-code-change", REV1, _change(lambda d: d["steps"][2].update(code="deploy --env Y")), USED1, 2,
     "The deploy flag was wrong"),
    ("amend-new-step-gets-rev-id", REV1, _change(lambda d: d["steps"].insert(1, {"do": "Tag it"})), USED1, 2,
     "A step was missing"),
    ("amend-two-new-steps", REV1, _change(lambda d: d["steps"].extend([{"do": "Tell the team"}, {"do": "Close"}])),
     USED1, 2, "Two steps were missing"),
    ("amend-new-id-skips-a-used-one", _change(lambda d: d["steps"].append({"do": "Old", "id": "r3-1"})),
     _change(lambda d: d["steps"].append({"do": "New"}), _change(lambda d: d["steps"].append(
         {"do": "Old", "id": "r3-1"}))), USED1 + ["r3-1"], 3, "Another step"),
    ("amend-step-removed", REV1, _change(lambda d: d["steps"].pop(0)), USED1, 2, "Already built"),
    ("amend-steps-moved", REV1, _change(lambda d: d["steps"].reverse()), USED1, 2, "Wrong order"),
    ("amend-substep-list-changed", REV1, _change(lambda d: d["steps"][1]["substeps"].pop()), USED1, 2,
     "One check is enough"),
    ("amend-inside-a-substep", REV1, _change(lambda d: d["steps"][1]["substeps"][0].update(where="Grafana")), USED1,
     2, "Name the dashboard"),
    ("amend-question-changed", REV1, _change(lambda d: d["steps"][2]["question"]["choices"].append("Dev")), USED1, 2,
     "Dev is a target too"),
    ("amend-top-keys", REV1, _change(lambda d: d.update(why="It is urgent now.", verify={"do": "Check again"},
                                                        links=[{"label": "Runbook", "url": "https://example.com/rb"}])),
     USED1, 2, "Better context"),
    ("amend-several-fields", REV1, _change(lambda d: d["steps"][0].update(
        expect="build ok", lang="sh", detail="Takes a minute.", warning="Uses the CI cache.")), USED1, 2, "More detail"),
    ("amend-from-filled-in", _change(lambda d: d.update({"from": ORIGIN})),
     _change(lambda d: d["steps"][0].update(code="make all")), USED1, 2, "Build everything"),
    ("amend-from-changed", _change(lambda d: d.update({"from": ORIGIN})),
     _change(lambda d: d.update({"from": {**ORIGIN, "host": "other"}})), USED1, 2, "Another host"),
    ("amend-title-cannot-change", REV1, _change(lambda d: d.update(title="Deploy it now")), USED1, 2, "Clearer"),
    ("amend-urgency-cannot-change", REV1, _change(lambda d: d.update(urgency="now")), USED1, 2, "Sooner"),
    ("amend-id-used-before", _change(lambda d: d["steps"].pop(0)), _change(lambda d: None), USED1, 3,
     "Bring it back"),
    ("amend-id-duplicate", REV1, _change(lambda d: d["steps"][1].update(id="s1")), USED1, 2, "Same id"),
    ("amend-id-bad-pattern", REV1, _change(lambda d: d["steps"][0].update(id="S1x")), USED1, 2, "Upper case"),
    ("amend-id-step-number", REV1, _change(lambda d: d["steps"][0].update(id="2")), USED1, 2, "A number"),
    ("amend-reason-missing", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2, ""),
    ("amend-reason-too-long", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2, "r" * 201),
    ("amend-reason-200", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2, "r" * 200),
    ("amend-reason-two-lines", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2, "one\ntwo"),
    ("amend-reason-hidden", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2, "a‮b"),
    ("amend-reason-secret", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2,
     "use ghp_example now"),
    ("amend-reason-not-string", REV1, _change(lambda d: d["steps"][0].update(code="make")), USED1, 2, 5),
    ("amend-no-change", REV1, _change(lambda d: None), USED1, 2, "Nothing really"),
]


def _amend_case(name: str, current: dict, new: dict, used: list[str], rev: int, reason: Any) -> dict:
    """The Pond's pure part of an amend, in its order: the instructable (new ids allowed),
    then the amendment rules, then "no change" when the canonical bodies are equal."""
    clean, errors = validate(new, new_ids=True)
    output, summary = None, None
    if not errors:
        out, summ, errors = validate_amendment(current, clean, set(used), rev, reason)
        if not errors and canonical(out) == canonical(current):
            errors = ["no change"]
        if not errors:
            output, summary = out, summ
    return {"name": name, "kind": "amend", "current": current, "new": new, "usedIds": used, "rev": rev,
            "reason": reason, "output": output, "summary": summary, "errors": errors}


# --- answerRule ------------------------------------------------------------------------


def _answer(seq: int, body: dict, rev: Optional[int], actor: str = "owner", kind: str = "answer") -> dict:
    return {"seq": seq, "kind": kind, "actor": actor, "at": f"2026-10-29T11:00:{seq:02d}+00:00",
            "text": json.dumps(body, ensure_ascii=False, separators=(",", ":")), "rev": rev}


PROD = {"step": "3", "picked": ["Prod"]}
RULE_REV2_CHOICES = _change(lambda d: d["steps"][2]["question"]["choices"].append("Dev"))
RULE_REV2_CODE = _change(lambda d: d["steps"][2].update(code="deploy --env Z"))
RULE_REV2_MOVED = _change(lambda d: d["steps"].insert(0, {"do": "New first step", "id": "r2-1"}))
RULE_REV2_GONE = _change(lambda d: d["steps"].pop(2))
RULE_FREE = _change(lambda d: d["steps"][0].update(question={"prompt": "Notes?", "choices": []}))

ANSWER_RULES: list[tuple[str, dict[int, dict], int, list[dict]]] = [
    ("answer-rev1-counted", {1: REV1}, 1, [_answer(1, PROD, None)]),
    ("answer-rev1-explicit-rev", {1: REV1}, 1, [_answer(1, PROD, 1)]),
    ("answer-choices-changed-earlier", {1: REV1, 2: RULE_REV2_CHOICES}, 2, [_answer(1, PROD, None)]),
    ("answer-code-only-change-counted", {1: REV1, 2: RULE_REV2_CODE}, 2, [_answer(1, PROD, None)]),
    ("answer-follows-a-moved-step", {1: REV1, 2: RULE_REV2_MOVED}, 2, [_answer(1, PROD, 1)]),
    ("answer-by-id-after-a-move", {1: REV1, 2: RULE_REV2_MOVED}, 2,
     [_answer(1, {"step": "4", "id": "s3", "picked": ["Staging"]}, 2)]),
    ("answer-step-gone-earlier", {1: REV1, 2: RULE_REV2_GONE}, 2, [_answer(1, PROD, 1)]),
    ("answer-accepted-still-rev1", {1: REV1, 2: RULE_REV2_CHOICES}, 1, [_answer(1, PROD, 1)]),
    ("answer-latest-wins", {1: REV1}, 1, [_answer(1, PROD, None), _answer(2, {"step": "3", "picked": ["Staging"]}, 1)]),
    ("answer-latest-wins-by-seq", {1: REV1}, 1,
     [_answer(5, {"step": "3", "picked": ["Staging"]}, 1), _answer(2, PROD, 1)]),
    ("answer-reanswered-after-change", {1: REV1, 2: RULE_REV2_CHOICES}, 2,
     [_answer(1, PROD, 1), _answer(2, {"step": "3", "picked": ["Dev"]}, 2)]),
    ("answer-session-events-ignored", {1: REV1}, 1,
     [_answer(1, PROD, 1, actor="session"), _answer(2, PROD, 1, kind="reply")]),
    ("answer-unreadable-text-skipped", {1: REV1}, 1,
     [{"seq": 1, "kind": "answer", "actor": "owner", "at": "t", "text": "not json", "rev": 1}]),
    ("answer-unknown-path-skipped", {1: REV1}, 1, [_answer(1, {"step": "9", "picked": ["Prod"]}, 1)]),
    ("answer-free-text", {1: RULE_FREE}, 1, [_answer(1, {"step": "1", "text": "All good"}, 1), _answer(2, PROD, 1)]),
]


def _rule_case(name: str, revs: dict[int, dict], in_force: int, events: list[dict]) -> dict:
    counted, earlier = current_answers(events, revs, revs[in_force])
    return {"name": name, "kind": "answerRule",
            "revisions": [{"rev": rev, "body": canonical(body)} for rev, body in sorted(revs.items())],
            "inForce": in_force, "events": events, "counted": counted, "earlier": earlier}


# --- enrollCode, keyHash, stub, duplicateKey ---------------------------------------------

KEYS = [
    ("key-edit-key", "Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5"),
    ("key-owner-key", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    ("key-dashes-underscores", "-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-"),
    ("key-short", "k"),
    ("key-non-ascii", "anahtar-ğüşiöç"),
]
ENROLLS = [
    ("enroll-1", "6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5", owner.key_hash(KEYS[1][1])),
    ("enroll-2", "00000000-0000-4000-8000-000000000000", owner.key_hash(KEYS[0][1])),
    ("enroll-3", "ffffffff-ffff-4fff-bfff-ffffffffffff", "0" * 64),
    ("enroll-4", "6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5", "f" * 64),
    ("enroll-5", "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", owner.key_hash(KEYS[2][1])),
]
STUBS = [
    ("stub-with-from", "Deploy the release", "today", ORIGIN),
    ("stub-without-from", "Rotate the key", "now", None),
    ("stub-whenever-non-ascii", "Gör ölçü ☕", "whenever", {"host": "studio"}),
]
DUPLICATES = [
    ("dup-top-level", '{"v":1,"title":"a","title":"b","steps":[{"do":"x"}]}'),
    ("dup-same-value", '{"v":1,"v":1,"title":"a","steps":[{"do":"x"}]}'),
    ("dup-in-a-step", '{"v":1,"title":"a","steps":[{"do":"x","code":"ls","code":"rm -rf ~"}]}'),
    ("dup-in-a-substep", '{"v":1,"title":"a","steps":[{"do":"x","substeps":[{"do":"y","do":"z"}]}]}'),
    ("dup-step-id", '{"v":1,"title":"a","steps":[{"do":"x","id":"s1","id":"s2"}]}'),
    ("dup-escaped-key", '{"v":1,"title":"a","\\u0074itle":"b","steps":[{"do":"x"}]}'),
    ("dup-in-verify", '{"v":1,"title":"a","steps":[{"do":"x"}],"verify":{"do":"c","do":"d"}}'),
    ("dup-null-and-value", '{"v":1,"title":"a","why":null,"why":"run it","steps":[{"do":"x"}]}'),
]


def build() -> list[dict]:
    cases: list[dict] = []
    for name, rev, prev, at, reason, body in HASHES:
        cases.append({"name": name, "kind": "hash", "rev": rev, "prev": prev, "at": at, "reason": reason,
                      "body": body, "hash": revision_hash(rev, prev, at, reason, body)})
    for name, obj in CANONICAL:
        cases.append({"name": name, "kind": "canonical", "input": obj, "body": canonical(obj)})
    for name, obj in ASSIGN:
        output, errors = assign_ids(obj)
        cases.append({"name": name, "kind": "assign", "input": obj, "output": output or None, "errors": errors})
    for args in AMEND:
        cases.append(_amend_case(*args))
    for args in ANSWER_RULES:
        cases.append(_rule_case(*args))
    for name, enroll_id, key_hash in ENROLLS:
        cases.append({"name": name, "kind": "enrollCode", "id": enroll_id, "keyHash": key_hash,
                      "code": owner.enroll_code(enroll_id, key_hash)})
    for name, secret in KEYS:
        cases.append({"name": name, "kind": "keyHash", "secret": secret, "hash": owner.key_hash(secret)})
    for name, title, urgency, origin in STUBS:
        cases.append({"name": name, "kind": "stub", "title": title, "urgency": urgency, "from": origin,
                      "output": stub(title, urgency, origin)})
    for name, raw in DUPLICATES:
        try:
            json.loads(raw, object_pairs_hook=no_duplicates)
        except ValueError:
            pass
        else:
            raise SystemExit(f"gen_revision_vectors: {name} parses strictly")
        cases.append({"name": name, "kind": "duplicateKey", "raw": raw, "valid": False})
    names = [c["name"] for c in cases]
    if not cases or len(set(names)) != len(names):
        raise SystemExit("gen_revision_vectors: no cases, or a duplicate case name")
    if {c["kind"] for c in cases} != set(KINDS):
        raise SystemExit("gen_revision_vectors: a kind has no cases")
    return cases


def dump(cases: list[dict]) -> str:
    return json.dumps({"version": 1, "cases": cases}, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", type=Path, help="Hook's Tests/RelayCoreTests/Fixtures folder to write into too")
    args = parser.parse_args()
    text = dump(build())
    OUT.write_text(text, encoding="utf-8")
    if args.hook:
        if not args.hook.is_dir():
            raise SystemExit(f"gen_revision_vectors: {args.hook} is not a folder")
        (args.hook / NAME).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
