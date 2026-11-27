"""HTTP server for the mailbox API, plus the poll and backup threads."""
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from mailbox import PROTOCOL, VERSION, owner
from mailbox import link_page, maintenance
from mailbox.admission import AdmissionError
from mailbox.ask_about import Answerer
from mailbox.clock import describe_zone, household_zone
from mailbox.ct_client import CentralTimeClient
from mailbox.household import PRUNE_DAYS, notify_member
from mailbox.instructable import no_duplicates
from mailbox.instructable import validate as validate_instructable
from mailbox.instructable import validate_project
from mailbox.notification import validate as validate_notification
from mailbox.requirements import wire as requirements_wire
from mailbox.service import (
    AmendError, AskClosed, Corrected, DocsRefused, EnrollRefused, InvalidAnswers, InvalidProgress, RequirementsUnmet,
    RevokeRefused, Service, WaitingForSteps,
    labels_payload,
)
from mailbox.store import Store
from mailbox.telegram import DisabledTelegram, TelegramClient, TelegramError


logger = logging.getLogger("mailbox")

# A poll is considered healthy if getUpdates last succeeded within this window.
POLL_HEALTH_WINDOW_S = 120
# Nightly snapshot, in the container's LOCAL time (TZ env) — Ruling S3.
BACKUP_HOUR = 3
BACKUP_MINUTE = 20
# How many nightly snapshots to keep.
BACKUP_KEEP = 30
# Bounds on the numeric query parameters, so a silly value is a 400 and not a
# crashed request thread.
MAX_DAYS = 36500
MAX_LIMIT = 1000
# Largest request body we will read. Only the token holder can post at all,
# but a claimed Content-Length must never become an unbounded read.
MAX_BODY = 1 << 20
# What a client sends in X-Hook-Protocol: 5 and up read revision histories.
PROTOCOL_HEADER = "X-Hook-Protocol"
REVISIONS_FROM = 5
# Pond 1.4.0: a client from protocol 6 on reads choice objects; an older one gets labels.
CHOICES_FROM = 6
EDIT_KEY_HASH = re.compile(r"[0-9a-f]{64}")
# A Hook bug's signature (Pond 1.2.0): 12 lowercase hex.
BUG_SIGNATURE = re.compile(r"[0-9a-f]{12}")
# POST /asks `kind`, and the GET /asks filter.
ASK_KINDS = ("ask", "bug")


class _BodyTooLarge(Exception):
    """The request claims a body bigger than MAX_BODY."""


class MailboxHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for the mailbox API."""

    server_version = "xo-mailbox"
    sys_version = ""
    # Set by send_json, so the blanket 500 never writes a second response.
    _responded = False
    principal = None

    # --- routing ---

    def do_GET(self) -> None:
        """GET /health, /version, /asks, /asks/{id}, /asks/{id}/docs/{sha256}, /events."""
        self._responded = False
        try:
            self._route_get()
        except Exception as exc:                       # noqa: BLE001 — see _fail
            self._fail(exc)

    def do_POST(self) -> None:
        """POST /asks, /asks/{id}/ack, /asks/{id}/steps, /asks/{id}/resolve,
        /asks/{id}/reply, /asks/{id}/reopen, /asks/{id}/answers, /asks/{id}/docs/release,
        /asks/{id}/ask-about, /notify."""
        self._responded = False
        try:
            self._route_post()
        except Exception as exc:                       # noqa: BLE001 — see _fail
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        """Answer anything unforeseen with a 500 instead of dropping the
        connection (Ruling S39). Only the exception TYPE is logged: a message
        can carry request data, and the request line can carry a secret."""
        if isinstance(exc, AdmissionError):
            self.send_json({"error": str(exc)}, exc.status)
            return
        logger.error("[mailbox] request failed: %s", type(exc).__name__)
        if not self._responded:
            try:
                self.send_json({"error": "internal error"}, 500)
            except OSError:
                pass                                   # the client already left

    def _route_get(self) -> None:
        """Dispatch a GET."""
        if self.server.fenced and self.path not in ("/health", "/version"):
            return self.send_json({"error": "maintenance"}, 503)
        parts = urlparse(self.path).path.split("/")
        ticket = (self.server.installation and parts == ["", "version"]
                  and maintenance.consume_ticket(self.client_address[0], self._bearer()))
        if ticket:
            return self.do_version(local_probe=True)
        if self.server.fenced and parts == ["", "version"]:
            return self.send_json({"error": "maintenance"}, 503)
        self._authorize_path(self.command, parts)

        if parts[:2] == ["", "admission"]:
            self.admission_get(parts[2:])
        elif self.path.startswith("/i/"):
            self.do_link_page()
        elif parts == ["", "health"]:
            self.do_health()
        elif parts == ["", "version"]:
            self.do_version()
        elif parts == ["", "asks"]:
            self.do_list_asks()
        elif len(parts) == 3 and parts[0] == "" and parts[1] == "asks" and parts[2]:
            self.do_get_ask(parts[2])
        elif len(parts) == 5 and parts[0] == "" and parts[1] == "asks" and parts[2] and parts[3] == "docs":
            self.do_get_doc(parts[2], parts[4])
        elif parts == ["", "events"]:
            self.do_get_events()
        elif parts == ["", "owner"]:
            self.do_get_owner()
        elif len(parts) == 4 and parts[0] == "" and parts[1] == "owner" and parts[2] == "enroll" and parts[3]:
            self.do_get_enrollment(parts[3])
        elif len(parts) == 4 and parts[0] == "" and parts[1] == "owner" and parts[2] == "revoke-request" and parts[3]:
            self.do_get_revoke_request(parts[3])
        else:
            self.send_json({"error": "not found"}, 404)

    def do_link_page(self) -> None:
        page = link_page.render(self.path)
        if page is None:
            self.send_json({"error": "not found"}, 404)
            return
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Content-Security-Policy", link_page.CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)

    def _route_post(self) -> None:
        """Dispatch a POST."""
        if self.server.fenced:
            return self.send_json({"error": "maintenance"}, 503)
        parts = urlparse(self.path).path.split("/")
        self._authorize_path(self.command, parts)

        if parts[:2] == ["", "admission"]:
            self.admission_post(parts[2:])
        elif parts == ["", "asks"]:
            self.do_post_ask()
        elif parts == ["", "notify"]:
            self.do_notify()
        elif (
            len(parts) == 4
            and parts[0] == ""
            and parts[1] == "asks"
            and parts[2]
            and parts[3] in ("ack", "steps", "resolve", "reply", "reopen", "answers", "amend", "append", "accept",
                             "problems", "progress", "ask-about")
        ):
            # Exact split, never a substring check: /asks/xack is not /asks/x/ack.
            handler = {
                "ack": self.do_ack,
                "steps": self.do_steps,
                "resolve": self.do_resolve,
                "reply": self.do_reply,
                "reopen": self.do_reopen,
                "answers": self.do_answers,
                "amend": self.do_amend,
                "append": self.do_append,
                "accept": self.do_accept,
                "problems": self.do_problems,
                "progress": self.do_progress,
                "ask-about": self.do_ask_about,
            }
            handler[parts[3]](parts[2])
        elif (len(parts) == 5 and parts[0] == "" and parts[1] == "asks" and parts[2] and parts[3] == "docs"
              and parts[4] == "release"):
            self.do_release_docs(parts[2])
        elif len(parts) == 3 and parts[0] == "" and parts[1] == "owner" and parts[2] in ("enroll", "require", "revoke",
                                                                                      "revoke-request"):
            {
                "enroll": self.do_enroll,
                "require": self.do_require,
                "revoke": self.do_revoke,
                "revoke-request": self.do_revoke_request,
            }[parts[2]]()
        else:
            self.send_json({"error": "not found"}, 404)

    # --- helpers ---

    def _authorize_path(self, method, parts):
        """Resolve native credentials once per request and deny unclassified routes."""
        self.principal = None
        service = self.server.service
        if service.admission.mode == "legacy":
            return
        route = parts[1:]
        if method == "GET" and (route == ["health"] or route[:1] == ["i"]):
            return
        if route[:1] == ["admission"]:
            return  # each explicit enrollment route verifies its own proof in its write transaction
        if method == "POST" and route == ["notify"] and self.check_notify_auth():
            return
        self.principal = service.admission.authenticate(self._bearer())
        is_agent = self.principal["kind"] == "agent"
        if route == ["notify"] or route[:1] == ["owner"]:
            raise AdmissionError(403, "credential unavailable for this route")
        if method == "GET" and route in (["version"], ["events"]):
            return
        if route == ["asks"]:
            return
        if len(route) < 2 or route[0] != "asks":
            raise AdmissionError(404, "not found")
        if is_agent and not service.store.owns_ask(self.principal["id"], route[1]):
            raise AdmissionError(404, "not found")
        suffix = route[2:]
        if method == "GET" and (not suffix or len(suffix) == 2 and suffix[0] == "docs"):
            return
        if method == "POST":
            if suffix == ["docs", "release"] or (len(suffix) == 1 and
                    suffix[0] in ("ack", "steps", "resolve", "amend", "append")):
                return
            if len(suffix) == 1 and suffix[0] in (
                    "reply", "answers", "ask-about", "progress", "accept", "problems", "reopen"):
                if is_agent:
                    raise AdmissionError(403, "approved device required")
                return
        raise AdmissionError(404, "not found")

    @property
    def agent_id(self):
        if self.principal and self.principal["kind"] == "agent":
            return self.principal["id"]
        return None

    def _actor(self, legacy_actor):
        if self.principal:
            return "agent" if self.agent_id else "owner"
        return legacy_actor

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else None

    def admission_get(self, parts):
        a = self.server.service.admission
        if parts == ["me"]:
            result = a.me(self._bearer())
        elif parts == ["principals"]:
            result = a.principals(self._bearer())
        elif parts == ["pending"]:
            result = {"pending": a.pending(self._bearer())}
        else:
            return self.send_json({"error": "not found"}, 404)
        self.send_json(result, 200)

    def admission_post(self, parts):
        a = self.server.service.admission
        if parts not in (["claim"], ["invites"]) and not (
            len(parts) == 3 and (parts[0], parts[2]) in (("requests", "decision"), ("principals", "revoke"))
        ):
            return self.send_json({"error": "not found"}, 404)
        if parts == ["claim"]:
            self.server.limit_claims(self.client_address[0])
        body = self._json_body()
        if body is None:
            return
        if parts == ["claim"]:
            result = a.claim(body.get("invite"), body.get("keyHash"), body.get("kind"), body.get("label"))
        elif parts == ["invites"]:
            result = a.invite(self._bearer(), body.get("kind"))
        elif parts[0] == "requests":
            result = a.decide(self._bearer(), parts[1], body.get("decision"), body.get("code"))
        else:
            result = a.revoke(self._bearer(), parts[1])
        self.send_json(result, 201 if len(parts) == 1 else 200)

    def check_auth(self) -> bool:
        """Check the Bearer token in constant time.

        The comparison is on BYTES: http.client decodes headers as latin-1, and
        compare_digest raises TypeError on a str holding a non-ASCII character
        (Ruling S39) — a garbled header must be a 401, not a dead connection.
        """
        if self.principal is not None:
            return True
        auth = self.headers.get("Authorization", "")
        if not self.server.mailbox_token or not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(
            auth[7:].encode("latin-1", "replace"), self.server.mailbox_token.encode()
        )

    def check_notify_auth(self) -> bool:
        """The notify token (MAILBOX_NOTIFY_TOKEN), valid for POST /notify only;
        constant time, on bytes, as check_auth. Unset: nothing matches."""
        token = self.server.notify_token
        if not token:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(auth[7:].encode("latin-1", "replace"), token.encode())

    def _read_json(self) -> dict[str, Any]:
        """Body as a dict; an absent, empty or negative-length body is {}
        (ack sends none — and rfile.read(-1) would block until the client
        disconnects). Over MAX_BODY raises _BodyTooLarge without reading."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise _BodyTooLarge(length)
        raw = self.rfile.read(length)
        # A key given twice is refused (ValueError → 400 invalid json): the Pond and
        # Hook must never read different values from one body.
        body = json.loads(raw, object_pairs_hook=no_duplicates)
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        # A lone "\ud800" escape parses but can never be sent back as UTF-8:
        # refuse it here, before it is stored and poisons every later read.
        # (UnicodeEncodeError is a ValueError, so this answers 400 invalid json.)
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        return body

    def _json_body(self) -> Optional[dict[str, Any]]:
        """The request body, or None once the error response has been sent."""
        try:
            return self._read_json()
        except _BodyTooLarge:
            self.send_json({"error": "body too large"}, 413)
        except (json.JSONDecodeError, ValueError):
            self.send_json({"error": "invalid json"}, 400)
        return None

    def _owner_secret(self) -> Optional[str]:
        """The X-Hook-Owner-Key header, or None when absent or empty."""
        return self.headers.get(owner.KEY_HEADER) or None

    def _owner(self, always: bool) -> Optional[str]:
        """The owner check for an owner act: "owner-key" for an approved key;
        "token" when no key was sent, `always` is false and enforcement is off.
        Otherwise a 403 is sent ("owner key required" without a key, "unknown
        owner key" for a bad, pending or revoked one) and None comes back."""
        service = self.server.service
        if self.principal:
            if self.principal["kind"] == "device":
                return "owner-key"
            self.send_json({"error": "approved device required"}, 403)
            return None
        secret = self._owner_secret()
        if secret is None:
            if not always and not service.owner_key_required():
                return "token"
            self.send_json({"error": "owner key required"}, 403)
            return None
        if service.owner_for(secret) is None:
            self.send_json({"error": "unknown owner key"}, 403)
            return None
        return "owner-key"

    def _rev_hash(self, body: dict[str, Any]) -> Optional[tuple[Optional[int], Optional[str]]]:
        """The optional (rev, hash) an owner act is bound to; None once a 400 is sent."""
        rev, digest = body.get("rev"), body.get("hash")
        if (rev is not None and type(rev) is not int) or (digest is not None and not isinstance(digest, str)):
            self.send_json({"error": "invalid revision"}, 400)
            return None
        return rev, digest

    def _query(self) -> dict[str, list[str]]:
        """Parsed query string."""
        return parse_qs(urlparse(self.path).query)

    @staticmethod
    def _event_wire(item_id: str, seq: int, kind: str, actor: str, text: Optional[str], at: str, *,
                    rev: Optional[int] = None, auth: Optional[str] = None) -> dict[str, Any]:
        """One event as JSON — camelCase like the item envelope (Ruling S16). `rev`
        and `auth` are null when the event has none."""
        return {"seq": seq, "itemId": item_id, "kind": kind, "actor": actor, "text": text, "at": at,
                "rev": rev, "auth": auth}

    def _client_protocol(self) -> int:
        """The X-Hook-Protocol the client sent, 0..99; 0 when absent or unreadable."""
        try:
            value = int(self.headers.get(PROTOCOL_HEADER, "0"))
        except ValueError:
            return 0
        return min(max(value, 0), 99)

    def _wire(self, item, *, with_history: bool = False, requirements: bool = False) -> dict[str, Any]:
        """`requirements` only on answers that carry one item, never on GET /asks (RD 15)."""
        protocol = self._client_protocol()
        wire = self.server.service.wire_item(
            item, revisions=protocol >= REVISIONS_FROM, with_history=with_history,
            requirements=requirements, choice_objects=protocol >= CHOICES_FROM)

        return self._visible_item(wire)

    def _visible_item(self, wire):
        if self.agent_id:
            wire["payload"].pop("tg_chat_id", None)
            wire["payload"].pop("tg_message_id", None)
        return wire

    def _plain_wire(self, item) -> dict[str, Any]:
        """Item.to_wire() for the answers that keep its shape (ack, steps, reply), with
        choice objects as labels for a client before protocol 6."""
        wire = self._visible_item(item.to_wire())
        return wire if self._client_protocol() >= CHOICES_FROM else labels_payload(wire)

    # --- routes ---

    def do_health(self) -> None:
        """GET /health — no auth."""
        db_ok = self.server.service.store.health_check()
        managed = {"installation": self.server.installation} if self.server.installation else {}
        if self.server.fenced:
            return self.send_json({**managed, "ok": db_ok, "db_ok": db_ok, "fenced": True},
                                  200 if db_ok else 503)
        last_poll_age = None
        telegram_ok = None
        if self.server.service.telegram.enabled:
            last_poll_age = int(time.time() - self.server.last_poll_time)
            telegram_ok = last_poll_age < POLL_HEALTH_WINDOW_S
        ok = db_ok and telegram_ok is not False

        self.send_json({
            "ok": ok,
            **managed, "telegram_ok": telegram_ok,
            "db_ok": db_ok,
            "last_poll_age_s": last_poll_age,
        }, 200 if ok else 503)

    def do_version(self, *, local_probe=False) -> None:
        """GET /version — the Pond's semver release, the protocol integer that
        0.9.x Hooks and clients still compare, and the features list (1.8.0) that
        new clients gate on: FEATURES, plus "docs" while documents are on (1.10.0).
        No client reads `service`."""
        if not local_probe and not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        features = list(self.server.service.features())
        if self.server.installation:
            features.append("managedInstall")
        return self.send_json(
            {"service": "pond", "version": VERSION, "protocol": PROTOCOL, "features": features}, 200
        )

    def do_post_ask(self) -> None:
        """POST /asks — post an ask to Telegram. An optional `instructable`
        (Relay's v1 format) is validated first; `ask` then defaults to its
        title. The response carries `openUrl` when Relay is configured.

        With `kind: "bug"` and a `signature` (12 hex) it files a Hook bug instead
        (Pond 1.2.0): owner key always, an instructable required, never sent to
        Telegram, deduped by signature: 201 for a new bug, 200 {"deduped": true,
        "occurrences": n} when an open bug has the signature.

        An ask's optional `docs` (1.10.0) is the text of each `doc` in its instructable,
        by hash; Service.post checks it after every check here (DocsRefused: 409
        documents not enabled, 400 invalid documents, then from 1.12.0 409 document
        store full when its new texts would take the stored total over the limit). A bug takes no documents: a
        `doc` or a `docs` member on one is refused the same way (Service.post_bug)."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        if self.server.service.members is not None:
            # A member never receives an owner ask: the household bot carries household records only.
            return self.send_json({"error": "household instance takes no asks"}, 403)

        body = self._json_body()
        if body is None:
            return

        kind = "ask" if body.get("kind") is None else body["kind"]
        if not isinstance(kind, str) or kind not in ASK_KINDS:
            return self.send_json({"error": "invalid kind"}, 400)
        if kind == "bug":
            return self._post_bug(body)

        edit_key_hash = body.get("editKeyHash")
        if edit_key_hash is not None and not (isinstance(edit_key_hash, str)
                                              and EDIT_KEY_HASH.fullmatch(edit_key_hash)):
            return self.send_json({"error": "invalid editKeyHash"}, 400)

        instructable = body.get("instructable")
        if instructable is not None:
            instructable, problems = validate_instructable(instructable)
            if problems:
                # Field paths only, never values: a pasted secret is not echoed.
                return self.send_json(
                    {"error": "invalid instructable", "details": problems}, 400
                )

        project = body.get("project")
        why = body.get("why")
        if instructable is not None:
            # The instructable is the whole ask: its title is the headline and
            # only its why is shown, so unvalidated body text never renders.
            ask, why = instructable["title"], instructable.get("why")
            project_problems = validate_project(project)
            if project_problems:
                return self.send_json(
                    {"error": "invalid project", "details": project_problems}, 400
                )
        else:
            ask = body.get("ask")

        if not project or not ask:
            return self.send_json({"error": "missing fields"}, 400)

        # An absent context can never match the daemon's, so it lands here too.
        if not self.principal and body.get("context") != self.server.service.context:
            return self.send_json({"error": "wrong context"}, 403)

        try:
            item, resp = self.server.service.post(
                project, ask,
                why=why,
                cmd=body.get("cmd"),
                link=body.get("link"),
                instructable=instructable,
                edit_key_hash=edit_key_hash,
                docs=body.get("docs"),
                principal_id=self.principal["id"] if self.principal else None,
                actor=self._actor("session"),
            )
        except DocsRefused as exc:
            # Field paths and hashes only, never a document's text.
            return self.send_json(exc.body, exc.status)
        except TelegramError as exc:
            logger.warning("[mailbox] POST /asks failed: %s: %s", type(exc).__name__, exc)
            # Upstream refused the send; the request itself was fine (S39).
            return self.send_json({"error": str(exc)}, 502)

        status = 200 if resp.get("deduped") else 201
        return self.send_json({"item": self._wire(item), **resp}, status)

    def _post_bug(self, body: dict[str, Any]) -> None:
        """The kind "bug" branch of POST /asks."""
        if self._owner(always=True) is None:
            return
        signature = body.get("signature")
        if not isinstance(signature, str) or not BUG_SIGNATURE.fullmatch(signature):
            return self.send_json({"error": "invalid signature"}, 400)
        instructable = body.get("instructable")
        project = body.get("project")
        if instructable is None or not project:
            return self.send_json({"error": "missing fields"}, 400)
        instructable, problems = validate_instructable(instructable)
        if problems:
            # Field paths only, never values: a pasted secret is not echoed.
            return self.send_json({"error": "invalid instructable", "details": problems}, 400)
        project_problems = validate_project(project)
        if project_problems:
            return self.send_json({"error": "invalid project", "details": project_problems}, 400)
        if not self.principal and body.get("context") != self.server.service.context:
            return self.send_json({"error": "wrong context"}, 403)
        try:
            item, resp = self.server.service.post_bug(project, instructable, signature, docs=body.get("docs"))
        except DocsRefused as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({"item": self._wire(item), **resp}, 200 if resp.get("deduped") else 201)

    def do_list_asks(self) -> None:
        """GET /asks — list asks, newest first. `status` (default open), `days`
        (default 14) and, from Pond 1.2.0, `kind` ("bug" or "ask"; absent: both)."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        query = self._query()
        status = query.get("status", ["open"])[0]
        try:
            days = int(query.get("days", ["14"])[0])
        except ValueError:
            return self.send_json({"error": "invalid query"}, 400)
        if not 0 <= days <= MAX_DAYS:
            # timedelta(days=10**20) raises OverflowError deeper in the store.
            return self.send_json({"error": "invalid query"}, 400)
        # parse_qs drops a blank value, so ?kind= arrives as no kind unless kept.
        kinds = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("kind")
        kind = kinds[0] if kinds else None
        if kind is not None and kind not in ASK_KINDS:
            return self.send_json({"error": "invalid query"}, 400)

        items = self.server.service.store.list_items(status=status, days=days, kind=kind, principal_id=self.agent_id)
        return self.send_json({"items": [self._wire(i) for i in items]}, 200)

    def do_get_ask(self, item_id: str) -> None:
        """GET /asks/{id} — fetch ask with events."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        item, events = self.server.service.get_item_with_events(item_id, for_agent=bool(self.agent_id))
        if not item:
            return self.send_json({"error": "not found"}, 404)

        wire = self._wire(item, with_history=True, requirements=True)
        detail = {"item": wire, "events": [self._event_wire(item.id, **e) for e in events]}
        if "revisions" in wire:
            detail["revisions"] = wire.pop("revisions")
        return self.send_json(detail, 200)

    def do_get_doc(self, item_id: str, sha256: str) -> None:
        """GET /asks/{id}/docs/{sha256} — the exact bytes of a document the ask keeps
        (Pond 1.10.0), as text/plain; the token alone, as for GET /asks/{id}. 404 for an
        unknown ask, a malformed hash or one the ask never linked (the path is never
        echoed), 410 document no longer kept once the ask released it."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        try:
            body = self.server.service.read_doc(item_id, sha256)
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_release_docs(self, item_id: str) -> None:
        """POST /asks/{id}/docs/release — release a closed ask's documents (Pond 1.10.0):
        the token alone, so an agent that took the work over can release them; the body
        is ignored, but a malformed one is still a 400. 200 {"released": n}, the rows this
        call released (0 on a second call or an ask without documents); 404; 409 ask is
        open, nothing deleted."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        if self._json_body() is None:
            return
        try:
            released = self.server.service.release_docs(item_id)
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({"released": released}, 200)

    def do_ack(self, item_id: str) -> None:
        """POST /asks/{id}/ack — acknowledge. The client sends no body."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        if self._json_body() is None:      # ack carries no fields, but a
            return                         # malformed body is still a 400

        try:
            item = self.server.service.ack(item_id, actor=self._actor("session"))
        except ValueError:
            return self.send_json({"error": "not found"}, 404)

        return self.send_json({"item": self._plain_wire(item)}, 200)

    def do_steps(self, item_id: str) -> None:
        """POST /asks/{id}/steps — post numbered steps."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        body = self._json_body()
        if body is None:
            return

        text = body.get("text")
        if not text:
            return self.send_json({"error": "missing text"}, 400)

        try:
            item = self.server.service.steps(item_id, text, actor=self._actor("session"))
        except AmendError as exc:
            # Field paths only, never values: a pasted secret is not echoed.
            return self.send_json(exc.body, exc.status)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)

        return self.send_json({"item": self._plain_wire(item)}, 200)

    def do_resolve(self, item_id: str) -> None:
        """POST /asks/{id}/resolve — resolve with verdict. `actor` says who
        decided: "session" (the default, as before) or "owner" (Relay's Done
        and Skip). One owner: whoever holds the token is trusted to say which."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        body = self._json_body()
        if body is None:
            return

        verdict = body.get("verdict")
        if verdict not in ("done", "declined"):
            return self.send_json({"error": "invalid verdict"}, 400)

        actor = self._actor(body.get("actor") or "session")
        if not self.principal and actor not in ("owner", "session"):
            return self.send_json({"error": "invalid actor"}, 400)

        auth = None
        if actor == "owner":
            auth = self._owner(always=False)
            if auth is None:
                return

        stamp = self._rev_hash(body)
        if stamp is None:
            return

        try:
            item = self.server.service.resolve(
                item_id, verdict, note=body.get("note"), actor=actor, rev=stamp[0], hash=stamp[1], auth=auth
            )
        except Corrected as exc:
            return self.send_json(exc.body(), 409)
        except RequirementsUnmet as exc:
            # Pond 1.3.0: {"error": "requirements_unmet", "left", "details"} (RD 3).
            return self.send_json(exc.body(), 409)
        except WaitingForSteps as exc:
            # Pond 1.9.0: {"error": "waiting_for_steps", "until", "path"}.
            return self.send_json(exc.body(), 409)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)

        return self.send_json({"item": self._wire(item, requirements=True)}, 200)

    def do_reply(self, item_id: str) -> None:
        """POST /asks/{id}/reply {"text", "kind"?} — the owner's answer given in
        Relay, recorded as a `reply` event by the owner. With kind "message"
        (Pond 1.2.0) it is a message to the posting session instead: a `message`
        event, 400 invalid message, 409 ask is closed, 409 too many messages.
        Edits nothing on Telegram."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        body = self._json_body()
        if body is None:
            return

        kind = "reply" if body.get("kind") is None else body["kind"]
        if not isinstance(kind, str) or kind not in self.server.service.REPLY_KINDS:
            return self.send_json({"error": "invalid kind"}, 400)

        auth = self._owner(always=False)
        if auth is None:
            return

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return self.send_json({"error": "missing text"}, 400)

        try:
            item = self.server.service.reply(item_id, text, auth=auth, kind=kind)
        except AmendError as exc:
            # Field paths only, never values: a pasted secret is not echoed.
            return self.send_json(exc.body, exc.status)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)

        return self.send_json({"item": self._with_requirements(item, self._plain_wire(item))}, 200)

    def do_ask_about(self, item_id: str) -> None:
        """POST /asks/{id}/ask-about {"question"} (Pond 1.14.0, feature `askAbout`) — the
        owner's question about an ask, answered by a small model from the ask and its
        brief. Auth and the owner key as for an owner message. 404 while the Pond has no
        Anthropic API key, as for an unknown route. → 200 {"answer", "events": [the
        question, the answer]}; 400 invalid json, missing question or invalid question
        (`details`), 404 not found, 409 ask is closed, 429 too many questions or daily
        limit reached (with `message`), 502 answer unavailable. The question is never
        echoed in an error."""
        service = self.server.service
        if not service.ask_about_enabled():             # before auth, exactly as an unknown route
            return self.send_json({"error": "not found"}, 404)
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        auth = self._owner(always=False)
        if auth is None:
            return
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            return self.send_json({"error": "missing question"}, 400)
        try:
            answer, events = service.ask_about(item_id, question, auth=auth)
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({"answer": answer, "events": [
            self._event_wire(e.item_id, e.seq, e.kind, e.actor, e.text, e.at, rev=e.rev, auth=e.auth)
            for e in events]}, 200)

    def do_reopen(self, item_id: str) -> None:
        """POST /asks/{id}/reopen — bring a done or dismissed ask back to open
        (Relay's Reopen). Optional `actor`: "owner" (the default) or "session".
        Already open: nothing changes, 200 with the item."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        body = self._json_body()
        if body is None:
            return

        actor = self._actor(body.get("actor") or "owner")
        if not self.principal and actor not in ("owner", "session"):
            return self.send_json({"error": "invalid actor"}, 400)

        auth = None
        if actor == "owner":                # also a body without `actor`: it defaults to the owner
            auth = self._owner(always=False)
            if auth is None:
                return

        try:
            item = self.server.service.reopen(item_id, actor=actor, auth=auth)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)

        return self.send_json({"item": self._wire(item, requirements=True)}, 200)

    def do_answers(self, item_id: str) -> None:
        """POST /asks/{id}/answers — the owner's answers to step questions,
        {"answers": [...]}. Checked in this order (R3): auth, 404 unknown ask,
        400 invalid json / missing fields, 409 unless the ask is open, 400
        invalid answers with field paths (nothing stored); else all of them are
        stored in one transaction and the Telegram keyboard is redrawn."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        body = self._json_body()
        if body is None:
            return

        auth = self._owner(always=False)
        if auth is None:
            return

        if self.server.service.store.get_item(item_id) is None:
            return self.send_json({"error": "not found"}, 404)

        if "answers" not in body:
            return self.send_json({"error": "missing fields"}, 400)
        stamp = self._rev_hash(body)
        if stamp is None:
            return

        try:
            item = self.server.service.answer(item_id, body["answers"], rev=stamp[0], hash=stamp[1], auth=auth)
        except Corrected as exc:
            return self.send_json(exc.body(), 409)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)
        except AskClosed:
            return self.send_json({"error": "ask is closed"}, 409)
        except InvalidAnswers as exc:
            # Field paths only, never values: a pasted secret is not echoed.
            return self.send_json({"error": "invalid answers", "details": exc.details}, 400)

        return self.send_json({"item": self._wire(item, requirements=True)}, 200)

    def do_amend(self, item_id: str) -> None:
        """POST /asks/{id}/amend {editKey, baseRev, baseHash, reason, instructable, docs?} —
        correct an open ask in place (Pond 1.1.0). Checked in this order: 401, 404,
        409 corrections not enabled, 400 invalid json, 400 missing fields, then the
        service's checks (Service.amend). 200 {"item", "revision": {rev, hash,
        changed, added, removed, moved, top}}."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        service = self.server.service
        if service.store.get_item(item_id) is None:
            return self.send_json({"error": "not found"}, 404)
        if not service.corrections_enabled():
            return self.send_json({"error": "corrections not enabled"}, 409)
        body = self._json_body()
        if body is None:
            return
        edit_key, base_rev, base_hash = body.get("editKey"), body.get("baseRev"), body.get("baseHash")
        if (not isinstance(edit_key, str) or not edit_key or type(base_rev) is not int
                or not isinstance(base_hash, str) or body.get("instructable") is None):
            return self.send_json({"error": "missing fields"}, 400)
        try:
            item, revision, summary = service.amend(
                item_id, edit_key, base_rev, base_hash, body.get("reason"), body["instructable"],
                docs=body.get("docs"), actor=self._actor("session"))
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({
            "item": self._wire(item),
            "revision": {"rev": revision.rev, "hash": revision.hash, **summary},
        }, 200)

    def do_append(self, item_id: str) -> None:
        """POST /asks/{id}/append {editKey, baseRev, baseHash, note, steps, docs?} — add steps
        after the last step of an open ask (Pond 1.9.0). Checked in this order: 401,
        404, 409 appends not enabled, 400 invalid json, 400 missing fields, then the
        service's checks (Service.append). 200 {"item", "revision": {rev, hash, kind,
        added: [{id, path}]}, "openUrl"?}."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        service = self.server.service
        if service.store.get_item(item_id) is None:
            return self.send_json({"error": "not found"}, 404)
        if not service.appends_enabled():
            return self.send_json({"error": "appends not enabled"}, 409)
        body = self._json_body()
        if body is None:
            return
        edit_key, base_rev, base_hash = body.get("editKey"), body.get("baseRev"), body.get("baseHash")
        if not isinstance(edit_key, str) or not edit_key or type(base_rev) is not int or not isinstance(base_hash, str):
            return self.send_json({"error": "missing fields"}, 400)
        try:
            item, revision, added = service.append(
                item_id, edit_key, base_rev, base_hash, body.get("note"), body.get("steps"),
                docs=body.get("docs"), actor=self._actor("session"))
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({
            "item": self._wire(item),
            "revision": {"rev": revision.rev, "hash": revision.hash,
                         "kind": service.store.revision_kinds(item_id).get(revision.rev, "correction"), "added": added},
            **service._open_url(item_id),
        }, 200)

    def do_accept(self, item_id: str) -> None:
        """POST /asks/{id}/accept {"rev","hash"} — the owner accepted this revision
        (owner key always). 200 {"item"}, 404, 409 stale revision."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        auth = self._owner(always=True)
        if auth is None:
            return
        service = self.server.service
        if service.store.get_item(item_id) is None:
            return self.send_json({"error": "not found"}, 404)
        rev, digest = body.get("rev"), body.get("hash")
        if type(rev) is not int or not isinstance(digest, str):
            return self.send_json({"error": "missing fields"}, 400)
        try:
            item = service.accept(item_id, rev, digest, auth)
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({"item": self._wire(item, requirements=True)}, 200)

    def do_problems(self, item_id: str) -> None:
        """POST /asks/{id}/problems {"step","rev","hash","note"} — the owner reports a
        problem with a step (owner key always). The note reaches the posting
        session as quoted text, never as instructions."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        auth = self._owner(always=True)
        if auth is None:
            return
        service = self.server.service
        if service.store.get_item(item_id) is None:
            return self.send_json({"error": "not found"}, 404)
        if any(body.get(k) is None for k in ("step", "rev", "hash", "note")):
            return self.send_json({"error": "missing fields"}, 400)
        try:
            item = service.problem(item_id, body["step"], body["rev"], body["hash"], body["note"], auth)
        except AmendError as exc:
            return self.send_json(exc.body, exc.status)
        return self.send_json({"item": self._wire(item, requirements=True)}, 200)

    def do_progress(self, item_id: str) -> None:
        """POST /asks/{id}/progress {"tick": [ids], "untick": [ids], "rev", "hash"} — the
        owner's tick changes from Hook (Pond 1.3.0). Checked in this order: 401, 404,
        400 invalid json / missing fields (neither tick nor untick) / invalid revision,
        the owner check, 409 ask is closed, 409 ask was corrected, 400 invalid progress
        with field paths. 200 {"item"} with `requirements`; a delta that changes
        nothing, or one on an ask without requirements, stores nothing."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        service = self.server.service
        if service.store.get_item(item_id) is None:
            return self.send_json({"error": "not found"}, 404)
        body = self._json_body()
        if body is None:
            return
        if body.get("tick") is None and body.get("untick") is None:
            return self.send_json({"error": "missing fields"}, 400)
        stamp = self._rev_hash(body)
        if stamp is None:
            return
        auth = self._owner(always=False)
        if auth is None:
            return
        try:
            item = service.progress(item_id, body.get("tick"), body.get("untick"),
                                    rev=stamp[0], hash=stamp[1], auth=auth)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)
        except AskClosed:
            return self.send_json({"error": "ask is closed"}, 409)
        except Corrected as exc:
            return self.send_json(exc.body(), 409)
        except InvalidProgress as exc:
            # Field paths only, never values.
            return self.send_json({"error": "invalid progress", "details": exc.details}, 400)
        return self.send_json({"item": self._wire(item, requirements=True)}, 200)

    def _with_requirements(self, item, wire: dict[str, Any]) -> dict[str, Any]:
        """`wire` plus `requirements`, for an answer that keeps Item.to_wire()'s shape."""
        needs = self.server.service.requirements_left(item.id)
        if needs is not None:
            wire["requirements"] = requirements_wire(needs)
        return wire

    def do_notify(self) -> None:
        """POST /notify — a reminder or brief from Central Time core to the
        owner's phone, once per ref: 201 sent, 200 deduped (nothing sent),
        400 with field paths, 502 when Telegram refused (nothing stored, so
        the same ref sends on the retry). No title or body reaches a log."""
        if not (self.check_auth() or self.check_notify_auth()):
            return self.send_json({"error": "unauthorized"}, 401)
        if not self.server.service.telegram.enabled:
            return self.send_json({"error": "Telegram is disabled"}, 503)
        body = self._json_body()
        if body is None:
            return
        household_mode = self.server.service.members is not None
        clean, problems = validate_notification(body, household=household_mode)
        if problems:
            return self.send_json({"error": "invalid notification", "details": problems}, 400)
        try:
            if household_mode:
                notification, deduped = notify_member(self.server.service, **clean)
            else:
                notification, deduped = self.server.service.notify(**clean)
        except ValueError:
            return self.send_json({"error": "invalid notification", "details": ["to: not a member"]}, 400)
        except TelegramError as exc:
            logger.warning("[mailbox] POST /notify failed: %s", type(exc).__name__)
            return self.send_json({"error": "telegram send failed"}, 502)
        return self.send_json({"id": notification.id, "deduped": deduped}, 200 if deduped else 201)

    def do_get_events(self) -> None:
        """GET /events — read events after seq."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)

        query = self._query()
        try:
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
        except ValueError:
            return self.send_json({"error": "invalid query"}, 400)
        if after < 0 or not 1 <= limit <= MAX_LIMIT:
            return self.send_json({"error": "invalid query"}, 400)

        events, next_cursor = self.server.service.store.read_events(after_seq=after, limit=limit, principal_id=self.agent_id)
        return self.send_json({
            "events": [
                self._event_wire(e.item_id, e.seq, e.kind, e.actor, e.text, e.at, rev=e.rev, auth=e.auth)
                for e in events
            ],
            "next": next_cursor,
        }, 200)

    # --- the owner key ---

    @staticmethod
    def _key_wire(k) -> dict[str, Any]:
        """A key as listed: never its hash or code. `lastUsed` (Pond 1.11.0): when a
        request last authenticated with it, to the hour; null when never."""
        return {"id": k.id, "label": k.label, "status": k.status, "created": k.created,
                "approvedAt": k.approved_at, "lastUsed": k.last_used}

    def do_get_owner(self) -> None:
        """GET /owner — the requirement, the approved and pending keys, `self` (the key
        matching this request's X-Hook-Owner-Key, in any status, or null) and `maxKeys`
        (Pond 1.11.0). The Pond token alone reads it, so a Mac without a working key
        can still list the devices."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        service = self.server.service
        keys = service.owner_keys()
        secret = self._owner_secret()
        mine = None
        if secret is not None:
            digest = owner.key_hash(secret)
            mine = next(({"id": k.id, "status": k.status} for k in keys if k.key_hash == digest), None)
        return self.send_json({
            "required": service.owner_key_required(),
            "keys": [self._key_wire(k) for k in keys if k.status in ("approved", "pending")],
            "self": mine,
            "maxKeys": owner.MAX_KEYS,
        }, 200)

    def do_enroll(self) -> None:
        """POST /owner/enroll {"keyHash","label"} — 201 {"id","code","expires","status"}.
        The owner approves on Telegram; Hook polls GET /owner/enroll/{id}."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        digest, label = body.get("keyHash"), body.get("label")
        if not isinstance(digest, str) or not owner.KEY_HASH_RE.fullmatch(digest):
            return self.send_json({"error": "invalid keyHash"}, 400)
        try:
            k = self.server.service.enroll(digest, label)
        except EnrollRefused as exc:
            return self.send_json({"error": exc.error}, 400 if exc.error == "invalid label" else 409)
        except TelegramError as exc:
            logger.warning("[mailbox] POST /owner/enroll failed: %s", type(exc).__name__)
            return self.send_json({"error": "telegram send failed"}, 502)
        return self.send_json({"id": k.id, "code": k.code, "expires": k.expires, "status": k.status}, 201)

    def do_get_enrollment(self, enroll_id: str) -> None:
        """GET /owner/enroll/{id} — {"id","status"}, expiry applied."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        k = self.server.service.enrollment(enroll_id)
        if k is None:
            return self.send_json({"error": "not found"}, 404)
        return self.send_json({"id": k.id, "status": k.status}, 200)

    def do_require(self) -> None:
        """POST /owner/require {"required": bool}, owner key always — 200 {"required"}
        or, for off, 202 {"pending": true} until the owner taps Turn off on Telegram."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        if self._owner(always=True) is None:
            return
        required = body.get("required")
        if type(required) is not bool:
            return self.send_json({"error": "invalid required"}, 400)
        by = self.server.service.owner_for(self._owner_secret())
        if by is None:                                  # revoked since the check
            return self.send_json({"error": "unknown owner key"}, 403)
        try:
            outcome = self.server.service.request_enforcement(required, by=by)
        except TelegramError as exc:
            logger.warning("[mailbox] POST /owner/require failed: %s", type(exc).__name__)
            return self.send_json({"error": "telegram send failed"}, 502)
        if outcome == "pending":
            return self.send_json({"pending": True}, 202)
        return self.send_json({"required": outcome == "on"}, 200)

    def do_revoke(self) -> None:
        """POST /owner/revoke {"id"}, owner key always — 200 {"id","status":"revoked"}."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        if self._owner(always=True) is None:
            return
        key_id = body.get("id")
        if not isinstance(key_id, str) or not key_id:
            return self.send_json({"error": "missing fields"}, 400)
        by = self.server.service.owner_for(self._owner_secret())
        if by is None:                                  # revoked since the check
            return self.send_json({"error": "unknown owner key"}, 403)
        try:
            k = self.server.service.revoke(key_id, by=by)
        except ValueError:
            return self.send_json({"error": "not found"}, 404)
        return self.send_json({"id": k.id, "status": k.status}, 200)

    def do_revoke_request(self) -> None:
        """POST /owner/revoke-request {"id"}, Pond token only (Pond 1.11.0) — 201
        {"id","code","expires","status":"pending"}. The key is revoked only when the
        owner taps Revoke on Telegram; Hook polls GET /owner/revoke-request/{id}.
        404 unknown key; 409 already revoked, not approved or revoke request pending."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        body = self._json_body()
        if body is None:
            return
        key_id = body.get("id")
        if not isinstance(key_id, str) or not key_id:
            return self.send_json({"error": "missing fields"}, 400)
        try:
            r = self.server.service.request_revoke(key_id)
        except RevokeRefused as exc:
            return self.send_json({"error": exc.error}, exc.status)
        except TelegramError as exc:
            logger.warning("[mailbox] POST /owner/revoke-request failed: %s", type(exc).__name__)
            return self.send_json({"error": "telegram send failed"}, 502)
        return self.send_json({"id": r.id, "code": r.code, "expires": r.expires, "status": r.status}, 201)

    def do_get_revoke_request(self, request_id: str) -> None:
        """GET /owner/revoke-request/{id} — {"id","status"}: pending, approved,
        rejected or expired, expiry applied (Pond 1.11.0)."""
        if not self.check_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        r = self.server.service.revoke_request(request_id)
        if r is None:
            return self.send_json({"error": "not found"}, 404)
        return self.send_json({"id": r.id, "status": r.status}, 200)

    # --- plumbing ---

    def send_json(self, data: dict[str, Any], status: int) -> None:
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._responded = True
        self.send_response(status)
        if self.path.startswith("/admission/"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress the default per-request logging: request lines carry item
        ids and would be the one place a token could leak via a query."""
        pass


class MailboxHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the service, the token and poll health."""

    daemon_threads = True          # request threads never outlive the process
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], service: Service, mailbox_token: Optional[str],
        notify_token: Optional[str] = None,
    ):
        super().__init__(address, MailboxHTTPHandler)
        self.service = service
        self.installation = service.store.get_setting("pond_installation_id")
        self.fenced = os.environ.get("POND_MAINTENANCE") == "1"
        self.mailbox_token = mailbox_token
        # Opens POST /notify and nothing else; None = only the mailbox token does.
        self.notify_token = notify_token
        # Zero, not now: /health stays red until getUpdates has actually come
        # back once (Ruling S38), so a broken bot token can't look healthy for
        # the first two minutes after boot.
        self.last_poll_time = 0.0
        # Set while serve_forever is running: shutdown() blocks forever if it
        # is called when the loop was never started.
        self.serving = threading.Event()
        self.claim_lock = threading.Lock()
        self.claim_windows = OrderedDict()

    def limit_claims(self, peer):
        # Use the TCP peer, never an unverified forwarding header. Bound memory
        # independently of the number of addresses that contact this server.
        now = time.monotonic()
        with self.claim_lock:
            start, count = self.claim_windows.get(peer, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= 64:
                raise AdmissionError(429, "claim limit reached")
            self.claim_windows[peer] = start, count + 1
            self.claim_windows.move_to_end(peer)
            if len(self.claim_windows) > 1024:
                self.claim_windows.popitem(last=False)


    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Serve until shutdown(), tracking that the loop is live."""
        self.serving.set()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.serving.clear()


def _write_private(path: Path, text: str) -> None:
    """Create or replace a file that is never world-readable, not even briefly."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


class MailboxServer:
    """Mailbox daemon: HTTP server + Telegram poll loop + nightly backup."""

    def __init__(
        self,
        data_dir: Path,
        backup_dir: Path,
        telegram_token: Optional[str],
        owner_id: Optional[int],
        context: str,
        port: int = 8790,
        host: str = "0.0.0.0",
        relay_open_base: Optional[str] = None,
        ct_url: Optional[str] = None,
        ct_token: Optional[str] = None,
        notify_token: Optional[str] = None,
        brief_asks: bool = True,
        members: Optional[dict[int, str]] = None,
        telegram_mode: str = "full",
        anthropic_api_key: Optional[str] = None,
    ):
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.chmod(0o700)

        self.host = host
        self.port = port
        self.context = context

        # One Store, shared by the poll thread and every request thread
        # (Ruling S7): its RLock serializes all access to the connection.
        if installation := os.environ.get("POND_INSTALL_ID"):
            maintenance.bound_identity(self.data_dir, installation)
        self.store = Store(self.data_dir / "mailbox.db")
        self.store.open()

        self.telegram = (TelegramClient(telegram_token, owner_id) if telegram_token and owner_id is not None
                         else DisabledTelegram())
        # Capture and reminder acks go to Central Time with the capture addon token; None = capture off.
        self.ct_client = CentralTimeClient(ct_url, ct_token) if ct_url and ct_token else None
        self.zone = household_zone()
        self.service = Service(self.store, self.telegram, context, open_base=relay_open_base, ct_client=self.ct_client, zone=self.zone, brief_asks=brief_asks, members=members, telegram_mode=telegram_mode,
                               answerer=Answerer(anthropic_api_key) if anthropic_api_key else None)
        # The token core presents on POST /notify; None = only the mailbox token works there.
        self.notify_token = notify_token

        admission_mode = self.service.admission.mode
        if admission_mode != "native" and not self.telegram.enabled:
            self.store.close()
            raise ValueError(f"This Pond requires TELEGRAM_BOT_TOKEN and MAILBOX_OWNER_ID in {admission_mode} mode")
        self.mailbox_token = self._setup_mailbox_token() if admission_mode != "native" else None

        self.http_server = MailboxHTTPServer(
            (host, port), self.service, self.mailbox_token, notify_token=self.notify_token
        )

        self.poll_thread: Optional[threading.Thread] = None
        self.backup_thread: Optional[threading.Thread] = None
        self.running = False

    # --- token ---

    def _setup_mailbox_token(self) -> str:
        """Generate or load the mailbox token. It is NEVER logged or printed.

        Two 600 files hold it: <data>/mailbox-token (the daemon's own) and
        <backups>/mailbox-token (the mirror the operator copies from over ssh,
        exactly like Central Time's admin token)."""
        token_file = self.data_dir / "mailbox-token"
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
            logger.info("[mailbox] mailbox-token loaded")
        else:
            token = secrets.token_urlsafe(32)
            _write_private(token_file, token + "\n")
            logger.info("[mailbox] mailbox-token generated")
        try:
            _write_private(self.backup_dir / "mailbox-token", token + "\n")
        except OSError as exc:
            logger.warning(
                "[mailbox] could not mirror mailbox-token to backups: %s", type(exc).__name__
            )
        return token

    # --- poll loop ---

    def _poll_loop(self) -> None:
        """Long-poll Telegram for owner taps and replies."""
        offset = 0
        while self.running:
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=50)
                # Only a successful getUpdates counts as a live poll (S17).
                self.http_server.last_poll_time = time.time()
            except TelegramError as exc:
                logger.warning("[mailbox] poll error: %s: %s", type(exc).__name__, exc)
                time.sleep(5)
                continue
            except Exception as exc:
                logger.error("[mailbox] poll crashed: %s: %s", type(exc).__name__, exc)
                time.sleep(5)
                continue

            for update in updates:
                # Confirm the update before handling it: a single poisonous
                # update must not stall the loop on it forever.
                offset = max(offset, update.get("update_id", 0) + 1)
                try:
                    self.service.handle_update(update)
                except Exception as exc:
                    logger.error(
                        "[mailbox] update %s failed: %s: %s",
                        update.get("update_id"), type(exc).__name__, exc,
                    )
            # Pond 1.9.0: bring Done back on asks whose step wait ended.
            try:
                self.service.sweep_step_waits(self.service._utc())
            except Exception as exc:
                logger.error("[mailbox] step-wait sweep failed: %s: %s", type(exc).__name__, exc)

    # --- backup ---

    @staticmethod
    def _next_backup_time(now: datetime) -> datetime:
        """The next 03:20 in local time at or after `now`."""
        target = now.replace(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return target

    def _backup_loop(self) -> None:
        """Nightly snapshot at 03:20 local time (TZ env) — Ruling S3. Then the document
        sweep (Pond 1.10.0), also when the backup failed."""
        while self.running:
            target = self._next_backup_time(datetime.now())
            while self.running:
                remaining = (target - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 60.0))   # wake often enough to notice stop()
            if not self.running:
                break
            try:
                self._do_backup()
            except Exception as exc:
                logger.error("[mailbox] backup failed: %s: %s", type(exc).__name__, exc)
            self._sweep_docs()

    def _sweep_docs(self) -> None:
        """Release the documents of asks closed DOCS_KEEP_DAYS ago (Service.sweep_docs):
        nightly after the backup, and once at start. A failure is logged, never raised."""
        try:
            self.service.sweep_docs()
        except Exception as exc:
            logger.error("[mailbox] document sweep failed: %s: %s", type(exc).__name__, exc)

    def _do_backup(self) -> None:
        """Take a consistent snapshot: temp file, atomic rename, 600, prune."""
        stamp = datetime.now().strftime("%Y-%m-%d")
        tmp_path = self.backup_dir / f".mailbox-{stamp}.db.tmp"
        final_path = self.backup_dir / f"mailbox-{stamp}.db"

        # The process umask is already 0o077 (set in main, Ruling S6); a worker
        # thread must not touch that global, so the modes here are explicit.
        self.backup_dir.chmod(0o700)
        if tmp_path.exists():
            tmp_path.unlink()
        try:
            self.store.snapshot(tmp_path)
            tmp_path.chmod(0o600)
            os.replace(tmp_path, final_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        for old in sorted(self.backup_dir.glob("mailbox-*.db"))[:-BACKUP_KEEP]:
            old.unlink()

        logger.info("[mailbox] backup: %s", final_path.name)

        if self.service.members is not None:
            # The household instance keeps only transport copies; the records live in Central Time.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=PRUNE_DAYS)).isoformat(timespec="seconds")
            pruned = self.store.prune_notifications(cutoff)
            logger.info("[mailbox] household notifications pruned: %d", pruned)

    # --- lifecycle ---

    def start(self) -> None:
        """Sweep expired documents once, start the threads, then serve HTTP until stop()."""
        self.running = True
        if self.http_server.fenced:
            self.http_server.serve_forever()
            return
        self._sweep_docs()

        if self.telegram.enabled:
            self.poll_thread = threading.Thread(
                target=self._poll_loop, name="mailbox-poll", daemon=True
            )
            self.poll_thread.start()
            logger.info("[mailbox] poll thread started")

        self.backup_thread = threading.Thread(
            target=self._backup_loop, name="mailbox-backup", daemon=True
        )
        self.backup_thread.start()
        logger.info(
            "[mailbox] backup thread started (nightly %02d:%02d local)",
            BACKUP_HOUR, BACKUP_MINUTE,
        )
        # The resolved zone with its offset, not the TZ variable: a box without
        # tzdata resolves to UTC whatever TZ says, and this line must show it.
        mode = ""
        if self.service.members is not None:
            # Names only, never Telegram ids.
            mode = f", household: {len(self.service.members)} members ({', '.join(self.service.members.values())})"
        logger.info(
            "[mailbox] capture: %s, notify: %s (tz %s)%s",
            "enabled" if self.ct_client else "disabled",
            "enabled" if self.notify_token and self.telegram.enabled else "disabled",
            describe_zone(self.zone),
            mode,
        )

        logger.info("[mailbox] listening on http://%s:%d", self.host, self.port)
        self.http_server.serve_forever()

    def stop(self) -> None:
        """Stop serving, then close the store."""
        self.running = False
        if self.http_server.serving.is_set():
            self.http_server.shutdown()
        self.http_server.server_close()
        self.store.close()
