"""Owner answers to the questions on an instructable's steps.

Pure functions, no I/O. POST /asks/{id}/answers and a Telegram tap both run
validate_answers(); each answer it accepts becomes one `answer` event whose
text is the compact JSON it returns. Relay's Swift copy of these rules is held
to the same vectors (tests/answers-vectors.json). Errors name the field and
never echo a value.
"""
import json
import re
from typing import Any

from mailbox.instructable import MULTI, _keys, _text, choice_label, choice_labels, choice_text, looks_secret
from mailbox.revisions import ID_RE, id_paths, question_at

MAX_ANSWERS = 20
# A free-text answer, and the words on a pick: as long as a message to the session
# (revisions.MAX_MESSAGE). Pond 1.15.0, feature `longAnswers`; 1000 before.
MAX_ANSWER_TEXT = 8000
ANSWER_KEYS = {"step", "picked", "text"}
# "3" is steps[2]; "3.2" is steps[2].substeps[1]. No leading zeros.
_PATH = re.compile(r"[1-9][0-9]?(?:\.[1-9][0-9]?)?")


def questions(instructable: Any) -> dict[str, dict]:
    """Every question of a stored instructable by step path, in document order:
    a parent's own question comes before those of its sub-steps."""
    found: dict[str, dict] = {}
    steps = instructable.get("steps") if isinstance(instructable, dict) else None
    if not isinstance(steps, list):
        return found
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if isinstance(step.get("question"), dict):
            found[f"{i + 1}"] = step["question"]
        subs = step.get("substeps")
        for j, sub in enumerate(subs if isinstance(subs, list) else []):
            if isinstance(sub, dict) and isinstance(sub.get("question"), dict):
                found[f"{i + 1}.{j + 1}"] = sub["question"]
    return found


def _picked(errors: list[str], path: str, question: dict, picked: Any) -> list[str] | None:
    """The picks (labels) of a choice question in the order of its choices, or None."""
    choices = choice_labels(question.get("choices"))
    multi = question.get("multi") is True
    if picked is None:
        errors.append(f"{path}.picked: required")
        return None
    if not isinstance(picked, list) or not picked or (not multi and len(picked) != 1):
        errors.append(f"{path}.picked: must list "
                      + ("1 or more of the choices" if multi else "exactly 1 choice"))
        return None
    ok = True
    for k, pick in enumerate(picked):
        if not isinstance(pick, str) or pick not in choices:
            errors.append(f"{path}.picked[{k}]: not one of the choices")
            ok = False
        elif pick in picked[:k]:
            errors.append(f"{path}.picked[{k}]: picked twice")
            ok = False
    return [c for c in choices if c in picked] if ok else None


def _id(by_path: dict[str, str], path: str) -> dict[str, str]:
    return {"id": by_path[path]} if path in by_path else {}


def validate_answers(instructable: Any, answers: Any) -> tuple[list[str], list[str]]:
    """Check a batch of answers against the questions of an ask's instructable.

    → (one event text per answer, in batch order, []) when every answer fits,
      ([], ["<field path>: <problem>", …]) when any does not; nothing is stored then.
    An event text is compact JSON: {"step":"3.2","picked":["Staging"]} for a choice
    question (a label; Pond 1.4.0 adds "text", the owner's words, when the one pick is
    a choice that takes them: {"step":"2","picked":["Mix"],"text":"…"}), {"step":"4","text":"…"} for a free-text one, plus the step's "id"
    when the instructable has ids: {"step":"3","id":"s3","picked":["Prod"]}.
    `step` may be a path or a step id; a value that reads as a path is a path.
    """
    if not isinstance(answers, list) or not 1 <= len(answers) <= MAX_ANSWERS:
        return [], [f"answers: must be a list of 1 to {MAX_ANSWERS} answers"]
    asked = questions(instructable)
    by_id = id_paths(instructable)
    by_path = {path: step_id for step_id, path in by_id.items()}
    errors: list[str] = []
    texts: list[str] = []
    seen: set[str] = set()
    for i, answer in enumerate(answers):
        path = f"answers[{i}]"
        if not _keys(errors, path, answer, ANSWER_KEYS):
            continue
        step = answer.get("step")
        if step is None:
            errors.append(f"{path}.step: required")
            continue
        if not isinstance(step, str):
            errors.append(f"{path}.step: must be a string")
            continue
        if not _PATH.fullmatch(step) and ID_RE.fullmatch(step) and step in by_id:
            step = by_id[step]
        question = asked.get(step) if _PATH.fullmatch(step) else None
        if question is None:
            errors.append(f"{path}.step: no question at this step")
            continue
        if step in seen:
            errors.append(f"{path}.step: answered twice")
            continue
        seen.add(step)
        if question.get("choices"):
            words = {choice_label(c) for c in question["choices"] if choice_text(c) is not None}
            text = answer.get("text")
            if text is not None and not words:
                errors.append(f"{path}.text: this question takes picks")
                continue
            picks = _picked(errors, path, question, answer.get("picked"))
            if picks is None:
                continue
            body: dict = {"step": step, **_id(by_path, step), "picked": picks}
            if text is not None:
                # Pond 1.4.0: the owner's words beside a single pick of a choice that takes them.
                if len(picks) != 1 or picks[0] not in words:
                    errors.append(f"{path}.text: this choice takes no words")
                    continue
                before = len(errors)
                _text(errors, f"{path}.text", text, MAX_ANSWER_TEXT, required=True, kind=MULTI)
                if len(errors) > before:
                    continue
                if looks_secret(text):
                    errors.append(f"{path}.text: secret-like value")
                    continue
                body["text"] = text
        else:
            if answer.get("picked") is not None:
                errors.append(f"{path}.picked: this question takes text")
                continue
            before = len(errors)
            text = answer.get("text")
            _text(errors, f"{path}.text", text, MAX_ANSWER_TEXT, required=True, kind=MULTI)
            if len(errors) > before:
                continue
            if looks_secret(text):
                errors.append(f"{path}.text: secret-like value")
                continue
            body = {"step": step, **_id(by_path, step), "text": text}
        texts.append(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    if errors:
        return [], errors
    return texts, []


def latest(events: list[dict]) -> dict[str, dict]:
    """The owner's current answer per step path: the latest `answer` event wins.
    Each value is the parsed event text plus `at` and `seq`; unreadable texts are skipped."""
    current: dict[str, dict] = {}
    for event in sorted(events, key=lambda e: e.get("seq") or 0):
        if event.get("kind") != "answer" or event.get("actor") != "owner":
            continue
        try:
            body = json.loads(event.get("text") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and isinstance(body.get("step"), str):
            current[body["step"]] = {**body, "at": event.get("at"), "seq": event.get("seq")}
    return current


def current(events: list[dict], revisions: dict[int, dict], in_force: Any) -> tuple[dict[str, dict], dict[str, dict]]:
    """The owner's answers by step id, split into (counted, earlier).

    The latest `answer` event per step id decides. It counts only when the question
    at that id in the version in force is the one the answer was given to (the
    question in the revision at the event's `rev`); otherwise it is `earlier`: an
    answer to a question that has changed since. An event without a rev counts as
    rev 1, and one without an id is mapped from its path through that revision.
    With no revisions (an ask from before ids), `in_force` stands for every rev.
    Each value is the parsed event text plus `id`, `at`, `seq` and `rev`."""
    latest: dict[str, tuple[dict, bool]] = {}
    for event in sorted(events, key=lambda e: e.get("seq") or 0):
        if event.get("kind") != "answer" or event.get("actor") != "owner":
            continue
        try:
            body = json.loads(event.get("text") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        version = revisions.get(event.get("rev") or 1, in_force)
        step_id = body.get("id")
        if not isinstance(step_id, str):
            paths = {path: sid for sid, path in id_paths(version).items()}
            step_id = paths.get(body.get("step"))
        if step_id is None:
            continue
        asked = question_at(version, step_id)
        valid = asked is not None and asked == question_at(in_force, step_id)
        latest[step_id] = ({**body, "id": step_id, "at": event.get("at"), "seq": event.get("seq"),
                            "rev": event.get("rev")}, valid)
    counted = {sid: entry for sid, (entry, valid) in latest.items() if valid}
    earlier = {sid: entry for sid, (entry, valid) in latest.items() if not valid}
    return counted, earlier
