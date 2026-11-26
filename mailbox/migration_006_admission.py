"""Admission state lives beside the legacy tables so old readers can reopen the file."""
DDL = (
    """CREATE TABLE IF NOT EXISTS admission_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        mode TEXT NOT NULL, origin TEXT, bootstrapped INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS admission_invites (
        digest TEXT PRIMARY KEY NOT NULL, purpose TEXT NOT NULL, kind TEXT NOT NULL,
        origin TEXT NOT NULL, issued INTEGER NOT NULL, expires INTEGER NOT NULL,
        issuer TEXT, consumed INTEGER, replace_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS principals (
        id TEXT PRIMARY KEY NOT NULL, key_hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
        kind TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
        expires INTEGER NOT NULL, last_used INTEGER, invite_digest TEXT UNIQUE,
        approved_by TEXT, revoked_by TEXT)""",
    """CREATE TABLE IF NOT EXISTS ask_ownership (
        item_id TEXT PRIMARY KEY REFERENCES items(id), principal_id TEXT NOT NULL REFERENCES principals(id))""",
    "CREATE INDEX IF NOT EXISTS ask_ownership_principal ON ask_ownership(principal_id)",
)
