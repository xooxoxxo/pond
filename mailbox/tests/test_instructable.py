"""Tests for the instructable v1 validator."""
import json
from pathlib import Path

import pytest

from mailbox.instructable import validate

MINIMAL = {"v": 1, "title": "Create the Slack channel", "steps": [{"do": "Open Slack"}]}


def test_a_minimal_instructable_is_valid_and_defaults_the_urgency():
    clean, errors = validate(MINIMAL)
    assert errors == []
    assert clean == {**MINIMAL, "urgency": "today"}
    assert "urgency" not in MINIMAL                     # the input is not mutated


def test_unknown_keys_are_rejected_with_their_path():
    _, errors = validate({**MINIMAL, "colour": "red", "steps": [{"do": "x", "wher": "Slack"}]})
    assert errors == ["colour: unknown key", "steps[0].wher: unknown key"]


def test_each_limit_is_enforced():
    cases = [
        ({**MINIMAL, "v": 2}, "v: must be 1"),
        ({**MINIMAL, "title": "x" * 121}, "title: longer than 120 characters"),
        ({**MINIMAL, "title": "two\nlines"}, "title: must be a single line"),
        ({**MINIMAL, "urgency": "later"}, "urgency: must be one of now, today, whenever"),
        ({**MINIMAL, "steps": []}, "steps: must be a list of 1 to 20 steps"),
        ({**MINIMAL, "steps": [{"do": "x"}] * 21}, "steps: must be a list of 1 to 20 steps"),
        ({**MINIMAL, "steps": [{"do": "x", "code": "y" * 4001}]},
         "steps[0].code: longer than 4000 characters"),
        ({**MINIMAL, "reply": {"prompt": "Which?", "choices": ["a"] * 7}},
         "reply.choices: must be a list of at most 6 choices"),
        ({**MINIMAL, "links": [{"label": "l", "url": "https://x.dev"}] * 11},
         "links: must be a list of at most 10 links"),
    ]
    for obj, expected in cases:
        assert validate(obj) == ({}, [expected])


def test_urls_must_be_http():
    _, errors = validate({
        **MINIMAL,
        "steps": [{"do": "Open it", "url": "javascript:alert(1)"}],
        "links": [{"label": "Spec", "url": "ftp://example.invalid/spec"}],
    })
    assert errors == ["steps[0].url: must be an http(s) URL", "links[0].url: must be an http(s) URL"]


def test_a_secret_like_value_is_named_by_path_and_never_echoed():
    secret = "sk-" + "ant-" + "api03-" + "Q" * 32       # built at runtime: no literal key in the repo
    _, errors = validate({**MINIMAL, "steps": [{"do": "a"}, {"do": "b", "code": f"export KEY={secret}"}]})
    assert errors == ["steps[1].code: secret-like value"]
    assert all(secret not in e for e in errors)


def test_an_instructable_over_32_kb_is_rejected_whole():
    big = {**MINIMAL, "steps": [{"do": "x", "code": "y" * 3000}] * 20}
    assert validate(big) == ({}, ["instructable: larger than 32768 bytes"])


def test_a_hidden_character_in_code_is_named_by_path_and_code_point_not_value():
    code = "rm -rf ./build\u202e# safe"
    _, errors = validate({**MINIMAL, "steps": [{"do": "Run it", "code": code}]})
    assert errors == ["steps[0].code: contains hidden or control characters (U+202E)"]
    assert all(code not in e and "rm -rf" not in e for e in errors)


def test_a_newline_in_the_title_breaks_the_line_rule():
    _, errors = validate({**MINIMAL, "title": "Create\nthe channel"})
    assert errors == ["title: must be a single line"]


def test_a_family_emoji_with_zwj_is_accepted_in_prose():
    family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
    clean, errors = validate({**MINIMAL, "title": f"Add {family} to the plan"})
    assert errors == []
    assert clean["title"] == f"Add {family} to the plan"


def test_a_tag_character_in_why_is_rejected():
    _, errors = validate({**MINIMAL, "why": "Because\U000E0041 it matters"})
    assert errors == ["why: contains hidden or control characters (U+E0041)"]


def test_a_lone_surrogate_is_rejected_as_hidden():
    _, errors = validate({**MINIMAL, "steps": [{"do": "run it", "code": "echo \ud800"}]})
    assert errors == ["steps[0].code: contains hidden or control characters (U+D800)"]


def test_a_secret_split_by_a_prose_allowed_joiner_or_in_fullwidth_is_caught():
    token = "gh" + "‍" + "p_" + "A" * 36
    fullwidth = "ｇｈｐ＿" + "A" * 36       # NFKC → ghp_…
    _, errors = validate({**MINIMAL, "steps": [{"do": f"use {token}"}], "why": fullwidth})
    assert errors == ["steps[0].do: secret-like value", "why: secret-like value"]


def test_invisible_format_and_default_ignorable_code_points_are_rejected_in_code():
    for cp in (0x1D173, 0xFFF0, 0x1BCA0, 0x0600, 0xE0080):
        _, errors = validate({**MINIMAL, "steps": [{"do": "x", "code": f"ls{chr(cp)} -la"}]})
        assert errors == [f"steps[0].code: contains hidden or control characters (U+{cp:04X})"]


# --- step detail (Relay 0.4.0) ----------------------------------------------


def test_a_multiline_detail_is_accepted():
    detail = "First open the console.\n\tThen pick the project.\n" + "x" * 1400
    obj = {**MINIMAL, "steps": [{"do": "Open it", "detail": detail[:1500]}]}
    clean, errors = validate(obj)
    assert errors == []
    assert clean["steps"][0]["detail"] == detail[:1500]


def test_a_detail_over_1500_characters_is_refused_by_path():
    obj = {**MINIMAL, "steps": [{"do": "a"}, {"do": "b", "detail": "y" * 1501}]}
    assert validate(obj) == ({}, ["steps[1].detail: longer than 1500 characters"])


def test_a_detail_on_verify_follows_the_same_rules():
    obj = {**MINIMAL, "verify": {"do": "check", "detail": "y" * 1501}}
    assert validate(obj) == ({}, ["verify.detail: longer than 1500 characters"])


def test_a_secret_like_detail_is_refused_and_never_echoed():
    secret = "gh" + "p_" + "Z" * 36
    _, errors = validate({**MINIMAL, "steps": [{"do": "a", "detail": f"paste {secret} here"}]})
    assert errors == ["steps[0].detail: secret-like value"]
    assert all(secret not in e for e in errors)


def test_a_hidden_character_in_detail_is_refused_by_path_and_code_point():
    _, errors = validate({**MINIMAL, "steps": [{"do": "a", "detail": "safe‮ text"}]})
    assert errors == ["steps[0].detail: contains hidden or control characters (U+202E)"]


def test_a_detail_keeps_the_emoji_allowance_of_prose():
    _, errors = validate({**MINIMAL, "steps": [{"do": "a", "detail": "done 👍🏽‍ ok ️"}]})
    assert errors == []


# --- sub-steps (Relay 0.6.0, design §7.2) -------------------------------------

SUBS = [
    {"do": "Click Create New App, then From scratch", "url": "https://api.slack.com/apps"},
    {"do": "Name it Acme and pick your workspace"},
    {"do": "Under OAuth & Permissions, add the chat:write scope", "code": "chat:write", "lang": "text"},
]
PARENT = {"do": "Create the Slack app for Acme", "where": "api.slack.com", "substeps": SUBS}


def _three(third):
    return {**MINIMAL, "steps": [{"do": "a"}, {"do": "b"}, third]}


def test_a_step_with_three_substeps_is_valid():
    clean, errors = validate({**MINIMAL, "steps": [PARENT]})
    assert errors == []
    assert clean["steps"][0]["substeps"] == SUBS


def test_substeps_must_be_a_list_of_1_to_10():
    expected = ["steps[2].substeps: must be a list of 1 to 10 steps"]
    assert validate(_three({"do": "c", "substeps": []})) == ({}, expected)
    assert validate(_three({"do": "c", "substeps": [{"do": "x"}] * 11})) == ({}, expected)
    assert validate(_three({"do": "c", "substeps": {"do": "x"}})) == ({}, expected)


def test_null_substeps_count_as_absent():
    assert validate({**MINIMAL, "steps": [{"do": "a", "substeps": None}]})[1] == []


def test_substeps_cannot_nest():
    nested = {"do": "c", "substeps": [{"do": "x", "substeps": [{"do": "deeper"}]}]}
    assert validate(_three(nested)) == ({}, ["steps[2].substeps[0].substeps: unknown key"])


def test_verify_cannot_have_substeps():
    obj = {**MINIMAL, "verify": {"do": "check", "substeps": [{"do": "x"}]}}
    assert validate(obj) == ({}, ["verify.substeps: unknown key"])


def test_an_unknown_key_in_a_substep_is_named_by_path():
    obj = {**MINIMAL, "steps": [{"do": "a", "substeps": [{"do": "x"}, {"do": "y", "colour": "red"}]}]}
    assert validate(obj) == ({}, ["steps[0].substeps[1].colour: unknown key"])


def test_a_newline_in_a_substep_do_breaks_the_line_rule():
    obj = {**MINIMAL, "steps": [{"do": "a", "substeps": [{"do": "two\nlines"}]}]}
    assert validate(obj) == ({}, ["steps[0].substeps[0].do: must be a single line"])


def test_substep_field_errors_carry_the_substep_path():
    third = {"do": "c", "substeps": [{"do": "x", "url": "ftp://example.invalid"}, {"do": "y" * 301}]}
    assert validate(_three(third)) == ({}, [
        "steps[2].substeps[0].url: must be an http(s) URL",
        "steps[2].substeps[1].do: longer than 300 characters",
    ])


def test_sixty_steps_and_substeps_are_valid_and_sixty_one_are_refused():
    steps = [{"do": f"step {i}"} for i in range(20)]
    for i in range(4):
        steps[i] = {"do": f"parent {i}", "substeps": [{"do": f"sub {i}.{j}"} for j in range(10)]}
    assert validate({**MINIMAL, "steps": steps})[1] == []                      # 20 + 40 = 60
    steps[4] = {"do": "parent 4", "substeps": [{"do": "one more"}]}
    assert validate({**MINIMAL, "steps": steps}) == ({}, ["steps: more than 60 steps and sub-steps in total"])


def test_out_of_range_substep_lists_still_count_toward_the_total():
    # Same count as Relay's validator: every list counts, even one that is out of range.
    steps = [{"do": f"s{i}", "substeps": [{"do": "x"}] * 11} for i in range(6)]   # 6 + 66 = 72
    assert validate({**MINIMAL, "steps": steps}) == ({}, [
        *(f"steps[{i}].substeps: must be a list of 1 to 10 steps" for i in range(6)),
        "steps: more than 60 steps and sub-steps in total",
    ])
    # A value that isn't a list counts as 0.
    steps = [{"do": f"s{i}", "substeps": "x" * 100} for i in range(20)]
    assert "steps: more than 60 steps and sub-steps in total" not in validate({**MINIMAL, "steps": steps})[1]


def test_a_secret_inside_a_substep_code_is_named_by_path():
    secret = "gh" + "p_" + "Q" * 36                   # built at runtime: no literal token in the repo
    subs = [{"do": "x"}, {"do": "y"}, {"do": "z", "code": f"export T={secret}"}]
    _, errors = validate({**MINIMAL, "steps": [{"do": "a", "substeps": subs}]})
    assert errors == ["steps[0].substeps[2].code: secret-like value"]
    assert all(secret not in e for e in errors)


_VECTORS = json.loads((Path(__file__).parent / "substeps-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _VECTORS, ids=[c["name"] for c in _VECTORS])
def test_substeps_vectors_match_relays_validator(case):
    """The substeps-* cases copied from Relay's shared validator vectors."""
    clean, errors = validate(case["input"])
    assert (errors == []) == case["valid"]
    assert sorted(errors) == sorted(case["errors"])
    assert errors == case["errors"]
    assert (clean or None) == case["cleaned"]


def test_substeps_vectors_are_all_present():
    assert len(_VECTORS) == 14


_QUESTIONS = json.loads((Path(__file__).parent / "questions-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _QUESTIONS, ids=[c["name"] for c in _QUESTIONS])
def test_questions_vectors_match_relays_validator(case):
    """The questions-* cases shared with Relay's validator vectors."""
    clean, errors = validate(case["input"])
    assert errors == case["errors"]
    assert (clean or None) == case["cleaned"]


def test_questions_vectors_are_all_present():
    assert len(_QUESTIONS) == 40


_BLOCKING = json.loads((Path(__file__).parent / "blocking-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _BLOCKING, ids=[c["name"] for c in _BLOCKING])
def test_blocking_vectors_match_the_reference(case):
    """The blocking-* and warning-* cases shared with Hook's validator vectors."""
    clean, errors = validate(case["input"])
    assert errors == case["errors"]
    assert (errors == []) == case["valid"]
    assert (clean or None) == case["cleaned"]


def test_blocking_vectors_are_all_present_and_current():
    from mailbox.tests.gen_blocking_vectors import build
    assert len(_BLOCKING) == 23
    assert build() == _BLOCKING                        # rerun the generator after a rule change


_OPTIONAL = json.loads((Path(__file__).parent / "optional-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _OPTIONAL, ids=[c["name"] for c in _OPTIONAL])
def test_optional_vectors_match_the_reference(case):
    """The optional-* and file-* cases shared with Hook's validator vectors."""
    clean, errors = validate(case["input"])
    assert errors == case["errors"]
    assert (errors == []) == case["valid"]
    assert (clean or None) == case["cleaned"]


def test_optional_vectors_are_all_present_and_current():
    from mailbox.tests.gen_optional_vectors import build
    assert len(_OPTIONAL) == 141
    assert sum(c["name"].startswith("optional-") for c in _OPTIONAL) == 30
    assert sum(c["name"].startswith("file-") for c in _OPTIONAL) == 111
    assert build() == _OPTIONAL                        # rerun the generator after a rule change


# --- step ids (protocol 5) ---------------------------------------------------


def test_step_ids_are_allowed_on_steps_and_substeps():
    doc = {**MINIMAL, "steps": [{"do": "a", "id": "s1"},
                                {"do": "b", "id": "s2", "substeps": [{"do": "c", "id": "s2.1"}]}]}
    clean, errors = validate(doc)
    assert errors == []
    assert clean["steps"][1]["substeps"][0]["id"] == "s2.1"


def test_verify_id_is_an_unknown_key():
    _, errors = validate({**MINIMAL, "verify": {"do": "Check", "id": "v1"}})
    assert errors == ["verify.id: unknown key"]


def test_step_id_shape_errors():
    cases = [
        ({"do": "a", "id": "S1"}, "steps[0].id: must match [a-z0-9][a-z0-9.-]{0,23}"),
        ({"do": "a", "id": "s1x" + "a" * 24}, "steps[0].id: must match [a-z0-9][a-z0-9.-]{0,23}"),
        ({"do": "a", "id": 3}, "steps[0].id: must be a string"),
    ]
    for step, expected in cases:
        assert validate({**MINIMAL, "steps": [step]}) == ({}, [expected])


def test_a_step_id_that_reads_as_a_step_number_is_refused():
    """`step` in an answer or a problem may be a path or an id; an id such as "2"
    would be read as the path of another step."""
    for bad in ("1", "2", "3.2", "10", "0", "01", "20.10"):
        doc = {**MINIMAL, "steps": [{"do": "a", "id": bad}]}
        assert validate(doc) == ({}, ["steps[0].id: must not look like a step number"]), bad
        assert validate(doc, new_ids=True)[1] == ["steps[0].id: must not look like a step number"], bad
    sub = {**MINIMAL, "steps": [{"do": "a", "id": "s1", "substeps": [{"do": "b", "id": "1.1"}]}]}
    assert validate(sub)[1] == ["steps[0].substeps[0].id: must not look like a step number"]
    for good in ("s2", "1a", "2-1", "1.2.3", "v1.2", "r2-1"):
        assert validate({**MINIMAL, "steps": [{"do": "a", "id": good}]})[1] == [], good


def test_duplicate_step_ids_are_refused_in_document_order():
    doc = {**MINIMAL, "steps": [{"do": "a", "id": "s1"},
                                {"do": "b", "id": "s2", "substeps": [{"do": "c", "id": "s1"}]},
                                {"do": "d", "id": "s2"}]}
    assert validate(doc)[1] == ["steps[1].substeps[0].id: duplicate", "steps[2].id: duplicate"]


def test_step_ids_are_all_or_none():
    doc = {**MINIMAL, "steps": [{"do": "a", "id": "s1"}, {"do": "b"}]}
    assert validate(doc)[1] == ["steps: ids must be given for every step or for none"]
    sub = {**MINIMAL, "steps": [{"do": "a", "id": "s1", "substeps": [{"do": "c"}]}]}
    assert validate(sub)[1] == ["steps: ids must be given for every step or for none"]
    # An amendment may leave new steps without ids: they are numbered then.
    assert validate(doc, new_ids=True)[1] == []


def test_no_duplicates_refuses_a_repeated_key():
    from mailbox.instructable import no_duplicates
    assert json.loads('{"a":1,"b":2}', object_pairs_hook=no_duplicates) == {"a": 1, "b": 2}
    with pytest.raises(ValueError):
        json.loads('{"a":1,"a":2}', object_pairs_hook=no_duplicates)
    with pytest.raises(ValueError):
        json.loads('{"x":{"a":1,"a":1}}', object_pairs_hook=no_duplicates)


# --- required and done (Pond 1.3.0) -------------------------------------------

TWO = [{"do": "Open the console"}, {"do": "Rotate the key"}]


def _req(*steps, **top):
    return {**MINIMAL, "steps": list(steps) or TWO, **top}


def test_required_is_allowed_on_a_leaf_a_parent_a_substep_and_a_question():
    for doc in (
        _req({"do": "a", "required": True}),
        _req({"do": "a", "required": False}),
        _req({"do": "a", "required": None}),
        _req({"do": "a", "required": True, "substeps": [{"do": "b"}]}),
        _req({"do": "a", "substeps": [{"do": "b", "required": True}]}),
        _req({"do": "a", "required": True, "question": {"prompt": "Env?", "choices": []}}),
        _req({"do": "a", "required": True, "blocking": True}),
    ):
        assert validate(doc)[1] == [], doc


def test_required_must_be_a_bool():
    for bad in ("true", 1, [], {}):
        assert validate(_req({"do": "a", "required": bad}))[1] == [
            "steps[0].required: must be true or false"]
    assert validate(_req({"do": "a", "substeps": [{"do": "b", "required": 0}]}))[1] == [
        "steps[0].substeps[0].required: must be true or false"]


def test_required_and_optional_are_refused_together():
    assert validate(_req({"do": "a", "required": True, "optional": True}))[1] == [
        "steps[0].required: can't be required and optional"]
    assert validate(_req({"do": "a", "required": False, "optional": True}))[1] == []


def test_required_inside_an_optional_parent_is_refused():
    doc = _req({"do": "a", "optional": True, "substeps": [{"do": "b"}, {"do": "c", "required": True}]})
    assert validate(doc)[1] == ["steps[0].substeps[1].required: can't be required inside an optional step"]


def test_required_on_verify_is_an_unknown_key():
    assert validate(_req(verify={"do": "Check", "required": True}))[1] == ["verify.required: unknown key"]


def test_required_on_a_parent_with_nothing_to_finish_is_refused():
    doc = _req({"do": "a", "required": True,
                "substeps": [{"do": "b", "optional": True}, {"do": "c", "optional": True}]})
    assert validate(doc)[1] == ["steps[0].required: has nothing to finish (every sub-step is optional)"]
    asks = _req({"do": "a", "required": True, "question": {"prompt": "Which?", "choices": []},
                 "substeps": [{"do": "b", "optional": True}]})
    assert validate(asks)[1] == []                      # its own question is something to finish


def test_required_errors_come_after_optional_and_before_warning():
    step = {"do": "a", "url": "ftp://x", "file": "rel.md", "blocking": "x", "optional": 1,
            "required": "x", "warning": "w" * 301, "question": {"prompt": ""}, "substeps": []}
    assert validate(_req(step))[1] == [
        "steps[0].url: must be an http(s) URL",
        "steps[0].file: must be an absolute path",
        "steps[0].blocking: must be true or false",
        "steps[0].optional: must be true or false",
        "steps[0].required: must be true or false",
        "steps[0].warning: longer than 300 characters",
        "steps[0].question.prompt: must not be empty",
        "steps[0].substeps: must be a list of 1 to 10 steps",
    ]


REPLY = {"prompt": "Did it work?", "choices": []}


def test_done_accepts_each_requirement_in_any_order():
    for done in (["steps"], ["message"], ["reply"], ["message", "steps", "reply"]):
        assert validate(_req(done=done, reply=REPLY))[1] == [], done
    clean, _ = validate(_req(done=["steps"]))
    assert clean["done"] == ["steps"]


def test_done_must_be_a_list_of_one_to_three():
    expected = ["done: must be a list of 1 to 3 of steps, reply, message"]
    for bad in ([], "steps", {"steps": True}, 1, ["steps", "reply", "message", "steps"]):
        assert validate(_req(done=bad, reply=REPLY))[1] == expected, bad
    assert validate(_req(done=None))[1] == []          # null counts as absent


def test_done_entries_are_known_and_distinct():
    assert validate(_req(done=["steps", "ticks", 3]))[1] == [
        "done[1]: must be steps, reply or message", "done[2]: must be steps, reply or message"]
    assert validate(_req(done=["steps", "steps"]))[1] == ["done[1]: duplicate"]


def test_done_reply_needs_a_reply_prompt():
    assert validate(_req(done=["reply"]))[1] == ["done[0]: reply needs a reply prompt"]


def test_done_steps_needs_a_step_that_is_not_optional():
    doc = _req({"do": "a", "optional": True}, {"do": "b", "optional": True}, done=["message", "steps"])
    assert validate(doc)[1] == ["done[1]: steps has nothing to finish (every step is optional)"]
    assert validate(_req({"do": "a", "required": True}, {"do": "b"}, done=["steps"]))[1] == []


def test_done_errors_come_after_reply_and_before_links():
    doc = _req(done=["reply"], reply={"prompt": ""}, links="x")
    assert validate(doc)[1] == [
        "reply.prompt: must not be empty",
        "reply.choices: must be a list of at most 6 choices",
        "links: must be a list of at most 10 links",
    ]
    doc = _req(done=["nope"], links="x")
    assert validate(doc)[1] == ["done[0]: must be steps, reply or message",
                                "links: must be a list of at most 10 links"]


def test_done_values_are_never_echoed():
    _, errors = validate(_req(done=["sk-ant-abc"]))
    assert all("sk-ant" not in e for e in errors)


_REQUIRED = json.loads((Path(__file__).parent / "required-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _REQUIRED, ids=[c["name"] for c in _REQUIRED])
def test_required_vectors_match_the_reference(case):
    """The required-* and done-* cases shared with Hook's validator vectors."""
    clean, errors = validate(case["input"])
    assert errors == case["errors"]
    assert (errors == []) == case["valid"]
    assert (clean or None) == case["cleaned"]


def test_required_vectors_are_all_present_and_current():
    from mailbox.tests.gen_required_vectors import build
    assert len(_REQUIRED) == 41
    assert sum(c["name"].startswith("required-") for c in _REQUIRED) == 23
    assert sum(c["name"].startswith("done-") for c in _REQUIRED) == 18
    assert build() == _REQUIRED                        # rerun the generator after a rule change


# --- choice objects {label, text} (Pond 1.4.0) ------------------------------------

MIX = {"label": "Mix", "text": "What should change?"}


def _ask(question_choices=None, reply_choices=None, multi=None):
    question = {"prompt": "How?", "choices": question_choices or ["All at once", "Staff first"]}
    if multi is not None:
        question["multi"] = multi
    doc = {"v": 1, "title": "Roll out the change", "steps": [{"do": "Pick the rollout", "question": question}]}
    if reply_choices is not None:
        doc["reply"] = {"prompt": "Go ahead?", "choices": reply_choices}
    return doc


def test_choice_object_accepted():
    from mailbox.instructable import choice_label, choice_labels, choice_text, has_choice_objects
    doc = _ask(["All at once", MIX, "Staff first"], ["Yes", {"label": "Changes", "text": "What to change?"}])
    clean, errors = validate(doc)
    assert errors == []
    assert clean["steps"][0]["question"]["choices"][1] == MIX
    assert has_choice_objects(doc) and not has_choice_objects(_ask())
    assert not has_choice_objects(_ask(reply_choices=["Yes"]))
    assert has_choice_objects({"v": 1, "steps": [{"do": "a", "substeps": [
        {"do": "b", "question": {"prompt": "?", "choices": ["A", MIX]}}]}]})
    assert choice_labels(doc["steps"][0]["question"]["choices"]) == ["All at once", "Mix", "Staff first"]
    assert (choice_label("A"), choice_label(MIX), choice_label(5)) == ("A", "Mix", None)
    assert (choice_text("A"), choice_text(MIX)) == (None, "What should change?")
    assert choice_labels(None) == [] and choice_labels("x") == []


Q = "steps[0].question.choices"


@pytest.mark.parametrize("choices,details", [
    (["A", 5], [f"{Q}[1]: must be a string or {{label, text}}"]),
    (["A", ["B"]], [f"{Q}[1]: must be a string or {{label, text}}"]),
    (["A", {"label": "B", "text": "Why?", "x": 1}], [f"{Q}[1].x: unknown key"]),
    (["A", {"text": "Why?"}], [f"{Q}[1].label: required"]),
    (["A", {"label": " ", "text": "Why?"}], [f"{Q}[1].label: must not be empty"]),
    (["A", {"label": "B" * 61, "text": "Why?"}], [f"{Q}[1].label: longer than 60 characters"]),
    (["A", {"label": "B\nC", "text": "Why?"}], [f"{Q}[1].label: must be a single line"]),
    (["A", {"label": "B"}], [f"{Q}[1].text: required"]),
    (["A", {"label": "B", "text": "w" * 101}], [f"{Q}[1].text: longer than 100 characters"]),
    (["A", {"label": "B", "text": "two\nlines"}], [f"{Q}[1].text: must be a single line"]),
    (["A", {"label": "B", "text": 7}], [f"{Q}[1].text: must be a string"]),
    (["A", {"label": "B", "text": "key ghp_" + "a" * 36}], [f"{Q}[1].text: secret-like value"]),
    (["A", {}], [f"{Q}[1].label: required", f"{Q}[1].text: required"]),
])
def test_choice_object_errors(choices, details):
    assert validate(_ask(choices)) == ({}, details)
    reply = [d.replace(Q, "reply.choices") for d in details]
    assert validate(_ask(reply_choices=choices)) == ({}, reply)


def test_choice_object_in_a_multi_question_is_refused():
    assert validate(_ask(["A", MIX], multi=True)) == (
        {}, [f"{Q}[1]: a choice with text can't be in a multi question"])
    assert validate(_ask(["A", "B"], multi=True))[1] == []


def test_duplicate_label_across_string_and_object():
    assert validate(_ask(["Mix", MIX])) == ({}, [f"{Q}[1]: duplicate choice"])
    assert validate(_ask([MIX, {"label": "Mix", "text": "Other?"}])) == ({}, [f"{Q}[1]: duplicate choice"])
    assert validate(_ask(reply_choices=["Mix", MIX])) == ({}, ["reply.choices[1]: duplicate choice"])
    # A reply list of plain strings is as it was in 1.3.0: no duplicate rule.
    assert validate(_ask(reply_choices=["Yes", "Yes"]))[1] == []


def test_a_plain_string_ask_is_unchanged():
    doc = _ask(reply_choices=["Yes", "No"])
    assert validate(doc) == ({**doc, "urgency": "today"}, [])


def test_ask_warning_is_preserved_and_bounded():
    ins = {**MINIMAL, "warning": "Do not run twice"}
    clean, errors = validate(ins)
    assert errors == []
    assert clean["warning"] == ins["warning"]
    assert validate({**MINIMAL, "warning": "w" * 300})[1] == []
    assert validate({**MINIMAL, "warning": "w" * 301})[1] == ["warning: longer than 300 characters"]


# --- then: "steps" (Pond 1.9.0) -------------------------------------------------

def _then_ask(then):
    return {"v": 1, "title": "Clean up", "steps": [
        {"do": "Pick the cleanup", "question": {"prompt": "Which?", "choices": ["Logs", "Caches"], "then": then}},
    ]}


def test_then_validates():
    clean, errors = validate(_then_ask("steps"))
    assert errors == []
    assert clean["steps"][0]["question"]["then"] == "steps"
    sub = {"v": 1, "title": "Sub", "steps": [{"do": "Parent", "substeps": [
        {"do": "Child", "question": {"prompt": "Free text?", "then": "steps"}}]}]}
    assert validate(sub)[1] == []


@pytest.mark.parametrize("then", ["more", "", "Steps", True, 1, ["steps"], {"steps": 1}])
def test_then_bad_value(then):
    assert validate(_then_ask(then)) == ({}, ['steps[0].question.then: must be "steps"'])


THEN_CASES = json.loads((Path(__file__).parent / "then-instructable-vectors.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", THEN_CASES, ids=[c["name"] for c in THEN_CASES])
def test_then_matches_hooks_instructable_vectors(case):
    """Every case of Hook's instructable-vectors.json whose input has `then`, copied as it is."""
    clean, errors = validate(case["input"])
    assert errors == case["errors"]
    if case["valid"]:
        assert clean == case["cleaned"]


def test_then_null_is_absent():
    assert validate(_then_ask(None))[1] == []


def test_then_error_comes_after_the_multi_error():
    ask = {"v": 1, "title": "Clean up", "steps": [
        {"do": "Pick", "question": {"prompt": "Which?", "choices": ["Logs"], "multi": True, "then": "more"}}]}
    assert validate(ask)[1] == ["steps[0].question.multi: needs at least 2 choices",
                                'steps[0].question.then: must be "steps"']
