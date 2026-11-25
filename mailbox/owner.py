"""The owner key (Pond 1.1.0): each Mac's Hook holds a secret in its Keychain
and sends it as X-Hook-Owner-Key on owner acts. The Pond stores only its
SHA-256, and only after the owner approved that Mac on Telegram.

Pure helpers and constants; the flows live in Service, the check in the
server's _owner().
"""
import hashlib
import re
from typing import Any

from mailbox.revisions import check_note

KEY_HEADER = "X-Hook-Owner-Key"
ENROLL_TTL_S = 600
OFF_TTL_S = 600
MAX_KEYS = 10
REVOKE_TTL_S = 600
# lastUsed is stamped at most this often per key (Pond 1.11.0), so an act is not a write.
LAST_USED_EVERY_S = 3600
MAX_LABEL = 40

SETTING_REQUIRED = "owner_key_required"
SETTING_OFF_REQUEST = "owner_key_off_request"
SETTING_OFF_EXPIRES = "owner_key_off_expires"

KEY_HASH_RE = re.compile(r"[0-9a-f]{64}")


def key_hash(secret: str) -> str:
    """Lowercase hex SHA-256 of the key as sent."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def enroll_code(enroll_id: str, key_hash: str) -> str:
    """The 6-digit code Hook shows and the Telegram request repeats."""
    digest = hashlib.sha256(("hook-enroll-v1\n" + enroll_id + "\n" + key_hash).encode("utf-8")).hexdigest()
    return f"{int(digest[:12], 16) % 1_000_000:06d}"


def revoke_code(request_id: str, key_id: str) -> str:
    """The 6-digit code of a revoke request (Pond 1.11.0): Hook shows it, Telegram repeats it."""
    digest = hashlib.sha256(("hook-revoke-v1\n" + request_id + "\n" + key_id).encode("utf-8")).hexdigest()
    return f"{int(digest[:12], 16) % 1_000_000:06d}"


def check_label(label: Any) -> list[str]:
    """1 to 40 characters, one line, no hidden characters, nothing secret-like."""
    return check_note("label", label, MAX_LABEL)
