"""Tests for owner answers to step questions."""
import json
from pathlib import Path

import pytest

from mailbox.answers import latest, questions, validate_answers

_DOC = json.loads((Path(__file__).parent / "answers-vectors.json").read_text(encoding="utf-8"))
_CASES = _DOC["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_answers_vectors(case):
    """The answers-* cases shared with Relay's Swift copy of these rules."""
    instructable = case["instructable"] if "instructable" in case else _DOC["instructable"]
    events, errors = validate_answers(instructable, case["answers"])
    assert errors == case["errors"]
    assert events == case["events"]
    assert (errors == []) == case["valid"]


def test_questions_are_keyed_by_path_in_document_order():
    assert list(questions(_DOC["instructable"])) == ["1", "2", "3", "4", "4.2"]
    assert questions(None) == {} and questions({"steps": "x"}) == {}


def test_the_latest_answer_per_step_wins():
    events = [
        {"seq": 7, "kind": "answer", "actor": "owner", "text": '{"step":"1","picked":["Prod"]}', "at": "b"},
        {"seq": 5, "kind": "answer", "actor": "owner", "text": '{"step":"1","picked":["Staging"]}', "at": "a"},
        {"seq": 6, "kind": "answer", "actor": "owner", "text": '{"step":"3","text":"EU"}', "at": "a"},
        {"seq": 8, "kind": "reply", "actor": "owner", "text": '{"step":"3","text":"no"}', "at": "c"},
        {"seq": 9, "kind": "answer", "actor": "owner", "text": "not json", "at": "c"},
    ]
    assert latest(events) == {
        "1": {"step": "1", "picked": ["Prod"], "at": "b", "seq": 7},
        "3": {"step": "3", "text": "EU", "at": "a", "seq": 6},
    }


def test_errors_never_echo_the_value():
    secret = "gh" + "p_" + "Q" * 36                   # built at runtime: no literal token in the repo
    _, errors = validate_answers(_DOC["instructable"], [{"step": "3", "text": f"use {secret}"}])
    assert errors == ["answers[0].text: secret-like value"]
    assert all(secret not in e for e in errors)


# --- words on a word choice (Pond 1.4.0) --------------------------------------------

WORDS_DOC = {"v": 1, "title": "Roll out the change", "steps": [
    {"do": "Read the plan", "id": "s1"},
    {"do": "Pick the rollout", "id": "s2", "question": {"prompt": "How?", "choices": [
        "All at once", {"label": "Mix", "text": "What should change?"}, "Staff first"]}},
    {"do": "Pick the targets", "id": "s3", "question": {"prompt": "Which?", "multi": True,
                                                        "choices": ["Web", "Mobile"]}}]}


def test_answer_words_with_word_choice():
    events, errors = validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"], "text": "Staff on Monday"}])
    assert errors == []
    assert events == ['{"step":"2","id":"s2","picked":["Mix"],"text":"Staff on Monday"}']
    events, _ = validate_answers(WORDS_DOC, [{"step": "s2", "picked": ["Mix"], "text": "Staff\nthen all"}])
    assert json.loads(events[0])["text"] == "Staff\nthen all"


def test_answer_words_refused_on_plain_choice():
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Staff first"], "text": "now"}]) == (
        [], ["answers[0].text: this choice takes no words"])
    # A question with no word choice keeps its 1.3.0 words.
    assert validate_answers(WORDS_DOC, [{"step": "3", "picked": ["Web"], "text": "now"}]) == (
        [], ["answers[0].text: this question takes picks"])


def test_answer_pick_without_words_ok():
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"]}]) == (
        ['{"step":"2","id":"s2","picked":["Mix"]}'], [])
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"], "text": None}]) == (
        ['{"step":"2","id":"s2","picked":["Mix"]}'], [])
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["What should change?"]}]) == (
        [], ["answers[0].picked[0]: not one of the choices"])


def test_answer_words_limit_and_secret():
    """Pond 1.15.0 (`longAnswers`): the words on a pick take 8000 characters, as a message does."""
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"], "text": "w" * 8000}])[1] == []
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"], "text": "w" * 8001}]) == (
        [], ["answers[0].text: longer than 8000 characters"])
    secret = "gh" + "p_" + "Q" * 36
    _, errors = validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"], "text": f"use {secret}"}])
    assert errors == ["answers[0].text: secret-like value"]
    assert validate_answers(WORDS_DOC, [{"step": "2", "picked": ["Mix"], "text": "  "}]) == (
        [], ["answers[0].text: must not be empty"])


# --- long answers (Pond 1.15.0, feature `longAnswers`) -------------------------------


def test_the_answer_cap_is_the_message_cap():
    from mailbox import answers, revisions
    assert answers.MAX_ANSWER_TEXT == revisions.MAX_MESSAGE == 8000


def test_a_long_pasted_free_text_answer_is_kept_whole():
    """A pasted test summary: many lines, past the old 1000, kept verbatim with its breaks."""
    summary = "\n".join(f"PASS test_{i:04d} ({i % 7} ms)" for i in range(350))
    assert 1000 < len(summary) <= 8000
    events, errors = validate_answers(_DOC["instructable"], [{"step": "3", "text": summary}])
    assert errors == []
    assert json.loads(events[0]) == {"step": "3", "text": summary}


def test_the_free_text_cap_counts_characters_not_bytes():
    assert validate_answers(_DOC["instructable"], [{"step": "3", "text": "é" * 8000}])[1] == []
    assert validate_answers(_DOC["instructable"], [{"step": "3", "text": "é" * 8001}]) == (
        [], ["answers[0].text: longer than 8000 characters"])
