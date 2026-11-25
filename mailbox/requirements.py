"""What an ask needs before the owner's Done (Pond 1.3.0).

Pure functions, no I/O. An ask states its requirements with `required: true`
on a step or sub-step and the ask-level `done` list ("steps", "reply",
"message"). Ticks are shared on all asks (Pond 1.5); POST /asks/{id}/progress carries them here as
`progress`/owner events whose text is {"tick": [ids], "untick": [ids]}, and
ticked() folds them. A requirement on the wire is {"kind": "step", "id",
"path"}, {"kind": "reply"} or {"kind": "message"}; nothing here echoes a value.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from mailbox.instructable import MAX_STEP_TOTAL

PROGRESS_KEYS = ("tick", "untick")
MAX_PROGRESS_IDS = MAX_STEP_TOTAL + 1  # all steps plus Verify (Pond 1.5)
VERIFY_ID = "@verify"
LETTERS = "abcdefghij"                       # a sub-step's number, as Telegram shows it: 3b
# Pond 1.9.0: after the owner answers a question with `then: "steps"`, the owner's Done
# waits this long for the session's next revision.
STEP_WAIT_S = 600


def own_fields(step: dict) -> dict:
    """The step without id, substeps and required (RD 12): what a tick was given to.
    Toggling `required` in a correction, or changing a parent's sub-steps, keeps it."""
    return {k: v for k, v in step.items() if k not in ("id", "substeps", "required")}


def _entries(ins: Any) -> list[tuple[str, dict, bool]]:
    """(path, step, top level?) for every step and sub-step, in document order."""
    found: list[tuple[str, dict, bool]] = []
    steps = ins.get("steps") if isinstance(ins, dict) else None
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict):
            continue
        found.append((f"{i + 1}", step, True))
        subs = step.get("substeps")
        for j, sub in enumerate(subs if isinstance(subs, list) else []):
            if isinstance(sub, dict):
                found.append((f"{i + 1}.{j + 1}", sub, False))
    return found


def progress_id(step: dict, path: str) -> str:
    return step["id"] if isinstance(step.get("id"), str) else "@legacy:" + path


def identities(ins: Any) -> dict[str, dict]:
    """Reserved @ identities cannot collide with the real step-id grammar."""
    found = {progress_id(step, path): own_fields(step) for path, step, _ in _entries(ins)}
    if isinstance(ins, dict) and isinstance(ins.get("verify"), dict):
        found[VERIFY_ID] = ins["verify"]
    return found


def _done(ins: Any) -> list:
    done = ins.get("done") if isinstance(ins, dict) else None
    return done if isinstance(done, list) else []


def _step_item(path: str, step: dict) -> dict:
    item: dict[str, Any] = {"kind": "step"}
    if isinstance(step.get("id"), str):
        item["id"] = step["id"]
    item["path"] = path
    return item


def wanted(ins: Any) -> list[dict]:
    """Every requirement of a body, in document order: each step with its own
    `required`, and every top-level step that isn't optional when `done` holds
    "steps" (RD 6), each listed once; then the reply, then a message. [] means none."""
    done = _done(ins)
    every = "steps" in done
    found = [_step_item(path, step) for path, step, top in _entries(ins)
             if step.get("required") is True or (every and top and step.get("optional") is not True)]
    if "reply" in done:
        found.append({"kind": "reply"})
    if "message" in done:
        found.append({"kind": "message"})
    return found


def _delta(text: Any) -> tuple[list[str], list[str]] | None:
    """A progress event's (tick, untick), or None for an unreadable text."""
    try:
        body = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    lists = [body.get(key) if body.get(key) is not None else [] for key in PROGRESS_KEYS]
    if not all(isinstance(ids, list) and all(isinstance(i, str) for i in ids) for ids in lists):
        return None
    return lists[0], lists[1]


def ticked(events: list[dict], bodies: dict[int, dict], body: dict) -> set[str]:
    """The ticked step ids at `body` (the accepted revision): every progress/owner
    event folded in seq order, a tick adding and an untick removing. A tick counts
    only when its id is in `body` and that step's own fields equal those of the same
    id in the event's revision (RD 11). An event without a rev is rev 1; with no
    revisions at all, `body` stands for every rev."""
    now = identities(body)
    found: set[str] = set()
    for event in sorted(events, key=lambda e: e.get("seq") or 0):
        if event.get("kind") != "progress" or event.get("actor") != "owner":
            continue
        delta = _delta(event.get("text"))
        if delta is None:
            continue
        then_body = bodies.get(event.get("rev") or 1) if bodies else body
        if then_body is None:
            continue
        then = identities(then_body)
        tick, untick = delta
        for step_id in tick:
            if step_id in now and step_id in then and now[step_id] == then[step_id]:
                found.add(step_id)
        found.difference_update(untick)
    return found


def finished(step: dict, path: str, *, ticked: set[str], answered: set[str]) -> bool:
    """Is this step finished (RD 5)? A leaf without a question: its id is ticked. A
    leaf with a question: its path has a counted answer (a tick isn't enough). A
    parent: its own question answered, if it has one, and every sub-step that isn't
    optional finished."""
    subs = step.get("substeps")
    asks = isinstance(step.get("question"), dict)
    if isinstance(subs, list) and subs:
        if asks and path not in answered:
            return False
        return all(finished(sub, f"{path}.{j + 1}", ticked=ticked, answered=answered)
                   for j, sub in enumerate(subs)
                   if isinstance(sub, dict) and sub.get("optional") is not True)
    if asks:
        return path in answered
    return progress_id(step, path) in ticked


def left(ins: dict, *, ticked: set[str], answered: set[str], replied: bool, messaged: bool) -> list[dict]:
    """The requirements of `ins` not yet met, in wanted() order."""
    steps = {path: step for path, step, _ in _entries(ins)}
    out = []
    for item in wanted(ins):
        if item["kind"] == "step":
            if not finished(steps[item["path"]], item["path"], ticked=ticked, answered=answered):
                out.append(item)
        elif (item["kind"] == "reply" and not replied) or (item["kind"] == "message" and not messaged):
            out.append(item)
    return out


def _number(path: str) -> str:
    """"3.2" → "3b", as Telegram and Hook number a sub-step."""
    top, _, sub = path.partition(".")
    if not sub:
        return top
    index = int(sub) - 1 if sub.isdigit() else -1
    return f"{top}{LETTERS[index]}" if 0 <= index < len(LETTERS) else path


def words(left: list[dict]) -> list[str]:
    """What's left in words: ["step 2", "step 3b", "the reply", "a message"]."""
    names = {"reply": "the reply", "message": "a message"}
    return [f"step {_number(item.get('path', ''))}" if item.get("kind") == "step" else names[item["kind"]]
            for item in left]


def wire(left: list[dict]) -> dict:
    """`requirements` on an item: what's left, and whether the owner's Done may go."""
    return {"left": left, "ready": not left}


def _answer_body(event: dict) -> Optional[dict]:
    if event.get("kind") != "answer" or event.get("actor") != "owner":
        return None
    try:
        body = json.loads(event.get("text") or "")
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _time(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None


def questions_then(body: Any) -> bool:
    """Does any question of `body` carry `then: "steps"`?"""
    return any(isinstance(step.get("question"), dict) and step["question"].get("then") == "steps"
               for _, step, _ in _entries(body))


def step_waits(body: Any, events: list[dict], revisions: list) -> list[dict]:
    """Every wait a `then: "steps"` question of `body` started and no revision ended,
    whether or not its 10 minutes are over: {"id", "path", "until"} with `until` a
    datetime, in document order. A wait starts at the newest owner answer to that
    question (matched by step id, else by path) that changed its value, and lasts
    STEP_WAIT_S; a revision
    stamped after the answer, or a `revised` event after it, ends it."""
    revised = [e.get("seq") or 0 for e in events if e.get("kind") == "revised"]
    stamps = [t for t in (_time(getattr(r, "at", None)) for r in revisions) if t is not None]
    found = []
    for path, step, _ in _entries(body):
        question = step.get("question")
        if not isinstance(question, dict) or question.get("then") != "steps":
            continue
        step_id = step.get("id") if isinstance(step.get("id"), str) else None
        # The wait starts at the newest answer that changed the value: an unchanged
        # answer sent again (a resubmit) never starts it over.
        newest, value = None, None
        for event in sorted(events, key=lambda e: e.get("seq") or 0):
            answer = _answer_body(event)
            if answer is None:
                continue
            if (answer.get("id") == step_id) if step_id and isinstance(answer.get("id"), str) \
                    else answer.get("step") == path:
                given = (answer.get("picked"), answer.get("text"))
                if newest is None or given != value:
                    newest = event
                value = given
        at = _time(newest.get("at")) if newest else None
        if at is None:
            continue
        if any(seq > (newest.get("seq") or 0) for seq in revised) or any(t > at for t in stamps):
            continue
        found.append({"id": step_id, "path": path, "until": at + timedelta(seconds=STEP_WAIT_S)})
    return found


def step_wait(body: Any, events: list[dict], revisions: list, now: datetime) -> Optional[dict]:
    """The wait on the owner's Done (Pond 1.9.0), or None: of step_waits() still on at
    `now`, the one that ends first, as {"id", "path", "until"} with `until` in the
    stamps' ISO form."""
    live = [w for w in step_waits(body, events, revisions) if now < w["until"]]
    if not live:
        return None
    first = min(live, key=lambda w: w["until"])
    return {**first, "until": first["until"].isoformat(timespec="seconds")}
