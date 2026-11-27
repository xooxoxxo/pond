"""Offline data operations for a stopped, managed Pond and its disposable candidate."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import secrets
import time
import urllib.request
import urllib.error
import tempfile
from contextlib import closing
from pathlib import Path

DATABASE = "mailbox.db"
SIDECARS = {DATABASE + suffix for suffix in ("-journal", "-shm", "-wal")}


def sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def files(directory):
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode):
            if not stat.S_ISREG(mode):
                raise ValueError("Unexpected linked or special file in the recovery data")
            yield path.relative_to(directory)


def hashes(directory):
    result = {}
    for name in files(directory):
        with (directory / name).open("rb") as stream:
            result[str(name)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def database_info(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database failed integrity_check")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Database failed foreign_key_check")
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                  for name in ("items", "events", "principals", "admission_invites", "ask_ownership")
                  if name in tables}
        logical = hashlib.sha256()
        for line in db.iterdump():
            logical.update(line.encode() + b"\x00")
    return {"schema": schema, "counts": counts, "contents": logical.hexdigest()}


def copy_private(source, dest):
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as src, dest.open("xb") as out:
        os.fchmod(out.fileno(), 0o600)
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())


def snapshot(data, backups, op):
    final = backups / op
    if final.exists():
        raise ValueError("Snapshot already exists")
    if not (data / DATABASE).is_file():
        raise ValueError("The source volume has no Pond database")
    tmp = Path(tempfile.mkdtemp(dir=backups))
    try:
        dest = tmp / "data"
        dest.mkdir(mode=0o700)
        for name in files(data):
            if str(name) not in SIDECARS and str(name) != DATABASE:
                copy_private(data / name, dest / name)
        with closing(sqlite3.connect((data / DATABASE).as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(dest / DATABASE)) as backup:
                source.backup(backup)
                backup.execute("PRAGMA journal_mode=DELETE")
        (dest / DATABASE).chmod(0o600)
        manifest = {"database": database_info(dest / DATABASE), "files": hashes(dest)}
        record = tmp / "manifest.json"
        record.write_text(json.dumps(manifest, sort_keys=True))
        record.chmod(0o600)
        sync(dest / DATABASE)
        for directory in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
            sync(directory)
        for path in (dest, record, tmp):
            sync(path)
        tmp.rename(final)
        sync(backups)
        return manifest["database"]
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def check(backups, op):
    saved = backups / op
    source = saved / "data"
    record = json.loads((saved / "manifest.json").read_text())
    if hashes(source) != record["files"] or database_info(source / DATABASE) != record["database"]:
        raise ValueError("Recovery materials failed verification")
    return record["database"]


def restore(data, backups, op):
    if any(data.iterdir()):
        raise ValueError("A recovery copy needs an empty destination volume")
    check(backups, op)
    source = backups / op / "data"
    for name in files(source):
        copy_private(source / name, data / name)
    sync(data)
    return database_info(data / DATABASE)


def command(args, data, backups):
    if args == ["probe"] and os.environ.get("POND_MAINTENANCE") == "1":
        return probe(data)
    if len(args) != 2 or args[0] not in ("snapshot", "restore", "check") or not re.fullmatch("[0-9a-f]{32}", args[1]):
        raise ValueError("Expected maintenance snapshot|restore OPERATION_ID")
    if args[0] == "check":
        return check(backups, args[1])
    fn = snapshot if args[0] == "snapshot" else restore
    return fn(data, backups, args[1])

def ticket_directory():
    installation = os.environ.get("POND_INSTALL_ID", "")
    if not re.fullmatch("[0-9a-f]{32}", installation):
        raise ValueError("Managed installation ID is missing")
    return Path(tempfile.gettempdir()) / f"pond-version-{os.getuid()}-{installation}"


def ticket_path(token):
    return ticket_directory() / hashlib.sha256(token.encode("ascii")).hexdigest()


def status_ticket():
    directory = ticket_directory()
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Invalid status ticket directory")
    token = "pond-status-" + secrets.token_hex(32)
    fd = os.open(ticket_path(token), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return token


def consume_ticket(peer, token):
    if peer != "127.0.0.1" or not token or not re.fullmatch("pond-status-[0-9a-f]{64}", token):
        return False
    try:
        path = ticket_path(token)
        info = path.lstat()
        path.unlink()  # Only one concurrent consumer can succeed.
        return stat.S_ISREG(info.st_mode) and 0 <= time.time() - info.st_mtime < 30
    except (FileNotFoundError, ValueError):
        return False


def status(data):
    port = int(os.environ.get("MAILBOX_PORT", "8790"))
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    token = status_ticket()
    try:
        req = urllib.request.Request(base + "/version", headers={"Authorization": "Bearer " + token})
        with opener.open(req, timeout=5) as response:
            version = json.load(response)
    finally:
        ticket_path(token).unlink(missing_ok=True)
    try:
        response = opener.open(base + "/health", timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            raise
        response = exc
    with response:
        health = json.load(response)
    with closing(sqlite3.connect((data / DATABASE).as_uri() + "?mode=ro", uri=True)) as db:
        mode, claimed = db.execute("SELECT mode,bootstrapped FROM admission_state WHERE singleton=1").fetchone()
    return {"version": version, "health": health, "installation": health.get("installation"),
            "admission": mode, "claimed": bool(claimed), "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN"))}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def probe(data):
    path = data / DATABASE
    info = database_info(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("SELECT id FROM items LIMIT 1").fetchone()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("CREATE TABLE pond_probe(value TEXT NOT NULL)")
            db.execute("INSERT INTO pond_probe VALUES ('candidate')")
            if db.execute("SELECT value FROM pond_probe").fetchone() != ("candidate",):
                raise ValueError("Candidate transaction failed")
        finally:
            db.rollback()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        from mailbox.telegram import TelegramClient, TelegramError
        client = TelegramClient(token, int(os.environ["MAILBOX_OWNER_ID"]))
        try:
            if not client._call("getMe").get("is_bot") or client._call("getWebhookInfo").get("url") != "":
                raise ValueError("Configured bot is unavailable for polling")
        except TelegramError:
            raise ValueError("Telegram readiness check failed") from None
    return info
