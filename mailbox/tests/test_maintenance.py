"""Exercise the offline recovery commands against SQLite files, including WAL."""
import json
import sqlite3
from pathlib import Path

import pytest

from mailbox import admin


def prepare(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    monkeypatch.setenv("MAILBOX_DATA_DIR", str(data))
    monkeypatch.setenv("MAILBOX_BACKUP_DIR", str(backups))
    db = sqlite3.connect(data / "mailbox.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA user_version=6")
    db.execute("CREATE TABLE principals(id TEXT PRIMARY KEY, status TEXT)")
    db.execute("INSERT INTO principals VALUES ('test-device','revoked')")
    db.commit()
    (data / "mailbox-token").write_text("synthetic credential")
    return data, backups, db


OP = "7d8b03510f1e4c6399ebdb312d5400ba"


def test_snapshot_includes_committed_wal_and_credentials_and_only_restores_into_empty_volume(tmp_path, monkeypatch):
    source, backups, db = prepare(tmp_path, monkeypatch)
    try:
        assert admin.main(["maintenance", "snapshot", OP]) == 0
        copy = backups / OP / "data"
        assert (copy / "mailbox.db").stat().st_mode & 0o777 == 0o600
        assert (copy / "mailbox-token").read_text() == "synthetic credential"
        assert (backups / OP).stat().st_mode & 0o777 == 0o700
        assert admin.main(["maintenance", "restore", OP]) == 1
        destination = tmp_path / "candidate"
        destination.mkdir()
        monkeypatch.setenv("MAILBOX_DATA_DIR", str(destination))
        assert admin.main(["maintenance", "restore", OP]) == 0
        with sqlite3.connect(destination / "mailbox.db") as restored:
            assert restored.execute("SELECT status FROM principals").fetchone() == ("revoked",)
            assert restored.execute("PRAGMA user_version").fetchone() == (6,)
    finally:
        db.close()


def test_snapshot_rejects_bad_foreign_keys_without_publishing(tmp_path, monkeypatch):
    _, backups, db = prepare(tmp_path, monkeypatch)
    db.execute("CREATE TABLE grants(device TEXT REFERENCES principals(id))")
    db.execute("INSERT INTO grants VALUES ('missing')")
    db.commit()
    db.close()
    assert admin.main(["maintenance", "snapshot", OP]) == 1
    assert not (backups / OP).exists()


def test_corrupt_snapshot_never_restores_or_overwrites_prior_snapshot(tmp_path, monkeypatch):
    _, backups, db = prepare(tmp_path, monkeypatch)
    db.close()
    assert admin.main(["maintenance", "snapshot", OP]) == 0
    assert admin.main(["maintenance", "check", OP]) == 0
    (backups / OP / "data" / "mailbox-token").write_text("changed")
    assert admin.main(["maintenance", "check", OP]) == 1
    assert admin.main(["maintenance", "snapshot", OP]) == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("MAILBOX_DATA_DIR", str(empty))
    assert admin.main(["maintenance", "restore", OP]) == 1
    assert not list(empty.iterdir())


def test_refuses_linked_files_and_missing_database(tmp_path, monkeypatch):
    data, backups, db = prepare(tmp_path, monkeypatch)
    db.close()
    (data / "other").symlink_to(data / "mailbox-token")
    assert admin.main(["maintenance", "snapshot", OP]) == 1
    assert not (backups / OP).exists()
    (data / "other").unlink()
    (data / "mailbox.db").unlink()
    assert admin.main(["maintenance", "snapshot", OP]) == 1
    assert not (data / "mailbox.db").exists()

def test_managed_status_reads_real_version_and_a_one_use_credential_only_works_there(tmp_path, monkeypatch, capsys):
    from mailbox.tests.test_server import _running, call
    from mailbox import maintenance
    monkeypatch.setenv("POND_INSTALL_ID", OP)
    monkeypatch.setenv("MAILBOX_DATA_DIR", str(tmp_path / "data"))
    running = _running(tmp_path, native=True)
    srv, base, _ = next(running)
    monkeypatch.setenv("MAILBOX_PORT", str(srv.port))
    try:
        assert admin.main(["status", "--json"]) == 0
        reply = json.loads(capsys.readouterr().out)
        assert reply["installation"] == OP
        assert reply["version"]["service"] == "pond"
        assert reply["admission"] == "native"
        assert reply["claimed"] is False
        ticket = maintenance.status_ticket()
        assert call(base, "/asks", token=ticket)[0] == 401
        assert call(base, "/version", token=ticket)[0] == 200
        assert call(base, "/version", token=ticket)[0] == 401
    finally:
        running.close()


def test_fenced_candidate_exposes_only_probes_and_has_no_background_workers(tmp_path, monkeypatch):
    import threading
    from mailbox.server import MailboxServer
    from mailbox.tests.test_server import call
    monkeypatch.setenv("POND_INSTALL_ID", OP)
    monkeypatch.setenv("POND_MAINTENANCE", "1")
    srv = MailboxServer(tmp_path / "data", tmp_path / "backup", "synthetic", 1,
                        "personal", port=0, host="127.0.0.1")
    thread = threading.Thread(target=srv.start)
    thread.start()
    try:
        assert srv.http_server.serving.wait(5)
        base = "http://127.0.0.1:" + str(srv.http_server.server_port)
        assert call(base, "/health")[1]["installation"] == OP
        assert call(base, "/asks")[0] == 503
        assert call(base, "/admission/claim", method="POST", body={})[0] == 503
        assert srv.backup_thread is None and srv.poll_thread is None
    finally:
        srv.stop()
        thread.join(5)

def test_candidate_probe_exercises_data_and_checks_bot_without_polling(tmp_path, monkeypatch):
    from mailbox import maintenance
    from mailbox.store import Store
    data = tmp_path / "data"
    data.mkdir()
    store = Store(data / "mailbox.db")
    store.open()
    store.close()
    methods = []
    def fake_call(self, method, **kwargs):
        methods.append(method)
        return {"is_bot": True} if method == "getMe" else {"url": ""}
    monkeypatch.setattr("mailbox.telegram.TelegramClient._call", fake_call)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("MAILBOX_OWNER_ID", "1")
    result = maintenance.probe(data)
    assert result["counts"]["items"] == 0
    assert methods == ["getMe", "getWebhookInfo"]
    with sqlite3.connect(data / "mailbox.db") as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='pond_probe'").fetchone()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    methods.clear()
    maintenance.probe(data)
    assert not methods
