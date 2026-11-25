"""Tests for the revision rules: step ids, the canonical body, its hash,
amendments and their summaries (protocol 5)."""
import copy
import hashlib

from mailbox.instructable import validate
from mailbox.revisions import (
    ID_RE, MAX_REV, US, assign_ids, canonical, check_note, id_paths, question_at,
    revision_hash, step_ids, stub, summarize, validate_amendment,
)

DOC = {
    "v": 1, "title": "Rotate the key", "urgency": "today",
    "steps": [
        {"do": "Open the console"},
        {"do": "Find the key", "substeps": [{"do": "Search"}, {"do": "Open it", "code": "open k"}]},
        {"do": "Rotate", "question": {"prompt": "Env?", "choices": ["Staging", "Prod"]}},
    ],
}


def _with_ids(doc=DOC):
    out, errors = assign_ids(doc)
    assert errors == []
    return out


# --- ids -------------------------------------------------------------------


def test_assign_ids_numbers_steps_then_their_substeps():
    out = _with_ids()
    assert step_ids(out) == ["s1", "s2", "s2.1", "s2.2", "s3"]
    assert id_paths(out) == {"s1": "1", "s2": "2", "s2.1": "2.1", "s2.2": "2.2", "s3": "3"}
    assert "id" not in DOC["steps"][0]                   # the input is not mutated


def test_assign_ids_keeps_a_full_set_as_given():
    doc = copy.deepcopy(DOC)
    for n, step in enumerate(doc["steps"]):
        step["id"] = f"x{n}"
        for m, sub in enumerate(step.get("substeps") or []):
            sub["id"] = f"x{n}.{m}"
    out, errors = assign_ids(doc)
    assert errors == []
    assert step_ids(out) == ["x0", "x1", "x1.0", "x1.1", "x2"]


def test_assign_ids_refuses_a_partial_set():
    doc = copy.deepcopy(DOC)
    doc["steps"][0]["id"] = "first"
    assert assign_ids(doc) == ({}, ["steps: ids must be given for every step or for none"])


def test_the_id_pattern_is_matched_whole():
    for bad in ("S1x", "a" * 25, "-s1", "s 1", ""):
        assert ID_RE.fullmatch(bad) is None, bad
    for good in ("s1", "r2-1", "s3.1", "a" * 24):
        assert ID_RE.fullmatch(good), good


def test_question_at_finds_a_question_by_id():
    out = _with_ids()
    assert question_at(out, "s3") == {"prompt": "Env?", "choices": ["Staging", "Prod"]}
    assert question_at(out, "s1") is None
    assert question_at(out, "nope") is None


# --- canonical body and hash -------------------------------------------------


def test_canonical_sorts_keys_and_keeps_non_ascii_and_slashes():
    assert canonical({"b": 1, "a": "é/"}) == '{"a":"é/","b":1}'
    assert canonical({"z": {"b": [1, {"d": 1, "c": 2}], "a": None}}) == '{"z":{"a":null,"b":[1,{"c":2,"d":1}]}}'


def test_revision_hash_matches_a_hand_computation():
    body = '{"a":1}'
    at = "2026-10-29T10:00:00+00:00"
    expected = hashlib.sha256(
        ("hook-rev-v1\x1f1\x1f\x1f2026-10-29T10:00:00+00:00\x1f\x1f" + body).encode("utf-8")
    ).hexdigest()
    assert US == "\x1f"
    assert revision_hash(1, "", at, "", body) == expected
    assert revision_hash(2, expected, at, "fix", body) != expected


# --- amendments ---------------------------------------------------------------


def _amend(new, *, current=None, used=None, rev=2, reason="the path was wrong"):
    current = current or _with_ids()
    used = set(step_ids(current)) if used is None else used
    return validate_amendment(current, new, used, rev, reason)


def test_an_amendment_cannot_change_the_title_urgency_from_or_v():
    current = _with_ids({**DOC, "from": {"session": "a", "host": "h", "cwd": "/x"}})
    new = {**copy.deepcopy(current), "title": "Other", "urgency": "now",
           "from": {"session": "b", "host": "h", "cwd": "/x"}, "v": 2}
    _, _, errors = _amend(new, current=current)
    assert errors == ["title: cannot change", "urgency: cannot change", "from: cannot change", "v: cannot change"]


def test_an_amendment_may_omit_from():
    current = _with_ids({**DOC, "from": {"session": "a", "host": "h", "cwd": "/x"}})
    new = copy.deepcopy(current)
    del new["from"]
    new["steps"][0]["do"] = "Open the admin console"
    out, summary, errors = _amend(new, current=current)
    assert errors == []
    assert out["from"] == current["from"]
    assert summary["changed"] == [{"id": "s1", "path": "1", "fields": ["do"]}]


def test_a_new_step_without_an_id_gets_a_revision_id():
    new = copy.deepcopy(_with_ids())
    new["steps"].append({"do": "Tell the team"})
    new["steps"][1]["substeps"].append({"do": "Copy its name"})
    out, summary, errors = _amend(new)
    assert errors == []
    assert step_ids(out) == ["s1", "s2", "s2.1", "s2.2", "r2-1", "s3", "r2-2"]
    assert summary["added"] == [{"id": "r2-1", "path": "2.3"}, {"id": "r2-2", "path": "4"}]


def test_a_generated_id_skips_used_ones():
    new = copy.deepcopy(_with_ids())
    new["steps"].append({"do": "Tell the team"})
    out, _, errors = _amend(new, used=set(step_ids(_with_ids())) | {"r2-1"})
    assert errors == []
    assert step_ids(out)[-1] == "r2-2"


def test_a_removed_id_can_never_come_back():
    current = _with_ids({**DOC, "steps": DOC["steps"] + [{"do": "Clean up"}]})
    new = copy.deepcopy(current)
    del new["steps"][3]
    used = set(step_ids(current))
    out, _, errors = _amend(new, current=current, used=used)
    assert errors == []
    again = copy.deepcopy(out)
    again["steps"].append({"do": "Clean up", "id": "s4"})
    _, _, errors = validate_amendment(out, again, used, 3, "put it back")
    assert errors == ["steps[3].id: used before"]


def test_a_duplicate_id_in_an_amendment_is_refused():
    new = copy.deepcopy(_with_ids())
    new["steps"][2]["id"] = "s1"
    _, _, errors = _amend(new)
    assert errors == ["steps[2].id: duplicate"]


def test_the_reason_rules():
    new = copy.deepcopy(_with_ids())
    new["steps"][0]["do"] = "Changed"
    secret = "sk-" + "ant-x"
    assert _amend(new, reason="two\nlines")[2] == ["reason: must be a single line"]
    assert _amend(new, reason=secret)[2] == ["reason: secret-like value"]
    assert _amend(new, reason=None)[2] == ["reason: missing"]
    assert _amend(new, reason="  ")[2] == ["reason: missing"]
    assert _amend(new, reason="x" * 201)[2] == ["reason: too long"]
    assert _amend(new, reason="a​b")[2] == ["reason: contains hidden or control characters (U+200B)"]
    assert _amend(new, reason="x" * 200)[2] == []


def test_check_note_allows_newlines_only_when_multi():
    assert check_note("text", "one\ntwo", 1500, multi=True) == []
    assert check_note("note", "one\ntwo", 300) == ["note: must be a single line"]
    assert check_note("note", 3, 300) == ["note: must be a string"]


def test_rev_limit_constant():
    assert MAX_REV == 21


# --- summaries -------------------------------------------------------------------


def _summary(mutate, doc=DOC):
    old = _with_ids(doc)
    new = copy.deepcopy(old)
    mutate(new)
    return summarize(old, new)


def test_summary_of_a_verify_change():
    doc = {**DOC, "verify": {"do": "Check", "code": "curl a"}}
    summary = _summary(lambda n: n["verify"].update(code="curl b"), doc)
    assert summary == {"changed": [], "added": [], "removed": [], "moved": [], "top": ["verify"]}


def test_summary_of_a_link_change():
    doc = {**DOC, "links": [{"label": "Spec", "url": "https://a.dev"}]}
    assert _summary(lambda n: n["links"][0].update(url="https://b.dev"), doc)["top"] == ["links"]


def test_summary_of_why_and_reply_changes():
    doc = {**DOC, "why": "a", "reply": {"prompt": "Ok?", "choices": []}}

    def mutate(n):
        n["why"] = "b"
        n["reply"]["prompt"] = "Fine?"
    assert _summary(mutate, doc)["top"] == ["why", "reply"]


def test_summary_of_a_removed_key():
    doc = copy.deepcopy(DOC)
    doc["steps"][1]["blocking"] = True
    summary = _summary(lambda n: n["steps"][1].pop("blocking"), doc)
    assert summary["changed"][0]["fields"] == ["blocking"]
    assert summary["changed"][0]["id"] == "s2"


def test_summary_of_a_swap():
    doc = {**DOC, "steps": DOC["steps"] + [{"do": "Clean up"}]}

    def mutate(n):
        n["steps"][2], n["steps"][3] = n["steps"][3], n["steps"][2]
    summary = _summary(mutate, doc)
    assert summary["moved"] == [{"id": "s3", "from": "3", "to": "4"}, {"id": "s4", "from": "4", "to": "3"}]
    assert summary["changed"] == []


def test_summary_of_a_substep_change_names_only_the_substep():
    summary = _summary(lambda n: n["steps"][1]["substeps"][0].update(code="ls"))
    assert summary["changed"] == [{"id": "s2.1", "path": "2.1", "fields": ["code"]}]


def test_summary_of_reordered_substeps_marks_the_parent():
    def mutate(n):
        subs = n["steps"][1]["substeps"]
        subs[0], subs[1] = subs[1], subs[0]
    summary = _summary(mutate)
    assert summary["changed"] == [{"id": "s2", "path": "2", "fields": ["substeps"]}]
    assert [m["id"] for m in summary["moved"]] == ["s2.1", "s2.2"]


def test_summary_of_a_removed_step():
    summary = _summary(lambda n: n["steps"].pop(0))
    assert summary["removed"] == [{"id": "s1", "path": "1"}]


def test_summary_of_a_question_change():
    summary = _summary(lambda n: n["steps"][2]["question"].update(choices=["Prod", "Dev"]))
    assert summary["changed"] == [{"id": "s3", "path": "3", "fields": ["question"]}]


# --- the stub ---------------------------------------------------------------------


def test_the_stub_is_a_valid_instructable():
    origin = {"session": "abc", "host": "mac", "cwd": "/tmp"}
    out = stub("Rotate the key", "now", origin)
    clean, errors = validate(out)
    assert errors == []
    assert out["from"] == origin
    assert out["steps"] == [{"do": "Update Hook: menu bar › Check for Updates…", "id": "s1"}]
    assert "from" not in stub("Rotate the key", "today", None)
    assert validate(stub("Rotate the key", "today", None))[1] == []


def test_summary_names_a_required_change_in_the_step_fields():
    summary = _summary(lambda n: n["steps"][0].update(required=True))
    assert summary["changed"] == [{"id": "s1", "path": "1", "fields": ["required"]}]
    doc = copy.deepcopy(DOC)
    doc["steps"][0]["optional"] = True
    summary = _summary(lambda n: (n["steps"][0].pop("optional"), n["steps"][0].update(required=True)), doc)
    assert summary["changed"][0]["fields"] == ["optional", "required"]


def test_summary_names_a_done_change_in_top():
    doc = {**DOC, "reply": {"prompt": "Ok?", "choices": []}}

    def mutate(n):
        n["done"] = ["reply"]
        n["reply"]["prompt"] = "Fine?"
    assert _summary(mutate, doc)["top"] == ["reply", "done"]
    assert _summary(lambda n: n.update(done=["steps"]))["top"] == ["done"]


def test_correction_changing_choice_text_is_a_question_change():
    doc = {**DOC, "steps": [*DOC["steps"][:1], {"do": "Pick", "question": {
        "prompt": "How?", "choices": ["All", {"label": "Mix", "text": "What should change?"}]}}]}

    def reword(new):
        new["steps"][1]["question"]["choices"][1]["text"] = "Which teams first?"
    summary = _summary(reword, doc)
    assert summary["changed"] == [{"id": "s2", "path": "2", "fields": ["question"]}]
