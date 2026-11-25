"""SQLite store for the mailbox queue. Central Time item envelope + events append-log."""
import hashlib
from contextlib import contextmanager
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from mailbox import docs as _documents
from mailbox import migrations
from mailbox.instructable import MAX_DOC_BYTES


@dataclass
class Item:
    """Central Time item envelope, column-for-column match."""
    id: str
    source: str
    title: str
    anchor_date: Optional[str] = None
    anchor_time: Optional[str] = None
    anchor_end: Optional[str] = None
    all_day: int = 0
    recurrence: Optional[str] = None
    status: str = "open"
    snooze_until: Optional[str] = None
    pinned: int = 0
    rolling: int = 0
    priority: int = 1
    project_id: Optional[str] = None
    lane: Optional[str] = None
    payload_type: str = "xo.ask"
    payload: str = "{}"
    links: str = "[]"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    completed_at: Optional[str] = None

    def to_wire(self) -> dict[str, Any]:
        """Item → camelCase JSON for wire format (Central Time envelope shape)."""
        row = asdict(self)
        wire = {
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "status": row["status"],
            "priority": row["priority"],
            "pinned": bool(row["pinned"]),
            "rolling": bool(row["rolling"]),
            "payloadType": row["payload_type"],
            "payload": json.loads(row["payload"]),
            "links": json.loads(row["links"]),
            "created": row["created"],
            "updated": row["updated"],
        }
        if row["anchor_date"]:
            anchor = {"date": row["anchor_date"]}
            if row["anchor_time"]:
                anchor["time"] = row["anchor_time"]
            if row["anchor_end"]:
                anchor["end"] = row["anchor_end"]
            if row["all_day"]:
                anchor["allDay"] = True
            wire["anchor"] = anchor
        else:
            wire["anchor"] = None
        if row["recurrence"]:
            wire["recurrence"] = json.loads(row["recurrence"])
        else:
            wire["recurrence"] = None
        if row["snooze_until"]:
            wire["snoozeUntil"] = row["snooze_until"]
        else:
            wire["snoozeUntil"] = None
        if row["project_id"]:
            wire["projectId"] = row["project_id"]
        else:
            wire["projectId"] = None
        wire["lane"] = row["lane"]
        if row["completed_at"]:
            wire["completedAt"] = row["completed_at"]
        else:
            wire["completedAt"] = None
        return wire


@dataclass
class Event:
    """Append-only event log entry. `rev` and `auth` live in the event_meta side
    table (never a column of events) and are filled by a LEFT JOIN on read."""
    seq: int
    item_id: str
    kind: str
    actor: str
    text: Optional[str]
    at: str
    rev: Optional[int] = None
    auth: Optional[str] = None


@dataclass
class Revision:
    """One revision of an ask's instructable: the canonical body and its hash."""
    item_id: str
    rev: int
    hash: str
    prev: str
    at: str
    reason: str
    body: str


@dataclass
class OwnerKey:
    """A Mac's owner key, by the SHA-256 of its secret; the secret is never stored.
    status: pending, approved, rejected, expired or revoked."""
    id: str
    key_hash: str
    label: str
    status: str
    code: str
    created: str
    expires: str
    approved_at: Optional[str] = None
    revoked_at: Optional[str] = None
    tg_message_id: Optional[int] = None
    # When a request last authenticated with this key (Pond 1.11.0), stamped at most hourly.
    last_used: Optional[str] = None


@dataclass
class RevokeRequest:
    """A token-only request to revoke an owner key (Pond 1.11.0): the key is revoked
    only when the owner taps Revoke on Telegram. status: pending, approved, rejected
    or expired."""
    id: str
    key_id: str
    status: str
    code: str
    created: str
    expires: str
    decided_at: Optional[str] = None
    tg_message_id: Optional[int] = None


class StaleRevision(Exception):
    """The amendment's base is not the ask's current revision."""

    def __init__(self, current: Optional[Revision]):
        super().__init__("stale revision")
        self.current = current


class AskNotOpen(Exception):
    """The ask is closed: its revisions can't change."""


class StoreFull(Exception):
    """The texts a request would add take the stored total over docs.MAX_STORE_BYTES
    (Pond 1.12.0). Raised inside the writing transaction, which then rolls back."""


@dataclass
class Notification:
    """A reminder or brief sent to the owner; not an ask, never in /asks.
    `action_taken` is the outcome that ended it: done, tomorrow, closed or gone."""
    id: str
    kind: str
    ref: str
    title: str
    body: Optional[str]
    actions: str                       # JSON list of {id, label}
    tg_chat_id: Optional[int]
    tg_message_id: Optional[int]
    action_taken: Optional[str]
    acted_at: Optional[str]
    created: str


def _parse_stamp(value: Any) -> Optional[datetime]:
    """An ISO 8601 stamp as an aware time (no offset: UTC), or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class Store:
    """SQLite persistence layer with WAL mode and consistent backups."""

    # Columns update_item may write. id/source/created/updated/etc. are not
    # caller-settable — updated is always derived, the rest are immutable.
    _UPDATABLE_FIELDS = {
        "title", "anchor_date", "anchor_time", "anchor_end", "all_day",
        "recurrence", "status", "snooze_until", "pinned", "rolling",
        "priority", "project_id", "lane", "payload_type", "payload",
        "links", "completed_at",
    }

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn: Optional[sqlite3.Connection] = None
        # One RLock serializes all access to self.conn: Task 4 shares a single
        # Store between the Telegram poll thread and ThreadingHTTPServer
        # request threads. RLock (not Lock) so a public method may call
        # another public method (e.g. update_item -> get_item) on the same
        # thread without deadlocking.
        self._lock = threading.RLock()

    def open(self) -> None:
        """Open or create the database with WAL mode."""
        with self._lock:
            create = not self.db_path.exists()
            self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            if create:
                self._schema()
            self._notifications_table()
            self.conn.commit()
            migrations.migrate(self.conn)

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None

    def _schema(self) -> None:
        """Create tables and indexes on first boot."""
        self.conn.execute("""
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                anchor_date TEXT,
                anchor_time TEXT,
                anchor_end TEXT,
                all_day INTEGER DEFAULT 0,
                recurrence TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                snooze_until TEXT,
                pinned INTEGER DEFAULT 0,
                rolling INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 1,
                project_id TEXT,
                lane TEXT,
                payload_type TEXT NOT NULL DEFAULT 'xo.ask',
                payload TEXT NOT NULL,
                links TEXT NOT NULL DEFAULT '[]',
                created TEXT NOT NULL,
                updated TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        self.conn.execute("CREATE INDEX items_status ON items(status)")
        self.conn.execute("CREATE INDEX items_lane ON items(lane)")
        self.conn.execute("""
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL REFERENCES items(id),
                kind TEXT NOT NULL,
                actor TEXT NOT NULL,
                text TEXT,
                at TEXT NOT NULL
            )
        """)

    def _notifications_table(self) -> None:
        """Additive, idempotent: a database from before Phase 1 gains the table on open.
        The unique ref is the persisted dedupe for POST /notify."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                ref TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                body TEXT,
                actions TEXT NOT NULL DEFAULT '[]',
                tg_chat_id INTEGER,
                tg_message_id INTEGER,
                action_taken TEXT,
                acted_at TEXT,
                created TEXT NOT NULL
            )
        """)

    def insert_notification(self, n: Notification) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO notifications (id, kind, ref, title, body, actions, tg_chat_id, tg_message_id,
                   action_taken, acted_at, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (n.id, n.kind, n.ref, n.title, n.body, n.actions, n.tg_chat_id, n.tg_message_id,
                 n.action_taken, n.acted_at, n.created),
            )
            self.conn.commit()

    def _one_notification(self, where: str, value: str) -> Optional[Notification]:
        cursor = self.conn.execute(f"SELECT * FROM notifications WHERE {where} = ?", (value,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return Notification(**{cols[i]: row[i] for i in range(len(cols))})

    def get_notification(self, nid: str) -> Optional[Notification]:
        with self._lock:
            return self._one_notification("id", nid)

    def get_notification_by_ref(self, ref: str) -> Optional[Notification]:
        with self._lock:
            return self._one_notification("ref", ref)

    def set_notification_action(self, nid: str, action: str) -> Optional[Notification]:
        """Record the outcome once: a second action never overwrites the first."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.conn.execute(
                "UPDATE notifications SET action_taken = ?, acted_at = ? WHERE id = ? AND action_taken IS NULL",
                (action, now, nid),
            )
            self.conn.commit()
            return self._one_notification("id", nid)

    def prune_notifications(self, before_iso: str) -> int:
        """Household instance only: drop transport copies created before `before_iso`."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM notifications WHERE created < ?", (before_iso,))
            self.conn.commit()
            return cur.rowcount

    def get_item(self, item_id: str) -> Optional[Item]:
        """Fetch one item by id."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return Item(**{cols[i]: row[i] for i in range(len(cols))})

    # The kind filter of list_items: a bug has a bugs row, an ask has none.
    _KINDS = {
        None: "",
        "bug": " AND id IN (SELECT item_id FROM bugs)",
        "ask": " AND id NOT IN (SELECT item_id FROM bugs)",
    }

    def list_items(self, status: Optional[str] = None, days: int = 14, kind: Optional[str] = None) -> list[Item]:
        """List items (optionally filtered by status and by kind, "bug" or "ask"),
        newest first, last N days. An unknown kind raises ValueError."""
        if kind not in self._KINDS:
            raise ValueError(f"list_items: unknown kind {kind!r}")
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            # Assume items in range; a proper calendar anchor is future work
            query, args = "SELECT * FROM items WHERE created >= ?", [cutoff]
            if status is not None:
                query += " AND status = ?"
                args.append(status)
            cursor = self.conn.execute(query + self._KINDS[kind] + " ORDER BY created DESC", args)
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [Item(**{cols[i]: row[i] for i in range(len(cols))}) for row in rows]

    def insert_item(self, item: Item) -> Item:
        """Insert a new item; update the timestamp."""
        with self._lock:
            item.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.conn.execute(
                """INSERT INTO items (
                    id, source, title, anchor_date, anchor_time, anchor_end, all_day,
                    recurrence, status, snooze_until, pinned, rolling, priority,
                    project_id, lane, payload_type, payload, links, created, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.id, item.source, item.title, item.anchor_date, item.anchor_time,
                    item.anchor_end, item.all_day, item.recurrence, item.status,
                    item.snooze_until, item.pinned, item.rolling, item.priority,
                    item.project_id, item.lane, item.payload_type, item.payload,
                    item.links, item.created, item.updated,
                ),
            )
            self.conn.commit()
            return item

    def update_item(self, item_id: str, fields: dict[str, Any]) -> Optional[Item]:
        """Update specific fields of an item.

        Only columns in _UPDATABLE_FIELDS may be written — id/source/created
        are immutable and updated is always derived here, never caller-set.
        Mirrors Central Time's PATCH semantics: closing (status -> done or
        dismissed) derives completed_at when not explicitly given; reopening
        (status -> open) clears completed_at and snooze_until.
        """
        with self._lock:
            owns_transaction = not self.conn.in_transaction
            for key in fields:
                if key not in self._UPDATABLE_FIELDS:
                    raise ValueError(f"update_item: field not updatable: {key}")
            if not self.get_item(item_id):
                return None
            fields = dict(fields)
            if fields.get("status") in ("done", "dismissed") and "completed_at" not in fields:
                fields["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            elif fields.get("status") == "open":
                fields["completed_at"] = None
                fields["snooze_until"] = None
            fields["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
            values = list(fields.values()) + [item_id]
            self.conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", values)
            if owns_transaction:
                self.conn.commit()
            return self.get_item(item_id)

    def reopen_item(self, item_id: str) -> Optional[bool]:
        """Bring a closed item (done or dismissed) back to open, atomically.

        Returns None for an unknown id, True when the item was closed and is
        now open, False when it was not closed (nothing changed). The check
        and the write share the lock, so two concurrent reopens change the
        item, and report True, exactly once."""
        with self._lock:
            item = self.get_item(item_id)
            if item is None:
                return None
            if item.status not in ("done", "dismissed"):
                return False
            self.update_item(item_id, {"status": "open"})
            return True

    def _insert_event(self, item_id: str, kind: str, actor: str, text: Optional[str], at: str,
                      rev: Optional[int], auth: Optional[str]) -> Event:
        """One events row, plus its event_meta row when rev or auth is set. No commit."""
        cursor = self.conn.execute(
            "INSERT INTO events (item_id, kind, actor, text, at) VALUES (?, ?, ?, ?, ?)",
            (item_id, kind, actor, text, at),
        )
        if rev is not None or auth is not None:
            self.conn.execute("INSERT INTO event_meta (seq, rev, auth) VALUES (?, ?, ?)",
                              (cursor.lastrowid, rev, auth))
        return Event(seq=cursor.lastrowid, item_id=item_id, kind=kind, actor=actor, text=text, at=at,
                     rev=rev, auth=auth)

    def append_event(self, item_id: str, kind: str, actor: str, text: Optional[str], *,
                     rev: Optional[int] = None, auth: Optional[str] = None) -> Event:
        """Append an event and return it with the assigned seq. `rev` and `auth`
        go to event_meta in the same transaction."""
        with self._lock:
            owns_transaction = not self.conn.in_transaction
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                event = self._insert_event(item_id, kind, actor, text, at, rev, auth)
                if owns_transaction:
                    self.conn.commit()
            except BaseException:
                if owns_transaction:
                    self.conn.rollback()
                raise
            return event

    @contextmanager
    def event_transaction(self):
        """Serialize validation, reads and event append against every store writer.

        BEGIN IMMEDIATE also excludes other SQLite connections. The caller must
        perform only store reads, update_item and event appends here; network work
        belongs after this context. Those writers leave this commit to us.
        """
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def append_events(
        self, item_id: str, kind: str, actor: str, texts: list[str], *, only_if_open: bool = False,
        rev: Optional[int] = None, auth: Optional[str] = None, at: Optional[str] = None,
    ) -> Optional[list[Event]]:
        """Append one event per text in a single transaction: all land or none.
        With only_if_open the item's status is checked under the same lock, so
        a Done racing the batch can never be followed by its events; a closed
        or missing item then returns None and nothing is written. `at` is the
        stamp to give them (the caller's clock), else now."""
        with self._lock:
            owns_transaction = not self.conn.in_transaction
            if only_if_open:
                item = self.get_item(item_id)
                if item is None or item.status != "open":
                    return None
            at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
            events: list[Event] = []
            try:
                for text in texts:
                    events.append(self._insert_event(item_id, kind, actor, text, at, rev, auth))
                if owns_transaction:
                    self.conn.commit()
            except BaseException:
                if owns_transaction:
                    self.conn.rollback()
                raise
            return events

    def append_pair(self, item_id: str, first: tuple[str, str, str], second: tuple[str, str, str], *,
                    at: str, rev: Optional[int] = None, auth: Optional[str] = None) -> Optional[list[Event]]:
        """Two events (kind, actor, text) in one transaction, only while the item is open
        (Ask about this, Pond 1.14.0: the owner's question and the answer land together
        or not at all). `auth` goes on the first event only. → [first, second], or None
        when the item is missing or not open (nothing is written)."""
        with self._lock:
            item = self.get_item(item_id)
            if item is None or item.status != "open":
                return None
            owns_transaction = not self.conn.in_transaction
            try:
                events = [self._insert_event(item_id, first[0], first[1], first[2], at, rev, auth),
                          self._insert_event(item_id, second[0], second[1], second[2], at, rev, None)]
                if owns_transaction:
                    self.conn.commit()
            except BaseException:
                if owns_transaction:
                    self.conn.rollback()
                raise
            return events

    def count_events(self, kind: str, *, since: str) -> int:
        """Events of this kind on any item stamped at or after `since` (an ISO-8601 UTC
        stamp in the store's own format, so the text order is the time order)."""
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind = ? AND at >= ?",
                                    (kind, since)).fetchone()
            return int(row[0])

    # Events with their side-table metadata (rev, auth), NULL when there is none.
    _EVENTS = "SELECT e.*, m.rev, m.auth FROM events e LEFT JOIN event_meta m ON m.seq = e.seq"

    def read_events(self, after_seq: int = 0, limit: int = 100) -> tuple[list[Event], int]:
        """Events after seq, newest first; return (events, next_cursor)."""
        with self._lock:
            cursor = self.conn.execute(
                f"{self._EVENTS} WHERE e.seq > ? ORDER BY e.seq ASC LIMIT ?",
                (after_seq, limit + 1),
            )
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            events = [Event(**{cols[i]: row[i] for i in range(len(cols))}) for row in rows[:limit]]
            next_cursor = events[-1].seq if events else after_seq
            return events, next_cursor

    def events_for_item(self, item_id: str) -> list[Event]:
        """All events for one item, in seq order — unlike read_events, this
        has no store-wide window, so a busy store (many events on OTHER
        items) can never push an item's own history out of view (S25)."""
        with self._lock:
            cursor = self.conn.execute(
                f"{self._EVENTS} WHERE e.item_id = ? ORDER BY e.seq",
                (item_id,),
            )
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [Event(**{cols[i]: row[i] for i in range(len(cols))}) for row in rows]

    def lookup_dedup_key(self, key: str, within_hours: int = 24, now: Optional[datetime] = None) -> Optional[Item]:
        """Find an open item with this payload.key created within N hours of now."""
        with self._lock:
            cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=within_hours)).isoformat(timespec="seconds")
            cursor = self.conn.execute(
                """SELECT * FROM items WHERE status = 'open' AND
                   json_extract(payload, '$.key') = ? AND created > ?
                   ORDER BY created DESC LIMIT 1""",
                (key, cutoff),
            )
            rows = cursor.fetchall()
            if not rows:
                return None
            cols = [d[0] for d in cursor.description]
            return Item(**{cols[i]: rows[0][i] for i in range(len(cols))})

    def get_by_tg_message_id(self, message_id: int) -> Optional[Item]:
        """The ask whose Telegram message has this id (a reply carries only that id):
        the tg_messages row first, then the payload's message id."""
        with self._lock:
            found = self.tg_message(message_id)
            if found is not None:
                item = self.get_item(found[0])
                if item is not None:
                    return item
            cursor = self.conn.execute(
                """SELECT * FROM items WHERE json_extract(payload, '$.tg_message_id') = ?
                   ORDER BY created DESC LIMIT 1""",
                (int(message_id),),
            )
            rows = cursor.fetchall()
            if not rows:
                return None
            cols = [d[0] for d in cursor.description]
            return Item(**{cols[i]: rows[0][i] for i in range(len(cols))})

    # --- revisions (Pond 1.1.0) ---------------------------------------------

    def _revisions_where(self, where: str, args: tuple) -> list[Revision]:
        cursor = self.conn.execute(
            f"SELECT item_id, rev, hash, prev, at, reason, body FROM revisions WHERE {where} ORDER BY rev", args)
        return [Revision(*row) for row in cursor.fetchall()]

    def _insert_revision(self, r: Revision) -> None:
        self.conn.execute(
            "INSERT INTO revisions (item_id, rev, hash, prev, at, reason, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r.item_id, r.rev, r.hash, r.prev, r.at, r.reason, r.body),
        )

    def insert_ask(self, item: Item, rev1: Optional[Revision], edit_key_hash: Optional[str], *,
                   docs: Optional[Mapping[str, bytes]] = None) -> Item:
        """A new ask, all in one transaction: the items row, its revision 1 (None for an
        ask without an instructable), its edit key hash, the posted/session event, the
        rev-1 tg_messages row (from payload.tg_message_id, when set) and, from Pond
        1.10.0, its documents ({sha256: exact bytes}, stored and linked to the ask)."""
        with self._lock:
            item.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    """INSERT INTO items (
                        id, source, title, anchor_date, anchor_time, anchor_end, all_day,
                        recurrence, status, snooze_until, pinned, rolling, priority,
                        project_id, lane, payload_type, payload, links, created, updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.id, item.source, item.title, item.anchor_date, item.anchor_time,
                        item.anchor_end, item.all_day, item.recurrence, item.status,
                        item.snooze_until, item.pinned, item.rolling, item.priority,
                        item.project_id, item.lane, item.payload_type, item.payload,
                        item.links, item.created, item.updated,
                    ),
                )
                if rev1 is not None:
                    self._insert_revision(rev1)
                self._keep_docs(item.id, docs, item.updated)
                if edit_key_hash is not None:
                    self.conn.execute("INSERT INTO edit_keys (item_id, key_hash) VALUES (?, ?)",
                                      (item.id, edit_key_hash))
                self._insert_event(item.id, "posted", "session", None, item.updated, None, None)
                message_id = json.loads(item.payload).get("tg_message_id")
                if message_id:
                    self.conn.execute("INSERT OR IGNORE INTO tg_messages (message_id, item_id, rev) VALUES (?, ?, 1)",
                                      (int(message_id), item.id))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return item

    # --- bugs (Pond 1.2.0) ----------------------------------------------------

    def insert_bug(self, item: Item, signature: str, excerpt: str, rev1: Optional[Revision] = None) -> Item:
        """A new Hook bug, all in one transaction: the items row, its revision 1 (when
        given), its bugs row (count 1, first and last seen = item.created) and the
        posted/hook event. No edit key and no tg_messages row: a bug is never sent to
        Telegram and no session amends it."""
        with self._lock:
            item.updated = item.updated or item.created
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    """INSERT INTO items (
                        id, source, title, anchor_date, anchor_time, anchor_end, all_day,
                        recurrence, status, snooze_until, pinned, rolling, priority,
                        project_id, lane, payload_type, payload, links, created, updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.id, item.source, item.title, item.anchor_date, item.anchor_time,
                        item.anchor_end, item.all_day, item.recurrence, item.status,
                        item.snooze_until, item.pinned, item.rolling, item.priority,
                        item.project_id, item.lane, item.payload_type, item.payload,
                        item.links, item.created, item.updated,
                    ),
                )
                if rev1 is not None:
                    self._insert_revision(rev1)
                self.conn.execute(
                    """INSERT INTO bugs (item_id, signature, count, first_seen, last_seen, excerpt)
                       VALUES (?, ?, 1, ?, ?, ?)""",
                    (item.id, signature, item.created, item.created, excerpt),
                )
                self._insert_event(item.id, "posted", "hook", None, item.created, None, None)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return item

    def open_bug(self, signature: str) -> Optional[Item]:
        """The newest open bug with this signature, or None. Closed bugs never match:
        a crash after a fix files a new bug."""
        with self._lock:
            cursor = self.conn.execute(
                """SELECT i.* FROM items i JOIN bugs b ON b.item_id = i.id
                   WHERE b.signature = ? AND i.status = 'open' ORDER BY i.created DESC LIMIT 1""",
                (signature,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return Item(**{cols[i]: row[i] for i in range(len(cols))})

    def add_occurrence(self, item_id: str, at: str, excerpt: str) -> int:
        """One more sighting of an open bug, in one transaction: count + 1, last seen
        and the excerpt replaced, items.updated = at, and an occurrence/hook event
        {"count": n}. Returns the new count; KeyError when the item is no bug."""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                cur = self.conn.execute(
                    "UPDATE bugs SET count = count + 1, last_seen = ?, excerpt = ? WHERE item_id = ?",
                    (at, excerpt, item_id))
                if cur.rowcount != 1:
                    raise KeyError(item_id)
                count = self.conn.execute("SELECT count FROM bugs WHERE item_id = ?", (item_id,)).fetchone()[0]
                self.conn.execute("UPDATE items SET updated = ? WHERE id = ?", (at, item_id))
                self._insert_event(item_id, "occurrence", "hook", json.dumps({"count": count}), at, None, None)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return count

    def bug_meta(self, item_id: str) -> Optional[dict[str, Any]]:
        """{signature, count, firstSeen, lastSeen, excerpt} for a bug; None for anything else."""
        with self._lock:
            row = self.conn.execute(
                "SELECT signature, count, first_seen, last_seen, excerpt FROM bugs WHERE item_id = ?",
                (item_id,)).fetchone()
            if row is None:
                return None
            return {"signature": row[0], "count": row[1], "firstSeen": row[2], "lastSeen": row[3],
                    "excerpt": row[4]}

    def revisions(self, item_id: str) -> list[Revision]:
        """Every revision of the ask, ascending by rev."""
        with self._lock:
            return self._revisions_where("item_id = ?", (item_id,))

    def current_revision(self, item_id: str) -> Optional[Revision]:
        with self._lock:
            found = self._revisions_where("item_id = ? AND rev = (SELECT MAX(rev) FROM revisions WHERE item_id = ?)",
                                          (item_id, item_id))
            return found[0] if found else None

    def edit_key_hash(self, item_id: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT key_hash FROM edit_keys WHERE item_id = ?", (item_id,)).fetchone()
            return row[0] if row else None

    def amend_tx(self, item_id: str, *, base_rev: int, base_hash: str, new: Revision, event_text: str,
                 kind: str = "correction", advance: bool = False,
                 docs: Optional[Mapping[str, bytes]] = None) -> Revision:
        """Append `new` by compare-and-swap, in one transaction: the ask must be open
        (else AskNotOpen) and its current revision must be (base_rev, base_hash) (else
        StaleRevision). Writes the revision, its revision_meta row (`kind`),
        items.updated = new.at and the revised/session event with its rev. With
        `advance` (Pond 1.9.0: an append on the version in force) the new revision is
        accepted in the same transaction: an accepts row with auth "append" and an
        accepted/daemon event, never the owner's. `docs` (Pond 1.10.0, {sha256: exact
        bytes}) are stored and linked to the ask in the same transaction."""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
                if row is None or row[0] != "open":
                    raise AskNotOpen()
                current = self.current_revision(item_id)
                if current is None or current.rev != base_rev or current.hash != base_hash:
                    raise StaleRevision(current)
                self._insert_revision(new)
                self._keep_docs(item_id, docs, new.at)
                self.conn.execute("INSERT INTO revision_meta (item_id, rev, kind) VALUES (?, ?, ?)",
                                  (item_id, new.rev, kind))
                self.conn.execute("UPDATE items SET updated = ? WHERE id = ?", (new.at, item_id))
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._insert_event(item_id, "revised", "session", event_text, now, new.rev, None)
                if advance:
                    self.conn.execute("INSERT INTO accepts (item_id, rev, hash, at, auth) VALUES (?, ?, ?, ?, ?)",
                                      (item_id, new.rev, new.hash, now, "append"))
                    self._insert_event(item_id, "accepted", "daemon", None, now, new.rev, "append")
                self.conn.commit()
            except sqlite3.IntegrityError:
                self.conn.rollback()
                raise StaleRevision(self.current_revision(item_id)) from None
            except BaseException:
                self.conn.rollback()
                raise
            return new

    def append_tx(self, item_id: str, *, base_rev: int, base_hash: str, new: Revision, kind: str,
                  advance: bool, event_text: str, docs: Optional[Mapping[str, bytes]] = None) -> Revision:
        """An append (Pond 1.9.0): amend_tx with its kind, the Pond's advance and its documents."""
        return self.amend_tx(item_id, base_rev=base_rev, base_hash=base_hash, new=new, event_text=event_text,
                             kind=kind, advance=advance, docs=docs)

    def revision_kinds(self, item_id: str) -> dict[int, str]:
        """rev → kind for the ask's revisions written from Pond 1.9.0 on. A revision
        2 or later without an entry is a correction."""
        with self._lock:
            rows = self.conn.execute("SELECT rev, kind FROM revision_meta WHERE item_id = ?", (item_id,)).fetchall()
            return {rev: kind for rev, kind in rows}

    def accept_tx(self, item_id: str, rev: int, hash: str, auth: str) -> Event:
        """Record that the owner accepted this revision: the accepts row once (a second
        Mac accepting the same rev and hash is not an error) and always an
        accepted/owner event with its rev and auth."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("INSERT OR IGNORE INTO accepts (item_id, rev, hash, at, auth) VALUES (?, ?, ?, ?, ?)",
                                  (item_id, rev, hash, now, auth))
                event = self._insert_event(item_id, "accepted", "owner", None, now, rev, auth)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return event

    def accepted_rev(self, item_id: str) -> int:
        """The highest revision the owner accepted, else 1."""
        with self._lock:
            row = self.conn.execute("SELECT MAX(rev) FROM accepts WHERE item_id = ?", (item_id,)).fetchone()
            return row[0] if row and row[0] is not None else 1

    # --- documents (Pond 1.10.0) -------------------------------------------
    # An ask's documents are written only inside the transaction that writes the ask
    # or its new revision: insert_ask and amend_tx call _keep_docs between their BEGIN
    # IMMEDIATE and their commit, so put_doc and link_docs land together with the ask
    # or not at all. Each writer joins a transaction the caller already holds and
    # leaves its commit to that caller, as update_item and append_event do; called
    # alone (tests), it commits its own. A release runs only inside the transaction of
    # the check that allows it: release_closed_docs (the route) checks the ask is closed,
    # expire_ask_docs (the sweep) that it has been closed since a cutoff.

    @contextmanager
    def _write(self):
        with self._lock:
            owns_transaction = not self.conn.in_transaction
            try:
                yield
                if owns_transaction:
                    self.conn.commit()
            except BaseException:
                if owns_transaction:
                    self.conn.rollback()
                raise

    def _keep_docs(self, item_id: str, docs: Optional[Mapping[str, bytes]], at: str) -> None:
        """Store each document and link the ask to it, inside the caller's transaction.
        Raises StoreFull first, writing nothing, when the texts not stored yet would
        take the total over the limit (Pond 1.12.0): the caller rolls back."""
        if not docs:
            return
        self.check_doc_room(docs)
        for sha256, body in docs.items():
            self.put_doc(sha256, body, at)
        self.link_docs(item_id, docs.keys())

    def check_doc_room(self, docs: Optional[Mapping[str, bytes]]) -> None:
        """Raise StoreFull when the texts among `docs` that are not stored yet would take
        the sum of docs.bytes over docs.MAX_STORE_BYTES (Pond 1.12.0). A text already
        stored, by this ask or another, adds nothing; with nothing new it never raises.

        Inside a writer's transaction (_keep_docs, between insert_ask's or amend_tx's
        BEGIN IMMEDIATE and their commit) the answer is exact: the transaction holds
        the store lock and SQLite's write lock, so no other write lands between this
        check and the commit. Called alone (Service.post, before its Telegram send)
        it is an early answer that a later write can make stale; the transaction's
        own check still decides."""
        if not docs:
            return
        with self._lock:
            new = 0
            for sha256, body in docs.items():
                if self.conn.execute("SELECT 1 FROM docs WHERE sha256 = ?", (sha256,)).fetchone() is None:
                    new += len(body)
            if new == 0:
                return
            stored = self.conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM docs").fetchone()[0]
            if stored + new > _documents.MAX_STORE_BYTES:
                raise StoreFull()

    def put_doc(self, sha256: str, body: bytes, at: str) -> None:
        """Store a document's exact bytes under its hash. A hash already stored keeps
        its first row: the bytes are the same by definition. Raises ValueError for a
        body that is empty, longer than MAX_DOC_BYTES or not the one `sha256` names
        (docs.check refuses all three first, so this only guards against a caller's bug)."""
        if not 1 <= len(body) <= MAX_DOC_BYTES or hashlib.sha256(body).hexdigest() != sha256:
            raise ValueError("not a document with this hash")
        with self._write():
            self.conn.execute("INSERT OR IGNORE INTO docs (sha256, bytes, body, at) VALUES (?, ?, ?, ?)",
                              (sha256, len(body), body, at))

    def get_doc(self, sha256: str) -> Optional[bytes]:
        """The stored bytes, or None when the text is not (or no longer) here."""
        with self._lock:
            row = self.conn.execute("SELECT body FROM docs WHERE sha256 = ?", (sha256,)).fetchone()
            return bytes(row[0]) if row else None

    def link_docs(self, item_id: str, hashes: Iterable[str]) -> None:
        """Link the ask to each hash: a new row, or an existing one set back to unreleased."""
        with self._write():
            self.conn.executemany(
                """INSERT INTO ask_docs (item_id, sha256, released) VALUES (?, ?, 0)
                   ON CONFLICT (item_id, sha256) DO UPDATE SET released = 0""",
                [(item_id, sha256) for sha256 in hashes],
            )

    def ask_docs(self, item_id: str) -> set[str]:
        """Every hash the ask ever linked, released or not."""
        with self._lock:
            rows = self.conn.execute("SELECT sha256 FROM ask_docs WHERE item_id = ?", (item_id,)).fetchall()
            return {row[0] for row in rows}

    def doc_status(self, item_id: str, sha256: str) -> Optional[str]:
        """The document's state on this ask: "kept" (linked, not released, text present),
        "released" (linked and released, or the text is missing), None (never linked)."""
        with self._lock:
            row = self.conn.execute(
                """SELECT a.released, d.sha256 IS NOT NULL FROM ask_docs a
                   LEFT JOIN docs d ON d.sha256 = a.sha256 WHERE a.item_id = ? AND a.sha256 = ?""",
                (item_id, sha256)).fetchone()
            if row is None:
                return None
            released, present = row
            return "kept" if not released and present else "released"

    def _release_docs(self, item_id: str) -> int:
        """Mark the ask's rows released, then delete the texts this ask linked that no
        unreleased ask_docs row links any more. A text the ask never linked is never
        touched. Returns how many rows it marked (a second call: 0).

        It checks nothing about the ask: the caller holds the transaction and has
        checked, inside it, that the ask may release (release_closed_docs, expire_ask_docs)."""
        with self._write():
            marked = self.conn.execute("UPDATE ask_docs SET released = 1 WHERE item_id = ? AND released = 0",
                                       (item_id,)).rowcount
            self.conn.execute(
                """DELETE FROM docs WHERE sha256 IN (SELECT sha256 FROM ask_docs WHERE item_id = ?)
                   AND sha256 NOT IN (SELECT sha256 FROM ask_docs WHERE released = 0)""",
                (item_id,))
            return marked

    def release_closed_docs(self, item_id: str) -> Optional[int]:
        """_release_docs for a closed ask only, in one transaction: the status check and
        the release share the lock and a BEGIN IMMEDIATE, as amend_tx's open check and
        its write do, so a reopen (Service.reopen, under event_transaction) lands wholly
        before or wholly after. None for an unknown ask; AskNotOpen for an open one,
        and nothing changes."""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
                if row is None:
                    marked = None
                elif row[0] == "open":
                    raise AskNotOpen()
                else:
                    marked = self._release_docs(item_id)    # joins this transaction
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return marked

    def doc_totals(self) -> tuple[int, int]:
        """(how many texts are stored, their bytes in all): `docs status` shows them
        beside docs.MAX_STORE_BYTES."""
        with self._lock:
            count, size = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM docs").fetchone()
            return count, size

    def closed_asks_keeping_docs(self) -> list[str]:
        """The ids of the closed (done or dismissed) asks that still keep a document: the
        sweep's candidates. expire_ask_docs checks each again in its own transaction."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT DISTINCT a.item_id FROM ask_docs a JOIN items i ON i.id = a.item_id
                   WHERE a.released = 0 AND i.status IN ('done', 'dismissed') ORDER BY a.item_id""").fetchall()
            return [row[0] for row in rows]

    def expire_ask_docs(self, item_id: str, cutoff: datetime) -> int:
        """_release_docs for an ask closed (done or dismissed) at or before `cutoff`, in
        one transaction: the check and the release share the lock and a BEGIN IMMEDIATE,
        as release_closed_docs does, so a reopen lands wholly before (the ask is open
        and keeps its documents) or wholly after. The close time is `completed_at`,
        which update_item stamps on every close and clears on a reopen, so it is the
        last close. It is compared as a time (an ISO 8601 stamp; one without an offset
        is read as UTC); an ask without one, or with one that does not parse, is left
        alone. → how many rows it released (0 when the ask is not due)."""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute("SELECT status, completed_at FROM items WHERE id = ?",
                                        (item_id,)).fetchone()
                marked = 0
                if row is not None and row[0] in ("done", "dismissed"):
                    closed = _parse_stamp(row[1])
                    if closed is not None and closed <= cutoff:
                        marked = self._release_docs(item_id)   # joins this transaction
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return marked

    # --- Telegram messages per revision -----------------------------------

    def record_tg_message(self, message_id: int, item_id: str, rev: int) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO tg_messages (message_id, item_id, rev) VALUES (?, ?, ?)",
                              (int(message_id), item_id, rev))
            self.conn.commit()

    def tg_message(self, message_id: int) -> Optional[tuple[str, int]]:
        """(item id, rev) of a recorded Telegram message, or None."""
        with self._lock:
            row = self.conn.execute("SELECT item_id, rev FROM tg_messages WHERE message_id = ?",
                                    (int(message_id),)).fetchone()
            return (row[0], row[1]) if row else None

    def newest_tg_message(self, item_id: str) -> Optional[tuple[int, int]]:
        """(message id, rev) of the ask's newest message: the highest rev, then the newest id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT message_id, rev FROM tg_messages WHERE item_id = ? ORDER BY rev DESC, message_id DESC LIMIT 1",
                (item_id,)).fetchone()
            return (row[0], row[1]) if row else None

    # --- owner keys ---------------------------------------------------------

    _OWNER_COLS = ("id, key_hash, label, status, code, created, expires, approved_at, revoked_at, tg_message_id, "
                   "last_used")

    def _owner_keys_where(self, where: str, args: tuple) -> list[OwnerKey]:
        cursor = self.conn.execute(f"SELECT {self._OWNER_COLS} FROM owner_keys WHERE {where} ORDER BY rowid",
                                   args)
        return [OwnerKey(*row) for row in cursor.fetchall()]

    def insert_enrollment(self, k: OwnerKey) -> None:
        with self._lock:
            self.conn.execute(
                f"INSERT INTO owner_keys ({self._OWNER_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (k.id, k.key_hash, k.label, k.status, k.code, k.created, k.expires, k.approved_at,
                 k.revoked_at, k.tg_message_id, k.last_used),
            )
            self.conn.commit()

    def get_owner_key(self, key_id: str) -> Optional[OwnerKey]:
        with self._lock:
            found = self._owner_keys_where("id = ?", (key_id,))
            return found[0] if found else None

    def owner_key_by_hash(self, key_hash: str) -> Optional[OwnerKey]:
        with self._lock:
            found = self._owner_keys_where("key_hash = ?", (key_hash,))
            return found[0] if found else None

    def owner_keys(self) -> list[OwnerKey]:
        with self._lock:
            return self._owner_keys_where("1 = 1", ())

    def pending_enrollments(self) -> list[OwnerKey]:
        with self._lock:
            return self._owner_keys_where("status = 'pending'", ())

    def set_owner_key_status(self, key_id: str, status: str, *, tg_message_id: Optional[int] = None) -> Optional[OwnerKey]:
        """Set the status (stamping approved_at or revoked_at) and, when given, the message id."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sets, args = ["status = ?"], [status]
            if status == "approved":
                sets.append("approved_at = ?")
                args.append(now)
            elif status == "revoked":
                sets.append("revoked_at = ?")
                args.append(now)
            if tg_message_id is not None:
                sets.append("tg_message_id = ?")
                args.append(tg_message_id)
            self.conn.execute(f"UPDATE owner_keys SET {', '.join(sets)} WHERE id = ?", (*args, key_id))
            self.conn.commit()
            return self.get_owner_key(key_id)

    def stamp_owner_key_used(self, key_id: str, at: str, before: str) -> None:
        """Set last_used to `at` unless it is already `before` or later (one write an hour)."""
        with self._lock:
            self.conn.execute(
                "UPDATE owner_keys SET last_used = ? WHERE id = ? AND (last_used IS NULL OR last_used < ?)",
                (at, key_id, before))
            self.conn.commit()

    # --- revoke requests (Pond 1.11.0) -------------------------------------------

    _REVOKE_COLS = "id, key_id, status, code, created, expires, decided_at, tg_message_id"

    def insert_revoke_request(self, r: RevokeRequest) -> None:
        with self._lock:
            self.conn.execute(
                f"INSERT INTO owner_revoke_requests ({self._REVOKE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (r.id, r.key_id, r.status, r.code, r.created, r.expires, r.decided_at, r.tg_message_id))
            self.conn.commit()

    def get_revoke_request(self, request_id: str) -> Optional[RevokeRequest]:
        with self._lock:
            row = self.conn.execute(f"SELECT {self._REVOKE_COLS} FROM owner_revoke_requests WHERE id = ?",
                                    (request_id,)).fetchone()
            return RevokeRequest(*row) if row else None

    def pending_revoke_requests(self) -> list[RevokeRequest]:
        with self._lock:
            rows = self.conn.execute(f"SELECT {self._REVOKE_COLS} FROM owner_revoke_requests "
                                     "WHERE status = 'pending' ORDER BY rowid").fetchall()
            return [RevokeRequest(*row) for row in rows]

    def set_revoke_request_status(self, request_id: str, status: str) -> Optional[RevokeRequest]:
        """Set the status; any status but pending stamps decided_at."""
        with self._lock:
            decided = None if status == "pending" else datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.conn.execute("UPDATE owner_revoke_requests SET status = ?, decided_at = ? WHERE id = ?",
                              (status, decided, request_id))
            self.conn.commit()
            return self.get_revoke_request(request_id)

    # --- settings ------------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            self.conn.commit()

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self.conn.commit()

    def snapshot(self, dest_path: Path) -> None:
        """Copy the database to dest_path with sqlite's online backup API.

        Taken under the same lock as every other access, so the nightly
        backup thread never touches self.conn concurrently with an HTTP
        request thread (Ruling S7) — callers must use this instead of
        reaching into store.conn. The copy includes writes still in the WAL.
        """
        with self._lock:
            dest = sqlite3.connect(str(dest_path))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()

    def health_check(self) -> bool:
        """Test that the DB is readable and writable."""
        with self._lock:
            try:
                self.conn.execute("SELECT COUNT(*) FROM items")
                # Attempt a write (canary row); rollback so it doesn't persist
                self.conn.execute("INSERT INTO events (item_id, kind, actor, at) VALUES (?, ?, ?, ?)",
                                ("__health__", "health", "daemon", datetime.now(timezone.utc).isoformat(timespec="seconds")))
                self.conn.rollback()
                return True
            except (sqlite3.Error, AttributeError):
                return False
