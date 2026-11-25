"""The additive migrations. Schema 1 (Pond 1.1.0): side tables for revisions,
edit keys, accepts, event metadata, Telegram messages, owner keys and settings.
Schema 2 (Pond 1.2.0): the bugs table, one row per Hook bug. Schema 3 (Pond
1.9.0): revision_meta, the kind of each revision from 1.9.0 on. Schema 4 (Pond
1.10.0): docs and ask_docs, the checked copy of a Markdown document an ask carries.
Schema 5 (Pond 1.11.0): owner_keys.last_used and the owner_revoke_requests table.

Hard rule: no column is ever added to `items` or `events`. Pond 1.0.0 and 1.1.0
code build Item(**cols) and Event(**cols) from SELECT *, so an added column
would crash them after a rollback. New data lives in side tables only, which
older code never reads; 1.1.0's own migration is gated by user_version < 1, so
it leaves a schema 2 database alone.

Each schema's DDL runs once, gated by PRAGMA user_version. The backfill runs on
every open: an ask that older code posted during a rollback window gets its
revision 1 and its Telegram message row the next time this code starts.
"""
import json
import logging
import sqlite3

from mailbox.revisions import assign_ids, canonical, revision_hash

logger = logging.getLogger("mailbox")

SCHEMA_VERSION = 5

_DDL_1 = (
    """CREATE TABLE IF NOT EXISTS revisions (item_id TEXT NOT NULL REFERENCES items(id), rev INTEGER NOT NULL,
         hash TEXT NOT NULL, prev TEXT NOT NULL, at TEXT NOT NULL, reason TEXT NOT NULL, body TEXT NOT NULL,
         PRIMARY KEY (item_id, rev))""",
    """CREATE TRIGGER IF NOT EXISTS revisions_no_update BEFORE UPDATE ON revisions
         BEGIN SELECT RAISE(ABORT, 'revisions are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS revisions_no_delete BEFORE DELETE ON revisions
         BEGIN SELECT RAISE(ABORT, 'revisions are append-only'); END""",
    """CREATE TABLE IF NOT EXISTS edit_keys (item_id TEXT PRIMARY KEY REFERENCES items(id),
         key_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS accepts (item_id TEXT NOT NULL, rev INTEGER NOT NULL, hash TEXT NOT NULL,
         at TEXT NOT NULL, auth TEXT NOT NULL, PRIMARY KEY (item_id, rev))""",
    "CREATE TABLE IF NOT EXISTS event_meta (seq INTEGER PRIMARY KEY, rev INTEGER, auth TEXT)",
    """CREATE TABLE IF NOT EXISTS tg_messages (message_id INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
         rev INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS owner_keys (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE,
         label TEXT NOT NULL, status TEXT NOT NULL, code TEXT NOT NULL, created TEXT NOT NULL,
         expires TEXT NOT NULL, approved_at TEXT, revoked_at TEXT, tg_message_id INTEGER)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

# Schema 2: a Hook bug is an ordinary ask plus this row. `count` is how many
# times its signature was filed while it was open; `excerpt` is the newest report.
_DDL_2 = (
    """CREATE TABLE IF NOT EXISTS bugs (item_id TEXT PRIMARY KEY REFERENCES items(id),
         signature TEXT NOT NULL, count INTEGER NOT NULL, first_seen TEXT NOT NULL,
         last_seen TEXT NOT NULL, excerpt TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS bugs_signature ON bugs(signature)",
)

# Schema 3: the kind of a revision, "append" or "correction" (Pond 1.9.0). No
# backfill: a revision 2 or later without a row is a correction, as every one
# was before 1.9.0. Older code never reads this table.
_DDL_3 = (
    """CREATE TABLE IF NOT EXISTS revision_meta (item_id TEXT NOT NULL, rev INTEGER NOT NULL,
         kind TEXT NOT NULL, PRIMARY KEY (item_id, rev))""",
)

# Schema 4: documents (Pond 1.10.0). `docs` holds each text once, by its sha256,
# as the exact bytes posted. `ask_docs` is every hash an ask ever linked; release
# marks the ask's rows and deletes a text no unreleased row links any more. No
# backfill. Older code never reads these tables.
_DDL_4 = (
    """CREATE TABLE IF NOT EXISTS docs (sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, body BLOB NOT NULL,
         at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ask_docs (item_id TEXT NOT NULL, sha256 TEXT NOT NULL,
         released INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (item_id, sha256))""",
)

# Schema 5: when each owner key last authenticated a request, and the revoke
# requests the owner confirms on Telegram (Pond 1.11.0). owner_keys is a side
# table read by column name (never SELECT *), so an added column is safe for older
# code. A database at 3 or 4 runs this step.
def _add_last_used(conn: sqlite3.Connection) -> None:
    """ALTER TABLE has no IF NOT EXISTS: add the column only when it is missing."""
    if "last_used" not in {row[1] for row in conn.execute("PRAGMA table_info(owner_keys)")}:
        conn.execute("ALTER TABLE owner_keys ADD COLUMN last_used TEXT")


_DDL_5 = (
    _add_last_used,
    """CREATE TABLE IF NOT EXISTS owner_revoke_requests (id TEXT PRIMARY KEY, key_id TEXT NOT NULL,
         status TEXT NOT NULL, code TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL,
         decided_at TEXT, tg_message_id INTEGER)""",
)

# (version, its DDL), in order: a database at user_version N runs every step above N.
_STEPS = ((1, _DDL_1), (2, _DDL_2), (3, _DDL_3), (4, _DDL_4), (5, _DDL_5))


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the database to SCHEMA_VERSION, then backfill. Idempotent."""
    if conn.in_transaction:
        conn.commit()
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Read again under the write lock: another process may have migrated meanwhile.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for step, statements in _STEPS:
                if version < step:
                    for statement in statements:
                        if callable(statement):
                            statement(conn)
                        else:
                            conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {max(version, SCHEMA_VERSION)}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    _backfill(conn)


def _backfill(conn: sqlite3.Connection) -> None:
    """Revision 1 for every instructable ask without one, and a tg_messages row
    (rev 1) for every message id an ask's payload names. INSERT OR IGNORE, so a
    second run changes nothing."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT i.id, i.created, i.payload, r.rev FROM items i
               LEFT JOIN revisions r ON r.item_id = i.id AND r.rev = 1"""
        ).fetchall()
        for item_id, created, raw, rev in rows:
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            instructable = payload.get("instructable")
            if rev is None and isinstance(instructable, dict):
                with_ids, errors = assign_ids(instructable)
                if errors:
                    # Cannot come from a validated post; never guessed at.
                    logger.warning("[mailbox] migrate: ask %s has no usable step ids", item_id)
                else:
                    body = canonical(with_ids)
                    conn.execute(
                        """INSERT OR IGNORE INTO revisions (item_id, rev, hash, prev, at, reason, body)
                           VALUES (?, 1, ?, '', ?, '', ?)""",
                        (item_id, revision_hash(1, "", created, "", body), created, body),
                    )
            message_id = payload.get("tg_message_id")
            if isinstance(message_id, int) and not isinstance(message_id, bool):
                conn.execute("INSERT OR IGNORE INTO tg_messages (message_id, item_id, rev) VALUES (?, ?, 1)",
                             (message_id, item_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
