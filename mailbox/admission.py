"""One-time invites and proof of possession of a per-principal bearer."""
import hmac
import ipaddress
import re
import secrets
import time
import uuid
from urllib.parse import urlencode, urlsplit

from mailbox import owner

INVITE_SECONDS = 900
MAX_OUTSTANDING = 32
INVITES_PER_WINDOW = 64
_COLUMNS = "id, kind, role, label, status, created, last_used AS lastUsed"
KEYS = ("id", "kind", "role", "label", "status", "created", "lastUsed")


class AdmissionError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def https_origin(value):
    """Accept only an HTTPS origin, including an optional explicit port."""
    try:
        if not isinstance(value, str) or not value.isascii() or not value.isprintable() or re.search(r"[\s\\%?#@]", value):
            raise ValueError
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.path not in ("", "/"):
            raise ValueError
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        elif not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
            raise ValueError
        port = url.port
        if port == 0 or url.netloc.endswith(":"):
            raise ValueError
        canonical = f"https://{host}"
        if port not in (None, 443):
            canonical += f":{port}"
        return canonical
    except ValueError:
        raise AdmissionError(400, "a valid HTTPS Pond origin is required") from None


class Admission:
    def __init__(self, store):
        self.store = store

    @property
    def db(self):
        return self.store.conn

    @property
    def mode(self):
        with self.store._lock:
            return self.db.execute("SELECT mode FROM admission_state WHERE singleton=1").fetchone()[0]

    def _mint(self, purpose, kind, origin, issuer, now, replace_id=None):
        token = secrets.token_urlsafe(32)
        expires = now + INVITE_SECONDS
        self.db.execute("""INSERT INTO admission_invites
            (digest,purpose,kind,origin,issued,expires,issuer,replace_id) VALUES (?,?,?,?,?,?,?,?)""",
                        (owner.key_hash(token), purpose, kind, origin, now, expires, issuer, replace_id))
        return {"link": f"hook://join?{urlencode({'v': 2, 'pond': origin})}#{token}", "expires": expires}

    def operator_invite(self, purpose, origin, now=None, *, replace_id=None):
        origin = https_origin(origin)
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            mode, bound, joined = self.db.execute(
                "SELECT mode,origin,bootstrapped FROM admission_state WHERE singleton=1").fetchone()
            if mode != "native" or purpose != ("recovery" if joined else "first"):
                raise AdmissionError(409, "operator invite unavailable in this state")
            if bound and bound != origin:
                raise AdmissionError(409, "Pond origin already set")
            if replace_id is not None:
                if purpose != "recovery" or not self.db.execute("""SELECT 1 FROM principals
                    WHERE id=? AND kind='device' AND status='approved'""", (replace_id,)).fetchone():
                    raise AdmissionError(409, "select an approved device to replace")
            elif purpose == "recovery" and self._devices() >= owner.MAX_KEYS:
                raise AdmissionError(409, "all slots occupied: select a device with --replace ID")
            if purpose == "recovery":
                self.db.execute("UPDATE principals SET status='expired' WHERE status='recovering'")
            self.db.execute("UPDATE admission_state SET origin=? WHERE singleton=1", (origin,))
            self.db.execute("UPDATE admission_invites SET consumed=? WHERE purpose=? AND consumed IS NULL",
                            (now, purpose))
            return self._mint(purpose, "device", origin, None, now, replace_id)

    def invite(self, bearer, kind, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            actor = self._authenticate(bearer, now, approving=True)
            if kind not in ("device", "agent"):
                raise AdmissionError(400, "invalid invite kind")
            issued = self.db.execute("SELECT COUNT(*) FROM admission_invites WHERE issuer=? AND issued>?",
                                     (actor["id"], now - INVITE_SECONDS)).fetchone()[0]
            if issued >= INVITES_PER_WINDOW:
                raise AdmissionError(429, "invite limit reached")
            origin = self.db.execute("SELECT origin FROM admission_state WHERE singleton=1").fetchone()[0]
            return self._mint("join", kind, origin, actor["id"], now)

    def claim(self, invite, key_hash, kind, label, now=None):
        if not isinstance(invite, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", invite):
            raise AdmissionError(410, "invite unavailable")
        if not isinstance(key_hash, str) or not owner.KEY_HASH_RE.fullmatch(key_hash) or owner.check_label(label):
            raise AdmissionError(400, "invalid key hash or label")
        digest = owner.key_hash(invite)
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            row = self.db.execute("""SELECT i.purpose,i.kind,i.expires,s.bootstrapped,i.replace_id FROM admission_invites i
                JOIN admission_state s ON s.origin=i.origin WHERE i.digest=? AND i.consumed IS NULL
                AND i.expires>? AND s.mode IN ('native','migrating')""", (digest, now)).fetchone()
            if not row:
                raise AdmissionError(410, "invite unavailable")
            purpose, expected, expires, joined, replace_id = row
            if kind != expected:
                raise AdmissionError(400, "invite kind mismatch")
            if purpose == "first" and joined:
                raise AdmissionError(410, "invite unavailable")
            if self.db.execute("SELECT 1 FROM principals WHERE key_hash=?", (key_hash,)).fetchone():
                raise AdmissionError(409, "credential already registered")
            pending = purpose == "join"
            if pending:
                count = self.db.execute("SELECT COUNT(*) FROM principals WHERE status='pending' AND expires>?",
                                        (now,)).fetchone()[0]
                if count >= MAX_OUTSTANDING:
                    raise AdmissionError(429, "pending limit reached")
            elif not replace_id:
                self._device_slot()
            pid = str(uuid.uuid4())
            role = ("owner" if kind == "device" else "agent") if pending else "admin"
            state = "pending" if pending else "approved"
            if replace_id:
                state = "recovering"
            until = min(expires, now + owner.ENROLL_TTL_S)
            self.db.execute("""INSERT INTO principals
                (id,key_hash,kind,role,label,status,created,expires,invite_digest) VALUES (?,?,?,?,?,?,?,?,?)""",
                            (pid, key_hash, kind, role, label, state, now, until, digest))
            self.db.execute("UPDATE admission_invites SET consumed=? WHERE digest=?", (now, digest))
            if purpose == "first":
                self.db.execute("UPDATE admission_state SET bootstrapped=1 WHERE singleton=1")
            if pending or replace_id:
                return {"id": pid, "code": owner.enroll_code(pid, key_hash), "status": "pending", "expires": until}
            return {"id": pid, "role": role, "status": state}

    def _device_slot(self):
        if self._devices() >= owner.MAX_KEYS:
            raise AdmissionError(409, "device limit reached")

    def operator_devices(self):
        with self.store._lock:
            return [dict(zip(KEYS, row)) for row in self.db.execute(
                f"SELECT {_COLUMNS} FROM principals WHERE kind='device' AND status='approved' ORDER BY id")]

    def _devices(self):
        return self.db.execute("SELECT COUNT(*) FROM principals WHERE kind='device' AND status='approved'").fetchone()[0]

    def _authenticate(self, bearer, now, *, approving=False, allow_pending=False):
        if not bearer or not isinstance(bearer, str):
            raise AdmissionError(401, "unauthorized")
        digest = owner.key_hash(bearer)
        row = self.db.execute(f"SELECT {_COLUMNS}, key_hash, expires FROM principals WHERE key_hash=?",
                              (digest,)).fetchone()
        if not row or not hmac.compare_digest(row[-2], digest):
            raise AdmissionError(401, "unauthorized")
        principal = dict(zip(KEYS, row))
        if allow_pending and principal["status"] == "recovering" and now < row[-1]:
            # The first successful /me proves the replacement holds the secret.
            # Exchange the operator-selected slot and activate the replacement
            # in this same transaction. The original remains usable until here.
            lost = self.db.execute("""SELECT i.replace_id FROM admission_invites i JOIN principals p
                ON p.invite_digest=i.digest WHERE p.id=?""", (principal["id"],)).fetchone()[0]
            changed = self.db.execute("""UPDATE principals SET status='revoked', revoked_by=?
                WHERE id=? AND kind='device' AND status='approved'""", (principal["id"], lost)).rowcount
            if not changed:
                raise AdmissionError(409, "replacement target no longer active")
            self.db.execute("UPDATE admission_invites SET consumed=? WHERE issuer=? AND consumed IS NULL", (now, lost))
            self.db.execute("UPDATE principals SET status='approved' WHERE id=?", (principal["id"],))
            principal["status"] = "approved"
        if principal["status"] == "pending" and now < row[-1]:
            if not allow_pending:
                raise AdmissionError(403, "approval required")
            principal.update(expires=row[-1], code=owner.enroll_code(principal["id"], digest))
        elif principal["status"] != "approved":
            raise AdmissionError(401, "unauthorized")
        if approving and principal["kind"] != "device":
            raise AdmissionError(403, "approved device required")
        if principal["status"] == "approved" and (
                principal["lastUsed"] is None or principal["lastUsed"] <= now - owner.LAST_USED_EVERY_S):
            self.db.execute("UPDATE principals SET last_used=? WHERE id=?", (now, principal["id"]))
            principal["lastUsed"] = now
        return principal

    def authenticate(self, bearer, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            return self._authenticate(bearer, now)

    def me(self, bearer, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            return self._authenticate(bearer, now, allow_pending=True)

    def pending(self, bearer, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            self._authenticate(bearer, now, approving=True)
            rows = self.db.execute("""SELECT id,label,kind,key_hash,expires FROM principals
                WHERE status='pending' AND expires>? ORDER BY created,id""", (now,))
            return [{"id": pid, "kind": kind, "label": label, "code": owner.enroll_code(pid, digest), "expires": end}
                    for pid, label, kind, digest, end in rows]

    def decide(self, bearer, pid, decision, code, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            actor = self._authenticate(bearer, now, approving=True)
            row = self.db.execute("SELECT key_hash,status,expires,kind FROM principals WHERE id=?", (pid,)).fetchone()
            if not row:
                raise AdmissionError(404, "unknown request")
            digest, state, expires, kind = row
            if state != "pending":
                raise AdmissionError(409, "request already decided")
            if expires <= now:
                raise AdmissionError(410, "request expired")
            if decision not in ("approve", "reject"):
                raise AdmissionError(400, "invalid decision")
            if not isinstance(code, str) or not code.isascii() or not hmac.compare_digest(code, owner.enroll_code(pid, digest)):
                raise AdmissionError(403, "incorrect code")
            if decision == "approve" and kind == "device":
                self._device_slot()
            state = "approved" if decision == "approve" else "rejected"
            self.db.execute("UPDATE principals SET status=?, approved_by=? WHERE id=?", (state, actor["id"], pid))
            return {"id": pid, "status": state}

    def principals(self, bearer, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            self._authenticate(bearer, now, approving=True)
            rows = self.db.execute(f"SELECT {_COLUMNS}, expires FROM principals ORDER BY created,id")
            items = []
            for row in rows:
                item = dict(zip(KEYS, row))
                if item["status"] == "recovering":
                    item["status"] = "pending"
                if item["status"] == "pending" and row[-1] <= now:
                    item["status"] = "expired"
                items.append(item)
            return {"maxDevices": owner.MAX_KEYS, "principals": items}

    def revoke(self, bearer, pid, now=None):
        with self.store.event_transaction():
            now = int(time.time()) if now is None else now
            actor = self._authenticate(bearer, now, approving=True)
            row = self.db.execute("SELECT kind,status FROM principals WHERE id=?", (pid,)).fetchone()
            if not row:
                raise AdmissionError(404, "unknown principal")
            if row[1] == "revoked":
                return {"id": pid, "status": "revoked"}
            if row == ("device", "approved"):
                other = self.db.execute("""SELECT 1 FROM principals WHERE id<>? AND kind='device'
                    AND status='approved' AND last_used IS NOT NULL LIMIT 1""", (pid,)).fetchone()
                if not other:
                    raise AdmissionError(409, "another approving device must verify its key first")
            self.db.execute("UPDATE principals SET status='revoked', revoked_by=? WHERE id=?", (actor["id"], pid))
            self.db.execute("UPDATE admission_invites SET consumed=? WHERE issuer=? AND consumed IS NULL", (now, pid))
            return {"id": pid, "status": "revoked"}
