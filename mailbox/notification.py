"""Notification v1: what Central Time core posts to POST /notify, a reminder
with its actions or the morning brief. Pure validation in the style of
instructable.validate: an error names the field, never the value. The
hidden-character and secret rules are the instructable ones, unchanged.

The household instance (household=True) takes reminders only, addressed with
`to` (a member's Telegram user id) and carrying done, accept or decline."""
import re
from typing import Any

from mailbox.instructable import MULTI, _keys, _secrets, _text

KINDS = ("reminder", "brief")
ACTION_IDS = ("done", "tomorrow")
HH_ACTION_IDS = ("done", "accept", "decline")
MAX_TITLE = 200
MAX_BODY = 2000
MAX_LABEL = 30
MAX_ACTIONS = 2
TOP_KEYS = {"kind", "ref", "title", "body", "actions"}
HH_TOP_KEYS = TOP_KEYS | {"to"}
ACTION_KEYS = {"id", "label"}
_REF = re.compile(r"[A-Za-z0-9:._-]{1,64}")  # used with fullmatch: no trailing newline


def validate(obj: Any, household: bool = False) -> tuple[dict[str, Any], list[str]]:
    """→ ({kind, ref, title, body, actions[, to]}, []) when valid, ({}, errors) when not.
    household=False is the asks instance, unchanged; True adds `to` and the household actions."""
    if not isinstance(obj, dict):
        return {}, ["notification: must be an object"]
    errors: list[str] = []
    _keys(errors, "", obj, HH_TOP_KEYS if household else TOP_KEYS)
    kind = obj.get("kind")
    if kind not in KINDS:
        errors.append("kind: must be one of reminder, brief")
    elif household and kind != "reminder":
        errors.append("kind: a household instance takes reminders only")
    ref = obj.get("ref")
    if not isinstance(ref, str) or not _REF.fullmatch(ref):
        errors.append("ref: must be 1 to 64 letters, digits, colon, dot, dash or underscore")
    _text(errors, "title", obj.get("title"), MAX_TITLE, required=True)
    _text(errors, "body", obj.get("body"), MAX_BODY, kind=MULTI)
    actions = obj.get("actions")
    if actions is None:
        actions = []
    ids = HH_ACTION_IDS if household else ACTION_IDS
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        errors.append(f"actions: must be a list of at most {MAX_ACTIONS} actions")
    else:
        for i, action in enumerate(actions):
            path = f"actions[{i}]"
            if _keys(errors, path, action, ACTION_KEYS):
                if action.get("id") not in ids:
                    errors.append(f"{path}.id: must be one of {', '.join(ids)}")
                _text(errors, f"{path}.label", action.get("label"), MAX_LABEL, required=True)
        if kind == "brief" and actions:
            errors.append("actions: a brief carries no actions")
    _secrets(errors, "", obj)
    if household:
        to = obj.get("to")
        if not isinstance(to, int) or isinstance(to, bool) or to <= 0:
            errors.append("to: must be a Telegram user id")
    if errors:
        return {}, errors
    clean = {"kind": kind, "ref": ref, "title": obj["title"], "body": obj.get("body"), "actions": actions}
    if household:
        clean["to"] = obj["to"]
    return clean, []
