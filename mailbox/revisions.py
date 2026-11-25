"""Revisions of an instructable (protocol 5): step ids, the canonical body and
its hash, the amendment rules and the change summary.

Pure functions, no I/O and no store. This is the one Python implementation of
the canonical form and of id assignment: the migration, the service and the
shared vectors all import it, and Hook hashes the served body string as is,
never re-encoding it. Errors name the field and never echo a value.
"""
import copy
import hashlib
import json
import re
from typing import Any, Optional

from mailbox.instructable import ID_PATTERN, IDS_ALL_OR_NONE, MULTI, SINGLE, _shape, looks_secret
from mailbox.instructable import validate as validate_instructable

US = "\x1f"
ID_RE = re.compile(ID_PATTERN)
MAX_CORRECTIONS = 10             # amendments per ask, counted apart from appends
MAX_APPENDS = 10                 # appends per ask (Pond 1.9.0), counted apart from corrections
MAX_REV = 21                     # rev 1, 10 corrections and 10 appends
MAX_APPEND_STEPS = 10            # steps one append call adds
MAX_APPEND_NOTE = 200            # an append's `note`: one line
MIN_APPEND_GAP_S = 15            # seconds since the ask's last revision of any kind, for an append
KIND_APPEND = "append"           # revision_meta.kind; a missing row is a correction
KIND_CORRECTION = "correction"
MIN_GAP_S = 60                   # seconds since the ask's last revision
MAX_REASON = 200
MAX_NOTE = 300                   # the owner key's label and any other short note
MAX_PROBLEM_NOTE = 8000          # one problem report, multi-line (Pond 1.4.0; 300 before)
MAX_PROBLEMS = 20
MAX_STEPS_NOTE = 1500
MAX_MESSAGE = 8000               # one owner message to the session (Pond 1.4.0; 1000 in 1.2.0)
MAX_MESSAGES = 50                # messages per ask

# The step keys a change summary names, in this order.
SUMMARY_FIELDS = ("do", "where", "code", "lang", "expect", "detail", "fields", "url", "file", "doc", "question",
                  "blocking", "warning", "optional", "required", "substeps")
# The top-level keys a summary names in `top`, in this order.
# `brief` (Pond 1.14.0) is reported here as why is: Hook reads it as "Background changed".
SUMMARY_TOP = ("why", "brief", "warning", "verify", "reply", "done", "links")
# Keys an amendment can never change.
FIXED_KEYS = ("title", "urgency", "from", "v")

STUB_WHY = ("This ask was corrected after it was posted. Update Hook to see the corrected steps; "
            "don't press Done here.")
STUB_STEP = "Update Hook: menu bar › Check for Updates…"


def canonical(obj: dict) -> str:
    """The canonical body: sorted keys, no spaces, non-ASCII kept as is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def revision_hash(rev: int, prev: str, at: str, reason: str, body: str) -> str:
    """Lowercase hex SHA-256 over the revision's fields, joined by US."""
    text = US.join(("hook-rev-v1", str(rev), prev, at, reason, body))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entries(ins: Any) -> list[tuple[str, dict, Optional[dict]]]:
    """(display path, step, parent or None) for every step and sub-step, in document order."""
    found: list[tuple[str, dict, Optional[dict]]] = []
    steps = ins.get("steps") if isinstance(ins, dict) else None
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict):
            continue
        found.append((f"{i + 1}", step, None))
        subs = step.get("substeps")
        for j, sub in enumerate(subs if isinstance(subs, list) else []):
            if isinstance(sub, dict):
                found.append((f"{i + 1}.{j + 1}", sub, step))
    return found


def step_ids(ins: Any) -> list[str]:
    """Every step id in document order: a step, then its sub-steps."""
    return [step["id"] for _, step, _ in _entries(ins) if isinstance(step.get("id"), str)]


def id_paths(ins: Any) -> dict[str, str]:
    """Step id → display path ("3", "3.2")."""
    return {step["id"]: path for path, step, _ in _entries(ins) if isinstance(step.get("id"), str)}


def question_at(ins: Any, step_id: str) -> Optional[dict]:
    """The question of the step with this id, or None."""
    for _, step, _ in _entries(ins):
        if step.get("id") == step_id:
            question = step.get("question")
            return question if isinstance(question, dict) else None
    return None


def assign_ids(ins: dict) -> tuple[dict, list[str]]:
    """At post: a full set of ids is kept as given; none at all are assigned by
    position (s1…s20, sub-steps s3.1); a partial set is refused. The input is
    never mutated. → (the instructable with ids, []) or ({}, [error])."""
    out = copy.deepcopy(ins)
    entries = _entries(out)
    given = sum(1 for _, step, _ in entries if step.get("id") is not None)
    if given == len(entries):
        return out, []
    if given:
        return {}, [IDS_ALL_OR_NONE]
    for path, step, _ in entries:
        step["id"] = f"s{path}"
    return out, []


def check_note(path: str, value: Any, limit: int, *, multi: bool = False) -> list[str]:
    """The shared rule for an amendment's `reason`, a problem's `note` and the
    `/steps` text: 1 to `limit` characters, one line unless `multi`, the
    hidden-character policy and the secret scan. [] when it is fine."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return [f"{path}: missing"]
    if not isinstance(value, str):
        return [f"{path}: must be a string"]
    if len(value) > limit:
        return [f"{path}: too long"]
    problem = _shape(path, value, MULTI if multi else SINGLE)
    if problem is not None:
        return [problem]
    if looks_secret(value):
        return [f"{path}: secret-like value"]
    return []


def _id_errors(new: dict, used_ids: set[str], current_ids: set[str]) -> list[str]:
    """Duplicates, and ids of earlier revisions that are not in the current one."""
    errors: list[str] = []
    seen: set[str] = set()
    steps = new.get("steps") if isinstance(new.get("steps"), list) else []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        entries = [(f"steps[{i}]", step)]
        subs = step.get("substeps")
        if isinstance(subs, list):
            entries += [(f"steps[{i}].substeps[{j}]", sub) for j, sub in enumerate(subs) if isinstance(sub, dict)]
        for path, entry in entries:
            value = entry.get("id")
            if value is None:
                continue
            if value in seen:
                errors.append(f"{path}.id: duplicate")
            elif value in used_ids and value not in current_ids:
                errors.append(f"{path}.id: used before")
            seen.add(value)
    return errors


def validate_amendment(
    current: dict, new: dict, used_ids: set[str], rev: int, reason: Any
) -> tuple[dict, dict, list[str]]:
    """Check a corrected instructable against the current one.

    `new` has passed instructable.validate(new_ids=True). `used_ids` holds every id of
    every earlier revision. `rev` is the revision this would become. `from` may be
    omitted and is then filled from `current`; title, urgency, from and v can't change.
    A step without an id is new and gets r<rev>-<n> (n counts new steps in document
    order, skipping any id in use). → (new with ids, summary, []) or ({}, {}, errors).
    """
    errors: list[str] = []
    out = copy.deepcopy(new)
    if "from" not in out and current.get("from") is not None:
        out["from"] = copy.deepcopy(current["from"])
    for key in FIXED_KEYS:
        if out.get(key) != current.get(key):
            errors.append(f"{key}: cannot change")
    errors += _id_errors(out, used_ids, set(step_ids(current)))
    errors += check_note("reason", reason, MAX_REASON)
    if errors:
        return {}, {}, errors
    taken = set(used_ids) | set(step_ids(out))
    n = 0
    for _, step, _ in _entries(out):
        if step.get("id") is not None:
            continue
        while True:
            n += 1
            candidate = f"r{rev}-{n}"
            if candidate not in taken:
                break
        step["id"] = candidate
        taken.add(candidate)
    return out, summarize(current, out), []


class Num(str):
    """A JSON number kept as its source text (parse_exact): the append rule compares
    numbers by text, so 1 and 1.0 differ and nothing goes through a float."""


def _no_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def parse_exact(text: str, object_pairs_hook: Any = None) -> Any:
    """`text` parsed with every number kept as a Num of its source text."""
    return json.loads(text, parse_int=Num, parse_float=Num, parse_constant=_no_constant,
                      object_pairs_hook=object_pairs_hook)


def _number_text(x: Any) -> Optional[str]:
    """A number's JSON text: a Num's source text, or how the Pond writes a Python number
    (canonical); None for anything that isn't a number (a bool is never one)."""
    if isinstance(x, Num):
        return str(x)
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return json.dumps(x)
    return None


def json_equal(a: Any, b: Any) -> bool:
    """Equal as JSON values, exactly (the shared append rule): key order never matters,
    strings and keys compare by code point (no normalisation), numbers by their text
    (1 is not 1.0), and true is never 1 (Python's == says it is)."""
    na, nb = _number_text(a), _number_text(b)
    if na is not None or nb is not None:
        return na is not None and nb is not None and na == nb
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def is_append(base: Any, new: Any) -> bool:
    """`new` only adds steps after the last step of `base` (Pond 1.9.0, the shared
    rule): it has more steps, every step of `base` is still there at its index as an
    equal JSON value (ids, sub-steps and unknown keys included), and every other
    top-level key is in both or in neither, with equal values (json_equal: exact).
    Parsed bodies only, never decoded models; a served body is read with parse_exact so
    its numbers keep their text."""
    if not isinstance(base, dict) or not isinstance(new, dict):
        return False
    old_steps, new_steps = base.get("steps"), new.get("steps")
    if not isinstance(old_steps, list) or not isinstance(new_steps, list) or len(new_steps) <= len(old_steps):
        return False
    if not all(json_equal(a, b) for a, b in zip(old_steps, new_steps)):
        return False
    keys = (set(base) | set(new)) - {"steps"}
    return all(k in base and k in new and json_equal(base[k], new[k]) for k in keys)


_STEP_PATH = re.compile(r"^steps\[(\d+)\]")


def to_request(error: str, offset: int) -> str:
    """An error on the merged body, with its step index made relative to the call."""
    found = _STEP_PATH.match(error)
    if found is None or int(found.group(1)) < offset:
        return error
    return f"steps[{int(found.group(1)) - offset}]" + error[found.end():]


def _new_entries(steps: list) -> list[tuple[str, dict]]:
    """(request path, entry) for each appended step and its sub-steps, in document order."""
    found: list[tuple[str, dict]] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        found.append((f"steps[{i}]", step))
        subs = step.get("substeps")
        if isinstance(subs, list):
            found += [(f"steps[{i}].substeps[{j}]", sub) for j, sub in enumerate(subs) if isinstance(sub, dict)]
    return found


def build_append(
    current: dict, steps: Any, used_ids: set[str], rev: int, note: Any
) -> tuple[dict, list[dict], list[str]]:
    """The body of an append (Pond 1.9.0): `current` with `steps` after its last step
    and every other key kept. `used_ids` holds every id of every earlier revision;
    `rev` is the revision this would become. A step or sub-step without an id gets
    r<rev>-<n>, numbered as validate_amendment numbers them. The whole result passes
    the post rules. Error paths name the call's steps (steps[0] is its first), except
    a limit on the whole result, which names the result as the post rules do.
    → (new body, added [{id, path}], []) or ({}, [], errors). Inputs are never mutated."""
    errors = check_note("note", note, MAX_APPEND_NOTE)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_APPEND_STEPS:
        return {}, [], errors + [f"steps: 1 to {MAX_APPEND_STEPS}"]
    added_steps = copy.deepcopy(steps)
    used = set(used_ids) | set(step_ids(current))
    flagged: set[str] = set()
    for path, entry in _new_entries(added_steps):
        if isinstance(entry.get("id"), str) and entry["id"] in used:
            errors.append(f"{path}.id: used before")
            flagged.add(f"{path}.id")
    out = copy.deepcopy(current)
    base = out.get("steps") if isinstance(out.get("steps"), list) else []
    out["steps"] = base + added_steps
    _, problems = validate_instructable(out, new_ids=True)
    for problem in problems:
        mapped = to_request(problem, len(base))
        if mapped.split(":", 1)[0] in flagged and mapped.endswith(": duplicate"):
            continue                                    # already named: used before
        errors.append(mapped)
    if errors:
        return {}, [], errors
    taken = used | set(step_ids(out))
    n = 0
    for _, entry in _new_entries(added_steps):
        if entry.get("id") is not None:
            continue
        while True:
            n += 1
            candidate = f"r{rev}-{n}"
            if candidate not in taken:
                break
        entry["id"] = candidate
        taken.add(candidate)
    added = [{"id": step["id"], "path": str(len(base) + i + 1)} for i, step in enumerate(added_steps)]
    return out, added, []


def _sub_ids(step: dict) -> list[str]:
    subs = step.get("substeps")
    return [s.get("id") for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []


def summarize(old: dict, new: dict) -> dict:
    """What changed between two revisions, by step id.

    changed: [{id, path (new), fields}] for steps in both whose SUMMARY_FIELDS differ
    (`substeps` when the parent's list of sub-step ids differs; a change inside a
    sub-step names the sub-step only); added: [{id, path}] (new paths); removed:
    [{id, path}] (old paths); moved: [{id, from, to}] in old document order; top: the
    SUMMARY_TOP keys whose values differ."""
    old_steps = {step.get("id"): (path, step) for path, step, _ in _entries(old)}
    new_steps = {step.get("id"): (path, step) for path, step, _ in _entries(new)}
    changed, added, removed, moved = [], [], [], []
    for step_id, (path, step) in new_steps.items():
        if step_id not in old_steps:
            added.append({"id": step_id, "path": path})
            continue
        before = old_steps[step_id][1]
        fields = [f for f in SUMMARY_FIELDS
                  if (f == "substeps" and _sub_ids(before) != _sub_ids(step))
                  or (f != "substeps" and before.get(f) != step.get(f))]
        if fields:
            changed.append({"id": step_id, "path": path, "fields": fields})
    for step_id, (path, _) in old_steps.items():
        if step_id not in new_steps:
            removed.append({"id": step_id, "path": path})
        elif new_steps[step_id][0] != path:
            moved.append({"id": step_id, "from": path, "to": new_steps[step_id][0]})
    top = [key for key in SUMMARY_TOP if old.get(key) != new.get(key)]
    return {"changed": changed, "added": added, "removed": removed, "moved": moved, "top": top}


def stub(title: str, urgency: str, origin: Optional[dict]) -> dict:
    """What an old client gets in place of a corrected ask: a one-step instructable
    telling the owner to update Hook (and not to press Done)."""
    out: dict[str, Any] = {
        "v": 1, "title": title, "urgency": urgency, "why": STUB_WHY,
        "steps": [{"do": STUB_STEP, "id": "s1"}],
    }
    if origin is not None:
        out["from"] = origin
    return out
