"""POST /notify body validation: field paths in errors, never values."""
import pytest

from mailbox.notification import validate

REMINDER = {"kind": "reminder", "ref": "7c1b2e0e-0000-4000-8000-000000000001", "title": "call the notary",
            "body": "today 14:00", "actions": [{"id": "done", "label": "✅ Done"}, {"id": "tomorrow", "label": "⏭ Tomorrow"}]}
BRIEF = {"kind": "brief", "ref": "brief:2026-10-17", "title": "Saturday 17 October", "body": "Today (1): 09:00 standup\nNext reminder: today 09:00 standup"}


def test_valid_reminder_and_brief():
    clean, errors = validate(REMINDER)
    assert errors == []
    assert clean == REMINDER
    clean, errors = validate(BRIEF)
    assert errors == []
    assert clean == {**BRIEF, "actions": []}
    clean, _ = validate({"kind": "reminder", "ref": "x", "title": "t"})
    assert (clean["body"], clean["actions"]) == (None, [])


@pytest.mark.parametrize(
    "patch, error",
    [
        ({"kind": None}, "kind: must be one of reminder, brief"),
        ({"kind": "alert"}, "kind: must be one of reminder, brief"),
        ({"ref": "a/b"}, "ref: must be 1 to 64 letters, digits, colon, dot, dash or underscore"),
        ({"ref": "x" * 65}, "ref: must be 1 to 64 letters, digits, colon, dot, dash or underscore"),
        ({"title": "x" * 201}, "title: longer than 200 characters"),
        ({"title": None}, "title: required"),
        ({"title": "two\nlines"}, "title: must be a single line"),
        ({"body": "x" * 2001}, "body: longer than 2000 characters"),
        ({"body": "a\u0007b"}, "body: contains hidden or control characters (U+0007)"),
        ({"actions": REMINDER["actions"] + [{"id": "done", "label": "x"}]}, "actions: must be a list of at most 2 actions"),
        ({"actions": [{"id": "snooze", "label": "x"}]}, "actions[0].id: must be one of done, tomorrow"),
        ({"actions": [{"id": "done", "label": ""}]}, "actions[0].label: must not be empty"),
        ({"actions": [{"id": "done", "label": "x", "url": "http://x"}]}, "actions[0].url: unknown key"),
        ({"extra": 1}, "extra: unknown key"),
        ({"body": "token ghp_" + "a" * 40}, "body: secret-like value"),
    ],
)
def test_reminder_errors(patch, error):
    clean, errors = validate({**REMINDER, **patch})
    assert clean == {}
    assert error in errors


def test_brief_carries_no_actions():
    _, errors = validate({**BRIEF, "actions": [{"id": "done", "label": "x"}]})
    assert errors == ["actions: a brief carries no actions"]


def test_not_an_object():
    assert validate(["x"]) == ({}, ["notification: must be an object"])


def test_errors_never_echo_a_value():
    """The hidden-range and secret rules are the instructable ones, unchanged:
    a title holding a tag character and a secret-like body both fail, and
    neither value appears in the errors."""
    _, errors = validate({**REMINDER, "title": "call\U000E0041 mum", "body": "ghp_" + "b" * 40})
    assert "title: contains hidden or control characters (U+E0041)" in errors
    assert "body: secret-like value" in errors
    assert not any("call" in e or "ghp_" in e for e in errors)


@pytest.mark.parametrize("ref", ["x\n", "brief:2026-10-17\n", "\nx"])
def test_a_ref_with_a_newline_is_refused(ref):
    _, errors = validate(dict(REMINDER, ref=ref))
    assert errors == ["ref: must be 1 to 64 letters, digits, colon, dot, dash or underscore"]
