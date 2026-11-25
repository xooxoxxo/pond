"""Instructable v1: the structured form of an owner ask that Hook shows.

Pure functions, no I/O. The daemon is the single source of truth for the
format: POST /asks runs validate() and answers 400 with the errors it
returns. An error names WHERE the problem is and never echoes a value, so a
secret pasted by mistake is not sent back over the wire or into a log.
"""
import copy
import json
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

MAX_BYTES = 32768
MAX_STEPS = 20
MAX_CHOICES = 6
MAX_LINKS = 10
URGENCIES = ("now", "today", "whenever")

TOP_KEYS = {"v", "title", "why", "warning", "urgency", "steps", "verify", "reply", "done", "links", "from", "brief"}
# Pond 1.14.0: `brief`, the background a session would have told the owner in chat (the
# why, the context, the links it used): Markdown, multi-line, at most BRIEF_LIMIT
# characters, under the prose hidden-character rule and the secret scan. Hook shows it
# behind a "Background" disclosure, and "Ask about this" answers from it. It is a
# top-level key: no step identity (requirements.identities) ever covers it.
BRIEF_LIMIT = 8000
STEP_LIMITS = {"do": 300, "where": 60, "code": 4000, "lang": 20, "expect": 300, "detail": 1500}
STEP_KINDS = {"code": "code", "expect": "multi", "detail": "multi"}   # the rest are single-line prose
MAX_SUBSTEPS = 10
MAX_STEP_TOTAL = 60
MAX_QUESTIONS = 20
WARNING_LIMIT = 300                          # `warning`: multi-line, like detail
VERIFY_KEYS = set(STEP_LIMITS) | {"url"}     # verify: a leaf that asks and blocks nothing
LEAF_KEYS = VERIFY_KEYS | {"question", "blocking", "warning", "optional", "required", "file",
                           "doc", "id", "fields"}      # a sub-step
STEP_KEYS = LEAF_KEYS | {"substeps"}         # an entry of steps
QUESTION_KEYS = {"prompt", "choices", "multi", "then"}
# Pond 1.13.0: `fields`, a settings table on a step or sub-step (never verify). 1 to 20
# rows {label, value, copy?}: a label of 1 to 40 characters on one line, unique within
# the step; a value of 1 to 500 characters on one line, held to the hidden-character
# rule of `code` (no ZWJ or variation selector); copy a bool (absent means false).
FIELD_KEYS = {"label", "value", "copy"}
MAX_FIELDS = 20
FIELD_LABEL_LIMIT = 40
FIELD_VALUE_LIMIT = 500
# Pond 1.9.0: `then: "steps"` says the session adds steps after the owner answers; the
# owner's Done then waits for them (up to requirements.STEP_WAIT_S). "steps" is the only value.
QUESTION_THEN = ("steps",)
REPLY_KEYS = {"prompt", "choices"}
# Pond 1.4.0: a choice may be an object, a label plus the placeholder of the field the
# owner writes words in. A plain string is a choice without words, as before.
CHOICE_KEYS = {"label", "text"}
CHOICE_LABEL_LIMIT = 60
CHOICE_TEXT_LIMIT = 100
LINK_KEYS = {"label", "url", "path", "doc"}
# Pond 1.3.0: what `done` may hold, in this order. "steps": every non-optional step is
# finished; "reply": the reply prompt is answered; "message": the owner sent a message.
REQUIREMENTS = ("steps", "reply", "message")
DONE_SHAPE = f"done: must be a list of 1 to {len(REQUIREMENTS)} of {', '.join(REQUIREMENTS)}"
MAX_PATH = 500                               # `file` and a link's `path`: an absolute local path
# A `doc` beside `file` (step, sub-step) or a link's `path`: the fingerprint of that
# document, {"sha256": 64 lowercase hex, "bytes": 1 to MAX_DOC_BYTES}.
MAX_DOC_BYTES = 262144
DOC_KEYS = {"sha256", "bytes"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
FROM_KEYS = ("session", "host", "cwd")
# A step id (protocol 5): matched whole, unique within the ask, on steps and
# sub-steps but never on verify. revisions.ID_RE is this same pattern.
ID_PATTERN = r"[a-z0-9][a-z0-9.-]{0,23}"
_ID = re.compile(ID_PATTERN)
# An id that reads as a step number ("2", "3.1") is refused: `step` in an answer or a
# problem report may be a path or an id, and such an id would land on another step.
PATHLIKE_ID = re.compile(r"\d+(?:\.\d+)?")
PATHLIKE_ID_ERROR = "must not look like a step number"
IDS_ALL_OR_NONE = "steps: ids must be given for every step or for none"

SECRET_PATTERNS = tuple(re.compile(p) for p in (
    r"sk-ant-",
    r"\bsk-[A-Za-z0-9]{20,}",
    r"xox[abp]-",
    r"ghp_",
    r"github_pat_",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"AKIA[0-9A-Z]{16}",
))


# Shared hidden-character policy, kept identical to Relay's Swift validator:
# a code point is hidden when its general category is a control, format,
# surrogate, private-use, unassigned or line/paragraph separator, or when it
# is Default_Ignorable_Code_Point. TAB and LF are always allowed.
_HIDDEN_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
# Default_Ignorable_Code_Point outside Cf, plus lone surrogates for clarity.
# Ranges are inclusive (first, last) code points.
_HIDDEN_RANGES = (
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8),
    (0xD800, 0xDFFF),                      # lone UTF-16 surrogates
    (0xE0000, 0xE0FFF),                    # tags, variation selectors, reserved
)
_ALWAYS_ALLOWED = frozenset((0x09, 0x0A))
# Prose may keep what emoji need: ZWJ and the text/emoji presentation selectors.
_PROSE_ALLOWED = frozenset((0x200D, 0xFE0E, 0xFE0F))


def _is_hidden(ch: str) -> bool:
    """Is this code point hidden by the policy (before field exceptions)?"""
    cp = ord(ch)
    if cp in _ALWAYS_ALLOWED:
        return False
    if unicodedata.category(ch) in _HIDDEN_CATEGORIES:
        return True
    return any(lo <= cp <= hi for lo, hi in _HIDDEN_RANGES)

CODE, MULTI, SINGLE, PATH = "code", "multi", "single", "path"   # field classes for _text
# PATH: a single line where even the emoji joiners count as hidden (a file name
# shown on Telegram and in Hook must read as what it is).


def _hidden(text: str, code: bool) -> int | None:
    """The first code point the policy hides for this field class, or None."""
    for ch in text:
        cp = ord(ch)
        if not code and cp in _PROSE_ALLOWED:
            continue
        if _is_hidden(ch):
            return cp
    return None


def _shape(path: str, text: str, kind: str) -> str | None:
    """The hidden-character or line-rule error for a string field, or None.
    Names the first offending code point; never the value."""
    cp = _hidden(text, code=(kind in (CODE, PATH)))
    if cp is not None:
        return f"{path}: contains hidden or control characters (U+{cp:04X})"
    if kind in (SINGLE, PATH) and ("\n" in text or "\t" in text):
        return f"{path}: must be a single line"
    return None


def _skeleton(text: str) -> str:
    """NFKC with every hidden and prose-allowed code point removed, so a token
    split by ZWJ or written in fullwidth forms reads as itself."""
    try:
        text = unicodedata.normalize("NFKC", text)
    except (TypeError, ValueError):        # defensive: keep the raw text
        pass
    return "".join(ch for ch in text if ord(ch) not in _PROSE_ALLOWED and not _is_hidden(ch))


def looks_secret(text: str) -> bool:
    """True when the text, or its skeleton, holds anything shaped like a credential.
    The skeleton is built once, and only when the raw text holds no such shape."""
    if any(p.search(text) for p in SECRET_PATTERNS):
        return True
    skeleton = _skeleton(text)
    return any(p.search(skeleton) for p in SECRET_PATTERNS)


def _name(key: Any) -> str:
    """A key as it may appear in an error: unknown keys are echoed, so a key
    that itself looks like a secret is redacted and a long one is cut."""
    text = str(key)
    if looks_secret(text) or _hidden(text, code=True) is not None:
        return "<redacted>"
    return text[:60]


def _join(path: str, key: Any) -> str:
    return f"{path}.{_name(key)}" if path else _name(key)


def _keys(errors: list[str], path: str, obj: Any, allowed: set[str]) -> bool:
    """Is obj an object holding only allowed keys? Each unknown key is an error."""
    if not isinstance(obj, dict):
        errors.append(f"{path}: must be an object")
        return False
    for key in obj:
        if key not in allowed:
            errors.append(f"{_join(path, key)}: unknown key")
    return True


def _text(errors: list[str], path: str, value: Any, limit: int | None = None,
          *, required: bool = False, kind: str = SINGLE) -> None:
    """A string of at most `limit` characters; null counts as absent.
    `kind` (CODE, MULTI or SINGLE) picks the hidden-character and line rules."""
    if value is None:
        if required:
            errors.append(f"{path}: required")
    elif not isinstance(value, str):
        errors.append(f"{path}: must be a string")
    elif required and not value.strip():
        errors.append(f"{path}: must not be empty")
    elif limit is not None and len(value) > limit:
        errors.append(f"{path}: longer than {limit} characters")
    elif (problem := _shape(path, value, kind)) is not None:
        errors.append(problem)


def _url(errors: list[str], path: str, value: Any, *, required: bool = False) -> None:
    """An absolute http(s) URL."""
    before = len(errors)
    _text(errors, path, value, required=required)
    if value is None or len(errors) > before:
        return
    try:
        parts = urlparse(value)
    except ValueError:                     # e.g. "http://[" — an unclosed IPv6 host
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        errors.append(f"{path}: must be an http(s) URL")


NETWORK_ROOTS = ("/net", "/network", "/home")   # autofs and Finder network roots: no fetch on open
FIRMLINK_PREFIXES = ("/private/var/automount", "/var/automount", "/private", "/system/volumes/data")


def _network_form(value: str) -> str:
    """The lexical form the network test compares (N1): runs of "/" collapsed into one,
    lowercased (APFS ignores case), and /private, /var/automount, /private/var/automount
    and /System/Volumes/Data prefixes stripped in a loop until nothing changes, since
    each leads back to the same roots, however they are stacked."""
    form = re.sub(r"/+", "/", value).lower()
    changed = True
    while changed:
        changed = False
        for prefix in FIRMLINK_PREFIXES:
            if form == prefix or form.startswith(prefix + "/"):
                form = form[len(prefix):] or "/"
                changed = True
                break
    return form


def _path(errors: list[str], path: str, value: Any) -> None:
    """An absolute local path ("/…" or "~/…"): one line of 1 to MAX_PATH characters,
    no hidden characters, not even ZWJ or a variation selector, no "." or ".." segment
    (P1), no first segment starting with "." such as /.vol or /.nofollow (N2), a first segment that is plain ASCII (N3: case folding
    turns "ſystem", "Syﬆem" or the Kelvin sign into ASCII letters), and not
    /net, /Network or /home or under them, compared case-insensitively after collapsing
    "//" and stripping /private, /var/automount, /private/var/automount and /System/Volumes/Data prefixes in a loop
    (N1: opening it would reach out to a server). Null counts as absent."""
    before = len(errors)
    _text(errors, path, value, MAX_PATH, kind=PATH)
    if value is None or len(errors) > before:
        return
    segments = [segment for segment in value.split("/") if segment]
    form = _network_form(value)
    if not (value.startswith("/") or value.startswith("~/")):
        errors.append(f"{path}: must be an absolute path")
    elif any(segment in (".", "..") for segment in value.split("/")):
        errors.append(f"{path}: must not contain . or .. segments")
    elif value.startswith("/") and segments and segments[0].startswith("."):
        errors.append(f"{path}: can't start with a hidden folder")
    elif value.startswith("/") and segments and not segments[0].isascii():
        errors.append(f"{path}: the first folder name must be plain ASCII")
    elif value.startswith("/") and (form in NETWORK_ROOTS
                                    or form.startswith(tuple(r + "/" for r in NETWORK_ROOTS))):
        errors.append(f"{path}: can't be a network path")


def _doc(errors: list[str], path: str, holder: dict, anchor: str) -> None:
    """The `doc` of a step, sub-step or link (`path` names that holder): it needs the
    holder's `anchor` (`file` or `path`) to be given, then is an object holding only a
    `sha256` of 64 lowercase hex characters and `bytes`, a whole number from 1 to
    MAX_DOC_BYTES (true and false are not numbers). Null counts as absent."""
    doc = holder.get("doc")
    if doc is None:
        return
    path = f"{path}.doc"
    if holder.get(anchor) is None:
        errors.append(f"{path}: needs {anchor}")
        return
    if not _keys(errors, path, doc, DOC_KEYS):
        return
    sha256 = doc.get("sha256")
    if not (isinstance(sha256, str) and _SHA256.fullmatch(sha256)):
        errors.append(f"{path}.sha256: must be 64 lowercase hex characters")
    size = doc.get("bytes")
    if type(size) is not int or not 1 <= size <= MAX_DOC_BYTES:   # `type`: True is an int
        errors.append(f"{path}.bytes: must be a whole number from 1 to {MAX_DOC_BYTES}")


def doc_refs(obj: dict) -> list[tuple[str, str, int]]:
    """Every `doc` of a valid instructable as (field path of its holder, sha256, bytes):
    the steps in order, each before its sub-steps, then the links."""
    holders: list[tuple[str, Any]] = []
    for i, step in enumerate(obj.get("steps") or []):
        holders.append((f"steps[{i}]", step))
        subs = step.get("substeps") if isinstance(step, dict) else None
        holders += [(f"steps[{i}].substeps[{j}]", sub) for j, sub in enumerate(subs or [])]
    holders += [(f"links[{i}]", link) for i, link in enumerate(obj.get("links") or [])]
    return [(path, h["doc"]["sha256"], h["doc"]["bytes"]) for path, h in holders
            if isinstance(h, dict) and isinstance(h.get("doc"), dict)]


def doc_keys(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every `doc` key anywhere in `obj`, in document order, as (field path of its
    holder, its value), a null value included: the documents switch counts a `doc` key
    that is present, whatever it holds."""
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        if "doc" in obj:
            found.append((path, obj["doc"]))
        for key, value in obj.items():
            found += doc_keys(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found += doc_keys(value, f"{path}[{i}]")
    return found


def _fields(errors: list[str], path: str, fields: Any) -> None:
    """A step's `fields` (Pond 1.13.0): a list of 1 to MAX_FIELDS rows, each an object
    holding only label, value and copy. `label` is required, at most FIELD_LABEL_LIMIT
    characters on one line, and unique within the list (compared exactly); `value` is
    required, at most FIELD_VALUE_LIMIT characters on one line, with the hidden-character
    rule of `code`; `copy` is true or false. Null counts as absent. The secret scan
    covers both strings, as it covers every string."""
    if fields is None:
        return
    path = f"{path}.fields"
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        errors.append(f"{path}: must be a list of 1 to {MAX_FIELDS} rows")
        return
    seen: set[str] = set()
    for i, row in enumerate(fields):
        at = f"{path}[{i}]"
        if not _keys(errors, at, row, FIELD_KEYS):
            continue
        before = len(errors)
        _text(errors, f"{at}.label", row.get("label"), FIELD_LABEL_LIMIT, required=True)
        label = row.get("label")
        if len(errors) == before and isinstance(label, str):
            if label in seen:
                errors.append(f"{at}.label: duplicate label")
            seen.add(label)
        _text(errors, f"{at}.value", row.get("value"), FIELD_VALUE_LIMIT, required=True, kind=PATH)
        copy_ = row.get("copy")
        if copy_ is not None and type(copy_) is not bool:
            errors.append(f"{at}.copy: must be true or false")


def _question(errors: list[str], path: str, question: Any) -> None:
    """A step's question: a prompt, then 0 to 6 distinct choices (none asks for free
    text), `multi`, which allows several picks and needs at least 2 choices, and
    `then` (Pond 1.9.0), whose only value is "steps"."""
    if not _keys(errors, path, question, QUESTION_KEYS):
        return
    _text(errors, f"{path}.prompt", question.get("prompt"), 300, required=True, kind=MULTI)
    choices = question.get("choices")
    count: int | None = 0                  # None: the choices are malformed
    if choices is not None:
        if not isinstance(choices, list) or len(choices) > MAX_CHOICES:
            errors.append(f"{path}.choices: must be a list of at most {MAX_CHOICES} choices")
            count = None
        else:
            seen: set[str] = set()
            multi = question.get("multi") is True
            for i, choice in enumerate(choices):
                _choice(errors, f"{path}.choices[{i}]", choice, seen, multi=multi)
            count = len(choices)
    multi = question.get("multi")
    if multi is not None and type(multi) is not bool:
        errors.append(f"{path}.multi: must be true or false")
    elif multi is True and count is not None and count < 2:
        errors.append(f"{path}.multi: needs at least 2 choices")
    then = question.get("then")
    if then is not None and not (isinstance(then, str) and then in QUESTION_THEN):
        errors.append(f'{path}.then: must be "steps"')


def _choice(errors: list[str], path: str, choice: Any, seen: set[str] | None,
            *, multi: bool = False) -> None:
    """One entry of a `choices` list: a string of 1 to 60 characters on one line, or
    (Pond 1.4.0) an object {label, text}, both required: the label follows the string
    rules and `text`, the placeholder of the owner's words, is 1 to 100 characters on
    one line. A choice with text can't be in a multi question. With `seen`, labels must
    be distinct (a string and an object's label alike)."""
    before = len(errors)
    if isinstance(choice, dict):
        _keys(errors, path, choice, CHOICE_KEYS)
        _text(errors, f"{path}.label", choice.get("label"), CHOICE_LABEL_LIMIT, required=True)
        _text(errors, f"{path}.text", choice.get("text"), CHOICE_TEXT_LIMIT, required=True)
        if multi:
            errors.append(f"{path}: a choice with text can't be in a multi question")
    elif choice is None or isinstance(choice, str):
        _text(errors, path, choice, CHOICE_LABEL_LIMIT, required=True)
    else:
        errors.append(f"{path}: must be a string or {{label, text}}")
    label = choice_label(choice)
    if seen is not None and len(errors) == before and label is not None:
        if label in seen:
            errors.append(f"{path}: duplicate choice")
        seen.add(label)


def choice_label(choice: Any) -> str | None:
    """A choice's label: the string itself, or an object's `label`; None otherwise."""
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict) and isinstance(choice.get("label"), str):
        return choice["label"]
    return None


def choice_text(choice: Any) -> str | None:
    """The placeholder of a choice that takes words (an object's `text`), else None."""
    if isinstance(choice, dict) and isinstance(choice.get("text"), str):
        return choice["text"]
    return None


def choice_labels(choices: Any) -> list[str]:
    """The labels of a `choices` list, in order; [] for anything that isn't a list."""
    if not isinstance(choices, list):
        return []
    return [label for label in (choice_label(c) for c in choices) if label is not None]


def _choice_lists(instructable: Any) -> list[list]:
    """Every `choices` list of an instructable: each step's and sub-step's question, then the reply's."""
    found: list[list] = []
    steps = instructable.get("steps") if isinstance(instructable, dict) else None
    for step in _every_step(steps if isinstance(steps, list) else []):
        question = step.get("question")
        if isinstance(question, dict) and isinstance(question.get("choices"), list):
            found.append(question["choices"])
    reply = instructable.get("reply") if isinstance(instructable, dict) else None
    if isinstance(reply, dict) and isinstance(reply.get("choices"), list):
        found.append(reply["choices"])
    return found


def has_choice_objects(instructable: Any) -> bool:
    """Does any question or the reply offer a choice object (Pond 1.4.0)?"""
    return any(isinstance(c, dict) for choices in _choice_lists(instructable) for c in choices)


def labels_only(body: Any) -> Any:
    """A copy of an instructable with every question's and the reply's `choices` as
    plain labels: what a client before protocol 6 reads in place of choice objects.
    The input is never mutated."""
    out = copy.deepcopy(body)
    for choices in _choice_lists(out):
        choices[:] = choice_labels(choices)
    return out


def _step(errors: list[str], path: str, step: Any, allowed: set[str] = LEAF_KEYS,
          *, in_optional: bool = False) -> None:
    """One Step: `do` required, the rest optional, each within its limit. An entry of
    `steps` (allowed = STEP_KEYS) may hold 1 to 10 sub-steps, which are leaves: they can't
    nest, and `verify` can't have them (`substeps` is then an unknown key). A step or
    sub-step may ask a `question` and carry `blocking` (a bool: later steps wait for it)
    a `warning` (multi-line text) and `optional` (a bool, never together with
    blocking: the owner may mark it done without doing it), `required` (a bool, never
    together with optional: Done waits until it is finished), and `file` (an absolute
    local path to a document Hook opens) with its `doc` fingerprint, and `fields` (Pond
    1.13.0, a settings table); `verify` (allowed = VERIFY_KEYS) can't.
    An optional parent makes its sub-steps optional (`in_optional`), so none of them
    may be blocking or required. A required parent with no question needs a sub-step
    that isn't optional. Errors come in this order: the STEP_LIMITS keys, url, fields,
    file, doc, blocking, optional, required, warning, question, substeps: the reference order
    Hook follows."""
    if not _keys(errors, path, step, allowed):
        return
    for key, limit in STEP_LIMITS.items():
        _text(errors, f"{path}.{key}", step.get(key), limit, required=(key == "do"),
              kind=STEP_KINDS.get(key, SINGLE))
    _url(errors, f"{path}.url", step.get("url"))
    if "fields" in allowed:
        _fields(errors, path, step.get("fields"))
    if "file" in allowed:
        _path(errors, f"{path}.file", step.get("file"))
        _doc(errors, path, step, "file")
    if "blocking" in allowed:
        blocking = step.get("blocking")
        if blocking is not None and type(blocking) is not bool:
            errors.append(f"{path}.blocking: must be true or false")
        elif blocking is True and in_optional:
            errors.append(f"{path}.blocking: can't be blocking inside an optional step")
        optional = step.get("optional")
        if optional is not None and type(optional) is not bool:
            errors.append(f"{path}.optional: must be true or false")
        elif optional is True and blocking is True:
            errors.append(f"{path}.optional: can't be optional and blocking")
        _required(errors, path, step, in_optional=in_optional)
        _text(errors, f"{path}.warning", step.get("warning"), WARNING_LIMIT, kind=MULTI)
    if "question" in allowed and step.get("question") is not None:
        _question(errors, f"{path}.question", step["question"])
    if "substeps" in allowed and step.get("substeps") is not None:
        subs = step["substeps"]
        if not isinstance(subs, list) or not 1 <= len(subs) <= MAX_SUBSTEPS:
            errors.append(f"{path}.substeps: must be a list of 1 to {MAX_SUBSTEPS} steps")
        else:
            for j, sub in enumerate(subs):
                _step(errors, f"{path}.substeps[{j}]", sub, LEAF_KEYS,
                      in_optional=step.get("optional") is True)


def _required(errors: list[str], path: str, step: dict, *, in_optional: bool) -> None:
    """A step's `required` (Pond 1.3.0): a bool, never beside optional or inside an
    optional parent, and never on a parent with nothing to finish: no question of its
    own and every sub-step optional. A leaf always has a tick or an answer to give."""
    required = step.get("required")
    if required is None:
        return
    if type(required) is not bool:
        errors.append(f"{path}.required: must be true or false")
    elif required is True and step.get("optional") is True:
        errors.append(f"{path}.required: can't be required and optional")
    elif required is True and in_optional:
        errors.append(f"{path}.required: can't be required inside an optional step")
    elif required is True and step.get("question") is None and _all_optional(step.get("substeps")):
        errors.append(f"{path}.required: has nothing to finish (every sub-step is optional)")


def _all_optional(steps: Any) -> bool:
    """A non-empty list whose entries are all steps with optional: true."""
    return (isinstance(steps, list) and bool(steps)
            and all(isinstance(s, dict) and s.get("optional") is True for s in steps))


def _done(errors: list[str], obj: dict) -> None:
    """The ask's `done` list (Pond 1.3.0): 1 to 3 distinct REQUIREMENTS. "reply" needs
    a reply prompt, and "steps" needs a step that isn't optional. Null counts as absent;
    errors name the index, never the value."""
    done = obj.get("done")
    if done is None:
        return
    if not isinstance(done, list) or not 1 <= len(done) <= len(REQUIREMENTS):
        errors.append(DONE_SHAPE)
        return
    seen: list[str] = []
    for i, entry in enumerate(done):
        if not isinstance(entry, str) or entry not in REQUIREMENTS:
            errors.append(f"done[{i}]: must be steps, reply or message")
        elif entry in seen:
            errors.append(f"done[{i}]: duplicate")
        elif entry == "reply" and obj.get("reply") is None:
            errors.append(f"done[{i}]: reply needs a reply prompt")
        elif entry == "steps" and _all_optional(obj.get("steps")):
            errors.append(f"done[{i}]: steps has nothing to finish (every step is optional)")
        if isinstance(entry, str):
            seen.append(entry)


def _secrets(errors: list[str], path: str, value: Any) -> None:
    """Every string anywhere in the document, checked against SECRET_PATTERNS."""
    if isinstance(value, str):
        if looks_secret(value):
            errors.append(f"{path}: secret-like value")
    elif isinstance(value, dict):
        for key, inner in value.items():
            _secrets(errors, _join(path, key), inner)
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            _secrets(errors, f"{path}[{i}]", inner)


def _every_step(steps: list) -> list[dict]:
    """Each object in `steps`, followed by the objects in its `substeps` list."""
    found: list[dict] = []
    for step in steps:
        if isinstance(step, dict):
            found.append(step)
            if isinstance(step.get("substeps"), list):
                found.extend(sub for sub in step["substeps"] if isinstance(sub, dict))
    return found


def _ids(errors: list[str], steps: list, *, new_ids: bool = False) -> None:
    """The step ids: each a string matching ID_PATTERN whole, not reading as a step
    number (PATHLIKE_ID) and used once, in document order (a step, then its sub-steps),
    then the all-or-none rule. With `new_ids` (an
    amendment) a step may come without an id: it is new and is numbered then. Null
    counts as absent."""
    seen: set[str] = set()
    given = total = 0
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        entries = [(f"steps[{i}]", step)]
        subs = step.get("substeps")
        if isinstance(subs, list):
            entries += [(f"steps[{i}].substeps[{j}]", sub) for j, sub in enumerate(subs) if isinstance(sub, dict)]
        for path, entry in entries:
            total += 1
            value = entry.get("id")
            if value is None:
                continue
            given += 1
            if not isinstance(value, str):
                errors.append(f"{path}.id: must be a string")
            elif not _ID.fullmatch(value):
                errors.append(f"{path}.id: must match {ID_PATTERN}")
            elif PATHLIKE_ID.fullmatch(value):
                errors.append(f"{path}.id: {PATHLIKE_ID_ERROR}")
            elif value in seen:
                errors.append(f"{path}.id: duplicate")
            else:
                seen.add(value)
    if not new_ids and 0 < given < total:
        errors.append(IDS_ALL_OR_NONE)


def no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    """object_pairs_hook for json.loads that refuses a key given twice in one object
    (strict parsing: the Pond and Hook must never read different values)."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def validate(obj: Any, *, new_ids: bool = False) -> tuple[dict, list[str]]:
    """Check an instructable against v1.

    → (the instructable with defaults filled in, []) when it is valid,
      ({}, ["<field path>: <problem>", …]) when it is not.
    `new_ids` lets steps come without ids next to steps with them (an amendment).
    """
    if not isinstance(obj, dict):
        return {}, ["instructable: must be an object"]
    # surrogatepass: json.loads accepts a lone "\ud800", which strict UTF-8 cannot encode
    if len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass")) > MAX_BYTES:
        return {}, [f"instructable: larger than {MAX_BYTES} bytes"]

    errors: list[str] = []
    _keys(errors, "", obj, TOP_KEYS)

    v = obj.get("v")
    if type(v) is not int or v != 1:       # `type`, not isinstance: True == 1 in Python
        errors.append("v: must be 1")
    _text(errors, "title", obj.get("title"), 120, required=True)
    _text(errors, "why", obj.get("why"), 300, kind=MULTI)
    _text(errors, "warning", obj.get("warning"), WARNING_LIMIT, kind=MULTI)
    _text(errors, "brief", obj.get("brief"), BRIEF_LIMIT, kind=MULTI)
    urgency = obj.get("urgency")
    if urgency is not None and urgency not in URGENCIES:
        errors.append("urgency: must be one of now, today, whenever")

    steps = obj.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        errors.append(f"steps: must be a list of 1 to {MAX_STEPS} steps")
    else:
        for i, step in enumerate(steps):
            _step(errors, f"steps[{i}]", step, STEP_KEYS)
        # Counted as Relay counts: every substeps list, even one out of range; a
        # value that isn't a list counts as 0.
        total = len(steps) + sum(len(s["substeps"]) for s in steps
                                 if isinstance(s, dict) and isinstance(s.get("substeps"), list))
        if total > MAX_STEP_TOTAL:
            errors.append(f"steps: more than {MAX_STEP_TOTAL} steps and sub-steps in total")
        asked = sum(1 for s in _every_step(steps) if s.get("question") is not None)
        if asked > MAX_QUESTIONS:
            errors.append(f"steps: more than {MAX_QUESTIONS} questions in total")
        _ids(errors, steps, new_ids=new_ids)
    if obj.get("verify") is not None:
        _step(errors, "verify", obj["verify"], VERIFY_KEYS)

    reply = obj.get("reply")
    if reply is not None and _keys(errors, "reply", reply, REPLY_KEYS):
        _text(errors, "reply.prompt", reply.get("prompt"), 300, required=True, kind=MULTI)
        # choices is required (C1); [] asks for free text, as in schema/instructable.v1.json
        choices = reply.get("choices")
        if not isinstance(choices, list) or len(choices) > MAX_CHOICES:
            errors.append(f"reply.choices: must be a list of at most {MAX_CHOICES} choices")
        else:
            # A reply list of plain strings keeps the 1.3.0 rules (no duplicate check);
            # one that holds a choice object needs distinct labels.
            seen: set[str] | None = set() if any(isinstance(c, dict) for c in choices) else None
            for i, choice in enumerate(choices):
                _choice(errors, f"reply.choices[{i}]", choice, seen)
    _done(errors, obj)

    links = obj.get("links")
    if links is not None:
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            errors.append(f"links: must be a list of at most {MAX_LINKS} links")
        else:
            for i, link in enumerate(links):
                path = f"links[{i}]"
                if _keys(errors, path, link, LINK_KEYS):
                    _text(errors, f"{path}.label", link.get("label"), 60, required=True)
                    has_url, has_path = link.get("url") is not None, link.get("path") is not None
                    if has_url == has_path:            # exactly one of url and path
                        errors.append(f"{path}: needs url or path, not both")
                    elif has_url:
                        _url(errors, f"{path}.url", link.get("url"), required=True)
                    else:
                        _path(errors, f"{path}.path", link.get("path"))
                    _doc(errors, path, link, "path")

    origin = obj.get("from")
    if origin is not None and _keys(errors, "from", origin, set(FROM_KEYS)):
        for key in FROM_KEYS:
            _text(errors, f"from.{key}", origin.get(key))

    _secrets(errors, "", obj)
    if errors:
        return {}, errors
    clean = dict(obj)
    if clean.get("urgency") is None:
        clean["urgency"] = "today"
    return clean, []


MAX_PROJECT = 60


def validate_project(project: Any) -> list[str]:
    """The `project` sent beside an instructable: a short single line with no
    hidden characters and nothing secret-like. [] when it is fine."""
    errors: list[str] = []
    _text(errors, "project", project, MAX_PROJECT, required=True)
    if not errors and looks_secret(project):
        errors.append("project: secret-like value")
    return errors
