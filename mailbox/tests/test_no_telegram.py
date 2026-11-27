"""A native Pond can serve an agent and owner with only loopback networking."""
from contextlib import contextmanager
import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from mailbox import owner
from mailbox.__main__ import load_config
from mailbox.server import MailboxServer
from mailbox.tests.test_admission import mint
from mailbox.tests.test_server import call, legacy_database, post_ask


@contextmanager
def running(tmp_path):
    config = load_config({"MAILBOX_NOTIFY_TOKEN": "a" * 64, "MAILBOX_DATA_DIR": str(tmp_path / "data"),
                          "MAILBOX_BACKUP_DIR": str(tmp_path / "backup")})
    server = MailboxServer(**{**config, "port": 0}, host="127.0.0.1")
    thread = threading.Thread(target=server.start)
    thread.start()
    try:
        assert server.http_server.serving.wait(5)
        assert server.poll_thread is None
        yield server, f"http://127.0.0.1:{server.http_server.server_port}"
    finally:
        server.stop()
        thread.join(5)
        assert not thread.is_alive()


def enroll(url, key, kind, first=None):
    invite = first
    if invite is None:
        status, data = call(url, "/admission/invites", token="desktop", method="POST", body={"kind": kind})
        assert status == 201
        invite = urlsplit(data["link"]).fragment
    status, candidate = call(url, "/admission/claim", method="POST",
                             body={"kind": kind, "label": key, "invite": invite, "keyHash": owner.key_hash(key)})
    assert status == 201
    if first is None:
        assert call(url, f"/admission/requests/{candidate['id']}/decision", token="desktop", method="POST",
                    body={"decision": "approve", "code": candidate["code"]})[0] == 200
    return candidate["id"]


def test_native_pond_without_telegram(tmp_path, monkeypatch):
    connect = socket.socket.connect

    def loopback_only(sock, address):
        assert address[0] == "127.0.0.1"
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    for boot in range(2):
        with running(tmp_path) as (server, url):
            assert call(url, "/health") == (200, {"ok": True, "db_ok": True,
                                                  "telegram_ok": None, "last_poll_age_s": None})
            if boot == 0:
                enroll(url, "desktop", "device", mint(server.service.admission, now=int(time.time())))
                enroll(url, "agent", "agent")
            else:
                assert call(url, "/admission/me", token="phone-0")[0] == 401
            status, ask = post_ask(url, "agent", ask=f"Choose a color {boot}")
            assert status == 201
            path = "/asks/" + ask["item"]["id"]
            assert call(url, path + "/reply", token="desktop", method="POST", body={"text": "blue"})[0] == 200
            events = call(url, path, token="agent")[1]["events"]
            assert (events[-1]["actor"], events[-1]["kind"], events[-1]["text"]) == ("owner", "reply", "blue")
            phone = f"phone-{boot}"
            pid = enroll(url, phone, "device")
            assert call(url, "/asks", token=phone)[0] == 200
            assert call(url, f"/admission/principals/{pid}/revoke", token="desktop", method="POST", body={})[0] == 200
            assert call(url, "/asks", token=phone)[0] == 401
            assert call(url, "/notify", token="a" * 64, method="POST", body={})[0] == 503
            monkeypatch.setattr(server.store, "health_check", lambda: False)
            assert call(url, "/health")[0] == 503


def test_legacy_database_requires_telegram(tmp_path):
    legacy_database(tmp_path / "data")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "MAILBOX_"))}
    env.update(MAILBOX_DATA_DIR=str(tmp_path / "data"), MAILBOX_BACKUP_DIR=str(tmp_path / "backup"))
    result = subprocess.run([sys.executable, "-m", "mailbox"], env=env,
                            timeout=3, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == "This Pond requires TELEGRAM_BOT_TOKEN and MAILBOX_OWNER_ID in legacy mode"
