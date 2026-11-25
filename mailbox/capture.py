"""Capture: a plain text message from the owner becomes a Central Time item.

Pure functions, no I/O. The message is a title, optionally followed by a date
and a time, trailing, in either order, each with an optional filler before it
("on" before a date, "at" before a time):

    renew the car insurance tomorrow 9:00
    call the notary fri 14:00
    pay rent 2026-11-01
    dentist 24.10 at 10:30
    standup 9:15

Date forms: today, tomorrow, a weekday (mon..sun or the full English name),
YYYY-MM-DD, D.M (the next such day). Time: H:MM or HH:MM, 24 hour. A time
without a date means today when the time is still ahead, else tomorrow. A
date without a time is an all-day item. A weekday with no time is today when
today is that weekday. A trailing . , ; or ! on a date or time token is
dropped. Anything else is part of the title.

`now` is a naive wall-clock datetime in the owner's zone (`TZ`);
the caller converts, this module never touches a timezone.
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from mailbox.instructable import _PROSE_ALLOWED, _is_hidden

MAX_TITLE = 200
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WD_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DM = re.compile(r"^(\d{1,2})\.(\d{1,2})$")


@dataclass(frozen=True)
class Capture:
    """What a message meant: the title, and the date and time if it named them."""
    title: str
    date: Optional[str]      # YYYY-MM-DD
    time: Optional[str]      # HH:MM


def _cut(text: str, limit: int = MAX_TITLE) -> str:
    """At most `limit` (default MAX_TITLE) UTF-16 units (what JS .length counts), never splitting a
    surrogate pair. When cut, a trailing ZWJ, variation selector or combining
    mark left dangling at the boundary is dropped too."""
    units = 0
    end = len(text)
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            end = i
            break
    else:
        return text.strip()
    cut = text[:end]
    while cut and (cut[-1] == "\u200d" or 0xFE00 <= ord(cut[-1]) <= 0xFE0F
                   or unicodedata.category(cut[-1]) in ("Mn", "Mc", "Me")):
        cut = cut[:-1]
    return cut.strip()


def clean(text: str) -> str:
    """Hidden and control characters dropped (emoji joiners kept), whitespace
    collapsed to single spaces, cut to MAX_TITLE."""
    kept = "".join(ch for ch in text if ord(ch) in _PROSE_ALLOWED or not _is_hidden(ch))
    return _cut(" ".join(kept.split()))


def _time(tok: str) -> Optional[str]:
    m = _TIME.match(tok)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _date_spec(tok: str, today: date):
    """("today" | "tomorrow" | "weekday" | "abs", argument) or None."""
    low = tok.lower()
    if low == "today":
        return ("today", None)
    if low == "tomorrow":
        return ("tomorrow", None)
    if low in WEEKDAYS:
        return ("weekday", WEEKDAYS.index(low))
    if low in WEEKDAY_NAMES:
        return ("weekday", WEEKDAY_NAMES.index(low))
    m = _ISO.match(tok)
    if m:
        try:
            return ("abs", date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            return None
    m = _DM.match(tok)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= today:
                return ("abs", candidate)
        return None
    return None


def _resolve(spec, time: Optional[str], now: datetime) -> str:
    today = now.date()
    kind, arg = spec
    if kind == "today":
        return today.isoformat()
    if kind == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if kind == "abs":
        return arg.isoformat()
    delta = (arg - today.weekday()) % 7
    # Today's weekday means today, unless a time was given and has passed.
    if delta == 0 and time is not None and time <= now.strftime("%H:%M"):
        delta = 7
    return (today + timedelta(days=delta)).isoformat()


def parse(text: str, now: datetime) -> Capture:
    """The grammar in the module docstring. Never raises; the fallback is the
    whole cleaned text as the title with no date."""
    cleaned = clean(text)
    tokens = cleaned.split(" ") if cleaned else []
    end = len(tokens)
    time: Optional[str] = None
    spec = None
    for _ in range(2):
        if end == 0:
            break
        tok = tokens[end - 1].rstrip(".,;!")    # "tomorrow 9:00." and "fri, 10:00" still parse
        parsed_time = _time(tok)
        if parsed_time is not None and time is None:
            time = parsed_time
        else:
            parsed_spec = _date_spec(tok, now.date()) if spec is None else None
            if parsed_spec is None:
                break
            spec = parsed_spec
        end -= 1
        # "at" binds to a time; "on" to a weekday or a dated form, not to
        # today/tomorrow ("put the heating on tomorrow" keeps its "on").
        if end > 0:
            filler = tokens[end - 1].lower()
            if filler == "at" and parsed_time is not None and parsed_time == time:
                end -= 1
            elif filler == "on" and parsed_time is None and spec[0] in ("weekday", "abs"):
                end -= 1
    title = " ".join(tokens[:end]).strip()
    if not title or (spec is None and time is None):
        return Capture(title=cleaned, date=None, time=None)
    if spec is None:
        today = now.date()
        day = today if time > now.strftime("%H:%M") else today + timedelta(days=1)
        return Capture(title=title, date=day.isoformat(), time=time)
    return Capture(title=title, date=_resolve(spec, time, now), time=time)


def describe(cap: Capture, today: date) -> str:
    """The human form in the Added reply: inbox, today, tomorrow, or Fri 23 Oct
    (with the year only when it differs), followed by the time when set."""
    if not cap.date:
        return "inbox"
    day = date.fromisoformat(cap.date)
    if day == today:
        when = "today"
    elif day == today + timedelta(days=1):
        when = "tomorrow"
    else:
        when = f"{WD_SHORT[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
        if day.year != today.year:
            when += f" {day.year}"
    return f"{when} {cap.time}" if cap.time else when
