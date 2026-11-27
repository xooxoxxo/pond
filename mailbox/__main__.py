"""Mailbox entrypoint: python3 -m mailbox."""
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from mailbox.ask_about import load_api_key
from mailbox.household import parse_members
from mailbox.server import MailboxServer


def load_config(environ: Mapping[str, str]) -> dict[str, Any]:
    """MailboxServer keyword arguments read from the environment.

    Raises ValueError carrying the operator-facing message when a value is
    missing or unusable, so main() can turn it into a one-line exit and the
    parsing stays testable without an environment or a server.
    """
    telegram_token = (environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    owner_id_str = (environ.get("MAILBOX_OWNER_ID") or "").strip()
    if owner_id_str and not telegram_token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    if telegram_token and not owner_id_str:
        raise ValueError("MAILBOX_OWNER_ID not set")
    try:
        owner_id = int(owner_id_str) if owner_id_str else None
    except ValueError:
        raise ValueError("MAILBOX_OWNER_ID must be an integer") from None

    try:
        port = int(environ.get("MAILBOX_PORT", "8790"))
    except ValueError:
        raise ValueError("MAILBOX_PORT must be 1-65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("MAILBOX_PORT must be 1-65535")

    # Optional: Relay's loopback page base, e.g. http://127.0.0.1:47823/i.
    # Unset keeps today's keyboard and response exactly.
    relay_open_base = (environ.get("MAILBOX_RELAY_OPEN_BASE") or "").strip().rstrip("/")
    if relay_open_base and not relay_open_base.startswith(("http://", "https://")):
        raise ValueError("MAILBOX_RELAY_OPEN_BASE must be an http(s) URL")

    telegram_mode = environ.get("MAILBOX_TELEGRAM_MODE", "full").strip()
    if telegram_mode not in ("full", "alert"):
        raise ValueError("MAILBOX_TELEGRAM_MODE must be full or alert")

    # Optional: capture into Central Time (both keys or neither) and the
    # notify token core presents on POST /notify. Neither a half pair nor a
    # malformed value ever stops the daemon (ruling S125): it is every
    # project's ask channel under restart: unless-stopped, and an .env written
    # in two owner steps must never crash-loop it. The feature is disabled with
    # one warning naming the key; the value is never printed.
    warn = logging.getLogger("mailbox").warning
    ct_url = (environ.get("MAILBOX_CT_URL") or "").strip().rstrip("/")
    ct_token = (environ.get("MAILBOX_CT_TOKEN") or "").strip()
    if bool(ct_url) != bool(ct_token):
        warn("[mailbox] MAILBOX_CT_URL and MAILBOX_CT_TOKEN must be set together; capture disabled")
        ct_url, ct_token = "", ""
    elif ct_url and not ct_url.startswith(("http://", "https://")):
        warn("[mailbox] MAILBOX_CT_URL is malformed; capture disabled")
        ct_url, ct_token = "", ""
    elif ct_token and not re.fullmatch(r"ct_[0-9a-f]{48}", ct_token):
        warn("[mailbox] MAILBOX_CT_TOKEN is malformed; capture disabled")
        ct_url, ct_token = "", ""
    notify_token = (environ.get("MAILBOX_NOTIFY_TOKEN") or "").strip()
    if notify_token and not re.fullmatch(r"[0-9a-f]{64}", notify_token):
        warn("[mailbox] MAILBOX_NOTIFY_TOKEN is malformed; notify disabled")
        notify_token = ""

    # Optional: MAILBOX_BRIEF_ASKS=0 drops the "Open asks: N" line from the morning brief.
    # Unset (or 1) keeps today's brief exactly; a malformed value warns and keeps it (S125: never exit).
    brief_asks_raw = (environ.get("MAILBOX_BRIEF_ASKS") or "").strip()
    if brief_asks_raw not in ("", "0", "1"):
        warn("[mailbox] MAILBOX_BRIEF_ASKS must be 0 or 1; open asks stay on the brief")
        brief_asks_raw = ""

    # Household mode: members' private chats only, no asks. A malformed value exits: only the household
    # instance sets this key, never the ask channel (S125 is about the asks instance).
    members_raw = (environ.get("MAILBOX_MEMBERS") or "").strip()
    if members_raw and owner_id is None:
        raise ValueError("MAILBOX_MEMBERS needs TELEGRAM_BOT_TOKEN and MAILBOX_OWNER_ID")
    members = parse_members(members_raw, owner_id) if members_raw else None
    if members is not None and not (ct_url and ct_token):
        # A household daemon has no other purpose than Central Time; without it every member text would be
        # dropped in silence.
        raise ValueError("MAILBOX_MEMBERS needs MAILBOX_CT_URL and MAILBOX_CT_TOKEN")

    config = {
        "data_dir": Path(environ.get("MAILBOX_DATA_DIR", "/data")),
        "backup_dir": Path(environ.get("MAILBOX_BACKUP_DIR", "/backups")),
        "telegram_token": telegram_token,
        "owner_id": owner_id,
        "context": environ.get("MAILBOX_CONTEXT", "personal"),
        "port": port,
        "relay_open_base": relay_open_base or None,
        "ct_url": ct_url or None,
        "ct_token": ct_token or None,
        "notify_token": notify_token or None,
    }
    if telegram_mode != "full":
        config["telegram_mode"] = telegram_mode
    if brief_asks_raw == "0":
        config["brief_asks"] = False
    if members is not None:
        config["members"] = members
    # Optional (Pond 1.14.0): the Anthropic API key Ask about this answers with, from
    # ANTHROPIC_API_KEY or the file ANTHROPIC_API_KEY_FILE names. Unset or unreadable:
    # the feature is off, with one warning naming the variable, never the value (S125).
    api_key = load_api_key(environ)
    if api_key is not None:
        config["anthropic_api_key"] = api_key
    return config


def main() -> None:
    """Parse env, start the daemon."""
    # First statement, before logging and before any file is created: every
    # file the daemon makes (mailbox.db and its -wal/-shm, the mailbox token,
    # snapshots) is then born 600 (Ruling S6).
    os.umask(0o077)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_config(os.environ)
        server = MailboxServer(**config)
    except ValueError as exc:
        sys.exit(str(exc))

    # Never the bot token, never the mailbox token — just where it lives.
    logging.getLogger("mailbox").info(
        "[mailbox] context=%s data=%s backups=%s",
        config["context"], config["data_dir"], config["backup_dir"],
    )
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
