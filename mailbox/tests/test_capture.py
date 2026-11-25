"""The capture grammar: a title, then an optional date and time, trailing.

NOW is Saturday 17 October 2026 at 13:00, so weekday rules can be pinned."""
from datetime import date, datetime

import pytest

from mailbox.capture import MAX_TITLE, Capture, clean, describe, parse

NOW = datetime(2026, 10, 17, 13, 0)   # a Saturday
TODAY = NOW.date()


@pytest.mark.parametrize(
    "text, title, day, time",
    [
        ("renew the car insurance tomorrow 9:00", "renew the car insurance", "2026-10-18", "09:00"),
        ("call the notary fri 14:00", "call the notary", "2026-10-23", "14:00"),
        ("call the notary friday 14:00", "call the notary", "2026-10-23", "14:00"),
        ("pay rent 2026-11-01", "pay rent", "2026-11-01", None),
        ("dentist 24.10 at 10:30", "dentist", "2026-10-24", "10:30"),
        ("standup 9:15", "standup", "2026-10-18", "09:15"),          # 09:15 has passed: tomorrow
        ("call mum 18:30", "call mum", "2026-10-17", "18:30"),        # still ahead: today
        ("call mum at 18:30", "call mum", "2026-10-17", "18:30"),
        ("sat 18:30", "sat 18:30", None, None),                       # no title: the whole text is the title
        ("water plants sat 18:30", "water plants", "2026-10-17", "18:30"),   # today is Saturday, time ahead
        ("water plants sat 9:00", "water plants", "2026-10-24", "09:00"),    # time passed: next Saturday
        ("water plants sun", "water plants", "2026-10-18", None),
        ("water plants sat", "water plants", "2026-10-17", None),            # today is Saturday, no time: today
        ("pay the plumber today", "pay the plumber", "2026-10-17", None),
        ("lunch 12:00 today", "lunch", "2026-10-17", "12:00"),       # explicit today beats the time rule
        ("meet at 10:30 on fri", "meet", "2026-10-23", "10:30"),     # reverse order, both fillers
        ("tax 5.1", "tax", "2027-01-05", None),                      # 5 Jan has passed: next year
        ("call mum 9.3", "call mum", "2027-03-09", None),            # D.M wins over a dotted time: 9 March (pinned; owner question 2)
        ("call mum 9.30", "call mum 9.30", None, None),              # D.M with month 30 is impossible and a dotted time is not a time: stays title
        ("call the notary tomorrow 9:00.", "call the notary", "2026-10-18", "09:00"),   # trailing punctuation is dropped
        ("meet fri, 10:00", "meet", "2026-10-23", "10:00"),
        ("buy milk", "buy milk", None, None),
        ("order 2.5 kg", "order 2.5 kg", None, None),                # kg trails: no date
        ("fix 31.2", "fix 31.2", None, None),                        # impossible day
        ("x 2026-13-01", "x 2026-13-01", None, None),                # impossible month
        ("alarm 25:00", "alarm 25:00", None, None),                  # not a time
        ("tomorrow", "tomorrow", None, None),
        ("9:00", "9:00", None, None),
        ("TOMORROW 9:00 Dentist", "TOMORROW 9:00 Dentist", None, None),   # leading forms are title text
        ("Call Mum TOMORROW", "Call Mum", "2026-10-18", None),      # case-insensitive forms, title kept as typed
        ("put the heating on tomorrow", "put the heating on", "2026-10-18", None),   # "on" binds to weekday/dated forms only
        ("turn on 9:00", "turn on", "2026-10-18", "09:00"),
        ("dentist at 9:00", "dentist", "2026-10-18", "09:00"),
        ("dentist on fri", "dentist", "2026-10-23", None),
        # Known D.M consequence, documented behaviour (owner's open question on dotted forms).
        ("version 1.2", "version", "2027-02-01", None),
        ("pay 2026-10-01", "pay", "2026-10-01", None),               # an explicit past date is accepted
    ],
)
def test_parse(text, title, day, time):
    assert parse(text, NOW) == Capture(title=title, date=day, time=time)


def test_parse_drops_hidden_characters_and_collapses_whitespace():
    assert parse("pay​  the\trent   tomorrow", NOW) == Capture("pay the rent", "2026-10-18", None)


def test_parse_keeps_emoji_sequences():
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    cap = parse(f"family dinner {family} sun 18:00", NOW)
    assert cap.title == f"family dinner {family}"
    assert (cap.date, cap.time) == ("2026-10-18", "18:00")


def test_clean_cuts_to_max_title():
    text = "x" * 300
    assert len(clean(text)) == MAX_TITLE
    assert parse(text + " tomorrow", NOW).date is None        # the date was cut off with the rest


def test_clean_empty_and_control_only():
    assert clean("") == ""
    assert clean("\u0007​") == ""
    assert parse("", NOW) == Capture("", None, None)


@pytest.mark.parametrize(
    "cap, text",
    [
        (Capture("a", None, None), "inbox"),
        (Capture("a", "2026-10-17", None), "today"),
        (Capture("a", "2026-10-17", "18:30"), "today 18:30"),
        (Capture("a", "2026-10-18", "09:00"), "tomorrow 09:00"),
        (Capture("a", "2026-10-23", "14:00"), "Fri 23 Oct 14:00"),
        (Capture("a", "2026-11-01", None), "Sun 1 Nov"),
        (Capture("a", "2027-01-05", None), "Tue 5 Jan 2027"),
    ],
)
def test_describe(cap, text):
    assert describe(cap, TODAY) == text


def _units(s):
    return len(s.encode("utf-16-le")) // 2


def test_clean_cuts_astral_text_by_utf16_units():
    cut = clean("\U0001F600" * 200)
    assert cut == "\U0001F600" * 100
    assert _units(cut) <= MAX_TITLE


def test_clean_never_leaves_dangling_joiner_at_the_boundary():
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"   # 8 UTF-16 units
    for pad in range(185, 200):                                  # sweeps the cut across every position
        c = clean("x" * pad + family)
        assert _units(c) <= MAX_TITLE
        assert not c.endswith("\u200d")
    assert clean("x" * 193 + family) == "x" * 193 + "\U0001F468\u200d\U0001F469"   # cut lands after the second ZWJ: it goes


def test_clean_leaves_short_ascii_alone():
    assert clean("renew the car insurance") == "renew the car insurance"
