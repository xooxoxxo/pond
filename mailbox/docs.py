"""Documents beside an ask (Pond 1.10.0).

A step's `file` or a link's `path` may carry `doc`, the fingerprint {"sha256", "bytes"}
of the Markdown or text document it points at. The fingerprint sits inside the
instructable, so the revision hash covers it. The text travels beside the
instructable, in the request's `docs` member {<sha256>: <text>}, and is stored by
hash. `check` decides whether a request's documents may be stored with it.

Errors name a field path or a well-formed hash, never any text: a document can hold
anything, and an error is shown and logged.
"""
import hashlib
from typing import Any, Mapping, Optional

from mailbox.instructable import _SHA256, MAX_DOC_BYTES, doc_refs, looks_secret

# The most document text one request may carry, counted in UTF-8 bytes.
MAX_DOCS_BYTES = 786432
# The most document text the Pond keeps in all (Pond 1.12.0): the sum of docs.bytes,
# each stored text counted once. A request whose new texts would go over it is
# refused (Store.check_doc_room); releasing or expiring texts frees room.
MAX_STORE_BYTES = 268435456
# A closed (done or dismissed) ask keeps its documents this many days after it closed;
# Service.sweep_docs then releases them. A reopen stops the clock.
DOCS_KEEP_DAYS = 7
SHAPE_ERROR = "docs: must be an object of SHA-256 hash to text"


def digest(text: str) -> tuple[str, int]:
    """(lowercase hex SHA-256, byte count) of the text's UTF-8 bytes. Raises
    UnicodeEncodeError for a string that is not text (a lone surrogate)."""
    body = text.encode("utf-8")
    return hashlib.sha256(body).hexdigest(), len(body)


def is_sha256(value: Any) -> bool:
    """Is `value` a hash as the Pond keys documents: 64 lowercase hex?"""
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def well_formed(sent: Any) -> bool:
    """Is `sent` an object of 64-lowercase-hex keys to strings?"""
    return isinstance(sent, dict) and all(is_sha256(key) and isinstance(value, str) for key, value in sent.items())


def _problem(sha256: str, text: str) -> tuple[Optional[str], Optional[int]]:
    """(the first problem of one sent document or None, its byte count or None
    when it is not text). The secret scan is apart (check runs it last)."""
    if "\x00" in text:
        return "not text", None
    try:
        actual, size = digest(text)
    except UnicodeEncodeError:
        return "not text", None
    if actual != sha256:
        return "does not match its hash", size
    if size > MAX_DOC_BYTES:
        return f"larger than {MAX_DOC_BYTES} bytes", size
    return None, size


def check(instructable: dict, sent: Optional[dict[str, str]], held: set[str], *,
          held_bytes: Optional[Mapping[str, int]] = None) -> list[str]:
    """The errors of a request's documents; [] when they may be stored with it.

    `instructable` is valid (it passed instructable.validate). `sent` is the request's
    `docs` member (None when absent). `held` is every hash the ask linked before (empty
    for a new ask): a `doc` with such a hash needs no text. `held_bytes`, when given,
    is the byte count the ask's earlier revisions gave each held hash, and a `doc`
    that reuses one must give the same.

    The order is fixed: first one error per `doc` in doc_refs order, then one per
    sent document in hash order, then the total. A malformed `docs` member is the
    single SHAPE_ERROR, which echoes nothing.

    Only a text the request would keep is scanned for secret-like values: one with no
    other problem that a step or link uses, in a request within MAX_DOCS_BYTES. The
    scan is the slow part, and a refused text needs none."""
    if sent is None:
        sent = {}
    if not well_formed(sent):
        return [SHAPE_ERROR]
    problems: dict[str, Optional[str]] = {}
    sizes: dict[str, int] = {}
    for sha256 in sorted(sent):
        problems[sha256], size = _problem(sha256, sent[sha256])
        if size is not None:
            sizes[sha256] = size
    refs = doc_refs(instructable)
    used = {sha256 for _, sha256, _ in refs}
    over = sum(sizes.values()) > MAX_DOCS_BYTES
    for sha256 in sorted(sent):
        if not over and problems[sha256] is None and sha256 in used and looks_secret(sent[sha256]):
            problems[sha256] = "secret-like value"
    errors: list[str] = []
    for field, sha256, size in refs:
        if sha256 in sent:
            known = sizes[sha256] if problems[sha256] is None else None   # a bad text is named below
        elif sha256 in held:
            known = (held_bytes or {}).get(sha256)
        else:
            errors.append(f"{field}.doc: no document with this hash")
            continue
        if known is not None and known != size:
            errors.append(f"{field}.doc.bytes: does not match the document")
    for sha256 in sorted(sent):
        if problems[sha256] is not None:
            errors.append(f"docs.{sha256}: {problems[sha256]}")
        elif sha256 not in used:
            errors.append(f"docs.{sha256}: not used by any step or link")
    if over:
        errors.append(f"docs: more than {MAX_DOCS_BYTES} bytes in all")
    return errors


def bodies(sent: Optional[dict[str, str]]) -> dict[str, bytes]:
    """The exact bytes to store for each sent document (after check passed)."""
    return {sha256: text.encode("utf-8") for sha256, text in (sent or {}).items()}
