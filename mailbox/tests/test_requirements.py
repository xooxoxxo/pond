"""Tests for the requirement model (Pond 1.3.0): what an ask needs before the
owner's Done, the fold of `progress` events, and how it reads on the wire."""
import copy
import json

from mailbox.requirements import (
    MAX_PROGRESS_IDS, PROGRESS_KEYS, finished, left, own_fields, ticked, wanted, wire, words,
)

LEAF = {"id": "s1", "do": "Open the deploy settings"}
REQ = {"id": "s2", "do": "Rotate the key", "required": True}
PARENT = {"id": "s3", "do": "Check the deploy", "substeps": [
    {"id": "s3.1", "do": "Open the log"},
    {"id": "s3.2", "do": "Note anything odd", "optional": True},
]}
DOC = {"v": 1, "title": "Rotate the staging deploy key", "steps": [LEAF, REQ, PARENT]}


def _doc(*steps, **top):
    return {"v": 1, "title": "Rotate the staging deploy key", "steps": list(steps), **top}


def _progress(seq, tick=(), untick=(), rev=1, actor="owner", kind="progress"):
    return {"seq": seq, "kind": kind, "actor": actor, "rev": rev,
            "text": json.dumps({"tick": list(tick), "untick": list(untick)})}


def test_the_progress_limits():
    assert PROGRESS_KEYS == ("tick", "untick")
    assert MAX_PROGRESS_IDS == 61


# --- own_fields ------------------------------------------------------------------


def test_own_fields_leave_out_the_id_the_substeps_and_required():
    assert own_fields(PARENT) == {"do": "Check the deploy"}
    assert own_fields({**REQ, "code": "rotate"}) == {"do": "Rotate the key", "code": "rotate"}
    assert "required" in REQ                              # the input is not mutated


# --- wanted ----------------------------------------------------------------------


def test_an_ask_without_requirements_wants_nothing():
    assert wanted(_doc(LEAF, {**PARENT})) == []
    assert wanted(None) == [] and wanted({}) == []


def test_wanted_lists_each_own_required_step_in_document_order():
    doc = _doc(LEAF, {**PARENT, "required": True,
                      "substeps": [{"id": "s3.1", "do": "Open the log", "required": True}]}, REQ)
    assert wanted(doc) == [
        {"kind": "step", "id": "s3", "path": "2"},
        {"kind": "step", "id": "s3.1", "path": "2.1"},
        {"kind": "step", "id": "s2", "path": "3"},
    ]


def test_done_steps_wants_every_top_level_step_that_is_not_optional_once():
    doc = _doc(LEAF, REQ, {"id": "s4", "do": "Read the notes", "optional": True}, PARENT, done=["steps"])
    assert wanted(doc) == [
        {"kind": "step", "id": "s1", "path": "1"},
        {"kind": "step", "id": "s2", "path": "2"},
        {"kind": "step", "id": "s3", "path": "4"},
    ]


def test_done_reply_and_message_come_after_the_steps_in_that_order():
    doc = _doc(REQ, done=["message", "reply"], reply={"prompt": "Did it pass?", "choices": []})
    assert wanted(doc) == [{"kind": "step", "id": "s2", "path": "1"}, {"kind": "reply"}, {"kind": "message"}]


# --- finished --------------------------------------------------------------------


def _finished(step, path="1", *, ticks=(), answers=()):
    return finished(step, path, ticked=set(ticks), answered=set(answers))


def test_a_leaf_is_finished_when_ticked():
    assert not _finished(LEAF)
    assert _finished(LEAF, ticks={"s1"})


def test_a_question_is_finished_only_when_answered():
    question = {**LEAF, "question": {"prompt": "Which?", "choices": ["A", "B"]}}
    assert _finished(question, "1", answers={"1"})
    assert not _finished(question, "1", ticks={"s1"})    # a tick on a question that isn't optional
    assert not _finished(question, "1", answers={"2"})


def test_a_parent_needs_its_sub_steps_that_are_not_optional():
    assert not _finished(PARENT, "3")
    assert _finished(PARENT, "3", ticks={"s3.1"})        # the optional 3.2 is left open
    assert not _finished(PARENT, "3", ticks={"s3"})      # a tick on the parent alone isn't enough


def test_a_parent_with_its_own_question_needs_the_answer_too():
    asks = {**PARENT, "question": {"prompt": "Clean?", "choices": []}}
    assert not _finished(asks, "3", ticks={"s3.1"})
    assert _finished(asks, "3", ticks={"s3.1"}, answers={"3"})


def test_a_sub_step_question_is_answered_at_its_own_path():
    parent = {**PARENT, "substeps": [{"id": "s3.1", "do": "Pick", "question": {"prompt": "Env?", "choices": []}}]}
    assert not _finished(parent, "3", answers={"3"})
    assert _finished(parent, "3", answers={"3.1"})


# --- the fold --------------------------------------------------------------------


def test_a_tick_then_an_untick():
    assert ticked([_progress(1, ["s1", "s2"])], {}, DOC) == {"s1", "s2"}
    assert ticked([_progress(1, ["s1", "s2"]), _progress(2, untick=["s1"])], {}, DOC) == {"s2"}
    assert ticked([_progress(2, untick=["s1"]), _progress(1, ["s1"])], {}, DOC) == set()   # by seq


def test_deltas_from_two_macs_merge():
    a = {**_progress(1, ["s1"]), "auth": "owner-key"}
    b = {**_progress(2, ["s3.1"]), "auth": "token"}
    assert ticked([a, b], {}, DOC) == {"s1", "s3.1"}


def test_only_owner_progress_events_count():
    events = [_progress(1, ["s1"], actor="session"), _progress(2, ["s2"], kind="answer"),
              {"seq": 3, "kind": "progress", "actor": "owner", "rev": 1, "text": "not json"},
              {"seq": 4, "kind": "progress", "actor": "owner", "rev": 1, "text": '{"tick":"s1"}'},
              {"seq": 5, "kind": "progress", "actor": "owner", "rev": 1, "text": None}]
    assert ticked(events, {}, DOC) == set()


def _rev2(mutate):
    rev1 = copy.deepcopy(DOC)
    rev2 = copy.deepcopy(DOC)
    mutate(rev2)
    return {1: rev1, 2: rev2}, rev2


def test_a_tick_on_rev_1_carries_to_rev_2_when_the_step_is_unchanged():
    bodies, rev2 = _rev2(lambda d: d["steps"][2].update(do="Check the staging deploy"))
    assert ticked([_progress(1, ["s1", "s3.1"])], bodies, rev2) == {"s1", "s3.1"}


def test_a_tick_is_dropped_when_its_do_changed():
    bodies, rev2 = _rev2(lambda d: d["steps"][0].update(do="Open the new settings"))
    assert ticked([_progress(1, ["s1", "s2"])], bodies, rev2) == {"s2"}
    assert ticked([_progress(1, ["s1"]), _progress(2, ["s1"], rev=2)], bodies, rev2) == {"s1"}


def test_a_tick_is_kept_when_only_required_changed():
    bodies, rev2 = _rev2(lambda d: (d["steps"][0].update(required=True), d["steps"][1].pop("required")))
    assert ticked([_progress(1, ["s1", "s2"])], bodies, rev2) == {"s1", "s2"}


def test_a_tick_is_kept_on_a_parent_whose_sub_steps_changed():
    bodies, rev2 = _rev2(lambda d: d["steps"][2]["substeps"].pop())
    assert ticked([_progress(1, ["s3"])], bodies, rev2) == {"s3"}


def test_a_tick_on_a_step_rev_2_removed_is_ignored():
    bodies, rev2 = _rev2(lambda d: d["steps"].pop(0))
    assert ticked([_progress(1, ["s1", "s2"])], bodies, rev2) == {"s2"}


def test_an_event_without_a_rev_reads_as_rev_1():
    bodies, rev2 = _rev2(lambda d: d["steps"][0].update(do="Open the new settings"))
    assert ticked([{**_progress(1, ["s1", "s2"]), "rev": None}], bodies, rev2) == {"s2"}


# --- left, words and wire -------------------------------------------------------


def test_left_names_what_is_not_finished_in_order():
    doc = _doc(LEAF, REQ, {**PARENT, "required": True}, done=["reply", "message"],
               reply={"prompt": "Did it pass?", "choices": []})
    everything = [{"kind": "step", "id": "s2", "path": "2"}, {"kind": "step", "id": "s3", "path": "3"},
                  {"kind": "reply"}, {"kind": "message"}]
    assert left(doc, ticked=set(), answered=set(), replied=False, messaged=False) == everything
    assert left(doc, ticked={"s2", "s3.1"}, answered=set(), replied=True, messaged=False) == [{"kind": "message"}]
    assert left(doc, ticked={"s2", "s3.1"}, answered=set(), replied=True, messaged=True) == []


def test_left_on_an_ask_without_requirements_is_empty():
    assert left(_doc(LEAF), ticked=set(), answered=set(), replied=False, messaged=False) == []


def test_words_name_steps_as_telegram_numbers_them():
    assert words([{"kind": "step", "id": "s2", "path": "2"}, {"kind": "step", "id": "s3.2", "path": "3.2"},
                  {"kind": "reply"}, {"kind": "message"}]) == ["step 2", "step 3b", "the reply", "a message"]
    assert words([{"kind": "step", "id": "x", "path": "4.10"}]) == ["step 4j"]
    assert words([]) == []


def test_wire_says_what_is_left_and_whether_done_is_ready():
    item = [{"kind": "reply"}]
    assert wire(item) == {"left": [{"kind": "reply"}], "ready": False}
    assert wire([]) == {"left": [], "ready": True}


def test_progress_with_an_unknown_revision_does_not_borrow_current_body():
    assert ticked([_progress(1, ["s1"], rev=99)], {1: DOC}, DOC) == set()


def test_verify_and_legacy_paths_never_alias_real_ids_and_follow_content():
    first = _doc({"id": "verify", "do": "A real step"}, {"do": "Legacy step"},
                 verify={"do": "Verify response", "code": "check"})
    events = [_progress(1, ["verify", "@verify", "@legacy:2"])]
    assert ticked(events, {1: first}, first) == {"verify", "@verify", "@legacy:2"}
    changed = copy.deepcopy(first)
    changed["verify"]["code"] = "check corrected"
    changed["steps"][1]["do"] = "Corrected legacy step"
    assert ticked(events, {1: first, 2: changed}, changed) == {"verify"}
    assert ticked(events + [_progress(2, untick=["@verify"])], {1: first}, first) == {"verify", "@legacy:2"}
