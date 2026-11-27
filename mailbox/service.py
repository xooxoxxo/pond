"""Service layer: business logic and orchestration."""
import hashlib
import hmac
import html
import json
import logging
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Optional
from uuid import uuid4

from mailbox import FEATURES, ask_about, capture, household, owner
from mailbox import docs as documents             # `docs` is the request member here
from mailbox import requirements as req         # `requirements` is wire_item's flag
from mailbox import ADMISSION_FEATURE
from mailbox.admission import Admission
from mailbox.answers import current as current_answers
from mailbox.answers import questions, validate_answers
from mailbox.clock import household_zone
from mailbox.ct_client import CentralTimeClient, CentralTimeError
from mailbox.instructable import choice_labels, doc_keys, doc_refs, has_choice_objects, labels_only, looks_secret
from mailbox.instructable import validate as validate_instructable
from mailbox import revisions
from mailbox.notification import ACTION_IDS
from mailbox.store import (
    AskNotOpen, Item, Notification, OwnerKey, Revision, RevokeRequest, StaleRevision, Store, StoreFull,
)
from mailbox.telegram import TelegramClient, TelegramError

logger = logging.getLogger("mailbox")

# A forwarded message is someone else's words: never captured.
_FORWARD_KEYS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name")

# What the owner sees after a tap Central Time refused with a 409, by its
# reason. These refuse ONE action (Tomorrow); Done may still apply, so the
# keyboard stays. `closed` and a 404 end the reminder and are handled apart.
_ACK_REFUSED = {
    "recurring": "Not possible: it repeats",
    "stale": "Not possible: it was already moved",
    "untimed": "Not possible: it has no time now",
    "unanchored": "Not possible: it has no time now",
}
# Outcomes that end a reminder for both actions: the message closes and a
# later tap is "Already handled".
_ACK_ENDED = {
    (409, "closed"): ("closed", "Already closed in Central Time"),
    (404, None): ("gone", "Gone from Central Time"),
}
_TAP_ANSWERS = {"done": "Done", "tomorrow": "Moved to tomorrow"}
# The toast for a Done or answer tap on a message of a version that isn't in force.
CHANGED_TOAST = "This ask changed. Use the newest message."
# The in-thread answer to a verdict word replied to such a message.
OUT_OF_DATE = "This message is out of date. Reply to the newest one."
# The owner's Done while the ask still needs something (Pond 1.3.0, RD 18): the toast
# for a Done tap, and the in-thread answer to a verdict word, which is kept as a reply.
NOT_YET_TOAST = "Finish it in Hook first"
NOT_YET_REPLY = "Not closed: finish the marked steps in Hook first. Skip still works."
# That verdict word was the last thing the ask needed (done: reply): kept, still open.
REPLY_KEPT = "Kept as your reply. Tap Done to close it."
# The owner's Done while a `then: "steps"` question waits for the session's next steps
# (Pond 1.9.0): the toast for a Done tap and the in-thread answer to a verdict word.
WAITING_FOR_STEPS = "Waiting for next steps…"
# A Done bound to a version the session has since added steps to (StepsAdded).
STEPS_ADDED_TEXT = "New steps were added. Open the ask to see them."

class AskClosed(Exception):
    """The ask is not open: answers are read-only until it is reopened (R5)."""


class InvalidAnswers(Exception):
    """validate_answers refused the batch; `details` are its field-path errors."""

    def __init__(self, details: list[str]):
        super().__init__("invalid answers")
        self.details = details


# The settings row that opens POST /asks/{id}/amend ("1"); set with `python3 -m mailbox.admin`.
class InvalidProgress(Exception):
    """POST /asks/{id}/progress refused: `details` are field paths, never values."""

    def __init__(self, details: list[str]):
        super().__init__("invalid progress")
        self.details = details


class RequirementsUnmet(Exception):
    """The owner's Done while the version in force still needs something (Pond
    1.3.0, RD 3): `left` is requirements.left() order. → 409 body()."""

    def __init__(self, left: list[dict]):
        super().__init__("requirements_unmet")
        self.left = left

    def body(self) -> dict[str, Any]:
        return {"error": "requirements_unmet", "left": self.left, "details": req.words(self.left)}


class WaitingForSteps(Exception):
    """The owner's Done while a `then: "steps"` question waits for the session's next
    steps (Pond 1.9.0): `wait` is requirements.step_wait(). → 409 body()."""

    def __init__(self, wait: dict[str, Any]):
        super().__init__("waiting_for_steps")
        self.wait = wait

    def body(self) -> dict[str, Any]:
        return {"error": "waiting_for_steps", "until": self.wait["until"], "path": self.wait["path"]}


SETTING_CORRECTIONS = "corrections_enabled"
# The settings row that opens documents on asks ("1", Pond 1.10.0): the `docs` request
# member and `doc` in an instructable. Off by default; set with `python3 -m mailbox.admin docs on`.
SETTING_DOCS = "docs_enabled"
# The settings row that closes POST /asks/{id}/append ("1", Pond 1.9.0). Appends are
# on by default: `python3 -m mailbox.admin appends off` sets it, `on` deletes it.
SETTING_APPENDS_DISABLED = "appends_disabled"
# A correction is sent silently when the revision it replaces is younger than this.
QUIET_CORRECTION_S = 600
# Added steps are sent silently when the owner acted on the ask this recently (Pond 1.9.0).
QUIET_APPEND_S = 120
# The owner's acts that are fresh attention on an ask, as the Mac counts them. An
# `accepted` by the owner (Hook's seal accept, Use Version N) isn't one.
OWNER_ATTENTION_KINDS = frozenset({"answer", "reply", "message", "problem", "progress", "reopened", "resolved"})


class AmendError(Exception):
    """A refused amendment, or another refused revision-bound act: the HTTP
    status and the JSON body to answer with."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("error", "refused"))
        self.status = status
        self.body = body


class DocsRefused(AmendError):
    """A request's documents refused (Pond 1.10.0): 409 documents not enabled, or 400
    {"error": "invalid documents", "details": [...]} naming field paths and hashes only,
    or (Pond 1.12.0) 409 document store full (Store.check_doc_room: StoreFull).
    An AmendError, so the amend and append handlers answer it as they answer the rest."""


# The answer to a request whose new texts would take the stored total over
# docs.MAX_STORE_BYTES (Pond 1.12.0). Nothing is stored.
STORE_FULL = {"error": "document store full"}
# What a sent ask message becomes when its transaction refused it after the send:
# another request filled the store in between (Pond 1.12.0). No document text.
NOT_STORED_TEXT = "This ask was not stored: the document store is full."


class Corrected(Exception):
    """An owner act bound to a version that isn't the one in force: the ask was
    corrected and the correction isn't accepted, or the act names another rev or
    hash. → 409 {"error":"ask was corrected","rev","hash","acceptedRev"}."""

    def __init__(self, current: Revision, accepted: int):
        super().__init__("ask was corrected")
        self.current = current
        self.accepted = accepted

    def body(self) -> dict[str, Any]:
        return {"error": "ask was corrected", "rev": self.current.rev, "hash": self.current.hash,
                "acceptedRev": self.accepted}


class StepsAdded(Corrected):
    """The owner's Done bound to a version the session has since added steps to
    (Pond 1.9.0): the new steps are unseen, so Done waits. A Corrected, so every
    handler that refuses a stale act refuses this one too. → 409
    {"error": "steps_added", "rev", "hash"} with the current revision."""

    def __init__(self, current: Revision, accepted: int):
        super().__init__(current, accepted)
        self.args = ("steps_added",)

    def body(self) -> dict[str, Any]:
        return {"error": "steps_added", "rev": self.current.rev, "hash": self.current.hash}


class EnrollRefused(Exception):
    """An enrollment the Pond refuses outright: `error` is "too many keys",
    "already enrolled" or "invalid label"."""

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class RevokeRefused(Exception):
    """A revoke request the Pond refuses (Pond 1.11.0): `status` is the HTTP status
    and `error` its message: 404 "not found", 409 "already revoked", "not approved"
    or "revoke request pending"."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


_STUB_WHY = revisions.STUB_WHY


def labels_payload(wire: dict[str, Any]) -> dict[str, Any]:
    """An item's wire with payload.instructable as plain labels when it holds a choice
    object, for a client before protocol 6 (Pond 1.4.0). Changes `wire` in place."""
    payload = wire.get("payload")
    if isinstance(payload, dict) and has_choice_objects(payload.get("instructable")):
        payload["instructable"] = labels_only(payload["instructable"])
    return wire


def revisions_stub(body: dict[str, Any]) -> dict[str, Any]:
    """The stub an old client gets for a corrected ask, from its current body."""
    return revisions.stub(body.get("title") or "", body.get("urgency") or "today", body.get("from"))


def _render_fallback_message(ask: str, verdict: str, note: Optional[str]) -> str:
    """The fresh message sent when the original ask's message can't be edited
    (Ruling S12): '✅ resolved: {ask} — {note}' / '🚫 skipped: {ask} — {note}',
    note omitted when None, HTML-escaped per the client's renderer conventions.
    """
    emoji = "✅" if verdict == "done" else "🚫"
    verb = "resolved" if verdict == "done" else "skipped"
    text = f"{emoji} {verb}: {html.escape(ask)}"
    if note:
        text += f" — {html.escape(note)}"
    return text


class Service:
    """Mailbox operations: posts, resolves, event management."""

    def __init__(
        self,
        store: Store,
        telegram: TelegramClient,
        context: str,
        open_base: Optional[str] = None,
        ct_client: Optional[CentralTimeClient] = None,
        zone: Optional[tzinfo] = None,
        brief_asks: bool = True,
        members: Optional[dict[int, str]] = None,
        telegram_mode: str = "full",
        answerer: Optional["ask_about.Answerer"] = None,
    ):
        if telegram_mode not in ("full", "alert"):
            raise ValueError("telegram_mode must be full or alert")
        self.telegram_mode = telegram_mode
        self.store = store
        self.admission = Admission(store)
        self.brief_asks = brief_asks
        # Household mode (MAILBOX_MEMBERS): Telegram id -> name. None is the asks instance, unchanged.
        self.members = members
        self.telegram = telegram
        self.context = context
        # MAILBOX_RELAY_OPEN_BASE without a trailing slash, or None: Relay's
        # loopback page for an ask is f"{open_base}/{item_id}".
        self.open_base = open_base
        # Updates whose from.id isn't the configured owner are dropped
        # silently but counted here (Ruling S13).
        self.dropped_updates: int = 0
        self.ct_client = ct_client
        # Owner texts that are neither a reply to an ask nor a capture (no
        # client, a bot command) are dropped silently and counted here.
        self.ignored_messages: int = 0
        # The capture clock: zone-aware in the household zone (TZ), so today,
        # tomorrow and a bare time follow the household wall clock whatever the
        # C library resolved (ruling S117). Tests pin it.
        zone = zone or household_zone()
        self.now: Callable[[], datetime] = lambda: datetime.now(zone)
        # One lock per ref in flight, refcounted: two concurrent /notify calls
        # for the same ref (core re-posts after its 5 s timeout while Telegram
        # is slow) serialise, so the second sees the stored row and sends
        # nothing. The count, not lock.locked(), decides when the entry goes:
        # a waiter that has looked the lock up but not yet acquired it must
        # never be left holding an orphan while a third caller makes a new one.
        self._notify_lock = threading.Lock()
        self._ref_locks: dict[str, tuple[threading.Lock, int]] = {}
        # The clock for item and revision stamps, the amend gap and owner-key
        # expiries; apart from the capture clock above. Tests pin it.
        self.utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        # Serialises enrollments and the enforcement switch (check, then write).
        self._owner_lock = threading.Lock()
        self._presentation_mutex = threading.Lock()
        self._presentation_locks: dict[str, tuple[Any, int]] = {}
        # (ask id, until) of every step wait sweep_step_waits redrew (Pond 1.9.0):
        # each ended wait is redrawn once per process.
        self._swept_waits: set[tuple[str, str]] = set()
        # Ask about this (Pond 1.14.0): None while the Pond has no Anthropic API key, and
        # then the feature is off. The lock runs one question at a time, so the limits
        # are counted and the answer stored without a race (and the spend stays serial).
        self.answerer = answerer
        self._ask_about_lock = threading.Lock()

    @contextmanager
    def _ask_presentation(self, item_id: str):
        """Order network presentations per ask, never while holding the DB lock.

        Writers commit first. A queued presenter rereads that committed state,
        so a delayed Done/keyboard/correction cannot overwrite a newer reopen.
        Reference counts include waiters; the map retains only active asks.
        """
        with self._presentation_mutex:
            lock, count = self._presentation_locks.get(item_id) or (threading.RLock(), 0)
            self._presentation_locks[item_id] = (lock, count + 1)
        try:
            with lock:
                yield
        finally:
            with self._presentation_mutex:
                _, count = self._presentation_locks[item_id]
                if count == 1:
                    del self._presentation_locks[item_id]
                else:
                    self._presentation_locks[item_id] = (lock, count - 1)

    def _utc(self) -> datetime:
        """Now in UTC: item and revision stamps, the amend gap, expiries."""
        return self.utcnow()

    def _stamp(self, dt: Optional[datetime] = None) -> str:
        return (dt or self._utc()).isoformat(timespec="seconds")

    def _open_url(self, item_id: str) -> dict[str, str]:
        """{"openUrl": "<base>/<id>"} when Relay is configured, else {}."""
        return {"openUrl": f"{self.open_base}/{item_id}"} if self.open_base else {}

    def _compute_dedup_key(self, lane: str, title: str) -> str:
        """Dedup key = sha1(lane + title)[:16]."""
        return hashlib.sha1(f"{lane}{title}".encode()).hexdigest()[:16]

    def post(
        self,
        project: str,
        ask: str,
        why: Optional[str] = None,
        cmd: Optional[str] = None,
        link: Optional[str] = None,
        instructable: Optional[dict[str, Any]] = None,
        edit_key_hash: Optional[str] = None,
        docs: Optional[dict[str, str]] = None,
        principal_id: Optional[str] = None,
        actor: str = "session",
    ) -> tuple[Item, dict[str, Any]]:
        """Post an ask to Telegram. Deduped if same key within 24h.

        Returns (item, response_dict): 'deduped' on a dedup hit, and 'openUrl'
        whenever Relay is configured, on a hit as on a new ask. `instructable`
        has already been validated by the caller; its steps get ids (s1, s2.1…)
        unless it carries them, and that object is revision 1, stored in the
        payload as is. With an instructable the response carries 'revision'
        ({rev, hash} of the current revision) and 'editable': true when the
        caller's edit key hash is the one stored for the ask (a dedupe hit never
        replaces the stored hash). Raises TelegramError on send failure (caller
        handles retry/rollback): nothing is stored then.

        `docs` (Pond 1.10.0) is the request's `docs` member: the text of each `doc`
        in the instructable, by hash. They are checked before anything else here
        (DocsRefused) and stored with the ask in its transaction; a dedupe hit stores
        and links none of them.

        Then the limit (Pond 1.12.0): texts not stored yet that would take the total
        over docs.MAX_STORE_BYTES are refused with 409 document store full before the
        Telegram send, and again, exactly, inside the ask's transaction. When only the
        transaction refuses (another request filled the store after the first check),
        the message already sent is edited to say the ask was not stored, without its
        buttons, and the post is refused the same way: nothing is stored.
        """
        sent = self._docs_gate(instructable, docs, set())
        dedup_key = self._compute_dedup_key(self.context, ask)

        # Check for existing open ask with same key. Ruling S14: on a dedup
        # hit we return the existing item and make NO Telegram call at all.
        existing = self.store.lookup_dedup_key(dedup_key, within_hours=24, now=self._utc(), principal_id=principal_id)
        if existing:
            return existing, {"deduped": True, **self._open_url(existing.id), **self._post_extras(existing, edit_key_hash)}
        if sent:
            try:
                self.store.check_doc_room(sent)      # early: before anything reaches Telegram
            except StoreFull:
                raise DocsRefused(409, dict(STORE_FULL)) from None

        # Create the item
        item_id = str(uuid4())
        now = self._stamp()
        rev1 = None
        if instructable is not None:
            instructable, _ = revisions.assign_ids(instructable)
            body = revisions.canonical(instructable)
            rev1 = Revision(item_id=item_id, rev=1, hash=revisions.revision_hash(1, "", now, "", body),
                            prev="", at=now, reason="", body=body)
        payload = {
            "key": dedup_key,
            "why": why,
            "cmd": cmd,
            "link": link,
            "tg_chat_id": self.telegram.owner_id,
            "tg_message_id": None,
            "note": None,
        }
        if instructable is not None:
            payload["instructable"] = instructable

        item = Item(
            id=item_id,
            source="xo-mailbox",
            title=ask,
            project_id=project,
            lane=self.context,
            payload=json.dumps(payload),
            created=now,
            updated=now,
        )

        # Send to Telegram. TelegramError propagates to the caller, which
        # decides retry/rollback — nothing has been written to the store yet.
        text = (TelegramClient.render_alert(project, ask, instructable) if self.telegram_mode == "alert"
                else TelegramClient.render_ask_message(project, ask, why, cmd, link, instructable))
        # A new ask has nothing finished yet: Done waits when it has requirements.
        keyboard = (TelegramClient.alert_keyboard(item_id, self.open_base) if self.telegram_mode == "alert"
                    else self.telegram.ask_keyboard(item_id, self.open_base, instructable,
                                                    ready=not req.wanted(instructable)))
        response = self.telegram.send_message(
            self.telegram.owner_id, text, reply_markup=keyboard
        )

        # Store the message id and insert the item, its revision 1, its edit key
        # hash, the posted event and the message row, in one transaction.
        payload["tg_message_id"] = response.get("message_id")
        item.payload = json.dumps(payload)
        try:
            self.store.insert_ask(item, rev1, edit_key_hash if rev1 is not None else None, docs=sent,
                                  principal_id=principal_id, actor=actor)
        except StoreFull:
            self._withdraw_message(response.get("message_id"))
            raise DocsRefused(409, dict(STORE_FULL)) from None

        return item, {**self._open_url(item_id), **self._post_extras(item, edit_key_hash)}

    def _withdraw_message(self, message_id: Any) -> None:
        """A sent ask message whose ask was not stored (Pond 1.12.0): its text becomes
        NOT_STORED_TEXT and its buttons go, so no tap names an ask the Pond lacks. Any
        failure here (a TelegramError, or a timeout or unreadable body the client does
        not wrap) is logged by its type only and never raised: the post's answer stays
        409 document store full."""
        if not message_id:
            return
        try:
            self.telegram.edit_message_text(self.telegram.owner_id, message_id, NOT_STORED_TEXT,
                                            reply_markup={"inline_keyboard": []})
        except Exception as exc:  # noqa: BLE001 — the refusal must reach the caller
            logger.warning("[mailbox] post: withdrawing the unstored message failed: %s", type(exc).__name__)

    # --- Hook bugs (Pond 1.2.0) -----------------------------------------------

    @staticmethod
    def bug_excerpt(instructable: dict[str, Any]) -> str:
        """The report a bug carries: the first step's `code` (Hook puts it there), else ""."""
        for step in instructable.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("code"), str):
                return step["code"]
        return ""

    def post_bug(
        self, project: str, instructable: dict[str, Any], signature: str, excerpt: Optional[str] = None,
        docs: Optional[dict[str, str]] = None,
    ) -> tuple[Item, dict[str, Any]]:
        """File a Hook bug: an ask that never reaches Telegram, deduped by signature
        instead of by title. An open bug with this signature gains an occurrence (the
        count, last seen and the excerpt move, plus an occurrence/hook event) and comes
        back with 'deduped': true; otherwise a new item with its revision 1 and bugs
        row is stored. `instructable` has already been validated; `excerpt` defaults
        to its first step's code. → (item, {'occurrences', 'deduped'?, 'openUrl'?,
        'revision', 'editable': False}).

        A bug takes no documents (Pond 1.10.0): a `doc` in it, or a `docs` member, is
        refused as for an ask that holds none of them (DocsRefused), before anything
        is stored, and nothing is ever stored for a bug."""
        self._docs_gate(instructable, docs, set(), keep=False)
        excerpt = self.bug_excerpt(instructable) if excerpt is None else excerpt
        existing = self.store.open_bug(signature)
        if existing is not None:
            try:
                count = self.store.add_occurrence(existing.id, self._stamp(), excerpt)
            except KeyError:                         # closed or gone in between
                existing = None
            else:
                item = self.store.get_item(existing.id)
                return item, {"deduped": True, "occurrences": count, **self._open_url(item.id),
                              **self._post_extras(item, None)}

        item_id = str(uuid4())
        now = self._stamp()
        instructable, _ = revisions.assign_ids(instructable)
        body = revisions.canonical(instructable)
        rev1 = Revision(item_id=item_id, rev=1, hash=revisions.revision_hash(1, "", now, "", body),
                        prev="", at=now, reason="", body=body)
        payload = {
            # Never a title key (sha1, 16 hex), so a plain ask can't dedupe into a bug.
            "key": f"bug-{signature}",
            "why": instructable.get("why"),
            "cmd": None,
            "link": None,
            "tg_chat_id": self.telegram.owner_id,
            "tg_message_id": None,
            "note": None,
            "instructable": instructable,
        }
        item = Item(id=item_id, source="xo-mailbox", title=instructable["title"], project_id=project,
                    lane=self.context, payload=json.dumps(payload), created=now, updated=now)
        self.store.insert_bug(item, signature, excerpt, rev1)
        return item, {"occurrences": 1, **self._open_url(item_id), **self._post_extras(item, None)}

    def _post_extras(self, item: Item, edit_key_hash: Optional[str]) -> dict[str, Any]:
        """{'revision': {rev, hash}, 'editable': bool} for an ask with revisions;
        {'editable': False} for one without."""
        current = self.store.current_revision(item.id)
        if current is None:
            return {"editable": False}
        stored = self.store.edit_key_hash(item.id)
        editable = bool(stored and edit_key_hash
                        and hmac.compare_digest(stored.encode(), edit_key_hash.encode()))
        return {"revision": {"rev": current.rev, "hash": current.hash}, "editable": editable}

    # --- revisions (Pond 1.1.0) ---------------------------------------------

    def corrections_enabled(self) -> bool:
        return self.store.get_setting(SETTING_CORRECTIONS) == "1"

    def appends_enabled(self) -> bool:
        return self.store.get_setting(SETTING_APPENDS_DISABLED) != "1"

    def docs_enabled(self) -> bool:
        return self.store.get_setting(SETTING_DOCS) == "1"

    def ask_about_enabled(self) -> bool:
        return self.answerer is not None

    def features(self) -> tuple[str, ...]:
        """FEATURES, plus "docs" while documents are on (GET /version), plus "askAbout"
        while the Pond has an Anthropic API key (1.14.0)."""
        extra = (("docs",) if self.docs_enabled() else ()) + (("askAbout",) if self.ask_about_enabled() else ())
        if self.admission.mode != "legacy":
            extra += (ADMISSION_FEATURE,)
        return FEATURES + extra

    def _docs_gate(self, instructable: Optional[dict[str, Any]], docs: Any, held: set[str], *,
                   history: Optional[list[Revision]] = None, offset: Optional[int] = None,
                   keep: bool = True, current: Optional[dict[str, Any]] = None) -> dict[str, bytes]:
        """The documents of a request, checked (Pond 1.10.0): {sha256: exact bytes} to
        store with the ask. A request without a `docs` member and without any `doc` key
        passes untouched. Otherwise the switch must be on (else 409 documents not
        enabled; a present `doc: null` counts too, as 1.9.0 refused it as an unknown
        key) and docs.check must pass (else 400 invalid documents).

        On a correction or an append `current` is the ask's current body, and with the
        switch off the request passes the switch when it only keeps what the ask holds:
        no `docs` member, every `doc` a hash in `held`, every `doc: null` at a place
        where `current` has one. It is then checked as usual. `instructable`
        is valid; `held` the hashes the ask linked before; `history` its revisions, whose
        `doc` byte counts a reused hash must match; `offset` the step count before an
        append, so details name the request's steps. `keep` false (a bug) keeps no
        document: a malformed `docs` member is the shape error alone, as for an ask;
        otherwise each `doc` is checked as neither sent nor held, and each sent text is
        "not used by any step or link", never checked on its own."""
        keys = doc_keys(instructable)
        if docs is None and not keys:
            return {}
        if not self.docs_enabled():
            if not (docs is None and current is not None and self._keeps_only_held(keys, held, current)):
                raise DocsRefused(409, {"error": "documents not enabled"})
        held_bytes = {sha256: size for r in history or [] for _, sha256, size in doc_refs(json.loads(r.body))}
        if keep:
            problems = documents.check(instructable or {}, docs, held, held_bytes=held_bytes)
        elif docs is not None and not documents.well_formed(docs):
            problems = [documents.SHAPE_ERROR]                  # alone, as for an ask
        else:
            # The refs first, then each sent text: never kept, so never checked on its own.
            problems = documents.check(instructable or {}, None, set())
            problems += [f"docs.{sha256}: not used by any step or link" for sha256 in sorted(docs or {})]
        if offset is not None:
            problems = [revisions.to_request(problem, offset) for problem in problems]
        if problems:
            raise DocsRefused(400, {"error": "invalid documents", "details": problems})
        return documents.bodies(docs)

    @staticmethod
    def _keeps_only_held(keys: list[tuple[str, Any]], held: set[str], current: dict[str, Any]) -> bool:
        """Does a result with these `doc` keys hold only documents the ask already holds:
        each `doc` a held hash, each `doc: null` where the current body has one?"""
        nulls = {path for path, value in doc_keys(current) if value is None}
        return all(path in nulls if value is None else isinstance(value, dict) and value.get("sha256") in held
                   for path, value in keys)

    def read_doc(self, item_id: str, sha256: str) -> bytes:
        """The exact bytes of a document the ask keeps (GET /asks/{id}/docs/{sha256},
        Pond 1.10.0). AmendError 404 {"error": "not found"} for an unknown ask, a
        malformed hash or one the ask never linked; 410 {"error": "document no longer
        kept"} once the ask released it, also when another ask still keeps the text.
        Works with the switch off: a document already stored stays readable."""
        if not documents.is_sha256(sha256) or self.store.get_item(item_id) is None:
            raise AmendError(404, {"error": "not found"})
        status = self.store.doc_status(item_id, sha256)
        if status is None:
            raise AmendError(404, {"error": "not found"})
        body = self.store.get_doc(sha256) if status == "kept" else None
        if body is None:                         # released, or released in between
            raise AmendError(410, {"error": "document no longer kept"})
        return body

    def release_docs(self, item_id: str) -> int:
        """Release a closed ask's documents (POST /asks/{id}/docs/release, Pond 1.10.0):
        Store.release_closed_docs, → how many this call released. AmendError 404 for an
        unknown ask, 409 {"error": "ask is open"} for an open one (nothing deleted). The
        check and the release are one transaction, so a reopen never lands between them.
        Writes no event and changes nothing on the ask; works with the switch off."""
        try:
            released = self.store.release_closed_docs(item_id)
        except AskNotOpen:
            raise AmendError(409, {"error": "ask is open"}) from None
        if released is None:
            raise AmendError(404, {"error": "not found"})
        return released

    def sweep_docs(self) -> int:
        """Expire documents (Pond 1.10.0): release those of every ask closed (done or
        dismissed) at least DOCS_KEEP_DAYS ago by the service clock, exactly as POST
        /asks/{id}/docs/release does. Each ask's check and release are one transaction
        (Store.expire_ask_docs), so a reopen never lands between them. An open ask
        never expires. Runs whether the switch is on or off, writes no event and
        changes no item; logs counts only, and only when it released something.
        → how many rows it released."""
        cutoff = self._utc() - timedelta(days=documents.DOCS_KEEP_DAYS)
        asks = released = 0
        for item_id in self.store.closed_asks_keeping_docs():
            marked = self.store.expire_ask_docs(item_id, cutoff)
            if marked:
                asks += 1
                released += marked
        if released:
            logger.info("[mailbox] document sweep: %d asks, %d released", asks, released)
        return released

    def in_force(self, item_id: str) -> tuple[Optional[Revision], Optional[dict[str, Any]]]:
        """The revision the owner accepted (the one Telegram, answers and Done
        follow) and its decoded body. An ask without revisions: (None,
        payload.instructable)."""
        accepted = self.store.accepted_rev(item_id)
        found = next((r for r in self.store.revisions(item_id) if r.rev == accepted), None)
        if found is None:
            item = self.store.get_item(item_id)
            payload = json.loads(item.payload) if item else {}
            return None, payload.get("instructable")
        return found, json.loads(found.body)

    @staticmethod
    def _revision_wire(r: Revision, kinds: Optional[dict[int, str]] = None) -> dict[str, Any]:
        """A revision as JSON. An append (Pond 1.9.0) adds "kind": "append"; every other
        revision keeps the bytes it had before 1.9.0."""
        wire = {"rev": r.rev, "hash": r.hash, "prev": r.prev, "at": r.at, "reason": r.reason, "body": r.body}
        if kinds and kinds.get(r.rev) == revisions.KIND_APPEND:
            wire["kind"] = revisions.KIND_APPEND
        return wire

    def wire_item(self, item: Item, *, revisions: bool, with_history: bool = False,
                  requirements: bool = False, choice_objects: bool = True) -> dict[str, Any]:
        """An item as JSON for a client. `revisions` is true for a client that reads
        revision histories (X-Hook-Protocol >= 5): payload.instructable is then the
        current body and `revision` describes it. Any other client gets a corrected
        ask (current rev > 1) as the stub, which tells the owner to update Hook,
        and a rev-1 ask exactly as before (plus its step ids). `with_history` adds
        `revisions` (every revision) for the detail response; the caller lifts it
        to the top level. `requirements` (Pond 1.3.0, single-item answers only, RD 15)
        adds {"left", "ready"} for an ask whose version in force has requirements.

        `choice_objects` is false for a client before protocol 6 (Pond 1.4.0). Hook
        0.13 reads `choices` as strings and revision bodies strictly, so an ask with a
        choice object in any revision reaches it as labels_only() of a rev-1 body, or
        the stub from rev 2 on, and never with `revision` or `revisions`."""
        wire = self._wire_item(item, revisions=revisions, with_history=with_history,
                               choice_objects=choice_objects)
        if requirements:
            needs = self.requirements_left(item.id)
            if needs is not None:
                wire["requirements"] = req.wire(needs)
            wait = self.step_wait(item.id) if item.status == "open" else None
            if wait is not None:
                wire["stepWait"] = wait
        return wire

    def _wire_item(self, item: Item, *, revisions: bool, with_history: bool,
                   choice_objects: bool = True) -> dict[str, Any]:
        wire = item.to_wire()
        bug = self.store.bug_meta(item.id)
        if bug is not None:
            # Extra keys only: a 1.1.0 client reads a bug as an ordinary ask.
            wire["kind"] = "bug"
            wire["bug"] = {k: bug[k] for k in ("signature", "count", "firstSeen", "lastSeen")}
        history = self.store.revisions(item.id)
        if not choice_objects:
            labels_payload(wire)
        if not history:
            return wire
        current = history[-1]
        body = json.loads(current.body)
        if not choice_objects and any(has_choice_objects(json.loads(r.body)) for r in history):
            if current.rev > 1:
                wire["payload"]["instructable"] = revisions_stub(body)
                wire["payload"]["why"] = _STUB_WHY
            else:
                wire["payload"]["instructable"] = labels_only(body)
            return wire
        if revisions:
            kinds = self.store.revision_kinds(item.id)
            wire["payload"]["instructable"] = body
            wire["revision"] = {**self._revision_wire(current, kinds), "acceptedRev": self.store.accepted_rev(item.id)}
            if with_history:
                wire["revisions"] = [self._revision_wire(r, kinds) for r in history]
        elif current.rev > 1:
            wire["payload"]["instructable"] = revisions_stub(body)
            wire["payload"]["why"] = _STUB_WHY       # `revisions` is the flag here, not the module
        return wire

    def amend(
        self, item_id: str, edit_key: str, base_rev: int, base_hash: str, reason: Any, instructable: Any,
        docs: Optional[dict[str, str]] = None, actor: str = "session",
    ) -> tuple[Item, Revision, dict[str, Any]]:
        """Correct an open ask in place: revision N+1 by compare-and-swap. Raises
        AmendError, in this order: 404 not found, 409 corrections not enabled, 403
        not editable, 403 wrong edit key, 409 ask is closed, 409 stale revision, 409
        revision limit reached (10 corrections; appends are counted apart), 429 too
        soon, 400 invalid instructable, 400 invalid amendment, 400 no change. An
        amendment that only adds steps is stored as an append (Pond 1.9.0) and moves
        the version in force as Service.append does; one that fails the append gates
        is refused as the append route refuses it (409 appends not enabled, 409 append
        limit reached, 400 invalid steps over 10 steps) and nothing is stored. Then the
        documents (Pond 1.10.0, see _docs_gate): 409 documents not enabled (not when it
        only keeps documents the ask holds), 400 invalid documents; a `doc` the ask
        already holds needs no text. They are stored in the
        revision's transaction, which refuses new texts that would take the stored
        total over docs.MAX_STORE_BYTES: 409 document store full (Pond 1.12.0). Telegram is told after the commit and never fails the
        amend. → (item, the new revision, its change summary)."""
        item = self.store.get_item(item_id)
        if item is None:
            raise AmendError(404, {"error": "not found"})
        if not self.corrections_enabled():
            raise AmendError(409, {"error": "corrections not enabled"})
        stored = self.store.edit_key_hash(item_id)
        if stored is None:
            raise AmendError(403, {"error": "not editable"})
        given = hashlib.sha256(edit_key.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(given.encode(), stored.encode()):
            raise AmendError(403, {"error": "wrong edit key"})
        if item.status != "open":
            raise AmendError(409, {"error": "ask is closed"})
        history = self.store.revisions(item_id)
        current = history[-1]
        if base_rev != current.rev or not hmac.compare_digest(str(base_hash).encode(), current.hash.encode()):
            raise AmendError(409, {"error": "stale revision", "rev": current.rev, "hash": current.hash})
        kinds = self.store.revision_kinds(item_id)
        appends = sum(1 for kind in kinds.values() if kind == revisions.KIND_APPEND)
        if current.rev - 1 - appends >= revisions.MAX_CORRECTIONS:
            raise AmendError(409, {"error": "revision limit reached"})
        now = self._utc()
        waited = (now - datetime.fromisoformat(current.at)).total_seconds()
        if waited < revisions.MIN_GAP_S:
            raise AmendError(429, {"error": "too soon", "retryAfter": max(1, math.ceil(revisions.MIN_GAP_S - waited))})
        clean, problems = validate_instructable(instructable, new_ids=True)
        if problems:
            raise AmendError(400, {"error": "invalid instructable", "details": problems})
        used: set[str] = set()
        for r in history:
            used.update(revisions.step_ids(json.loads(r.body)))
        old = json.loads(current.body)
        new, summary, problems = revisions.validate_amendment(old, clean, used, current.rev + 1, reason)
        if problems:
            raise AmendError(400, {"error": "invalid amendment", "details": problems})
        body = revisions.canonical(new)
        if body == current.body:
            raise AmendError(400, {"error": "no change"})
        at = self._stamp(now)
        rev = current.rev + 1
        revision = Revision(item_id=item_id, rev=rev, hash=revisions.revision_hash(rev, current.hash, at, reason, body),
                            prev=current.hash, at=at, reason=reason, body=body)
        text = json.dumps({"hash": revision.hash, "reason": reason}, ensure_ascii=False, separators=(",", ":"))
        # An amend that only adds steps is an append, and must pass the append gates:
        # a Hook seals an append-shaped revision and accepts it as the owner, so one
        # stored as a correction would get past the switch and the limits. Refused with
        # the append route's errors, and nothing is stored.
        appended = revisions.is_append(revisions.parse_exact(current.body), new)
        if appended:
            if not self.appends_enabled():
                raise AmendError(409, {"error": "appends not enabled"})
            if appends >= revisions.MAX_APPENDS:
                raise AmendError(409, {"error": "append limit reached"})
            if len(new["steps"]) - len(old["steps"]) > revisions.MAX_APPEND_STEPS:
                raise AmendError(400, {"error": "invalid steps", "details": [f"steps: 1 to {revisions.MAX_APPEND_STEPS}"]})
        sent = self._docs_gate(new, docs, self.store.ask_docs(item_id), history=history, current=old)
        kind = revisions.KIND_APPEND if appended else revisions.KIND_CORRECTION
        advance = appended and self.store.accepted_rev(item_id) == current.rev
        try:
            self.store.amend_tx(item_id, base_rev=current.rev, base_hash=current.hash, new=revision, event_text=text,
                                kind=kind, advance=advance, docs=sent, actor=actor)
        except AskNotOpen:
            raise AmendError(409, {"error": "ask is closed"}) from None
        except StoreFull:
            raise DocsRefused(409, dict(STORE_FULL)) from None
        except StaleRevision as exc:
            latest = exc.current or current
            raise AmendError(409, {"error": "stale revision", "rev": latest.rev, "hash": latest.hash}) from None
        item = self.store.get_item(item_id)
        if kind == revisions.KIND_APPEND:
            # Stored as an append: Telegram shows it as one, not as a correction.
            self._announce_append(item, current, revision, summary.get("added") or [])
        else:
            self._announce_correction(item, current, revision, summary)
        return item, revision, summary

    def append(
        self, item_id: str, edit_key: str, base_rev: int, base_hash: str, note: Any, steps: Any,
        docs: Optional[dict[str, str]] = None, actor: str = "session",
    ) -> tuple[Item, Revision, list[dict]]:
        """Add steps after the last step of an open ask (Pond 1.9.0): revision N+1 by
        compare-and-swap, stored with kind "append". Raises AmendError, in this order:
        404 not found, 409 appends not enabled, 403 not editable, 403 wrong edit key,
        409 ask is closed, 409 stale revision, 409 append limit reached (10 appends,
        counted apart from corrections), 429 too soon (15 s since the last revision of
        any kind), 400 invalid steps with details naming the request's steps, then the
        documents (Pond 1.10.0, see _docs_gate) of the whole result: 409 documents not
        enabled (not when it only keeps documents the ask holds), 400 invalid documents
        naming the request's steps, and in the transaction 409 document store full
        (Pond 1.12.0) for new texts over the limit. When the owner had accepted the current revision, the append is accepted with it
        (auth "append"): acts on unchanged steps stay valid. Telegram is told after
        the commit and never fails the append. → (item, the new revision, added)."""
        item = self.store.get_item(item_id)
        if item is None:
            raise AmendError(404, {"error": "not found"})
        if not self.appends_enabled():
            raise AmendError(409, {"error": "appends not enabled"})
        stored = self.store.edit_key_hash(item_id)
        if stored is None:
            raise AmendError(403, {"error": "not editable"})
        given = hashlib.sha256(edit_key.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(given.encode(), stored.encode()):
            raise AmendError(403, {"error": "wrong edit key"})
        if item.status != "open":
            raise AmendError(409, {"error": "ask is closed"})
        history = self.store.revisions(item_id)
        current = history[-1]
        if base_rev != current.rev or not hmac.compare_digest(str(base_hash).encode(), current.hash.encode()):
            raise AmendError(409, {"error": "stale revision", "rev": current.rev, "hash": current.hash})
        kinds = self.store.revision_kinds(item_id)
        if sum(1 for kind in kinds.values() if kind == revisions.KIND_APPEND) >= revisions.MAX_APPENDS:
            raise AmendError(409, {"error": "append limit reached"})
        now = self._utc()
        waited = (now - datetime.fromisoformat(current.at)).total_seconds()
        if waited < revisions.MIN_APPEND_GAP_S:
            raise AmendError(429, {"error": "too soon",
                                   "retryAfter": max(1, math.ceil(revisions.MIN_APPEND_GAP_S - waited))})
        used: set[str] = set()
        for r in history:
            used.update(revisions.step_ids(json.loads(r.body)))
        old = json.loads(current.body)
        rev = current.rev + 1
        new, added, problems = revisions.build_append(old, steps, used, rev, note)
        if not problems:
            # The builder already checked the whole result; checked again as posted.
            problems = validate_instructable(new, new_ids=True)[1]
        if problems:
            raise AmendError(400, {"error": "invalid steps", "details": problems})
        sent = self._docs_gate(new, docs, self.store.ask_docs(item_id), history=history,
                               offset=len(old.get("steps") or []), current=old)
        body = revisions.canonical(new)
        appended = revisions.is_append(old, new)       # true by construction: guards the builder
        kind = revisions.KIND_APPEND if appended else revisions.KIND_CORRECTION
        advance = appended and self.store.accepted_rev(item_id) == current.rev
        at = self._stamp(now)
        revision = Revision(item_id=item_id, rev=rev, hash=revisions.revision_hash(rev, current.hash, at, note, body),
                            prev=current.hash, at=at, reason=note, body=body)
        text = json.dumps({"hash": revision.hash, "reason": note}, ensure_ascii=False, separators=(",", ":"))
        try:
            self.store.append_tx(item_id, base_rev=current.rev, base_hash=current.hash, new=revision, kind=kind,
                                 advance=advance, event_text=text, docs=sent, actor=actor)
        except AskNotOpen:
            raise AmendError(409, {"error": "ask is closed"}) from None
        except StoreFull:
            raise DocsRefused(409, dict(STORE_FULL)) from None
        except StaleRevision as exc:
            latest = exc.current or current
            raise AmendError(409, {"error": "stale revision", "rev": latest.rev, "hash": latest.hash}) from None
        item = self.store.get_item(item_id)
        self._announce_append(item, current, revision, added)
        return item, revision, added

    def _announce_append(self, item: Item, previous: Revision, revision: Revision, added: list[dict]) -> None:
        """After an append commit (Pond 1.9.0): the ask's newest message gains "Steps
        added: see below" and keeps its question buttons, bound to its own rev, while
        Done and Skip go; then the added steps are sent as a reply to it, with the
        keyboard of the new rev from the first added step on. In alert mode the old
        message only loses its buttons and the reply is the alert with "Steps added".
        While a correction waits for review the old message is left alone and the
        reply has the pending keyboard. The reply is silent when the owner acted on
        the ask in the last QUIET_APPEND_S. A Telegram failure is recorded as
        edit_failed and never raised."""
        with self._ask_presentation(item.id):
            item = self.store.get_item(item.id)
            if item.status != "open":
                self._show_resolution(item.id)
                return
            current = self.store.current_revision(item.id)
            if current is None or current.rev != revision.rev:
                self._show_open_again(item.id)
                return
            chat_id, message_id, message_rev, first_shown = self._message_view(item)
            alert = self.telegram_mode == "alert"
            pending = self._pending(item.id)
            body = json.loads(revision.body)
            first_new = len(json.loads(previous.body).get("steps") or []) + 1
            if message_id and not pending:
                try:
                    if alert:
                        self.telegram.edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
                    else:
                        text, shown = self._message_text(item, message_rev, first_shown)
                        self.telegram.edit_message_text(
                            chat_id, message_id, text + "\n" + TelegramClient.steps_added_line(),
                            reply_markup=TelegramClient.question_keyboard(
                                item.id, shown, self._current_answers(item.id), rev=message_rev,
                                first_path=first_shown or 1))
                except TelegramError as exc:
                    if "message is not modified" not in str(exc):
                        logger.warning("[mailbox] append %s: old message edit failed: %s", item.id,
                                       type(exc).__name__)
                        self.store.append_event(item.id, "edit_failed", "daemon", str(exc))
            steps = body.get("steps") or []
            if alert:
                text = TelegramClient.render_alert(item.project_id or "", item.title, body) + "\nSteps added"
                keyboard = TelegramClient.alert_keyboard(item.id, self.open_base)
            else:
                text = TelegramClient.render_appended(
                    item.project_id or "", item.title, len(steps) - first_new + 1, revision.reason,
                    list(enumerate(steps[first_new - 1:], first_new)))
                if pending:
                    text += "\nCorrected: review in Hook"
                    keyboard = TelegramClient.pending_keyboard(item.id, self.open_base)
                else:
                    keyboard = self.telegram.ask_keyboard(
                        item.id, self.open_base, body, self._current_answers(item.id), rev=revision.rev,
                        first_path=first_new, ready=self._ready(item.id))
            try:
                response = self.telegram.send_message(chat_id, text, reply_markup=keyboard,
                                                      disable_notification=self._owner_acted_lately(item.id),
                                                      reply_to=message_id)
            except TelegramError as exc:
                logger.warning("[mailbox] append %s: Telegram failed: %s", item.id, type(exc).__name__)
                self.store.append_event(item.id, "edit_failed", "daemon", str(exc))
                if message_id and not pending:
                    self._restore_after_failed_append(item, chat_id, message_id, message_rev, first_shown)
                return
            new_id = response.get("message_id")
            if new_id:
                self.store.record_tg_message(new_id, item.id, revision.rev)

    def _restore_after_failed_append(self, item: Item, chat_id: int, message_id: int, message_rev: int,
                                     first_shown: Optional[int]) -> None:
        """The append's reply wasn't sent, so the old message, which lost Done and Skip
        to it, is the ask's newest again: redraw it as a later redraw would, its own
        text (no "see below") with the keyboard of the version in force. Best effort."""
        try:
            if self.telegram_mode == "alert":
                self.telegram.edit_message_reply_markup(chat_id, message_id, self._keyboard(item))
            else:
                text, _ = self._message_text(item, message_rev, first_shown)
                self.telegram.edit_message_text(chat_id, message_id, text, reply_markup=self._keyboard(item))
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                logger.warning("[mailbox] append %s: old message restore failed: %s", item.id,
                               type(exc).__name__)

    def _owner_acted_lately(self, item_id: str) -> bool:
        """Did the owner act on this ask (an event whose actor is "owner" and whose kind
        is in OWNER_ATTENTION_KINDS) in the last QUIET_APPEND_S? The Pond's own events,
        such as an append's accepted/daemon, and the owner's `accepted` never count."""
        now = self._utc()
        for event in self.store.events_for_item(item_id):
            if event.actor != "owner" or event.kind not in OWNER_ATTENTION_KINDS:
                continue
            try:
                age = (now - datetime.fromisoformat(event.at)).total_seconds()
            except (TypeError, ValueError):
                continue
            if 0 <= age < QUIET_APPEND_S:
                return True
        return False

    def _message_view(self, item: Item) -> tuple[int, Optional[int], int, Optional[int]]:
        """(chat id, message id, rev, first step shown) of the ask's newest message. The
        first step is set when that message is the reply an append sent (Pond 1.9.0):
        recorded at an append rev and not the ask's own message, so it shows only the
        steps from there on; None for a message that shows the whole ask."""
        chat_id, message_id, message_rev = self._current_message(item)
        return chat_id, message_id, message_rev, self._first_shown(item, message_id, message_rev)

    def _first_shown(self, item: Item, message_id: Optional[int], message_rev: int) -> Optional[int]:
        """The first step a message of this ask shows when it is an append's reply,
        else None (see _message_view)."""
        if not message_id or message_id == json.loads(item.payload).get("tg_message_id"):
            return None
        if self.store.revision_kinds(item.id).get(message_rev) != revisions.KIND_APPEND:
            return None
        before = next((r for r in self.store.revisions(item.id) if r.rev == message_rev - 1), None)
        return None if before is None else len(json.loads(before.body).get("steps") or []) + 1

    def _refresh_tapped(self, item_id: str, message_id: Optional[int]) -> None:
        """After an answer tapped on an older message of the ask (one steps were added
        below, Pond 1.9.0), redraw that message's own question buttons, bound to its
        rev, so it shows the ✓ too. The newest message is redrawn by _redraw_keyboard.
        Best effort."""
        if not message_id or self.telegram_mode == "alert":
            return
        found = self.store.tg_message(message_id)
        item = self.store.get_item(item_id)
        if item is None or item.status != "open":
            return
        rev = found[1] if found and found[0] == item_id else None
        if rev is None and message_id == json.loads(item.payload).get("tg_message_id"):
            rev = 1
        if rev is None:
            return
        with self._ask_presentation(item_id):
            chat_id, newest, _ = self._current_message(item)
            if message_id == newest:
                return
            shown = next((r for r in self.store.revisions(item_id) if r.rev == rev), None)
            if shown is None:
                return
            keyboard = TelegramClient.question_keyboard(
                item_id, json.loads(shown.body), self._current_answers(item_id), rev=rev,
                first_path=self._first_shown(item, message_id, rev) or 1)
            try:
                self.telegram.edit_message_reply_markup(chat_id, message_id, keyboard)
            except TelegramError as exc:
                if "message is not modified" not in str(exc):
                    logger.warning("[mailbox] answers %s: old keyboard edit failed: %s", item_id,
                                   type(exc).__name__)

    def _message_text(self, item: Item, rev: int, first_shown: Optional[int]) -> tuple[str, Any]:
        """The full-mode text of a message that shows revision `rev` (from step
        `first_shown` on for an append's reply), and that revision's body."""
        found = next((r for r in self.store.revisions(item.id) if r.rev == rev), None)
        if found is None:
            _, body = self.in_force(item.id)
        else:
            body = json.loads(found.body)
        if first_shown is not None and found is not None:
            steps = body.get("steps") or []
            return TelegramClient.render_appended(
                item.project_id or "", item.title, len(steps) - first_shown + 1, found.reason,
                list(enumerate(steps[first_shown - 1:], first_shown))), body
        payload = json.loads(item.payload)
        return TelegramClient.render_ask_message(item.project_id or "", item.title, payload.get("why"),
                                                 payload.get("cmd"), payload.get("link"), body), body

    def _announce_correction(self, item: Item, previous: Revision, revision: Revision, summary: dict) -> None:
        """After an amend commit: strike the ask's current message through, then send
        the Corrected message as a reply to it, with Skip and Open in Hook only, and
        silently when the replaced revision is under 10 minutes old. A Telegram
        failure is recorded as edit_failed and never raised."""
        with self._ask_presentation(item.id):
            item = self.store.get_item(item.id)
            if item.status != "open":
                self._show_resolution(item.id)
                return
            current = self.store.current_revision(item.id)
            if current is None or current.rev != revision.rev:
                self._show_open_again(item.id)
                return
            chat_id, message_id, _ = self._current_message(item)
            _, body = self.in_force(item.id)
            alert = self.telegram_mode == "alert"
            try:
                if message_id:
                    try:
                        self.telegram.edit_message_text(chat_id, message_id,
                                                        TelegramClient.render_superseded(item.title, revision.rev),
                                                        reply_markup={"inline_keyboard": []})
                    except TelegramError as exc:
                        if "message is not modified" not in str(exc):
                            raise
                quiet = (datetime.fromisoformat(revision.at) - datetime.fromisoformat(previous.at)).total_seconds() \
                    < QUIET_CORRECTION_S
                response = self.telegram.send_message(
                    chat_id,
                    (TelegramClient.render_alert(item.project_id or "", item.title, body) + "\nCorrected: review in Hook"
                     if alert else TelegramClient.render_corrected(item.project_id or "", item.title, revision.rev, revision.reason, summary)),
                    reply_markup=(TelegramClient.alert_keyboard(item.id, self.open_base) if alert
                                  else TelegramClient.pending_keyboard(item.id, self.open_base)),
                    disable_notification=quiet, reply_to=message_id,
                )
            except TelegramError as exc:
                logger.warning("[mailbox] amend %s: Telegram failed: %s", item.id, type(exc).__name__)
                self.store.append_event(item.id, "edit_failed", "daemon", str(exc))
                return
            new_id = response.get("message_id")
            if new_id:
                self.store.record_tg_message(new_id, item.id, revision.rev)

    def _current_message(self, item: Item) -> tuple[int, Optional[int], int]:
        """(chat id, message id, rev) of the ask's newest Telegram message: the newest
        tg_messages row, else payload.tg_message_id at rev 1. Every edit goes here."""
        payload = json.loads(item.payload)
        chat_id = payload.get("tg_chat_id") or self.telegram.owner_id
        newest = self.store.newest_tg_message(item.id)
        if newest is not None:
            return chat_id, newest[0], newest[1]
        return chat_id, payload.get("tg_message_id"), 1

    def ack(self, item_id: str, *, actor: str = "session") -> Item:
        """Mark an ask as picked up (👀 prefix on Telegram), on its newest message,
        rendering the version in force."""
        item = self.store.get_item(item_id)
        if not item:
            raise ValueError(f"item {item_id} not found")

        with self._ask_presentation(item_id):
            item = self.store.get_item(item_id)
            if item.status == "open":
                chat_id, message_id, _ = self._current_message(item)
                if message_id:
                    current_text, keyboard = self._render_open(item)
                    prefixed = TelegramClient.render_ack_prefix() + current_text
                    try:
                        # Ruling S9: editMessageText drops the keyboard when
                        # reply_markup is omitted, so the ack prefix must re-send
                        # the Done/Skip keyboard explicitly.
                        self.telegram.edit_message_text(chat_id, message_id, prefixed, reply_markup=keyboard)
                    except TelegramError:
                        pass  # Log but don't fail

        self.store.append_event(item_id, "acked", actor, None)
        return self.store.get_item(item_id)

    def steps(self, item_id: str, text: str, *, actor: str = "session") -> Item:
        """Post a session note under an ask (a reply to its newest message): plain,
        escaped text under a fixed prefix, never part of the ask. Raises AmendError
        400 invalid steps for a text the note rules refuse."""
        item = self.store.get_item(item_id)
        if not item:
            raise ValueError(f"item {item_id} not found")
        problems = revisions.check_note("text", text, revisions.MAX_STEPS_NOTE, multi=True)
        if problems:
            raise AmendError(400, {"error": "invalid steps", "details": problems})

        chat_id, message_id, _ = self._current_message(item)
        if message_id and self.telegram_mode == "full":
            try:
                self.telegram.send_reply(chat_id, message_id, TelegramClient.render_session_note(text))
            except TelegramError:
                pass  # Log but don't fail

        self.store.append_event(item_id, "steps", actor, text)
        return self.store.get_item(item_id)

    def _pending(self, item_id: str) -> bool:
        """Is a correction waiting for the owner's review (current rev > accepted)?"""
        current = self.store.current_revision(item_id)
        return current is not None and current.rev > self.store.accepted_rev(item_id)

    def _render_open(self, item: Item, *, whole: bool = False) -> tuple[str, dict[str, Any]]:
        """The open ask's message text and keyboard, from the version in force; while
        a correction waits for review, a "Corrected: review in Hook" line and the
        pending keyboard (Skip and Open in Hook only). When the newest message is an
        append's reply (Pond 1.9.0) it keeps showing the added steps only, unless
        `whole` (a fresh message, which shows the whole ask)."""
        payload = json.loads(item.payload)
        _, body = self.in_force(item.id)
        first_shown = None if whole or self.telegram_mode == "alert" else self._message_view(item)[3]
        if self.telegram_mode == "alert":
            text = TelegramClient.render_alert(item.project_id or "", item.title, body)
        elif first_shown is not None:
            text = self._message_text(item, self._message_view(item)[2], first_shown)[0]
        else:
            text = TelegramClient.render_ask_message(
                item.project_id or "", item.title, payload.get("why"), payload.get("cmd"), payload.get("link"), body)
        if self._pending(item.id):
            text += "\nCorrected: review in Hook"
        return text, self._keyboard(item, whole=whole)

    REPLY_KINDS = ("reply", "message")

    def reply(self, item_id: str, text: str, *, rev: Optional[int] = None, auth: str = "token",
              kind: str = "reply") -> Item:
        """Record the owner's answer given in Relay. It is the same event as a
        text reply in Telegram (kind "reply", actor "owner"), so a session reading
        replies sees it. Telegram is not touched. `rev` and `auth` are
        recorded; a reply is never refused for a stale rev. When it turns the
        ask's Done on (done: reply or message), the keyboard is redrawn (RD 16).

        kind "message" (Pond 1.2.0) is the owner talking to the posting session: a
        message/owner event with the text verbatim, never an answer. It is bound to
        `rev`, else the current revision. Raises AmendError: 400 invalid message
        (the note rules, MAX_MESSAGE, multi-line), 409 ask is closed, 409 too many
        messages (MAX_MESSAGES per ask). An unknown kind raises ValueError."""
        if kind not in self.REPLY_KINDS:
            raise ValueError(f"reply: unknown kind {kind!r}")
        item = self.store.get_item(item_id)
        if not item:
            raise ValueError(f"item {item_id} not found")

        ready = self._ready(item_id)
        if kind == "message":
            problems = revisions.check_note("text", text, revisions.MAX_MESSAGE, multi=True)
            if problems:
                raise AmendError(400, {"error": "invalid message", "details": problems})
            if item.status != "open":
                raise AmendError(409, {"error": "ask is closed"})
            if sum(1 for e in self.store.events_for_item(item_id) if e.kind == "message") >= revisions.MAX_MESSAGES:
                raise AmendError(409, {"error": "too many messages"})
            if rev is None:
                current = self.store.current_revision(item_id)
                rev = current.rev if current is not None else None
            if self.store.append_events(item_id, "message", "owner", [text], only_if_open=True,
                                        rev=rev, auth=auth) is None:
                raise AmendError(409, {"error": "ask is closed"})     # closed between the check and the write
            self._redraw_if_flipped(item_id, ready)
            return self.store.get_item(item_id)

        self.store.append_event(item_id, "reply", "owner", text.strip(), rev=rev, auth=auth)
        self._redraw_if_flipped(item_id, ready)
        return self.store.get_item(item_id)

    # --- Ask about this (Pond 1.14.0) -------------------------------------------

    def ask_about_context(self, item: Item) -> str:
        """ask_about.build_context() of the version in force (the one answers and ticks
        follow), with the owner's counted answers and ticks."""
        _, body = self.in_force(item.id)
        if body is None:                           # an ask from before instructables
            payload = json.loads(item.payload) if item.payload else {}
            body = {"title": item.title, "why": payload.get("why")}
        events = self._event_dicts(item.id)
        return ask_about.build_context(body, status=item.status, ticked=self._ticked(item.id, body, events),
                                       answers=self._current_answers(item.id))

    def ask_about(self, item_id: str, question: Any, *, auth: str) -> tuple[str, list]:
        """The owner's question about an ask, answered by ask_about.MODEL from the ask and
        its brief. Raises AmendError, in this order: 404 not found (also while the
        feature is off), 400 invalid question (the note rules: 1 to MAX_QUESTION
        characters, multi-line, no hidden characters, nothing secret-like), 409 ask is
        closed, 429 too many questions (MAX_PER_ASK on this ask), 429 daily limit
        reached (MAX_PER_DAY on this Pond since 00:00 UTC), 502 answer unavailable.
        On success the question (ask_about_question, actor owner, `auth`) and the answer
        (ask_about_answer, actor hook) are stored together, bound to the current
        revision; on any failure nothing is stored, so a failed call never counts
        against a limit. The session is not told: no reader of owner acts lists these
        kinds. → (the answer, [question event, answer event])."""
        if self.answerer is None:
            raise AmendError(404, {"error": "not found"})
        item = self.store.get_item(item_id)
        if item is None:
            raise AmendError(404, {"error": "not found"})
        problems = revisions.check_note("question", question, ask_about.MAX_QUESTION, multi=True)
        if problems:
            raise AmendError(400, {"error": "invalid question", "details": problems})
        with self._ask_about_lock:
            item = self.store.get_item(item_id)
            if item is None:
                raise AmendError(404, {"error": "not found"})
            if item.status != "open":
                raise AmendError(409, {"error": "ask is closed"})
            asked = sum(1 for e in self.store.events_for_item(item_id) if e.kind == ask_about.KIND_QUESTION)
            if asked >= ask_about.MAX_PER_ASK:
                raise AmendError(429, {"error": "too many questions",
                                       "message": f"This ask has had {ask_about.MAX_PER_ASK} questions. "
                                                  "Ask the session instead."})
            now = self._utc()
            day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            if self.store.count_events(ask_about.KIND_QUESTION, since=self._stamp(day)) >= ask_about.MAX_PER_DAY:
                raise AmendError(429, {"error": "daily limit reached",
                                       "message": f"Hook answered {ask_about.MAX_PER_DAY} questions today. "
                                                  "Ask the session, or try again tomorrow."})
            context = self.ask_about_context(item)
            try:
                answer = self.answerer.answer(context, question)
            except ask_about.AnswerUnavailable:
                raise AmendError(502, {"error": "answer unavailable"}) from None
            current = self.store.current_revision(item_id)
            events = self.store.append_pair(
                item_id, (ask_about.KIND_QUESTION, "owner", question),
                (ask_about.KIND_ANSWER, ask_about.ACTOR_ANSWER, answer),
                at=self._stamp(now), rev=current.rev if current is not None else None, auth=auth)
            if events is None:
                raise AmendError(409, {"error": "ask is closed"})    # closed while the model answered
            return answer, events

    def _current_answers(self, item_id: str) -> dict[str, dict]:
        """The owner's counted answer per step path of the version in force."""
        revision, body = self.in_force(item_id)
        events = [
            {"seq": e.seq, "kind": e.kind, "actor": e.actor, "text": e.text, "at": e.at, "rev": e.rev}
            for e in self.store.events_for_item(item_id)
        ]
        bodies = {r.rev: json.loads(r.body) for r in self.store.revisions(item_id)}
        counted, _ = current_answers(events, bodies, body)
        paths = revisions.id_paths(body)
        return {paths[step_id]: entry for step_id, entry in counted.items() if step_id in paths}

    def _event_dicts(self, item_id: str) -> list[dict[str, Any]]:
        return [{"seq": e.seq, "kind": e.kind, "actor": e.actor, "text": e.text, "at": e.at, "rev": e.rev}
                for e in self.store.events_for_item(item_id)]

    def _ticked(self, item_id: str, body: Any, events: list[dict[str, Any]]) -> set[str]:
        bodies = {r.rev: json.loads(r.body) for r in self.store.revisions(item_id)}
        return req.ticked(events, bodies, body)

    def requirements_left(self, item_id: str) -> Optional[list[dict]]:
        """What the version in force still needs before the owner's Done (Pond 1.3.0):
        None when it has no requirements, else requirements.left() of the accepted
        body, where answered = the paths with a counted answer, replied = any
        reply/owner event and messaged = any message/owner event (RD 5)."""
        _, body = self.in_force(item_id)
        if not req.wanted(body):
            return None
        events = self._event_dicts(item_id)
        owned = {e["kind"] for e in events if e["actor"] == "owner"}
        return req.left(body, ticked=self._ticked(item_id, body, events),
                        answered=set(self._current_answers(item_id)),
                        replied="reply" in owned, messaged="message" in owned)

    def progress(self, item_id: str, tick: Any, untick: Any, *, rev: Optional[int], hash: Optional[str],
                 auth: str) -> Item:
        """The owner's tick changes from Hook (Pond 1.3.0, RD 1): step ids to tick and
        to untick, bound to the version in force (the binding rule). When the delta
        changes the folded set, one progress/owner event
        is stored with the bound rev, `auth` and the effective change as canonical
        JSON {"tick":[sorted],"untick":[sorted]}; otherwise nothing (RD 14). Null
        counts as an empty list. A stored progress that turns Done on or off
        redraws the Telegram keyboard (RD 16). Raises ValueError for an unknown
        ask, AskClosed unless it is open, Corrected for a stale version,
        InvalidProgress with field paths (never values)."""
        with self.store.event_transaction():
            item = self.store.get_item(item_id)
            if not item:
                raise ValueError(f"item {item_id} not found")
            if item.status != "open":
                raise AskClosed()
            named = {i for ids in (tick, untick) if isinstance(ids, list) for i in ids if isinstance(i, str)}
            bound = self._bind(item_id, rev, hash, steps=named)
            _, body = self.in_force(item_id)
            known = set(req.identities(body))
            lists = {"tick": [] if tick is None else tick, "untick": [] if untick is None else untick}
            details: list[str] = []
            for key in req.PROGRESS_KEYS:
                ids = lists[key]
                if not isinstance(ids, list):
                    details.append(f"{key}: must be a list of step ids")
                elif len(ids) > req.MAX_PROGRESS_IDS:
                    details.append(f"{key}: more than {req.MAX_PROGRESS_IDS} ids")
                else:
                    for i, step_id in enumerate(ids):
                        if not isinstance(step_id, str) or step_id not in known:
                            details.append(f"{key}[{i}]: unknown step")
                        elif key == "untick" and isinstance(lists["tick"], list) and step_id in lists["tick"]:
                            details.append(f"{key}[{i}]: also in tick")
            if details:
                raise InvalidProgress(details)
            before = self._ticked(item_id, body, self._event_dicts(item_id))
            after = (before | set(lists["tick"])) - set(lists["untick"])
            if after == before:
                return item
            text = json.dumps({"tick": sorted(after - before), "untick": sorted(before - after)},
                              separators=(",", ":"))
            ready = self._ready(item_id)
            if self.store.append_events(item_id, "progress", "owner", [text], only_if_open=True,
                                        rev=bound, auth=auth) is None:
                raise AskClosed()           # closed between the check and the write
        self._redraw_if_flipped(item_id, ready)
        return self.store.get_item(item_id)

    def _keyboard(self, item: Item, *, whole: bool = False) -> dict[str, Any]:
        """The open ask's keyboard, from the version in force: its question rows with
        the current ✓ and Done/Skip, every callback bound to that rev, Skip alone
        while the ask still needs something before Done (Pond 1.3.0); while a
        correction waits for review, Skip and Open in Hook only. On an append's reply
        (Pond 1.9.0) the question rows start at the first step it added, unless
        `whole`."""
        if self.telegram_mode == "alert":
            return TelegramClient.alert_keyboard(item.id, self.open_base)
        if self._pending(item.id):
            return TelegramClient.pending_keyboard(item.id, self.open_base)
        revision, body = self.in_force(item.id)
        first_shown = None if whole else self._message_view(item)[3]
        return self.telegram.ask_keyboard(
            item.id, self.open_base, body, self._current_answers(item.id),
            rev=revision.rev if revision is not None else 1, ready=self._ready(item.id),
            first_path=first_shown or 1,
        )

    def _ready(self, item_id: str) -> bool:
        """May the owner's Done go now? True for an ask without requirements, unless a
        `then: "steps"` question waits for the session's next steps (Pond 1.9.0)."""
        return not self.requirements_left(item_id) and self.step_wait(item_id) is None

    def step_wait(self, item_id: str) -> Optional[dict[str, Any]]:
        """requirements.step_wait() for the version in force, now: {"id", "path",
        "until"} while the owner's Done waits for the session's next steps, else None."""
        _, body = self.in_force(item_id)
        if not req.questions_then(body):
            return None
        return req.step_wait(body, self._event_dicts(item_id), self.store.revisions(item_id), self._utc())

    def sweep_step_waits(self, now: datetime) -> list[str]:
        """Redraw the keyboard of every open ask whose step wait ended by `now` and
        was not redrawn yet by this process, so Done comes back without an act (Pond
        1.9.0). There is no lower bound, so waits that ended while the Pond was down
        are redrawn on the first sweep. → the ids of those asks. Best effort."""
        if self.telegram_mode == "alert":
            return []                    # an alert keyboard has no Done to bring back
        ended: list[str] = []
        for item in self.store.list_items(status="open", days=36500):
            _, body = self.in_force(item.id)
            if not req.questions_then(body):
                continue
            waits = req.step_waits(body, self._event_dicts(item.id), self.store.revisions(item.id))
            done = {(item.id, w["until"].isoformat()) for w in waits if w["until"] <= now} - self._swept_waits
            if done:
                self._swept_waits |= done
                ended.append(item.id)
                self._redraw_keyboard(item.id)
        return ended

    def _redraw_if_flipped(self, item_id: str, was_ready: bool) -> None:
        """Redraw the keyboard only when a write turned Done on or off (RD 16)."""
        if self._ready(item_id) != was_ready:
            self._redraw_keyboard(item_id)

    def _bind(self, item_id: str, rev: Optional[int], hash: Optional[str], *,
              steps: Optional[set[str]] = None, done: bool = False) -> Optional[int]:
        """The binding rule for answers, progress and the owner's Done: the act's rev
        (absent = 1) must be the current rev and the accepted rev, and a given hash
        the current hash.

        After an append (Pond 1.9.0) an act may name an older rev: when the current
        rev is the accepted one, every rev after the act's is an append, and a given
        hash is the act's rev's hash, Done raises StepsAdded (the new steps are
        unseen), and an answer or progress act binds to the current rev when every
        id in `steps` is the same step, as a JSON value, in both revs. Raises
        Corrected otherwise. → the bound rev, or None for an ask without revisions."""
        current = self.store.current_revision(item_id)
        if current is None:
            return None
        accepted = self.store.accepted_rev(item_id)
        wanted = 1 if rev is None else rev
        if wanted == current.rev == accepted:
            if hash is not None and not hmac.compare_digest(str(hash).encode(), current.hash.encode()):
                raise Corrected(current, accepted)
            return current.rev
        if accepted != current.rev or type(wanted) is not int or not 1 <= wanted < current.rev:
            raise Corrected(current, accepted)
        history = {r.rev: r for r in self.store.revisions(item_id)}
        kinds = self.store.revision_kinds(item_id)
        older = history.get(wanted)
        if (older is None
                or (hash is not None and not hmac.compare_digest(str(hash).encode(), older.hash.encode()))
                or any(kinds.get(k) != revisions.KIND_APPEND for k in range(wanted + 1, current.rev + 1))):
            raise Corrected(current, accepted)
        if done:
            raise StepsAdded(current, accepted)
        then, now = self._steps_by_id(json.loads(older.body)), self._steps_by_id(json.loads(current.body))
        for step_id in steps or ():
            if step_id not in then or step_id not in now or not revisions.json_equal(then[step_id], now[step_id]):
                raise Corrected(current, accepted)
        return current.rev

    @staticmethod
    def _steps_by_id(body: Any) -> dict[str, dict]:
        """Step id → the step (or sub-step) object, for every step with an id."""
        return {step["id"]: step for _, step, _ in revisions._entries(body) if isinstance(step.get("id"), str)}

    def _ids_at(self, item_id: str, rev: Optional[int], refs: list[Any]) -> set[str]:
        """The step ids that answers' `step` references (a path such as "3" or "3.2",
        or an id) name in revision `rev` (absent = 1). A reference that names no step
        there gives an id no step has, so the binding refuses it."""
        wanted = 1 if rev is None else rev
        found = next((r for r in self.store.revisions(item_id) if r.rev == wanted), None)
        if found is None:
            return set()
        body = json.loads(found.body)
        by_path = {path: sid for sid, path in revisions.id_paths(body).items()}
        ids = set(self._steps_by_id(body))
        out: set[str] = set()
        for ref in refs:
            if isinstance(ref, str) and ref in by_path:
                out.add(by_path[ref])
            elif isinstance(ref, str) and ref in ids:
                out.add(ref)
            else:
                out.add("\x00missing")
        return out

    def answer(self, item_id: str, answers: Any, *, rev: Optional[int] = None, hash: Optional[str] = None,
               auth: str = "token") -> Item:
        """Record a batch of owner answers to the ask's step questions: one
        `answer` event per answer, all in one transaction, then redraw the
        Telegram keyboard (best effort). The ask stays open. The batch is bound
        to the version in force (the binding rule) and validated against it.

        Raises ValueError for an unknown ask, AskClosed unless it is open,
        Corrected for a stale version, InvalidAnswers when validate_answers
        refuses the batch (nothing stored)."""
        item = self.store.get_item(item_id)
        if not item:
            raise ValueError(f"item {item_id} not found")
        if item.status != "open":
            raise AskClosed()
        refs = [a.get("step") for a in answers if isinstance(a, dict)] if isinstance(answers, list) else []
        bound = self._bind(item_id, rev, hash, steps=self._ids_at(item_id, rev, refs))
        _, body = self.in_force(item_id)
        texts, errors = validate_answers(body, answers)
        if errors:
            raise InvalidAnswers(errors)
        # Stamped on the Pond's clock, which a step wait is measured on (Pond 1.9.0).
        if self.store.append_events(item_id, "answer", "owner", texts, only_if_open=True,
                                    rev=bound, auth=auth, at=self._stamp()) is None:
            raise AskClosed()           # closed between the check and the write
        self._redraw_keyboard(item_id)
        return self.store.get_item(item_id)

    def _redraw_keyboard(self, item_id: str) -> None:
        """Refresh an open ask's Telegram buttons, and its text in alert mode.
        A full-mode message may predate the switch to alert, so an alert redraw
        must replace its content too. Preserve the current correction and ack
        state. Failures are best effort: the owner action is already stored."""
        with self._ask_presentation(item_id):
            item = self.store.get_item(item_id)
            if item is None or item.status != "open":
                return
            chat_id, message_id, message_rev = self._current_message(item)
            if not message_id:
                return
            try:
                if self.telegram_mode == "alert":
                    text, keyboard = self._render_open(item)
                    # These events replace an open message's text without an ack
                    # prefix. Only an ack since the last replacement belongs to it.
                    for event in reversed(self.store.events_for_item(item_id)):
                        if event.kind == "acked":
                            text = TelegramClient.render_ack_prefix() + text
                            break
                        if (event.kind in ("posted", "revised", "reopened", "resolved", "declined")
                                or (event.kind == "accepted" and event.rev == message_rev)):
                            break
                    self.telegram.edit_message_text(chat_id, message_id, text, reply_markup=keyboard)
                else:
                    self.telegram.edit_message_reply_markup(chat_id, message_id, self._keyboard(item))
            except TelegramError as exc:
                if "message is not modified" in str(exc):
                    return              # already showing these ticks (cf. S12)
                logger.warning("[mailbox] answers %s: keyboard edit failed: %s", item_id, type(exc).__name__)

    def _answer_tap(self, rest: str, message_id: Optional[int] = None) -> str:
        """A tap on a question button (a:<id>:<path>:<index>[:<rev>]) → the toast text.
        Re-tapping the current pick stores nothing (R8)."""
        item_id, _, tail = rest.partition(":")
        path, _, tail = tail.partition(":")
        index, _, rev_text = tail.partition(":")
        rev = int(rev_text) if rev_text.isdigit() else 1           # no rev: rev 1
        item = self.store.get_item(item_id) if item_id else None
        if item is None or item.status != "open":
            return "This ask is closed"
        try:
            self._bind(item_id, rev, None, steps=self._ids_at(item_id, rev, [path]))
        except Corrected:
            return CHANGED_TOAST
        _, body = self.in_force(item_id)
        question = questions(body).get(path) or {}
        choices = choice_labels(question.get("choices"))         # a word choice by its label
        if not index.isdigit() or int(index) >= len(choices):
            return "That choice is gone"
        choice = choices[int(index)]
        toast = f"{path}: {choice}"
        current = self._current_answers(item_id).get(path) or {}
        if current.get("picked") == [choice]:
            return toast
        try:
            self.answer(item_id, [{"step": path, "picked": [choice]}], rev=rev, auth="telegram")
        except Corrected:
            return CHANGED_TOAST
        except (AskClosed, ValueError):
            return "This ask is closed"
        except InvalidAnswers:
            return "That choice is gone"
        self._refresh_tapped(item_id, message_id)
        return toast

    def resolve(
        self,
        item_id: str,
        verdict: str,
        note: Optional[str] = None,
        actor: str = "session",
        rev: Optional[int] = None,
        hash: Optional[str] = None,
        auth: Optional[str] = None,
    ) -> Item:
        """Resolve an ask: edit or replace the Telegram message, mark as done/dismissed.

        `actor` is "session" for a direct call (e.g. the HTTP API) and
        "owner" when this is invoked on the owner's behalf from
        `handle_update` (a button tap or a verdict-word reply) — Ruling S13.
        The owner's Done is bound to the version in force (Corrected otherwise),
        then refused with RequirementsUnmet while that version still needs
        something (Pond 1.3.0, RD 4), then with WaitingForSteps while a `then:
        "steps"` question waits for the session's next steps (Pond 1.9.0):
        nothing is written or edited then. Declined,
        and anything the session does, is never bound or gated (RD 19). `auth`
        (and the bound rev) are recorded on the event.
        """
        if verdict not in ("done", "declined"):
            raise ValueError(f"resolve: verdict must be 'done' or 'declined', got {verdict!r}")

        # Validation and both durable writes share the same transaction as
        # progress; amendments use BEGIN IMMEDIATE against the same connection.
        with self.store.event_transaction():
            item = self.store.get_item(item_id)
            if not item:
                raise ValueError(f"item {item_id} not found")
            bound = rev
            if verdict == "done" and actor == "owner":
                bound = self._bind(item_id, rev, hash, done=True)
                needs = self.requirements_left(item_id)
                if needs:
                    raise RequirementsUnmet(needs)
                wait = self.step_wait(item_id)
                if wait is not None:
                    raise WaitingForSteps(wait)
            self.store.update_item(item_id, {"status": "done" if verdict == "done" else "dismissed"})
            self.store.append_event(item_id, "resolved" if verdict == "done" else "declined",
                                    actor, note, rev=bound, auth=auth)
        self._show_resolution(item_id)
        return self.store.get_item(item_id)

    def _show_resolution(self, item_id: str) -> None:
        """Present the latest committed close, or a reopen that overtook it."""
        with self._ask_presentation(item_id):
            item = self.store.get_item(item_id)
            if item is None:
                return
            if item.status == "open":
                self._show_open_again(item_id)
                return
            verdict = "done" if item.status == "done" else "declined"
            latest = next((e for e in reversed(self.store.events_for_item(item_id))
                           if e.kind in ("resolved", "declined")), None)
            note = latest.text if latest else None
            chat_id, tg_message_id, _ = self._current_message(item)

            # Try to edit the ask's newest message.
            if tg_message_id:
                if self.telegram_mode == "alert":
                    _, body = self.in_force(item.id)
                    resolved_text = (TelegramClient.render_alert(item.project_id or "", item.title, body)
                                     + ("\nDone" if verdict == "done" else "\nSkipped"))
                else:
                    resolved_text = TelegramClient.render_resolved_message(item.title, verdict, note)
                try:
                    # Ruling S9: the Done/Skip keyboard must be explicitly
                    # cleared on resolve, or Telegram leaves it in place.
                    self.telegram.edit_message_text(
                        chat_id,
                        tg_message_id,
                        resolved_text,
                        reply_markup={"inline_keyboard": []},
                    )
                except TelegramError as e:
                    err_msg = str(e)
                    # Ruling S12: "message is not modified" means the message
                    # already shows the resolved text — that's success, not a
                    # failure; no fallback, no edit_failed event. Every other
                    # TelegramError (the whitelisted ones and anything else)
                    # falls back to a fresh message so the owner sees the
                    # resolution, and is recorded as edit_failed.
                    if "message is not modified" not in err_msg:
                        fallback_text = (resolved_text if self.telegram_mode == "alert"
                                         else _render_fallback_message(item.title, verdict, note))
                        try:
                            self.telegram.send_message(chat_id, fallback_text)
                        except TelegramError:
                            pass
                        self.store.append_event(item_id, "edit_failed", "daemon", err_msg)

    def reopen(self, item_id: str, actor: str = "owner", *, rev: Optional[int] = None,
               auth: Optional[str] = None) -> Item:
        """Bring a closed ask (done or dismissed) back to open.

        Records one `reopened` event and puts the ask back in front of the
        owner on Telegram: its message is edited back to the open rendering
        with the Done/Skip keyboard. When that edit fails, a fresh message is
        sent with the same rendering and its id is stored, so text replies
        find the ask again. A Telegram failure is logged, never raised.
        Reopening an ask that is already open changes nothing.
        """
        with self.store.event_transaction():
            reopened = self.store.reopen_item(item_id)
            if reopened is None:
                raise ValueError(f"item {item_id} not found")
            if not reopened:
                return self.store.get_item(item_id)
            self.store.append_event(item_id, "reopened", actor, None, rev=rev, auth=auth)
        self._show_open_again(item_id)
        return self.store.get_item(item_id)

    def _show_open_again(self, item_id: str) -> None:
        """Edit a reopened ask's newest Telegram message back to its open form (the
        version in force), or send it afresh and record the new message. Best
        effort: errors are logged, not raised."""
        with self._ask_presentation(item_id):
            item = self.store.get_item(item_id)
            if item is None:
                return
            if item.status != "open":
                self._show_resolution(item_id)
                return
            if self.store.bug_meta(item_id) is not None:
                return                                   # a Hook bug never reaches Telegram
            payload = json.loads(item.payload)
            chat_id, tg_message_id, _ = self._current_message(item)
            text, keyboard = self._render_open(item)

            if tg_message_id:
                try:
                    self.telegram.edit_message_text(
                        chat_id, tg_message_id, text, reply_markup=keyboard
                    )
                    return
                except TelegramError as exc:
                    # Already showing the open form: that is success (cf. S12).
                    if "message is not modified" in str(exc):
                        return
                    logger.warning(
                        "[mailbox] reopen %s: edit failed, sending afresh: %s", item_id, exc
                    )

            if tg_message_id:
                text, keyboard = self._render_open(item, whole=True)      # a fresh message shows it all
            try:
                response = self.telegram.send_message(chat_id, text, reply_markup=keyboard)
            except TelegramError as exc:
                logger.warning("[mailbox] reopen %s: send failed: %s", item_id, exc)
                return
            new_id = response.get("message_id")
            if new_id:
                current = self.store.current_revision(item_id)
                self.store.record_tg_message(new_id, item_id, current.rev if current else 1)
                # Kept for Pond 1.0.0 code after a rollback, which reads only the payload.
                payload["tg_message_id"] = new_id
                payload["tg_chat_id"] = chat_id
                self.store.update_item(item_id, {"payload": json.dumps(payload)})

    def list_open(self, days: int = 14) -> list[Item]:
        """List open asks from the last N days, newest first."""
        return self.store.list_items(status="open", days=days)

    def get_item_with_events(self, item_id: str, *, for_agent: bool = False) -> tuple[Optional[Item], list[dict[str, Any]]]:
        """Fetch an item and its full event history.

        Uses Store.events_for_item, which has no store-wide window — Finding
        1 (S25): the previous read_events(after_seq=0, limit=1000)+filter
        approach silently lost an item's history once the store held more
        than 1000 lifetime events across all items.
        """
        item = self.store.get_item(item_id)
        if not item:
            return None, []

        events = self.store.events_for_item(item_id, for_agent=for_agent)
        item_events = [
            {
                "seq": e.seq,
                "kind": e.kind,
                "actor": e.actor,
                "text": e.text,
                "at": e.at,
                "rev": e.rev,
                "auth": e.auth,
            }
            for e in events
        ]
        return item, item_events

    def handle_update(self, update: dict[str, Any]) -> None:
        """Process a Telegram update: owner taps or replies."""
        if self.members is not None:
            return household.handle_update(self, update)
        # Filter: only process updates from the configured owner
        from_id = None
        if "message" in update:
            from_id = update["message"].get("from", {}).get("id")
        elif "callback_query" in update:
            from_id = update["callback_query"].get("from", {}).get("id")

        if from_id != self.telegram.owner_id:
            # Drop silently, count in logs (Ruling S13)
            self.dropped_updates += 1
            return

        # Handle callback query (button tap). Ruling S26: every
        # callback_query that passes the owner filter is answered EXACTLY
        # once, so the owner's phone never shows a spinner that never
        # resolves — whether the tap succeeded, named an unknown/invalid
        # item, or carried callback_data we don't recognize.
        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb.get("id")
            data = cb.get("data", "")

            redraw = None
            if self.telegram_mode == "alert" and data.startswith(("d:", "s:", "a:")):
                answer_text = "Open in Hook to answer, Done or Skip."
            elif data.startswith("d:"):
                # Done tap: d:<id>[:<rev>], bound to that rev (no rev: rev 1).
                item_id, _, rev_text = data[2:].partition(":")
                try:
                    self.resolve(item_id, "done", actor="owner", auth="telegram",
                                 rev=int(rev_text) if rev_text.isdigit() else 1)
                    answer_text = "Done"
                except StepsAdded:
                    answer_text = STEPS_ADDED_TEXT
                except Corrected:
                    answer_text = CHANGED_TOAST
                except RequirementsUnmet:
                    # A Done the keyboard shouldn't have shown (RD 18): toast, then redraw.
                    answer_text = NOT_YET_TOAST
                    redraw = item_id
                except WaitingForSteps:
                    answer_text = WAITING_FOR_STEPS
                    redraw = item_id
                except ValueError:
                    # Unknown item id (or, in principle, a bad verdict).
                    answer_text = "Unknown ask"
            elif data.startswith("s:"):
                # Skip tap: never bound to a rev.
                item_id = data[2:]
                try:
                    self.resolve(item_id, "declined", actor="owner", auth="telegram")
                    answer_text = "Skipped"
                except ValueError:
                    answer_text = "Unknown ask"
            elif data.startswith("a:"):
                # A question button: one answer, recorded at once (R7).
                answer_text = self._answer_tap(data[2:], (cb.get("message") or {}).get("message_id"))
            elif data.startswith("r:"):
                # A reminder button: done or tomorrow, written back to Central Time.
                answer_text = self._notification_tap(data[2:])
            elif data.startswith("e:"):
                # Approve or reject a Mac's owner key.
                answer_text = self._enroll_tap(data[2:])
            elif data.startswith("v:"):
                # Revoke or keep a key, asked with the Pond token alone (Pond 1.11.0).
                answer_text = self._revoke_tap(data[2:])
            elif data.startswith("k:"):
                # Confirm turning the owner-key requirement off.
                answer_text = self._enforce_tap(data[2:], cb.get("message"))
            else:
                # callback_data we don't recognize at all.
                answer_text = "Ignored"

            try:
                self.telegram.answer_callback_query(cb_id, answer_text)
            except TelegramError:
                pass  # the tap already did its work; the ack is best-effort
            if redraw is not None:
                self._redraw_keyboard(redraw)

        # Handle a text reply: the owner answered an ask by replying to its message.
        elif "message" in update:
            msg = update["message"]
            reply_to = (msg.get("reply_to_message") or {}).get("message_id")
            text = " ".join((msg.get("text") or "").split())
            if not text:
                return
            if not reply_to:
                self._capture(msg, text)
                return
            # The message's own rev: a reply binds to what that message showed.
            found = self.store.tg_message(reply_to)
            item = self.store.get_item(found[0]) if found else self.store.get_by_tg_message_id(reply_to)
            rev = found[1] if found else 1
            if item is None:
                # A reply to something that is not an ask (a reminder, an
                # "Added:" line): only the buttons act on those. Counted, never captured.
                self.ignored_messages += 1
                return
            if self.telegram_mode == "alert":
                self._reply(msg.get("chat", {}).get("id") or self.telegram.owner_id, msg.get("message_id"),
                            "Open in Hook to reply, answer, Done or Skip.")
                return
            first = text.lower().split(" ", 1)[0].rstrip(".,:;!")
            ready = self._ready(item.id)
            if first in ("done", "resolved", "fixed", "finished", "completed"):
                try:
                    self.resolve(item.id, "done", note=text, actor="owner", rev=rev, auth="telegram")
                except StepsAdded:
                    # A Done on the version before steps were added: kept as a reply, answered in-thread.
                    self.store.append_event(item.id, "reply", "owner", text, rev=rev, auth="telegram")
                    self._reply(msg.get("chat", {}).get("id") or self.telegram.owner_id, msg.get("message_id"),
                                STEPS_ADDED_TEXT)
                    self._redraw_if_flipped(item.id, ready)
                except Corrected:
                    # A Done on a version that isn't in force: kept as a reply, answered in-thread.
                    self.store.append_event(item.id, "reply", "owner", text, rev=rev, auth="telegram")
                    self._reply(msg.get("chat", {}).get("id") or self.telegram.owner_id, msg.get("message_id"),
                                OUT_OF_DATE)
                    self._redraw_if_flipped(item.id, ready)
                except RequirementsUnmet:
                    # Something is left (RD 18): kept as a reply (it may finish done: reply),
                    # answered in-thread, the keyboard redrawn. It never closes the ask.
                    self.store.append_event(item.id, "reply", "owner", text, rev=rev, auth="telegram")
                    self._reply(msg.get("chat", {}).get("id") or self.telegram.owner_id, msg.get("message_id"),
                                NOT_YET_REPLY if self.requirements_left(item.id) else REPLY_KEPT)
                    self._redraw_keyboard(item.id)
                except WaitingForSteps:
                    # The session is adding steps: kept as a reply, answered in-thread.
                    self.store.append_event(item.id, "reply", "owner", text, rev=rev, auth="telegram")
                    self._reply(msg.get("chat", {}).get("id") or self.telegram.owner_id, msg.get("message_id"),
                                WAITING_FOR_STEPS)
                    self._redraw_keyboard(item.id)
            elif first in ("skip", "won't", "wont", "cancel", "cancelled", "decline", "declined"):
                self.resolve(item.id, "declined", note=text, actor="owner", rev=rev, auth="telegram")
            else:
                self.store.append_event(item.id, "reply", "owner", text, rev=rev, auth="telegram")
                self._redraw_if_flipped(item.id, ready)

    def accept(self, item_id: str, rev: int, hash: str, auth: str) -> Item:
        """The owner accepted this revision (with the owner key: the route always
        requires it): it becomes the version in force for Telegram, answers and
        Done. rev and hash must name a revision, and rev may not go below the
        accepted one (AmendError 409 stale revision); accepting the accepted rev
        again is fine (a second Mac). Allowed on any status. An open ask's message
        is redrawn when the accepted rev is the current one."""
        item = self.store.get_item(item_id)
        if item is None:
            raise AmendError(404, {"error": "not found"})
        history = self.store.revisions(item_id)
        if not history:
            raise AmendError(409, {"error": "stale revision"})
        current = history[-1]
        target = next((r for r in history if r.rev == rev), None)
        if (target is None or not hmac.compare_digest(str(hash).encode(), target.hash.encode())
                or rev < self.store.accepted_rev(item_id)):
            raise AmendError(409, {"error": "stale revision", "rev": current.rev, "hash": current.hash})
        self.store.accept_tx(item_id, rev, target.hash, auth)
        if item.status == "open" and rev == current.rev:
            self._show_accepted(item)
        return self.store.get_item(item_id)

    def _show_accepted(self, item: Item) -> None:
        """Redraw the ask's newest message (the Corrected one) as the accepted
        revision, with its keyboard bound to that rev. Best effort."""
        with self._ask_presentation(item.id):
            item = self.store.get_item(item.id)
            if item.status != "open":
                return
            chat_id, message_id, _ = self._current_message(item)
            if not message_id:
                return
            text, keyboard = self._render_open(item)
            try:
                self.telegram.edit_message_text(chat_id, message_id, text, reply_markup=keyboard)
            except TelegramError as exc:
                if "message is not modified" not in str(exc):
                    logger.warning("[mailbox] accept %s: edit failed: %s", item.id, type(exc).__name__)

    def problem(self, item_id: str, step: Any, rev: Any, hash: Any, note: Any, auth: str) -> Item:
        """The owner reports a problem with one step of the current or the accepted
        version: a problem/owner event {"id","step","note"} with its rev and auth.
        Raises AmendError: 404; 409 ask is closed; 409 stale revision (rev neither
        current nor accepted, or the hash isn't that rev's); 400 invalid problem
        (step unknown, the note's rules); 409 too many reports (20 per ask)."""
        item = self.store.get_item(item_id)
        if item is None:
            raise AmendError(404, {"error": "not found"})
        if item.status != "open":
            raise AmendError(409, {"error": "ask is closed"})
        history = self.store.revisions(item_id)
        current = history[-1] if history else None
        target = next((r for r in history if r.rev == rev), None) if type(rev) is int else None
        if (current is None or target is None or rev not in (current.rev, self.store.accepted_rev(item_id))
                or not hmac.compare_digest(str(hash).encode(), target.hash.encode())):
            stale: dict[str, Any] = {"error": "stale revision"}
            if current is not None:
                stale.update(rev=current.rev, hash=current.hash)
            raise AmendError(409, stale)
        body = json.loads(target.body)
        by_id = revisions.id_paths(body)
        by_path = {path: sid for sid, path in by_id.items()}
        step_id = None
        if isinstance(step, str):
            step_id = by_path.get(step) or (step if step in by_id else None)
        details = [] if step_id is not None else ["step: unknown"]
        details += revisions.check_note("note", note, revisions.MAX_PROBLEM_NOTE, multi=True)
        if details:
            raise AmendError(400, {"error": "invalid problem", "details": details})
        if sum(1 for e in self.store.events_for_item(item_id) if e.kind == "problem") >= revisions.MAX_PROBLEMS:
            raise AmendError(409, {"error": "too many reports"})
        text = json.dumps({"id": step_id, "step": by_id[step_id], "note": note},
                          ensure_ascii=False, separators=(",", ":"))
        self.store.append_event(item_id, "problem", "owner", text, rev=rev, auth=auth)
        return self.store.get_item(item_id)

    # --- the owner key (Pond 1.1.0) ------------------------------------------

    def _expired(self, k: OwnerKey) -> OwnerKey:
        """A pending request past its expiry becomes expired (applied on read)."""
        if k.status == "pending" and datetime.fromisoformat(k.expires) <= self._utc():
            return self.store.set_owner_key_status(k.id, "expired") or k
        return k

    def _edit_quietly(self, chat_id: Optional[int], message_id: Optional[int], text: str) -> None:
        """Edit a message and clear its keyboard; a failure is logged by type."""
        if not message_id:
            return
        try:
            self.telegram.edit_message_text(chat_id or self.telegram.owner_id, message_id, text,
                                            reply_markup={"inline_keyboard": []})
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                logger.warning("[mailbox] owner message edit failed: %s", type(exc).__name__)

    def _send_quietly(self, text: str, reply_markup: Optional[dict] = None) -> None:
        try:
            self.telegram.send_message(self.telegram.owner_id, text, reply_markup=reply_markup)
        except TelegramError as exc:
            logger.warning("[mailbox] owner message send failed: %s", type(exc).__name__)

    def enroll(self, key_hash: str, label: str) -> OwnerKey:
        """Ask the owner on Telegram to approve this Mac's key. Raises EnrollRefused
        (too many keys, already enrolled, invalid label). A second request while one
        is pending refuses both, so a look-alike request can't ride on the owner's
        tap: the new row comes back rejected. TelegramError propagates with nothing
        stored."""
        if owner.check_label(label):
            raise EnrollRefused("invalid label")
        with self._owner_lock:
            keys = [self._expired(k) for k in self.store.owner_keys()]
            if sum(1 for k in keys if k.status == "approved") >= owner.MAX_KEYS:
                raise EnrollRefused("too many keys")
            if any(k.key_hash == key_hash for k in keys):
                raise EnrollRefused("already enrolled")
            now = self._utc()
            enroll_id = str(uuid4())
            k = OwnerKey(
                id=enroll_id, key_hash=key_hash, label=label, status="pending",
                code=owner.enroll_code(enroll_id, key_hash), created=self._stamp(now),
                expires=self._stamp(now + timedelta(seconds=owner.ENROLL_TTL_S)),
            )
            pending = [p for p in keys if p.status == "pending"]
            if pending:
                k.status = "rejected"
                self.store.insert_enrollment(k)
                for p in pending:
                    self.store.set_owner_key_status(p.id, "rejected")
                    self._edit_quietly(None, p.tg_message_id,
                                       f"🚫 Refused: two devices asked at once (Hook on {html.escape(p.label)}).")
                self._send_quietly("Two devices asked to enroll at once, so both were refused. Start again on one device.")
                return k
            text = "\n".join((
                f"🔑 <b>Hook on {html.escape(label)}</b> asks to act as you.",
                f"Code: <b>{k.code}</b>",
                "Approve only if Hook shows this same code and you just pressed Set Up.",
            ))
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"e:{k.id}:y"},
                {"text": "🚫 Reject", "callback_data": f"e:{k.id}:n"},
            ]]}
            response = self.telegram.send_message(self.telegram.owner_id, text, reply_markup=keyboard)
            k.tg_message_id = response.get("message_id")
            self.store.insert_enrollment(k)
            return k

    def enrollment(self, enroll_id: str) -> Optional[OwnerKey]:
        """One enrollment by id, with expiry applied."""
        k = self.store.get_owner_key(enroll_id)
        return self._expired(k) if k is not None else None

    def owner_keys(self) -> list[OwnerKey]:
        """Every key, with expiry applied."""
        return [self._expired(k) for k in self.store.owner_keys()]

    def owner_for(self, secret: Optional[str]) -> Optional[OwnerKey]:
        """The approved key this secret belongs to, else None."""
        if not secret:
            return None
        k = self.store.owner_key_by_hash(owner.key_hash(secret))
        if k is None or k.status != "approved":
            return None
        self._stamp_used(k)
        return k

    def _stamp_used(self, k: OwnerKey) -> None:
        """Record that this key authenticated a request now (Pond 1.11.0), at most once
        an hour per key: the write is skipped while last_used is under an hour old."""
        now = self._utc()
        if k.last_used is not None:
            try:
                if now - datetime.fromisoformat(k.last_used) < timedelta(seconds=owner.LAST_USED_EVERY_S):
                    return
            except ValueError:
                pass                                    # an unreadable stamp is replaced
        stamp = self._stamp(now)
        self.store.stamp_owner_key_used(k.id, stamp,
                                        self._stamp(now - timedelta(seconds=owner.LAST_USED_EVERY_S - 1)))
        k.last_used = stamp

    def owner_key_required(self) -> bool:
        return self.store.get_setting(owner.SETTING_REQUIRED) == "1"

    def request_enforcement(self, on: bool, by: OwnerKey) -> str:
        """Turn the requirement on at once ("on"), or ask the owner on Telegram to
        turn it off ("pending"; the setting changes only on the matching tap). Off
        while already off → "off". Turning off never happens with the token alone."""
        label = html.escape(by.label)
        with self._owner_lock:
            if on:
                self.store.set_setting(owner.SETTING_REQUIRED, "1")
                self.store.delete_setting(owner.SETTING_OFF_REQUEST)
                self.store.delete_setting(owner.SETTING_OFF_EXPIRES)
                self._send_quietly(f"🔒 Owner key now required (turned on from Hook on {label}).")
                return "on"
            if not self.owner_key_required():
                return "off"
            request = str(uuid4())
            expires = self._stamp(self._utc() + timedelta(seconds=owner.OFF_TTL_S))
            text = "\n".join((
                "🔓 <b>Turn off the owner key?</b>",
                f"Asked from Hook on {label}. Without it, the Pond token alone can act as you.",
                "Tap only if you just asked for this in Hook.",
            ))
            keyboard = {"inline_keyboard": [[{"text": "Turn off", "callback_data": f"k:{request}:off"}]]}
            # Sent before it is stored: a refused send leaves no request a tap could match.
            self.telegram.send_message(self.telegram.owner_id, text, reply_markup=keyboard)
            self.store.set_setting(owner.SETTING_OFF_REQUEST, request)
            self.store.set_setting(owner.SETTING_OFF_EXPIRES, expires)
            return "pending"

    def revoke(self, key_id: str, by: OwnerKey) -> OwnerKey:
        """Revoke a key (ValueError when unknown) and tell the owner on Telegram."""
        k = self.store.get_owner_key(key_id)
        if k is None:
            raise ValueError(f"owner key {key_id} not found")
        revoked = self.store.set_owner_key_status(key_id, "revoked")
        self._send_quietly(f"🔑 Hook on {html.escape(k.label)} can no longer act as you "
                           f"(revoked from Hook on {html.escape(by.label)}).")
        return revoked

    # --- revoke requests (Pond 1.11.0) ----------------------------------------

    def _set_up_date(self, k: OwnerKey) -> str:
        """The day a key was set up, in the household zone: "4 Nov 2026"."""
        try:
            day = datetime.fromisoformat(k.created).astimezone(self.now().tzinfo)
        except ValueError:
            return k.created[:10]
        return f"{day.day} {day:%b %Y}"

    def _revoke_expired(self, r: RevokeRequest) -> RevokeRequest:
        """A pending request past its expiry becomes expired (applied on read)."""
        if r.status == "pending" and datetime.fromisoformat(r.expires) <= self._utc():
            k = self.store.get_owner_key(r.key_id)
            self._edit_quietly(None, r.tg_message_id,
                               f"⌛ Expired: revoke Hook on {html.escape(k.label if k else '?')}.")
            return self.store.set_revoke_request_status(r.id, "expired") or r
        return r

    def request_revoke(self, key_id: str) -> RevokeRequest:
        """Ask the owner on Telegram to revoke an approved key, with the Pond token
        alone: nothing changes until the owner taps Revoke after comparing the code.
        Raises RevokeRefused (404 unknown key; 409 already revoked, not approved, or
        another request still pending). TelegramError propagates with nothing stored."""
        with self._owner_lock:
            k = self.store.get_owner_key(key_id)
            if k is None:
                raise RevokeRefused(404, "not found")
            k = self._expired(k)
            if k.status == "revoked":
                raise RevokeRefused(409, "already revoked")
            if k.status != "approved":
                raise RevokeRefused(409, "not approved")
            if [r for r in self.store.pending_revoke_requests() if self._revoke_expired(r).status == "pending"]:
                raise RevokeRefused(409, "revoke request pending")
            now = self._utc()
            request_id = str(uuid4())
            r = RevokeRequest(
                id=request_id, key_id=k.id, status="pending", code=owner.revoke_code(request_id, k.id),
                created=self._stamp(now), expires=self._stamp(now + timedelta(seconds=owner.REVOKE_TTL_S)),
            )
            text = "\n".join((
                f"🔑 <b>Revoke Hook on {html.escape(k.label)}</b> (set up {self._set_up_date(k)})?",
                f"Code: <b>{r.code}</b>",
                "Revoke only if Hook shows this same code and you just asked for this.",
            ))
            keyboard = {"inline_keyboard": [[
                {"text": "Revoke", "callback_data": f"v:{r.id}:y"},
                {"text": "Keep", "callback_data": f"v:{r.id}:n"},
            ]]}
            # Sent before it is stored: a refused send leaves no request a tap could match.
            response = self.telegram.send_message(self.telegram.owner_id, text, reply_markup=keyboard)
            r.tg_message_id = response.get("message_id")
            self.store.insert_revoke_request(r)
            return r

    def revoke_request(self, request_id: str) -> Optional[RevokeRequest]:
        """One revoke request by id, with expiry applied."""
        with self._owner_lock:
            r = self.store.get_revoke_request(request_id)
            return self._revoke_expired(r) if r is not None else None

    def _revoke_tap(self, rest: str) -> str:
        """v:<id>:y (Revoke) or v:<id>:n (Keep) → the toast. Only a pending, unexpired
        request moves; Revoke revokes the key and sends the usual line."""
        request_id, _, verdict = rest.partition(":")
        with self._owner_lock:
            r = self.store.get_revoke_request(request_id) if request_id and verdict in ("y", "n") else None
            if r is None:
                return "Unknown request"
            k = self.store.get_owner_key(r.key_id)
            label = html.escape(k.label if k else "?")
            if r.status == "pending" and self._revoke_expired(r).status == "expired":
                return "Expired: ask again in Hook"
            if r.status != "pending":
                return "Already handled"
            if verdict == "n":
                self.store.set_revoke_request_status(r.id, "rejected")
                self._edit_quietly(None, r.tg_message_id, f"Kept: Hook on {label}.")
                return "Kept"
            self.store.set_revoke_request_status(r.id, "approved")
            if k is None or k.status != "approved":
                # Revoked meanwhile (from a Mac, or never approved): nothing left to revoke.
                self._edit_quietly(None, r.tg_message_id, f"Already revoked: Hook on {label}.")
                return "Already revoked"
            self.store.set_owner_key_status(k.id, "revoked")
            self._edit_quietly(None, r.tg_message_id, f"✅ Revoked: Hook on {label}.")
        self._send_quietly(f"🔑 Hook on {label} can no longer act as you (revoked on Telegram).")
        return "Revoked"

    def _enroll_tap(self, rest: str) -> str:
        """e:<id>:y or e:<id>:n → the toast. Only a pending, unexpired request moves."""
        enroll_id, _, verdict = rest.partition(":")
        with self._owner_lock:
            k = self.store.get_owner_key(enroll_id) if enroll_id and verdict in ("y", "n") else None
            if k is None:
                return "Unknown request"
            if k.status == "pending" and datetime.fromisoformat(k.expires) <= self._utc():
                self.store.set_owner_key_status(k.id, "expired")
                self._edit_quietly(None, k.tg_message_id, f"⌛ Expired: Hook on {html.escape(k.label)}.")
                return "Expired: start again in Hook"
            if k.status != "pending":
                return "Already handled"
            if verdict == "n":
                self.store.set_owner_key_status(k.id, "rejected")
                self._edit_quietly(None, k.tg_message_id, f"🚫 Rejected: Hook on {html.escape(k.label)}.")
                return "Rejected"
            if sum(1 for o in self.store.owner_keys() if o.status == "approved") >= owner.MAX_KEYS:
                self.store.set_owner_key_status(k.id, "rejected")
                self._edit_quietly(None, k.tg_message_id, f"🚫 Refused: {owner.MAX_KEYS} devices can already act as you.")
                return "Too many keys"
            self.store.set_owner_key_status(k.id, "approved")
            self._edit_quietly(None, k.tg_message_id, f"✅ Hook on {html.escape(k.label)} can now act as you.")
            return "Approved"

    def _enforce_tap(self, rest: str, message: Optional[dict[str, Any]] = None) -> str:
        """k:<uuid>:off → the toast. Acts only on the one stored, unexpired request;
        any other tap (an old message, a replaced or expired request) changes nothing."""
        request, _, verb = rest.partition(":")
        stale = "This request expired. Ask again from Hook."
        with self._owner_lock:
            stored = self.store.get_setting(owner.SETTING_OFF_REQUEST)
            expires = self.store.get_setting(owner.SETTING_OFF_EXPIRES)
            if (verb != "off" or not stored or not expires
                    or not hmac.compare_digest(request.encode(), stored.encode())
                    or datetime.fromisoformat(expires) <= self._utc()):
                return stale
            self.store.set_setting(owner.SETTING_REQUIRED, "0")
            self.store.delete_setting(owner.SETTING_OFF_REQUEST)
            self.store.delete_setting(owner.SETTING_OFF_EXPIRES)
        message = message or {}
        self._edit_quietly((message.get("chat") or {}).get("id"), message.get("message_id"),
                           "Owner key no longer required.")
        return "Owner key no longer required"

    def _capture(self, msg: dict[str, Any], text: str) -> None:
        """A plain owner text becomes a Central Time item and is answered under
        the message. Nothing of the text reaches a log line."""
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        forwarded = any(msg.get(k) for k in _FORWARD_KEYS)
        if (
            self.ct_client is None
            or text.startswith("/")
            or chat_id != self.telegram.owner_id
            or chat.get("type", "private") != "private"
            or forwarded
        ):
            self.ignored_messages += 1
            return
        message_id = msg.get("message_id")
        if looks_secret(text):
            self._reply(chat_id, message_id, "Not saved: that looks like a secret.")
            return
        now = self.now()
        cap = capture.parse(text, now)
        if not cap.title:
            # Hidden characters only: nothing to save (Central Time would 400 on an empty title).
            self.ignored_messages += 1
            return
        try:
            self.ct_client.create_item(cap.title, cap.date, cap.time)
        except CentralTimeError as exc:
            logger.warning("[mailbox] capture failed: %s %s", exc.what, exc.status)
            if 400 <= exc.status <= 499:
                self._reply(chat_id, message_id, "Not saved: Central Time refused it.")
            else:
                self._reply(chat_id, message_id, "Not saved: Central Time did not answer. Send it again in a minute.")
            return
        logger.info("[mailbox] capture: item created")
        self._reply(
            chat_id, message_id,
            f"Added: <b>{html.escape(cap.title)}</b> \u00b7 {capture.describe(cap, now.date())}",
        )

    def _reply(self, chat_id: int, message_id: Optional[int], text: str) -> None:
        """Answer under the owner's message; a send failure is logged by type."""
        try:
            if message_id:
                self.telegram.send_reply(chat_id, message_id, text)
            else:
                self.telegram.send_message(chat_id, text)
        except TelegramError as exc:
            logger.warning("[mailbox] capture reply failed: %s", type(exc).__name__)

    # --- notifications (reminders and the brief) ---------------------------

    def _ref_lock_acquire(self, ref: str) -> threading.Lock:
        with self._notify_lock:
            lock, count = self._ref_locks.get(ref) or (threading.Lock(), 0)
            self._ref_locks[ref] = (lock, count + 1)
        lock.acquire()
        return lock

    def _ref_lock_release(self, ref: str, lock: threading.Lock) -> None:
        lock.release()
        with self._notify_lock:
            _, count = self._ref_locks[ref]
            if count <= 1:
                del self._ref_locks[ref]
            else:
                self._ref_locks[ref] = (lock, count - 1)

    def notify(
        self, kind: str, ref: str, title: str, body: Optional[str] = None,
        actions: Optional[list[dict[str, str]]] = None,
    ) -> tuple[Notification, bool]:
        """Send a reminder or the brief to the owner, once per ref. On a repeat
        the stored notification comes back with deduped True and nothing is
        sent. A brief carries the count of open asks (the same 14-day window
        as GET /asks). The ref is claimed under a per-ref lock before the
        send and stored (the persisted claim, unique in the table) only after
        Telegram accepted: a TelegramError propagates with nothing stored, so
        the caller's retry sends again, and a 400 upstream of here never
        touches the ref at all."""
        open_asks = len(self.list_open()) if kind == "brief" and self.brief_asks else 0
        return self._send_once(ref, self.telegram.owner_id, kind, title, body, actions, open_asks)

    def _send_once(
        self, ref: str, chat_id: int, kind: str, title: str, body: Optional[str],
        actions: Optional[list[dict[str, str]]], open_asks: int = 0,
    ) -> tuple[Notification, bool]:
        """One notification per ref to `chat_id`: the per-ref lock, the dedupe read, the send and the
        store-on-success insert shared by the asks instance (notify) and the household one (notify_member)."""
        actions = list(actions or [])
        lock = self._ref_lock_acquire(ref)
        try:
            existing = self.store.get_notification_by_ref(ref)
            if existing is not None:
                return existing, True
            nid = str(uuid4())
            text = TelegramClient.render_notification(kind, title, body, open_asks=open_asks)
            keyboard = TelegramClient.notification_keyboard(nid, actions) if actions else None
            response = self.telegram.send_message(chat_id, text, reply_markup=keyboard)
            notification = Notification(
                id=nid, kind=kind, ref=ref, title=title, body=body, actions=json.dumps(actions),
                tg_chat_id=chat_id, tg_message_id=response.get("message_id"),
                action_taken=None, acted_at=None,
                created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            try:
                self.store.insert_notification(notification)
            except sqlite3.IntegrityError:
                # Cannot happen under the lock; kept as the last guard on the unique ref.
                return self.store.get_notification_by_ref(ref), True
            return notification, False
        finally:
            self._ref_lock_release(ref, lock)

    def _notification_tap(self, rest: str) -> str:
        """A tap on a reminder button: the toast text. Central Time decides;
        the message is edited only after it answered. No title reaches a log."""
        nid, _, action = rest.partition(":")
        notification = self.store.get_notification(nid) if nid and action in ACTION_IDS else None
        if notification is None or action not in {a.get("id") for a in json.loads(notification.actions)}:
            return "Unknown reminder"
        if notification.action_taken:
            return "Already handled"
        if self.ct_client is None:
            return "Central Time is not configured"
        try:
            result = self.ct_client.ack_fire(notification.ref, action)
        except CentralTimeError as exc:
            # The reason is one of ACK_REASONS or None: a fixed word, never response text.
            logger.warning("[mailbox] reminder ack failed: %s %s %s", exc.what, exc.status, exc.reason)
            ended = _ACK_ENDED.get((exc.status, exc.reason if exc.status == 409 else None))
            if ended is None:
                if exc.status == 409:
                    return _ACK_REFUSED.get(exc.reason, "Not possible now")
                if exc.status in (401, 403):
                    # The capture token is unknown or lacks reminders:ack: a retry cannot
                    # succeed, so say so instead of promising one. The keyboard stays.
                    return "Central Time refused the token"
                return "Central Time did not answer; try again"
            outcome, answer = ended
            self._end_notification(notification, outcome)
            return answer
        if result.get("applied") is False:
            # Already acked in Central Time (its row outlived a mailbox restart):
            # close the message with the action it holds.
            held = (result.get("fire") or {}).get("ack")
            self._end_notification(notification, held if held in ACTION_IDS else action)
            return "Already handled in Central Time"
        self._end_notification(notification, action)
        return _TAP_ANSWERS[action]

    def _end_notification(self, notification: Notification, outcome: str) -> None:
        """Record the outcome and strike the message through. The edit is best
        effort: the outcome is already in Central Time, and a failure is logged by type."""
        self.store.set_notification_action(notification.id, outcome)
        if not notification.tg_message_id:
            return
        try:
            self.telegram.edit_message_text(
                notification.tg_chat_id, notification.tg_message_id,
                TelegramClient.render_notification_done(notification.title, outcome),
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError as exc:
            logger.warning("[mailbox] reminder edit failed: %s", type(exc).__name__)
