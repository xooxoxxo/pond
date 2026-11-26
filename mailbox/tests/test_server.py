"""Tests for the HTTP server, the backup schedule and the token file.

The real MailboxServer is started on a free localhost port with a
FakeTelegramClient swapped in; every assertion goes over HTTP to
127.0.0.1. Nothing here touches the network beyond loopback, and the
mailbox token is read from the file the server generated in its tmp data
dir — it is never printed.
"""
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import pytest

from mailbox.__main__ import load_config
from mailbox.server import MailboxServer
from mailbox.telegram import FakeTelegramClient, TelegramError

REPO_ROOT = Path(__file__).resolve().parents[2]


class RunningServer(NamedTuple):
    """What the `server` fixture yields. Unpacks like the tuple it is, but its
    repr redacts the token so a failing assertion can never print it."""

    srv: MailboxServer
    base_url: str
    token: str

    def __repr__(self) -> str:
        return f"<RunningServer {self.base_url} token=(redacted)>"


OWNER_ID = 12345


def find_free_port() -> int:
    """Find an available port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def call(
    base_url: str,
    path: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict]:
    """One HTTP request; returns (status, parsed JSON body)."""
    headers: dict[str, str] = {}
    data = raw
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def raw_exchange(port: int, request: bytes, timeout: float = 10) -> tuple[int, dict]:
    """Send an exact request over a bare socket — for the things urllib will
    not do (a missing Content-Length, a non-ASCII header value, a body that is
    only promised). Fails loudly if the server answers nothing at all."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise AssertionError("the server closed the connection without a response")
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].split(b" ")[1])
    return status, json.loads(body or b"{}")


def raw_post_without_content_length(port: int, path: str, token: str) -> tuple[int, dict]:
    """POST with no body AND no Content-Length header at all (urllib always
    sends `Content-Length: 0`, so this goes over a bare socket)."""
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")
    return raw_exchange(port, request)


def _running(tmp_path, **kwargs):
    """Start a mailbox server on loopback with the Telegram fake, yield it,
    stop it. `kwargs` go to MailboxServer (e.g. notify_token=...)."""
    port = find_free_port()
    srv = MailboxServer(
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        telegram_token="test_token",
        owner_id=OWNER_ID,
        context="personal",
        port=port,
        host="127.0.0.1",
        **kwargs,
    )
    fake = FakeTelegramClient(owner_id=OWNER_ID)
    srv.telegram = fake
    srv.service.telegram = fake

    # Short poll interval so shutdown() is noticed fast (the 0.5 s default
    # would add half a second to every test in this file).
    thread = threading.Thread(
        target=srv.http_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # The token the server generated, read from its file (never printed).
    token = (tmp_path / "data" / "mailbox-token").read_text(encoding="utf-8").strip()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            call(base_url, "/health")
            break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("server did not start")

    yield RunningServer(srv, base_url, token)

    srv.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.fixture
def server(tmp_path):
    """A running mailbox server on loopback with the Telegram fake."""
    yield from _running(tmp_path)


def post_ask(base_url, token, *, project="Orbit", ask="rotate the key", context="personal", **extra):
    """POST /asks helper."""
    body = {"project": project, "ask": ask, "context": context, **extra}
    return call(base_url, "/asks", token=token, method="POST", body=body)


# --- token -------------------------------------------------------------


def test_token_files_are_private(server, tmp_path):
    """The generated token lives at 600 in the data dir and is mirrored to
    the backups dir at 600 for the operator to copy over ssh."""
    srv, _, token = server
    data_file = tmp_path / "data" / "mailbox-token"
    backup_file = tmp_path / "backups" / "mailbox-token"
    assert oct(data_file.stat().st_mode & 0o777) == "0o600"
    assert oct(backup_file.stat().st_mode & 0o777) == "0o600"
    assert backup_file.read_text(encoding="utf-8").strip() == token
    assert token == srv.mailbox_token
    assert len(token) >= 32


def test_token_is_reused_on_restart(tmp_path):
    """A second boot with the same data dir loads the token, not a new one."""
    kwargs = dict(
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        telegram_token="test_token",
        owner_id=OWNER_ID,
        context="personal",
        host="127.0.0.1",
    )
    first = MailboxServer(port=find_free_port(), **kwargs)
    first_token = first.mailbox_token
    first.stop()
    second = MailboxServer(port=find_free_port(), **kwargs)
    try:
        assert second.mailbox_token == first_token
    finally:
        second.stop()


# --- auth --------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/asks"),
        ("GET", "/asks/nope"),
        ("GET", "/events"),
        ("POST", "/asks"),
        ("POST", "/asks/nope/ack"),
        ("POST", "/asks/nope/steps"),
        ("POST", "/asks/nope/resolve"),
        ("POST", "/asks/nope/reply"),
        ("POST", "/asks/nope/reopen"),
        ("POST", "/asks/nope/answers"),
    ],
)
def test_no_bearer_is_401(server, method, path):
    """Everything but /health demands the bearer token."""
    _, base_url, _ = server
    status, data = call(base_url, path, method=method, body={} if method == "POST" else None)
    assert status == 401
    assert data == {"error": "unauthorized"}


def test_wrong_bearer_is_401(server):
    """A wrong token is rejected."""
    _, base_url, _ = server
    status, data = call(base_url, "/asks", token="not-the-token")
    assert status == 401
    assert data == {"error": "unauthorized"}


def test_non_ascii_bearer_is_401_not_a_dropped_connection(server):
    """S39: headers arrive latin-1 decoded, so a non-ASCII byte in the
    Authorization value used to blow up secrets.compare_digest (str) and the
    connection closed with no response at all. It must be a plain 401."""
    srv, _, _ = server
    request = (
        b"GET /asks HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer \xc3\xa9\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    status, data = raw_exchange(srv.port, request)
    assert status == 401
    assert data == {"error": "unauthorized"}


def test_non_ascii_bearer_on_post_is_401(server):
    """Same on a POST route."""
    srv, _, _ = server
    request = (
        b"POST /asks HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer \xc3\xa9\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"{}"
    )
    status, data = raw_exchange(srv.port, request)
    assert status == 401
    assert data == {"error": "unauthorized"}


def test_an_unexpected_error_is_a_500_with_a_response(server, monkeypatch):
    """S39: anything the routes don't foresee is answered 500, never a dead
    connection. GET path."""
    srv, base_url, token = server

    def boom(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(srv.store, "list_items", boom)
    status, data = call(base_url, "/asks", token=token)
    assert status == 500
    assert data == {"error": "internal error"}


def test_an_unexpected_error_on_post_is_a_500(server, monkeypatch):
    """The same blanket guard on the POST side."""
    srv, base_url, token = server

    def boom(*args, **kwargs):
        raise RuntimeError("service exploded")

    monkeypatch.setattr(srv.service, "post", boom)
    status, data = post_ask(base_url, token)
    assert status == 500
    assert data == {"error": "internal error"}


# --- version -----------------------------------------------------------


def test_version_reports_service_version_and_protocol(server):
    """GET /version with the token names the service, the semver release and the
    protocol integer kept for 0.9.x Hooks and clients."""
    _, base_url, token = server
    status, data = call(base_url, "/version", token=token)
    from mailbox import FEATURES
    assert status == 200
    assert data == {"service": "pond", "version": "1.15.1", "protocol": 5, "features": list(FEATURES)}


def test_version_lists_features(server):
    """GET /version names the Pond's features (1.8.0). Clients gate on these names,
    never on the version number; a Pond without the key has none of the listed-only ones."""
    from mailbox import FEATURES
    _, base_url, token = server
    status, data = call(base_url, "/version", token=token)
    assert status == 200
    assert data["features"] == list(FEATURES)
    assert data["protocol"] == 5
    assert FEATURES == ("answers", "blocking", "optional", "fileLinks", "corrections", "bugs",
                        "messages", "requirements", "longText", "sharedProgress", "askWarning",
                        "appendSteps", "ownerRevokeRequest", "ownerKeyDates", "fields", "brief",
                        "longAnswers")
    assert data["features"][-1] == "longAnswers"
    assert "askAbout" not in data["features"]      # the test Pond has no Anthropic API key


def test_the_version_is_strict_semver_with_a_changelog_entry():
    """A bump without a CHANGELOG.md entry fails here."""
    import re as _re
    from mailbox import PROTOCOL, VERSION
    assert _re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", VERSION)
    assert PROTOCOL == 5
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"\n## {VERSION}\n" in changelog
    headings = _re.findall(r"^## (\S+)$", changelog, flags=_re.M)
    assert headings[0] == VERSION and "1.0.0" in headings and "1.1.0" in headings


def test_version_150_retains_140_compatibility():
    """Pond 1.4.0 adds choice objects and 8000-character owner text without breaking a
    1.3.0 client: the version is 1.15.1, the protocol integer stays 5 and the schema
    is 5 (1.11.0's owner key dates; 1.4.0 itself needed no migration)."""
    from mailbox import PROTOCOL, VERSION, migrations
    assert (VERSION, PROTOCOL, migrations.SCHEMA_VERSION) == ("1.15.1", 5, 5)
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.4.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("Backwards compatible", "`protocol` stays 5", "8000", "text: too long", "note: too long",
                   "multi-line", "4096", '{"label"', "duplicate choice", "multi question",
                   "this choice takes no words", "choice: <label> · <text>", "X-Hook-Protocol: 6",
                   "labels only", "The agent client", "relay status <id> --json", "No migration"):
        assert needle in section, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert '"version": "1.15.1"' in readme and "X-Hook-Protocol: 6" in readme and '{"label"' in readme


def test_1_3_0_is_still_described():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.3.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("Backwards compatible", "`required`", "`done`", "POST /asks/{id}/progress",
                   "requirements_unmet", '"left"', '"details"', "`requirements`", "`progress`",
                   "Telegram", "(required)", "Before Done:", "The agent client", "No migration"):
        assert needle in section, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`/asks/{id}/progress`" in readme and "requirements_unmet" in readme


def test_1_2_0_is_still_described():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.2.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ('kind: "bug"', "signature", "GET /asks?kind=", '"message"', "list --kind", "schema 2"):
        assert needle in section, needle


def test_version_requires_the_token(server):
    """No token or a wrong one is a 401, like every route but /health."""
    _, base_url, _ = server
    assert call(base_url, "/version") == (401, {"error": "unauthorized"})
    assert call(base_url, "/version", token="wrong")[0] == 401


# --- health ------------------------------------------------------------


def test_health_is_503_until_the_first_successful_poll(server):
    """S38: last_poll_time starts at 0, so the daemon is unhealthy until
    getUpdates has actually come back once — no 120 s green window at boot."""
    srv, base_url, _ = server
    assert srv.http_server.last_poll_time == 0.0
    status, data = call(base_url, "/health")
    assert status == 503
    assert data["ok"] is False
    assert data["telegram_ok"] is False
    assert data["db_ok"] is True
    assert data["last_poll_age_s"] > 120


def test_health_is_open_and_ok(server):
    """GET /health needs no auth and reports the daemon's state."""
    srv, base_url, _ = server
    srv.http_server.last_poll_time = time.time()   # as the poll loop would
    status, data = call(base_url, "/health")
    assert status == 200
    assert data["ok"] is True
    assert data["telegram_ok"] is True
    assert data["db_ok"] is True
    assert isinstance(data["last_poll_age_s"], int)
    assert data["last_poll_age_s"] < 120


def test_health_needs_no_auth_but_still_answers(server):
    """A bearer token on /health is neither required nor rejected."""
    srv, base_url, token = server
    srv.http_server.last_poll_time = time.time()
    assert call(base_url, "/health", token=token)[0] == 200
    assert call(base_url, "/health", token="wrong")[0] == 200


def test_health_is_503_when_the_poll_loop_is_stale(server):
    """No successful poll for 200 s → telegram_ok false, ok false, 503."""
    srv, base_url, _ = server
    srv.http_server.last_poll_time = time.time() - 200
    status, data = call(base_url, "/health")
    assert status == 503
    assert data["ok"] is False
    assert data["telegram_ok"] is False
    assert data["db_ok"] is True
    assert data["last_poll_age_s"] >= 200


# --- POST /asks --------------------------------------------------------


def test_post_ask_sends_to_telegram(server):
    """POST /asks → 201 and exactly one Telegram message with the keyboard."""
    srv, base_url, token = server
    status, data = call(
        base_url,
        "/asks",
        token=token,
        method="POST",
        body={
            "project": "Orbit",
            "context": "personal",
            "ask": "rotate the deploy key",
            "why": "it expires tonight",
            "cmd": "ssh box 'doas rotate'",
            "link": "https://example.invalid/runbook",
        },
    )
    assert status == 201
    assert "deduped" not in data
    assert "openUrl" not in data
    item = data["item"]
    assert item["title"] == "rotate the deploy key"
    assert item["status"] == "open"
    assert item["projectId"] == "Orbit"
    assert item["lane"] == "personal"

    assert len(srv.telegram.sent_messages) == 1
    sent = srv.telegram.sent_messages[0]
    assert sent["chat_id"] == OWNER_ID
    assert "rotate the deploy key" in sent["text"]
    buttons = sent["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"d:{item['id']}", f"s:{item['id']}"]


def test_post_ask_dedupes_within_the_window(server):
    """The same ask again → 200 deduped, the same item, no second message."""
    srv, base_url, token = server
    first_status, first = post_ask(base_url, token)
    second_status, second = post_ask(base_url, token)
    assert first_status == 201
    assert second_status == 200
    assert second["deduped"] is True
    assert second["item"]["id"] == first["item"]["id"]
    assert len(srv.telegram.sent_messages) == 1


def test_post_ask_wrong_context_is_403(server):
    """A session in another context may not post here."""
    srv, base_url, token = server
    status, data = call(
        base_url,
        "/asks",
        token=token,
        method="POST",
        body={"project": "Orbit", "context": "company", "ask": "leak me"},
    )
    assert status == 403
    assert data == {"error": "wrong context"}
    assert srv.telegram.sent_messages == []


@pytest.mark.parametrize(
    "body",
    [
        {"context": "personal", "ask": "no project"},
        {"project": "Orbit", "context": "personal"},
        {},
    ],
)
def test_post_ask_missing_fields_is_400(server, body):
    """project and ask are required."""
    srv, base_url, token = server
    status, data = call(base_url, "/asks", token=token, method="POST", body=body)
    assert status == 400
    assert "error" in data
    assert srv.telegram.sent_messages == []


def test_post_ask_invalid_json_is_400(server):
    """A body that isn't a JSON object → 400 invalid json."""
    _, base_url, token = server
    status, data = call(base_url, "/asks", token=token, method="POST", raw=b"{not json")
    assert status == 400
    assert data == {"error": "invalid json"}

    status, data = call(base_url, "/asks", token=token, method="POST", raw=b"[1, 2]")
    assert status == 400
    assert data == {"error": "invalid json"}


# --- GET /asks and /asks/{id} -----------------------------------------


def test_list_asks(server):
    """GET /asks returns the open asks."""
    _, base_url, token = server
    _, created = post_ask(base_url, token, ask="first ask")
    status, data = call(base_url, "/asks?status=open&days=14", token=token)
    assert status == 200
    assert [i["id"] for i in data["items"]] == [created["item"]["id"]]

    status, data = call(base_url, "/asks?status=done", token=token)
    assert status == 200
    assert data["items"] == []


def test_list_asks_is_newest_first(server):
    """GET /asks orders by creation, newest first. `created` has one-second
    resolution, so the two posts are deliberately more than a second apart."""
    _, base_url, token = server
    _, older = post_ask(base_url, token, ask="the older ask")
    time.sleep(1.05)
    _, newer = post_ask(base_url, token, ask="the newer ask")

    status, data = call(base_url, "/asks", token=token)
    assert status == 200
    assert [i["title"] for i in data["items"]] == ["the newer ask", "the older ask"]
    assert [i["id"] for i in data["items"]] == [newer["item"]["id"], older["item"]["id"]]


def test_get_ask_with_events(server):
    """GET /asks/{id} returns the item and its event history."""
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]

    status, data = call(base_url, f"/asks/{item_id}", token=token)
    assert status == 200
    assert data["item"]["id"] == item_id
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["posted"]
    assert data["events"][0]["itemId"] == item_id
    assert data["events"][0]["actor"] == "session"


def test_get_unknown_ask_is_404(server):
    """An id nobody posted → 404."""
    _, base_url, token = server
    status, data = call(base_url, "/asks/does-not-exist", token=token)
    assert status == 404
    assert data == {"error": "not found"}


# --- ack / steps / resolve --------------------------------------------


def test_ack_with_no_body(server):
    """POST /asks/{id}/ack carries no body at all and still works; the edit
    re-sends the Done/Skip keyboard so Telegram doesn't drop it."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]

    status, data = call(base_url, f"/asks/{item_id}/ack", token=token, method="POST")
    assert status == 200
    assert data["item"]["id"] == item_id

    edit = srv.telegram.edited_messages[-1]
    assert edit["text"].startswith("👀")
    buttons = edit["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"d:{item_id}", f"s:{item_id}"]


def test_ack_without_content_length_header(server):
    """S15: an absent Content-Length is an empty body, not a 400."""
    srv, base_url, token = server
    port = srv.port
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    status, data = raw_post_without_content_length(port, f"/asks/{item_id}/ack", token)
    assert status == 200
    assert data["item"]["id"] == item_id


def test_negative_content_length_is_an_empty_body(server):
    """A negative Content-Length must not put rfile.read(-1) in charge — that
    blocks until the client gives up. It is simply an empty body."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    request = (
        f"POST /asks/{item_id}/ack HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Length: -1\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")
    status, data = raw_exchange(srv.port, request, timeout=5)
    assert status == 200
    assert data["item"]["id"] == item_id


def test_an_oversized_body_is_413(server):
    """A body over 1 MiB is refused on the declared length, before it is read."""
    srv, _, token = server
    request = (
        f"POST /asks HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {(1 << 20) + 1}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")
    status, data = raw_exchange(srv.port, request, timeout=5)
    assert status == 413
    assert data == {"error": "body too large"}


def test_telegram_failure_on_post_is_502(server, monkeypatch):
    """S39: the send failed upstream, so it is a 502 with Telegram's message —
    Task 6's client reads that code."""
    srv, base_url, token = server

    def boom(*args, **kwargs):
        raise TelegramError("sendMessage 403: bot was blocked by the user")

    monkeypatch.setattr(srv.telegram, "send_message", boom)
    status, data = post_ask(base_url, token)
    assert status == 502
    assert data["error"] == "sendMessage 403: bot was blocked by the user"


def test_ack_unknown_id_is_404(server):
    """ack on an unknown id → 404."""
    _, base_url, token = server
    status, data = call(base_url, "/asks/nope/ack", token=token, method="POST")
    assert status == 404
    assert data == {"error": "not found"}


def test_steps_with_a_secret_is_400_and_never_echoed(server):
    srv, base_url, token = server
    item_id = post_ask(base_url, token)[1]["item"]["id"]
    secret = "sk-" + "ant-" + "Q" * 20
    status, data = call(base_url, f"/asks/{item_id}/steps", token=token, method="POST", body={"text": secret})
    assert (status, data) == (400, {"error": "invalid steps", "details": ["text: secret-like value"]})
    assert srv.telegram.replies == []


def test_steps_posts_a_reply(server):
    """POST /asks/{id}/steps replies under the ask's message."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]

    status, data = call(
        base_url, f"/asks/{item_id}/steps", token=token, method="POST",
        body={"text": "1. open the console\n2. click Rotate"},
    )
    assert status == 200
    assert data["item"]["id"] == item_id
    assert srv.telegram.replies[-1]["text"] == (
        "📝 Session note, not part of the ask:\n1. open the console\n2. click Rotate")

    _, detail = call(base_url, f"/asks/{item_id}", token=token)
    assert [e["kind"] for e in detail["events"]] == ["posted", "steps"]


def test_steps_without_text_is_400(server):
    """steps needs text."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    status, data = call(base_url, f"/asks/{item_id}/steps", token=token, method="POST", body={})
    assert status == 400
    assert "error" in data
    assert srv.telegram.replies == []


def test_resolve_done(server):
    """POST /asks/{id}/resolve marks the item done and clears the keyboard."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]

    status, data = call(
        base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
        body={"verdict": "done", "note": "rotated at 14:02"},
    )
    assert status == 200
    assert data["item"]["status"] == "done"
    assert data["item"]["completedAt"]
    assert srv.telegram.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}

    _, listed = call(base_url, "/asks?status=open", token=token)
    assert listed["items"] == []


def test_resolve_declined(server):
    """A declined verdict dismisses the item."""
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    status, data = call(
        base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
        body={"verdict": "declined"},
    )
    assert status == 200
    assert data["item"]["status"] == "dismissed"


@pytest.mark.parametrize("body", [{}, {"verdict": "maybe"}, {"verdict": "dismissed"}])
def test_resolve_bad_verdict_is_400(server, body):
    """Only done and declined are verdicts."""
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    status, data = call(
        base_url, f"/asks/{item_id}/resolve", token=token, method="POST", body=body
    )
    assert status == 400
    assert "error" in data


def test_resolve_unknown_id_is_404(server):
    """resolve on an unknown id → 404."""
    _, base_url, token = server
    status, data = call(
        base_url, "/asks/nope/resolve", token=token, method="POST", body={"verdict": "done"}
    )
    assert status == 404
    assert data == {"error": "not found"}


# --- GET /events -------------------------------------------------------


def test_events_stream_and_cursor(server):
    """GET /events?after=0 returns the log with a cursor; re-reading with
    that cursor returns nothing new."""
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    call(base_url, f"/asks/{item_id}/ack", token=token, method="POST")
    call(
        base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
        body={"verdict": "done"},
    )

    status, data = call(base_url, "/events?after=0", token=token)
    assert status == 200
    assert [e["kind"] for e in data["events"]] == ["posted", "acked", "resolved"]
    assert {e["itemId"] for e in data["events"]} == {item_id}
    assert set(data["events"][0]) == {"seq", "itemId", "kind", "actor", "text", "at", "rev", "auth"}
    cursor = data["next"]
    assert cursor == data["events"][-1]["seq"]

    status, again = call(base_url, f"/events?after={cursor}", token=token)
    assert status == 200
    assert again["events"] == []
    assert again["next"] == cursor


def test_events_limit(server):
    """limit pages the log."""
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    call(base_url, f"/asks/{item_id}/ack", token=token, method="POST")

    status, data = call(base_url, "/events?after=0&limit=1", token=token)
    assert status == 200
    assert len(data["events"]) == 1
    assert data["events"][0]["kind"] == "posted"

    status, data = call(base_url, f"/events?after={data['next']}&limit=1", token=token)
    assert [e["kind"] for e in data["events"]] == ["acked"]


@pytest.mark.parametrize(
    "path",
    [
        "/events?after=soon",
        "/events?limit=lots",
        "/events?limit=0",
        "/events?after=-1",
        "/asks?days=fortnight",
        "/asks?days=99999999999999999999",
        "/asks?days=-1",
    ],
)
def test_bad_query_is_400(server, path):
    """A silly cursor or window is a bad request, never a dead request thread."""
    _, base_url, token = server
    status, data = call(base_url, path, token=token)
    assert status == 400
    assert "error" in data


# --- routing -----------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("GET", "/nope"),
        ("GET", "/asks/abc/extra"),
        ("POST", "/health"),
        ("POST", "/asksx"),
        ("POST", "/asks/abcack"),
        ("POST", "/asks/abc/unknown"),
        ("POST", "/asks/abc/ack/extra"),
    ],
)
def test_unknown_paths_are_404(server, method, path):
    """Routes match on an exact path split — /asks/xack is not /asks/x/ack."""
    _, base_url, token = server
    status, data = call(
        base_url, path, token=token, method=method, body={} if method == "POST" else None
    )
    assert status == 404
    assert data == {"error": "not found"}


# --- backup ------------------------------------------------------------


def test_next_backup_time_is_the_next_local_0320():
    """The schedule is 03:20 in local time and survives a month boundary."""
    assert MailboxServer._next_backup_time(datetime(2026, 1, 31, 1, 0)) == datetime(2026, 1, 31, 3, 20)
    assert MailboxServer._next_backup_time(datetime(2026, 1, 31, 23, 0)) == datetime(2026, 2, 1, 3, 20)
    assert MailboxServer._next_backup_time(datetime(2026, 12, 31, 4, 0)) == datetime(2027, 1, 1, 3, 20)


def test_do_backup_writes_a_private_snapshot_and_prunes(server):
    """The nightly snapshot is a readable copy at 600, and only the newest
    30 are kept."""
    srv, base_url, token = server
    post_ask(base_url, token)

    for day in range(1, 41):
        (srv.backup_dir / f"mailbox-2020-01-{day:02d}.db").write_bytes(b"")

    srv._do_backup()

    snapshots = sorted(srv.backup_dir.glob("mailbox-*.db"))
    assert len(snapshots) == 30
    final = srv.backup_dir / f"mailbox-{datetime.now().strftime('%Y-%m-%d')}.db"
    assert final in snapshots
    assert oct(final.stat().st_mode & 0o777) == "0o600"
    assert oct(srv.backup_dir.stat().st_mode & 0o777) == "0o700"
    assert not list(srv.backup_dir.glob("*.tmp"))

    conn = sqlite3.connect(str(final))
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    finally:
        conn.close()


# --- poll loop ---------------------------------------------------------


def _owner_tap(update_id: int, item_id: str) -> dict:
    """A Telegram update: the owner tapping Done on an ask."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": OWNER_ID},
            "data": f"d:{item_id}",
        },
    }


def test_poll_loop_backs_off_and_only_counts_a_successful_poll(server, monkeypatch):
    """S17: a failed getUpdates sleeps 5 s and does NOT refresh the health
    clock; the next success does, and its updates reach the service."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]

    srv.http_server.last_poll_time = 0.0
    sleeps: list[float] = []
    offsets: list[int] = []
    poll_clock: list[float] = []

    def fake_get_updates(offset=0, timeout=50):
        offsets.append(offset)
        poll_clock.append(srv.http_server.last_poll_time)
        if len(offsets) == 1:
            raise TelegramError("getUpdates 500: boom")
        if len(offsets) == 2:
            return [_owner_tap(7, item_id)]
        if len(offsets) > 3:
            raise AssertionError("the poll loop did not exit")
        srv.running = False
        return []

    monkeypatch.setattr(srv.telegram, "get_updates", fake_get_updates)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    srv.running = True
    srv._poll_loop()                       # returns when running goes false

    assert sleeps == [5]                   # backed off once, after the failure
    assert poll_clock[0] == 0.0
    assert poll_clock[1] == 0.0            # the failure did not advance it
    assert poll_clock[2] > 0.0             # the success did
    assert offsets == [0, 0, 8]            # nothing to confirm until the tap

    assert srv.telegram.callback_answers[-1]["callback_query_id"] == "cb7"
    assert srv.store.get_item(item_id).status == "done"


def test_poll_loop_skips_a_poisonous_update(server, monkeypatch):
    """An update that raises is logged and left behind — it is confirmed to
    Telegram first, so the loop can never be stuck re-fetching it."""
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]

    offsets: list[int] = []
    handled: list[int] = []

    def fake_get_updates(offset=0, timeout=50):
        offsets.append(offset)
        if len(offsets) == 1:
            return [_owner_tap(11, item_id), _owner_tap(12, item_id)]
        if len(offsets) > 2:
            raise AssertionError("the poll loop did not exit")
        srv.running = False
        return []

    def fake_handle(update):
        handled.append(update["update_id"])
        if update["update_id"] == 11:
            raise RuntimeError("poison")

    monkeypatch.setattr(srv.telegram, "get_updates", fake_get_updates)
    monkeypatch.setattr(srv.service, "handle_update", fake_handle)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    srv.running = True
    srv._poll_loop()

    assert handled == [11, 12]             # the bad one did not stop the good one
    assert offsets == [0, 13]              # both were confirmed


# --- entrypoint config -------------------------------------------------


BASE_ENV = {"TELEGRAM_BOT_TOKEN": "bot-token", "MAILBOX_OWNER_ID": "12345"}


def test_load_config_defaults():
    """The documented defaults, with the ids coerced to ints."""
    config = load_config(BASE_ENV)
    assert config["owner_id"] == 12345
    assert config["context"] == "personal"
    assert config["data_dir"] == Path("/data")
    assert config["backup_dir"] == Path("/backups")
    assert config["port"] == 8790
    assert config["relay_open_base"] is None


def test_load_config_overrides():
    """Every knob comes from the environment."""
    config = load_config({
        **BASE_ENV,
        "MAILBOX_CONTEXT": "company",
        "MAILBOX_DATA_DIR": "/srv/data",
        "MAILBOX_BACKUP_DIR": "/srv/backups",
        "MAILBOX_PORT": "9001",
        "MAILBOX_RELAY_OPEN_BASE": "http://127.0.0.1:47823/i/",
    })
    assert config["context"] == "company"
    assert config["data_dir"] == Path("/srv/data")
    assert config["backup_dir"] == Path("/srv/backups")
    assert config["port"] == 9001
    assert config["relay_open_base"] == "http://127.0.0.1:47823/i"


@pytest.mark.parametrize(
    "env,message",
    [
        ({}, "TELEGRAM_BOT_TOKEN not set"),
        ({"MAILBOX_OWNER_ID": "1"}, "TELEGRAM_BOT_TOKEN not set"),
        ({"TELEGRAM_BOT_TOKEN": "t"}, "MAILBOX_OWNER_ID not set"),
        ({**BASE_ENV, "MAILBOX_OWNER_ID": "abc"}, "MAILBOX_OWNER_ID must be an integer"),
        ({**BASE_ENV, "MAILBOX_PORT": "abc"}, "MAILBOX_PORT must be 1-65535"),
        ({**BASE_ENV, "MAILBOX_PORT": "0"}, "MAILBOX_PORT must be 1-65535"),
        ({**BASE_ENV, "MAILBOX_PORT": "-1"}, "MAILBOX_PORT must be 1-65535"),
        ({**BASE_ENV, "MAILBOX_PORT": "65536"}, "MAILBOX_PORT must be 1-65535"),
        ({**BASE_ENV, "MAILBOX_RELAY_OPEN_BASE": "relay://i"},
         "MAILBOX_RELAY_OPEN_BASE must be an http(s) URL"),
    ],
)
def test_load_config_rejects_bad_env(env, message):
    """Each bad value names itself, so the operator knows what to fix."""
    with pytest.raises(ValueError) as exc:
        load_config(env)
    assert str(exc.value) == message


def test_module_entrypoint_exits_with_a_message(tmp_path):
    """`python3 -m mailbox` with nothing configured exits 1 with one line —
    not a traceback. Nothing is started: it fails before any server exists."""
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("MAILBOX_") and k != "TELEGRAM_BOT_TOKEN"
    }
    proc = subprocess.run(
        [sys.executable, "-m", "mailbox"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.strip() == "TELEGRAM_BOT_TOKEN not set"
    assert "Traceback" not in proc.stderr


# --- instructables and Hook -------------------------------------------


OPEN_BASE = "http://127.0.0.1:47823/i"
INSTRUCTABLE = {
    "v": 1,
    "title": "Create the Slack channel",
    "steps": [
        {"do": "Open Slack and create #domain", "where": "Slack"},
        {"do": "Check Node", "code": "node --version", "lang": "bash", "expect": "v22"},
    ],
}


def test_post_ask_with_an_instructable_stores_it_and_links_relay(server):
    """ask defaults to the title; the payload keeps the instructable; the
    message lists the steps without code; the keyboard and response link Hook."""
    srv, base_url, token = server
    srv.service.open_base = OPEN_BASE
    status, data = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE)
    assert status == 201
    item = data["item"]
    assert item["title"] == "Create the Slack channel"
    # Revision 1 is the instructable with its step ids (s1, s2).
    assert item["payload"]["instructable"] == {
        **INSTRUCTABLE, "urgency": "today",
        "steps": [{**step, "id": f"s{n}"} for n, step in enumerate(INSTRUCTABLE["steps"], 1)]}
    assert data["openUrl"] == f"{OPEN_BASE}/{item['id']}"
    sent = srv.telegram.sent_messages[0]
    assert "1. Open Slack and create #domain" in sent["text"]
    assert "node --version" not in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][1] == [
        {"text": "📋 Open in Hook", "url": f"{OPEN_BASE}/{item['id']}"}
    ]


def test_post_ask_dedupe_returns_the_existing_item_and_the_open_url(server):
    """Review (b): the same instructable twice → 200, the first item, its openUrl."""
    srv, base_url, token = server
    srv.service.open_base = OPEN_BASE
    first_status, first = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE)
    second_status, second = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE)
    assert (first_status, second_status) == (201, 200)
    assert second["deduped"] is True
    assert second["item"]["id"] == first["item"]["id"]
    assert second["openUrl"] == first["openUrl"] == f"{OPEN_BASE}/{first['item']['id']}"
    assert len(srv.telegram.sent_messages) == 1


def test_post_ask_with_a_secret_in_the_instructable_is_400_and_never_echoed(server):
    """Review (c): steps[1].code holds a key → 400 naming the path, not the value;
    nothing reaches Telegram."""
    srv, base_url, token = server
    secret = "sk-" + "ant-" + "api03-" + "Q" * 32       # built at runtime: no literal key in the repo
    bad = {
        **INSTRUCTABLE,
        "steps": [INSTRUCTABLE["steps"][0], {"do": "Set the key", "code": f"export KEY={secret}"}],
    }
    status, data = post_ask(base_url, token, ask=None, instructable=bad)
    assert status == 400
    assert data["error"] == "invalid instructable"
    assert any(d.startswith("steps[1].code") for d in data["details"])
    assert secret not in json.dumps(data)
    assert srv.telegram.sent_messages == []


def test_a_lone_surrogate_in_the_body_is_400_and_never_poisons_the_list(server):
    """Adversarial review: "\\ud800" parses to a lone surrogate that cannot be
    encoded as UTF-8; it is refused at the boundary, so GET /asks stays 200."""
    srv, base_url, token = server
    bad = {**INSTRUCTABLE, "steps": [{"do": "run it", "code": "echo \ud800"}]}
    status, data = post_ask(base_url, token, ask=None, instructable=bad, cmd="x\udfff")
    assert (status, data["error"]) == (400, "invalid json")
    assert call(base_url, "/asks", token=token)[0] == 200
    assert srv.telegram.sent_messages == []


def test_with_an_instructable_the_body_ask_and_why_are_ignored(server):
    """Adversarial review: unvalidated body text never reaches the render."""
    srv, base_url, token = server
    secret = "gh" + "p_" + "A" * 36
    status, _ = post_ask(base_url, token, ask=f"paste {secret}", why=secret,
                         instructable=INSTRUCTABLE)
    assert status == 201
    assert secret not in srv.telegram.sent_messages[0]["text"]
    status, data = post_ask(base_url, token, project="P" * 61, ask=None, instructable=INSTRUCTABLE)
    assert (status, data["error"]) == (400, "invalid project")


# --- owner actions from Hook -------------------------------------------


def test_resolve_by_the_owner_records_the_owner(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    status, data = call(
        base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
        body={"verdict": "declined", "actor": "owner"},
    )
    assert status == 200
    assert data["item"]["status"] == "dismissed"
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert (got["events"][-1]["kind"], got["events"][-1]["actor"]) == ("declined", "owner")


def test_resolve_with_an_unknown_actor_is_400(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    status, data = call(
        base_url, f"/asks/{created['item']['id']}/resolve", token=token, method="POST",
        body={"verdict": "done", "actor": "daemon"},
    )
    assert status == 400
    assert data == {"error": "invalid actor"}


def test_reply_records_an_owner_event_and_leaves_telegram_alone(server):
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    calls_before = len(srv.telegram.calls)

    status, data = call(
        base_url, f"/asks/{item_id}/reply", token=token, method="POST",
        body={"text": "choice: #domain"},
    )
    assert status == 200
    assert data["item"]["id"] == item_id
    assert data["item"]["status"] == "open"
    assert len(srv.telegram.calls) == calls_before
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    last = got["events"][-1]
    assert (last["kind"], last["actor"], last["text"]) == ("reply", "owner", "choice: #domain")


@pytest.mark.parametrize("body", [{}, {"text": "   "}, {"text": 5}])
def test_reply_without_text_is_400(server, body):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    status, data = call(
        base_url, f"/asks/{created['item']['id']}/reply", token=token, method="POST", body=body
    )
    assert status == 400
    assert data == {"error": "missing text"}


def test_reply_unknown_id_is_404(server):
    _, base_url, token = server
    status, data = call(
        base_url, "/asks/nope/reply", token=token, method="POST", body={"text": "hello"}
    )
    assert status == 404
    assert data == {"error": "not found"}


# --- reopen -------------------------------------------------------------


def resolve(base_url, token, item_id, verdict="done"):
    return call(
        base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
        body={"verdict": verdict, "actor": "owner"},
    )


def test_reopen_without_auth_is_401(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    status, data = call(
        base_url, f"/asks/{created['item']['id']}/reopen", method="POST", body={}
    )
    assert status == 401
    assert data == {"error": "unauthorized"}


def test_reopen_unknown_id_is_404(server):
    _, base_url, token = server
    status, data = call(base_url, "/asks/nope/reopen", token=token, method="POST", body={})
    assert status == 404
    assert data == {"error": "not found"}


@pytest.mark.parametrize("actor", ["daemon", 5, "OWNER"])
def test_reopen_with_a_bad_actor_is_400(server, actor):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    resolve(base_url, token, item_id)
    status, data = call(
        base_url, f"/asks/{item_id}/reopen", token=token, method="POST", body={"actor": actor}
    )
    assert status == 400
    assert data == {"error": "invalid actor"}
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert got["item"]["status"] == "done"


def test_reopen_with_invalid_json_is_400(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    status, data = call(
        base_url, f"/asks/{created['item']['id']}/reopen", token=token, method="POST",
        raw=b"{not json",
    )
    assert status == 400
    assert data == {"error": "invalid json"}


@pytest.mark.parametrize("verdict,closed", [("done", "done"), ("declined", "dismissed")])
def test_reopen_a_closed_ask(server, verdict, closed):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    _, resolved = resolve(base_url, token, item_id, verdict)
    assert resolved["item"]["status"] == closed

    status, data = call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")
    assert status == 200
    assert data["item"]["id"] == item_id
    assert data["item"]["status"] == "open"
    assert data["item"]["completedAt"] is None
    assert data["item"]["updated"] >= resolved["item"]["updated"]

    _, got = call(base_url, f"/asks/{item_id}", token=token)
    last = got["events"][-1]
    assert (last["kind"], last["actor"], last["text"]) == ("reopened", "owner", None)
    assert [e["kind"] for e in got["events"]].count("reopened") == 1

    _, listed = call(base_url, "/asks?status=open", token=token)
    assert [i["id"] for i in listed["items"]] == [item_id]


def test_reopen_records_the_session_actor_when_asked(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    resolve(base_url, token, item_id)
    status, _ = call(
        base_url, f"/asks/{item_id}/reopen", token=token, method="POST", body={"actor": "session"}
    )
    assert status == 200
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert (got["events"][-1]["kind"], got["events"][-1]["actor"]) == ("reopened", "session")


def test_reopen_an_open_ask_changes_nothing(server):
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    _, before = call(base_url, f"/asks/{item_id}", token=token)
    calls_before = len(srv.telegram.calls)

    status, data = call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST", body={})
    assert status == 200
    assert data["item"] == before["item"]
    _, after = call(base_url, f"/asks/{item_id}", token=token)
    assert after["events"] == before["events"]
    assert len(srv.telegram.calls) == calls_before


def test_reopen_twice_adds_one_event(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    resolve(base_url, token, item_id)
    call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")
    call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert [e["kind"] for e in got["events"]] == ["posted", "resolved", "reopened"]


def test_events_show_reopened_after_resolved(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    resolve(base_url, token, item_id)
    call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")

    status, data = call(base_url, "/events?after=0", token=token)
    assert status == 200
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["posted", "resolved", "reopened"]
    reopened = data["events"][-1]
    assert reopened["itemId"] == item_id
    assert reopened["seq"] > data["events"][1]["seq"]


def test_reopen_edits_the_telegram_message_back_to_the_open_ask(server):
    srv, base_url, token = server
    _, created = post_ask(base_url, token, why="the old one leaked")
    item_id = created["item"]["id"]
    original = srv.telegram.sent_messages[0]
    message_id = created["item"]["payload"]["tg_message_id"]
    resolve(base_url, token, item_id)

    call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")
    edit = srv.telegram.edited_messages[-1]
    assert edit["message_id"] == message_id
    assert edit["text"] == original["text"]
    assert edit["reply_markup"] == original["reply_markup"]
    assert len(srv.telegram.sent_messages) == 1
    method, params = srv.telegram.calls[-1]
    assert method == "editMessageText"
    assert params["link_preview_options"] == {"is_disabled": True}


def test_reopen_survives_telegram_failures(server, monkeypatch):
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    resolve(base_url, token, item_id)

    def boom(*args, **kwargs):
        raise TelegramError("editMessageText 500: Internal Server Error")

    monkeypatch.setattr(srv.telegram, "edit_message_text", boom)
    monkeypatch.setattr(srv.telegram, "send_message", boom)

    status, data = call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")
    assert status == 200
    assert data["item"]["status"] == "open"
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert got["events"][-1]["kind"] == "reopened"


def test_a_reopened_ask_still_dedupes(server):
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    resolve(base_url, token, item_id)
    call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST")
    sent_before = len(srv.telegram.sent_messages)

    status, again = post_ask(base_url, token)
    assert status == 200
    assert again["deduped"] is True
    assert again["item"]["id"] == item_id
    assert len(srv.telegram.sent_messages) == sent_before


# --- capture and notify keys (Task 2) -------------------------------------

CT_TOKEN = "ct_" + "b" * 48
HEX = "c" * 64


def test_load_config_capture_and_notify_default_off():
    config = load_config(BASE_ENV)
    assert (config["ct_url"], config["ct_token"], config["notify_token"]) == (None, None, None)


def test_load_config_capture_and_notify_keys():
    config = load_config({**BASE_ENV, "MAILBOX_CT_URL": "http://host.docker.internal:8787/",
                          "MAILBOX_CT_TOKEN": CT_TOKEN, "MAILBOX_NOTIFY_TOKEN": HEX})
    assert config["ct_url"] == "http://host.docker.internal:8787"
    assert config["ct_token"] == CT_TOKEN
    assert config["notify_token"] == HEX


@pytest.mark.parametrize(
    "extra, warning, off",
    [
        ({"MAILBOX_CT_URL": "ct:8787", "MAILBOX_CT_TOKEN": CT_TOKEN, "MAILBOX_NOTIFY_TOKEN": HEX},
         "[mailbox] MAILBOX_CT_URL is malformed; capture disabled", ("ct_url", "ct_token")),
        ({"MAILBOX_CT_URL": "http://ct:8787", "MAILBOX_CT_TOKEN": "ct_short", "MAILBOX_NOTIFY_TOKEN": HEX},
         "[mailbox] MAILBOX_CT_TOKEN is malformed; capture disabled", ("ct_url", "ct_token")),
        ({"MAILBOX_CT_URL": "http://ct:8787", "MAILBOX_CT_TOKEN": CT_TOKEN, "MAILBOX_NOTIFY_TOKEN": "nothex"},
         "[mailbox] MAILBOX_NOTIFY_TOKEN is malformed; notify disabled", ("notify_token",)),
    ],
)
def test_load_config_malformed_capture_env_disables_that_feature_and_still_starts(extra, warning, off, caplog):
    """Ruling S125: a malformed value disables its feature with one warning
    naming the key only; the shared ask channel never exits over it."""
    with caplog.at_level("WARNING", logger="mailbox"):
        config = load_config({**BASE_ENV, **extra})
    for key in ("ct_url", "ct_token", "notify_token"):
        assert (config[key] is None) == (key in off), key
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [warning]
    for secret in (CT_TOKEN, HEX, "ct_short", "nothex", "ct:8787"):
        assert not any(secret in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("extra", [{"MAILBOX_CT_URL": "http://ct:8787"}, {"MAILBOX_CT_TOKEN": CT_TOKEN}])
def test_load_config_half_pair_disables_capture_and_still_starts(extra, caplog):
    """One key of the pair without the other (an .env written in two steps) must
    never crash-loop the shared ask channel: capture is off, the daemon starts."""
    with caplog.at_level("WARNING", logger="mailbox"):
        config = load_config({**BASE_ENV, **extra})
    assert (config["ct_url"], config["ct_token"]) == (None, None)
    assert any("MAILBOX_CT_URL and MAILBOX_CT_TOKEN must be set together; capture disabled" in r.getMessage() for r in caplog.records)
    assert not any(CT_TOKEN in r.getMessage() for r in caplog.records)


def test_server_builds_the_central_time_client_from_its_keys(tmp_path):
    srv = MailboxServer(
        data_dir=tmp_path / "data", backup_dir=tmp_path / "backups", telegram_token="test_token",
        owner_id=OWNER_ID, context="personal", port=find_free_port(), host="127.0.0.1",
        ct_url="http://ct.test", ct_token=CT_TOKEN, notify_token=HEX,
    )
    try:
        assert srv.service.ct_client is srv.ct_client
        assert srv.ct_client.base_url == "http://ct.test"
        assert srv.notify_token == HEX
    finally:
        srv.stop()


def test_server_without_the_keys_has_no_client(server):
    srv, _, _ = server
    assert srv.ct_client is None and srv.service.ct_client is None and srv.notify_token is None


# --- POST /notify (Task 6) ---------------------------------------------------

NOTIFY_BODY = {"kind": "reminder", "ref": "fire-1", "title": "call the notary", "body": "today 14:00",
               "actions": [{"id": "done", "label": "✅ Done"}]}


@pytest.fixture
def notify_server(tmp_path):
    """The server fixture with a notify token configured; same start and stop checks."""
    yield from _running(tmp_path, notify_token=HEX)


def test_notify_needs_the_notify_token_or_the_mailbox_token(notify_server):
    srv, base_url, token = notify_server
    assert call(base_url, "/notify", method="POST", body=NOTIFY_BODY)[0] == 401
    assert call(base_url, "/notify", token="d" * 64, method="POST", body=NOTIFY_BODY)[0] == 401
    status, body = call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)
    assert status == 201 and body["deduped"] is False and body["id"]
    status, body2 = call(base_url, "/notify", token=token, method="POST", body={**NOTIFY_BODY, "ref": "fire-2"})
    assert status == 201 and body2["id"] != body["id"]
    assert len(srv.telegram.sent_messages) == 2


def test_notify_without_a_notify_token_accepts_only_the_mailbox_token(server):
    srv, base_url, token = server
    assert call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)[0] == 401
    assert call(base_url, "/notify", token=token, method="POST", body=NOTIFY_BODY)[0] == 201


def test_the_notify_token_opens_nothing_but_notify(notify_server):
    """The notify token is not the mailbox token: every ask route refuses it."""
    _, base_url, _ = notify_server
    assert call(base_url, "/asks", token=HEX)[0] == 401
    assert call(base_url, "/events", token=HEX)[0] == 401
    assert post_ask(base_url, HEX)[0] == 401
    assert call(base_url, "/asks/x/resolve", token=HEX, method="POST", body={"verdict": "done"})[0] == 401
    assert call(base_url, "/notify", token=HEX)[0] == 404        # GET is not a route


def test_notify_dedupes_by_ref_without_a_second_send(notify_server):
    srv, base_url, _ = notify_server
    first = call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)
    second = call(base_url, "/notify", token=HEX, method="POST", body={**NOTIFY_BODY, "title": "changed"})
    assert first[0] == 201 and second[0] == 200
    assert second[1] == {"id": first[1]["id"], "deduped": True}
    assert len(srv.telegram.sent_messages) == 1
    sent = srv.telegram.sent_messages[0]
    assert sent["text"] == "⏰ <b>call the notary</b>\ntoday 14:00"
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"r:{first[1]['id']}:done"


def test_notify_dedupe_survives_a_restart(tmp_path):
    """Core retries a ref the mailbox already sent when the answer was lost in
    a restart: the second process finds the row and sends nothing."""
    first_run = _running(tmp_path, notify_token=HEX)
    srv1, base_url, _ = next(first_run)
    status, body = call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)
    assert status == 201 and len(srv1.telegram.sent_messages) == 1
    with pytest.raises(StopIteration):
        next(first_run)                                  # stop: same checks as the fixture

    second_run = _running(tmp_path, notify_token=HEX)
    srv2, base_url, _ = next(second_run)
    try:
        assert call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY) == (200, {"id": body["id"], "deduped": True})
        assert srv2.telegram.sent_messages == []
        assert call(base_url, "/notify", token=HEX, method="POST", body={**NOTIFY_BODY, "ref": "fire-2"})[0] == 201
    finally:
        with pytest.raises(StopIteration):
            next(second_run)


def test_concurrent_notify_requests_for_one_ref_send_once(notify_server, monkeypatch):
    """Two requests for one ref at the same time (core re-posts after its 5 s
    timeout while Telegram is slow): one 201, one 200, one message."""
    srv, base_url, _ = notify_server
    gate = threading.Event()
    real_send = srv.telegram.send_message

    def slow_send(*args, **kwargs):
        gate.wait(timeout=5)
        return real_send(*args, **kwargs)

    monkeypatch.setattr(srv.telegram, "send_message", slow_send)
    results = []
    threads = [threading.Thread(target=lambda: results.append(call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY))) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(timeout=10)
    assert sorted(status for status, _ in results) == [200, 201]
    assert len({body["id"] for _, body in results}) == 1
    assert len(srv.telegram.sent_messages) == 1


def test_notify_rejects_a_bad_body_with_field_paths(notify_server):
    _, base_url, _ = notify_server
    status, body = call(base_url, "/notify", token=HEX, method="POST", body={**NOTIFY_BODY, "kind": "alert", "title": "x" * 201})
    assert status == 400
    assert body["error"] == "invalid notification"
    assert "kind: must be one of reminder, brief" in body["details"]
    assert "title: longer than 200 characters" in body["details"]
    assert "x" * 201 not in json.dumps(body)
    assert call(base_url, "/notify", token=HEX, method="POST", raw=b"{")[0] == 400


def test_a_400_leaves_the_ref_free_for_the_fallback_post(notify_server):
    """Core answers a 400 by re-posting ONCE under the same ref with a safe
    title ("(reminder)", or counts only for a brief): that post must send."""
    srv, base_url, _ = notify_server
    secret = {**NOTIFY_BODY, "title": "token ghp_" + "a" * 40}
    status, body = call(base_url, "/notify", token=HEX, method="POST", body=secret)
    assert status == 400 and "title: secret-like value" in body["details"]
    assert srv.service.store.get_notification_by_ref("fire-1") is None
    status, body = call(base_url, "/notify", token=HEX, method="POST", body={**NOTIFY_BODY, "title": "(reminder)"})
    assert status == 201 and body["deduped"] is False
    assert srv.telegram.sent_messages[-1]["text"] == "⏰ <b>(reminder)</b>\ntoday 14:00"
    brief = {"kind": "brief", "ref": "brief:2026-10-17", "title": "Saturday", "body": "x\u0007y"}
    assert call(base_url, "/notify", token=HEX, method="POST", body=brief)[0] == 400
    status, _ = call(base_url, "/notify", token=HEX, method="POST", body={**brief, "body": "Today: 1 / Open: 0"})
    assert status == 201


def test_notify_answers_502_when_telegram_refuses(notify_server, monkeypatch, caplog):
    srv, base_url, _ = notify_server

    def refuse(*args, **kwargs):
        raise TelegramError("sendMessage: down")

    monkeypatch.setattr(srv.telegram, "send_message", refuse)
    with caplog.at_level("INFO", logger="mailbox"):
        status, body = call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)
    assert (status, body) == (502, {"error": "telegram send failed"})
    assert srv.service.store.get_notification_by_ref("fire-1") is None   # nothing stored: the retry will send
    monkeypatch.undo()
    assert call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)[0] == 201
    assert not any("call the notary" in r.getMessage() or HEX in r.getMessage() for r in caplog.records)


def test_notify_leaves_the_asks_flow_untouched(notify_server):
    """With notify on: asks post, list, resolve and reply exactly as before, and
    a notification is never an ask."""
    srv, base_url, token = notify_server
    status, body = call(base_url, "/notify", token=HEX, method="POST", body=NOTIFY_BODY)
    nid = body["id"]
    status, body = post_ask(base_url, token)
    assert status == 201
    item_id = body["item"]["id"]
    status, body = call(base_url, "/asks", token=token)
    assert status == 200 and [i["id"] for i in body["items"]] == [item_id]
    assert call(base_url, f"/asks/{nid}", token=token)[0] == 404
    assert call(base_url, f"/asks/{nid}/resolve", token=token, method="POST", body={"verdict": "done"})[0] == 404
    assert call(base_url, f"/asks/{item_id}/reply", token=token, method="POST", body={"text": "ok"})[0] == 200
    assert call(base_url, f"/asks/{item_id}/resolve", token=token, method="POST", body={"verdict": "done"})[0] == 200
    status, body = call(base_url, "/events", token=token)
    assert [e["kind"] for e in body["events"]] == ["posted", "reply", "resolved"]
    assert srv.service.store.get_notification(nid).action_taken is None


# --- answers ---------------------------------------------------

ANSWERS_DOC = json.loads((Path(__file__).parent / "answers-vectors.json").read_text(encoding="utf-8"))


def post_questions(base_url, token):
    """An ask whose instructable carries questions on steps 1, 2, 3, 4 and 4.2."""
    status, data = post_ask(base_url, token, project="Atlas", ask=None,
                            instructable=ANSWERS_DOC["instructable"])
    assert status == 201
    return data["item"]["id"]


def answers_call(base_url, token, item_id, answers):
    return call(base_url, f"/asks/{item_id}/answers", token=token, method="POST",
                body={"answers": answers})


def test_answers_store_one_event_each_and_redraw_the_keyboard(server):
    srv, base_url, token = server
    item_id = post_questions(base_url, token)
    status, data = answers_call(base_url, token, item_id, [
        {"step": "1", "picked": ["Prod"]}, {"step": "3", "text": "EU"}])
    assert status == 200
    assert data["item"]["id"] == item_id and data["item"]["status"] == "open"
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert [(e["kind"], e["actor"], e["text"]) for e in got["events"][1:]] == [
        ("answer", "owner", '{"step":"1","id":"s1","picked":["Prod"]}'),
        ("answer", "owner", '{"step":"3","id":"s3","text":"EU"}'),
    ]
    _, events = call(base_url, "/events", token=token)
    assert [e["kind"] for e in events["events"]] == ["posted", "answer", "answer"]
    edit = srv.telegram.edited_markups[-1]
    assert edit["reply_markup"]["inline_keyboard"][0][1]["text"] == "✓ Prod"


def test_invalid_answers_are_400_and_store_nothing(server):
    _, base_url, token = server
    item_id = post_questions(base_url, token)
    status, data = answers_call(base_url, token, item_id, [
        {"step": "1", "picked": ["Prod"]}, {"step": "5", "text": "x"}])
    assert status == 400
    assert data == {"error": "invalid answers", "details": ["answers[1].step: no question at this step"]}
    _, got = call(base_url, f"/asks/{item_id}", token=token)
    assert [e["kind"] for e in got["events"]] == ["posted"]


@pytest.mark.parametrize("body,error", [({}, "missing fields"), (None, "invalid json")])
def test_answers_without_a_list_are_400(server, body, error):
    _, base_url, token = server
    item_id = post_questions(base_url, token)
    status, data = call(base_url, f"/asks/{item_id}/answers", token=token, method="POST",
                        body=body, raw=None if body is not None else b"{nope")
    assert (status, data) == (400, {"error": error})


def test_answers_on_an_unknown_ask_are_404(server):
    _, base_url, token = server
    status, data = answers_call(base_url, token, "nope", [{"step": "1", "picked": ["Prod"]}])
    assert (status, data) == (404, {"error": "not found"})


def test_answers_on_a_closed_ask_are_409_until_it_is_reopened(server):
    _, base_url, token = server
    item_id = post_questions(base_url, token)
    call(base_url, f"/asks/{item_id}/resolve", token=token, method="POST", body={"verdict": "done"})
    status, data = answers_call(base_url, token, item_id, [{"step": "1", "picked": ["Prod"]}])
    assert (status, data) == (409, {"error": "ask is closed"})
    call(base_url, f"/asks/{item_id}/reopen", token=token, method="POST", body={})
    status, _ = answers_call(base_url, token, item_id, [{"step": "1", "picked": ["Prod"]}])
    assert status == 200


# --- MAILBOX_BRIEF_ASKS (household setups) -------------------------------


def test_load_config_brief_asks_default_absent():
    assert "brief_asks" not in load_config(BASE_ENV)


def test_load_config_brief_asks_off():
    assert load_config({**BASE_ENV, "MAILBOX_BRIEF_ASKS": "0"})["brief_asks"] is False


def test_load_config_brief_asks_malformed_warns_and_keeps_default(caplog):
    config = load_config({**BASE_ENV, "MAILBOX_BRIEF_ASKS": "no"})
    assert "brief_asks" not in config
    assert "[mailbox] MAILBOX_BRIEF_ASKS must be 0 or 1; open asks stay on the brief" in caplog.text


def test_brief_without_the_open_asks_line():
    from mailbox.service import Service
    from mailbox.store import Store
    from mailbox.telegram import FakeTelegramClient
    store = Store(":memory:"); store.open()
    tg = FakeTelegramClient()
    svc = Service(store, tg, "personal", brief_asks=False)
    svc.post("Orbit", "an ask")
    svc.notify("brief", "brief:2026-10-26", "Mon 26 Oct", "Today (0)")
    assert "Open asks" not in tg.sent_messages[-1]["text"]
    store.close()


# --- edit keys, amend, revisions on the wire (Pond 1.1.0) -------------------

import copy  # noqa: E402
import hashlib  # noqa: E402
from datetime import timedelta, timezone  # noqa: E402

from mailbox.instructable import validate as _validate  # noqa: E402
from mailbox.revisions import revision_hash, step_ids  # noqa: E402

EDIT_KEY = "k" * 43
EDIT_HASH = hashlib.sha256(EDIT_KEY.encode()).hexdigest()
T0 = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)
# GET /asks keeps the last `days` days by the store's real clock, so a list of asks
# stamped at the pinned T0 asks for a window that covers it whatever today's date is.
ALL_ASKS = "/asks?days=36500"


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def hcall(base_url, path, token, *, method="GET", body=None, raw=None, protocol=None):
    """call() with the X-Hook-Protocol header."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if protocol is not None:
        headers["X-Hook-Protocol"] = str(protocol)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def pond(server):
    """The server with a pinned clock and corrections enabled."""
    srv, base_url, token = server
    clock = _Clock()
    srv.service.utcnow = clock
    srv.service.store.set_setting("corrections_enabled", "1")
    return srv, base_url, token, clock


def _post_editable(base_url, token, instructable=INSTRUCTABLE, key_hash=EDIT_HASH):
    status, data = post_ask(base_url, token, ask=None, instructable=instructable, editKeyHash=key_hash)
    assert status in (200, 201), data
    return data


def _corrected(doc):
    new = copy.deepcopy(doc)
    new["steps"][1]["code"] = "node -v"
    return new


def _amend(base_url, token, item_id, base, instructable, *, key=EDIT_KEY, reason="the flag was wrong"):
    body = {"editKey": key, "baseRev": base["rev"], "baseHash": base["hash"], "reason": reason,
            "instructable": instructable}
    return hcall(base_url, f"/asks/{item_id}/amend", token, method="POST", body=body, protocol=5)


def test_post_with_an_edit_key_answers_revision_1(pond):
    srv, base_url, token, _ = pond
    status, data = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE, editKeyHash=EDIT_HASH)
    assert status == 201
    assert data["editable"] is True and data["revision"]["rev"] == 1
    detail = hcall(base_url, f"/asks/{data['item']['id']}", token, protocol=5)[1]
    served = detail["item"]["revision"]
    assert data["revision"]["hash"] == served["hash"] == revision_hash(1, "", served["at"], "", served["body"])
    assert served["at"] == T0.isoformat(timespec="seconds")
    assert step_ids(json.loads(served["body"])) == ["s1", "s2"]
    assert srv.service.store.edit_key_hash(data["item"]["id"]) == EDIT_HASH


def test_post_refuses_a_malformed_edit_key_hash(pond):
    srv, base_url, token, _ = pond
    for bad in (EDIT_HASH[:63], EDIT_HASH.upper(), 5):
        status, data = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE, editKeyHash=bad)
        assert (status, data) == (400, {"error": "invalid editKeyHash"})
    assert srv.telegram.sent_messages == []
    assert call(base_url, "/asks", token=token)[1]["items"] == []


def test_a_repost_is_editable_only_with_the_same_key_hash(pond):
    _, base_url, token, _ = pond
    _post_editable(base_url, token)
    status, other = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE, editKeyHash="0" * 64)
    assert (status, other["deduped"], other["editable"]) == (200, True, False)
    status, same = post_ask(base_url, token, ask=None, instructable=INSTRUCTABLE, editKeyHash=EDIT_HASH)
    assert (status, same["deduped"], same["editable"]) == (200, True, True)
    assert same["revision"]["rev"] == 1


def test_amend_check_order(pond):
    srv, base_url, token, clock = pond
    store = srv.service.store
    data = _post_editable(base_url, token)
    item_id, base = data["item"]["id"], data["revision"]
    clock.advance(61)
    new = _corrected(data["item"]["payload"]["instructable"])
    path = f"/asks/{item_id}/amend"
    ok_body = {"editKey": EDIT_KEY, "baseRev": 1, "baseHash": base["hash"], "reason": "fix", "instructable": new}

    assert hcall(base_url, path, None, method="POST", body=ok_body) == (401, {"error": "unauthorized"})
    assert hcall(base_url, "/asks/nope/amend", token, method="POST", body=ok_body) == (404, {"error": "not found"})
    store.delete_setting("corrections_enabled")
    assert hcall(base_url, path, token, method="POST", body=ok_body) == (409, {"error": "corrections not enabled"})
    store.set_setting("corrections_enabled", "1")
    assert hcall(base_url, path, token, method="POST", raw=b"{") == (400, {"error": "invalid json"})
    assert hcall(base_url, path, token, method="POST", body={"editKey": EDIT_KEY}) == (400, {"error": "missing fields"})

    plain = post_ask(base_url, token, ask=None, instructable={**INSTRUCTABLE, "title": "No key"})[1]
    assert plain["editable"] is False
    assert hcall(base_url, f"/asks/{plain['item']['id']}/amend", token, method="POST",
                 body={**ok_body, "baseHash": plain["revision"]["hash"]}) == (403, {"error": "not editable"})
    assert hcall(base_url, path, token, method="POST", body={**ok_body, "editKey": "x" * 43}) == (
        403, {"error": "wrong edit key"})

    stale = hcall(base_url, path, token, method="POST", body={**ok_body, "baseHash": "0" * 64})
    assert stale == (409, {"error": "stale revision", "rev": 1, "hash": base["hash"]})
    status, data = hcall(base_url, path, token, method="POST",
                         body={**ok_body, "instructable": {**new, "steps": []}})
    assert (status, data["error"]) == (400, "invalid instructable") and data["details"]
    status, data = hcall(base_url, path, token, method="POST",
                         body={**ok_body, "instructable": {**new, "title": "Another"}})
    assert (status, data) == (400, {"error": "invalid amendment", "details": ["title: cannot change"]})
    same = copy.deepcopy(new)
    same["steps"][1]["code"] = "node --version"
    assert hcall(base_url, path, token, method="POST", body={**ok_body, "instructable": same}) == (
        400, {"error": "no change"})

    closed = _post_editable(base_url, token, instructable={**INSTRUCTABLE, "title": "Closed"})
    call(base_url, f"/asks/{closed['item']['id']}/resolve", token=token, method="POST",
         body={"verdict": "done", "actor": "session"})
    assert hcall(base_url, f"/asks/{closed['item']['id']}/amend", token, method="POST",
                 body={**ok_body, "baseHash": closed["revision"]["hash"]}) == (409, {"error": "ask is closed"})


def test_amend_too_soon_and_the_revision_limit(pond):
    srv, base_url, token, clock = pond
    data = _post_editable(base_url, token)
    item_id, base = data["item"]["id"], data["revision"]
    doc = data["item"]["payload"]["instructable"]
    clock.advance(59)
    assert _amend(base_url, token, item_id, base, _corrected(doc)) == (429, {"error": "too soon", "retryAfter": 1})
    for n in range(10):
        clock.advance(60)
        doc = copy.deepcopy(doc)
        doc["steps"][0]["do"] = f"Open Slack, try {n}"
        status, out = _amend(base_url, token, item_id, base, doc)
        assert status == 200, out
        base = out["revision"]
    assert base["rev"] == 11
    clock.advance(60)
    doc["steps"][0]["do"] = "one too many"
    assert _amend(base_url, token, item_id, base, doc) == (409, {"error": "revision limit reached"})


def test_amend_success_and_the_read_shapes(pond):
    srv, base_url, token, clock = pond
    data = _post_editable(base_url, token)
    item_id, base = data["item"]["id"], data["revision"]
    clock.advance(61)
    status, out = _amend(base_url, token, item_id, base, _corrected(data["item"]["payload"]["instructable"]))
    assert status == 200
    assert out["revision"]["rev"] == 2
    assert out["revision"]["changed"] == [{"id": "s2", "path": "2", "fields": ["code"]}]
    assert {k: out["revision"][k] for k in ("added", "removed", "moved", "top")} == {
        "added": [], "removed": [], "moved": [], "top": []}

    status, detail = hcall(base_url, f"/asks/{item_id}", token, protocol=5)
    assert [r["rev"] for r in detail["revisions"]] == [1, 2]
    assert detail["item"]["revision"]["acceptedRev"] == 1
    assert detail["item"]["revision"]["prev"] == base["hash"]
    assert detail["item"]["payload"]["instructable"]["steps"][1]["code"] == "node -v"
    listed = hcall(base_url, ALL_ASKS, token, protocol=5)[1]["items"][0]
    assert listed["revision"]["rev"] == 2 and "revisions" not in listed

    for path in (f"/asks/{item_id}", ALL_ASKS):
        status, old = hcall(base_url, path, token)
        item = old["item"] if "item" in old else old["items"][0]
        assert "revision" not in item and "revisions" not in old
        stub = item["payload"]["instructable"]
        assert stub["steps"] == [{"do": "Update Hook: menu bar › Check for Updates…", "id": "s1"}]
        assert item["payload"]["why"] == stub["why"]
        assert _validate(stub)[1] == []


def test_a_rev_1_ask_without_the_header_is_not_a_stub(pond):
    _, base_url, token, _ = pond
    data = _post_editable(base_url, token)
    item = hcall(base_url, f"/asks/{data['item']['id']}", token)[1]["item"]
    assert item["payload"]["instructable"]["steps"][0]["do"] == INSTRUCTABLE["steps"][0]["do"]
    assert "revision" not in item


def test_a_duplicate_key_in_any_post_body_is_invalid_json(pond):
    _, base_url, token, _ = pond
    for path in ("/asks", "/notify", "/asks/x/reply", "/owner/enroll"):
        assert hcall(base_url, path, token, method="POST", raw=b'{"a":1,"a":2}') == (400, {"error": "invalid json"})


def test_events_carry_rev_and_auth_on_both_read_paths(pond):
    _, base_url, token, clock = pond
    data = _post_editable(base_url, token)
    item_id = data["item"]["id"]
    clock.advance(61)
    _amend(base_url, token, item_id, data["revision"], _corrected(data["item"]["payload"]["instructable"]))
    events = call(base_url, "/events", token=token)[1]["events"]
    detail = call(base_url, f"/asks/{item_id}", token=token)[1]["events"]
    for rows in (events, detail):
        by_kind = {e["kind"]: e for e in rows}
        assert by_kind["revised"]["rev"] == 2 and by_kind["revised"]["auth"] is None
        assert by_kind["posted"]["rev"] is None and by_kind["posted"]["auth"] is None
        assert json.loads(by_kind["revised"]["text"])["reason"] == "the flag was wrong"


# --- Hook bugs: POST /asks with kind "bug", GET /asks?kind= (Pond 1.2.0) ----------

BUG_SIG = "a3f9c2e17b04"


def _bug_doc(sig=BUG_SIG, code="Crash signature: hook-crash-a3f9c2e17b04\nException: EXC_BAD_ACCESS"):
    return {"v": 1, "title": f"Hook crash: EXC_BAD_ACCESS in SettingsWindow.open ({sig})", "urgency": "whenever",
            "why": "Hook quit unexpectedly.", "steps": [{"do": "Read the crash report", "code": code, "lang": "text"}]}


def _approved_key(srv, label="Studio"):
    """An approved owner key: enrolled, then the owner's Approve tap on Telegram."""
    from mailbox.owner import key_hash
    secret = "owner-secret-" + label
    k = srv.service.enroll(key_hash(secret), label)
    srv.service.handle_update({"callback_query": {
        "id": "cb", "from": {"id": OWNER_ID}, "data": f"e:{k.id}:y",
        "message": {"message_id": 1, "chat": {"id": OWNER_ID}},
    }})
    assert srv.service.store.get_owner_key(k.id).status == "approved"
    return secret


def kcall(base_url, path, token, *, method="GET", body=None, key=None, protocol=None):
    """call() with the owner key and X-Hook-Protocol headers."""
    from mailbox import owner
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers[owner.KEY_HEADER] = key
    if protocol is not None:
        headers["X-Hook-Protocol"] = str(protocol)
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def bugs(server):
    """The server, an approved owner key, and Telegram's record emptied."""
    srv, base_url, token = server
    secret = _approved_key(srv)
    srv.telegram.calls.clear()
    srv.telegram.sent_messages.clear()
    srv.telegram.edited_messages.clear()
    return srv, base_url, token, secret


def post_bug(base_url, token, key, *, sig=BUG_SIG, doc=None, protocol=None, **extra):
    body = {"project": "Hook", "context": "personal", "kind": "bug", "signature": sig,
            "instructable": doc or _bug_doc(sig), **extra}
    return kcall(base_url, "/asks", token, method="POST", body=body, key=key, protocol=protocol)


def test_a_new_bug_is_201_and_sends_nothing_to_telegram(bugs):
    srv, base_url, token, key = bugs
    status, data = post_bug(base_url, token, key)
    assert status == 201, data
    assert data["occurrences"] == 1 and "deduped" not in data
    item = data["item"]
    assert item["kind"] == "bug"
    assert item["bug"]["signature"] == BUG_SIG and item["bug"]["count"] == 1
    assert item["bug"]["firstSeen"] == item["bug"]["lastSeen"]
    assert item["payload"]["tg_message_id"] is None
    assert srv.telegram.calls == []


def test_a_repeated_signature_is_200_deduped_with_the_count(bugs):
    srv, base_url, token, key = bugs
    _, first = post_bug(base_url, token, key)
    status, again = post_bug(base_url, token, key, doc=_bug_doc(code="Crash signature: hook-crash-x\nsecond"))
    assert status == 200
    assert again["deduped"] is True and again["occurrences"] == 2
    assert again["item"]["id"] == first["item"]["id"] and again["item"]["bug"]["count"] == 2
    assert len(call(base_url, "/asks?kind=bug", token=token)[1]["items"]) == 1
    events = call(base_url, f"/asks/{first['item']['id']}", token=token)[1]["events"]
    assert [(e["kind"], e["actor"]) for e in events] == [("posted", "hook"), ("occurrence", "hook")]
    assert srv.telegram.calls == []


def test_resolving_a_bug_with_a_note_sends_nothing_and_its_signature_then_files_anew(bugs):
    srv, base_url, token, key = bugs
    _, first = post_bug(base_url, token, key)
    item_id = first["item"]["id"]
    status, _ = call(base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
                     body={"verdict": "done", "note": "fixed in 0.12.1"})
    assert status == 200
    status, again = post_bug(base_url, token, key)
    assert status == 201 and again["item"]["id"] != item_id and again["occurrences"] == 1
    assert srv.telegram.calls == []


def test_a_bug_without_the_owner_key_is_403(bugs):
    srv, base_url, token, _ = bugs
    assert post_bug(base_url, token, None) == (403, {"error": "owner key required"})
    assert post_bug(base_url, token, "not-a-key") == (403, {"error": "unknown owner key"})
    assert srv.service.store.list_items(kind="bug") == []


@pytest.mark.parametrize("sig", ["A3F9C2E17B04", "a3f9c2e17b0", "a3f9c2e17b045", "hook-crash-a3f9", "", 12, None])
def test_a_bad_signature_is_400(bugs, sig):
    _, base_url, token, key = bugs
    assert post_bug(base_url, token, key, sig=sig, doc=_bug_doc()) == (400, {"error": "invalid signature"})


@pytest.mark.parametrize("kind", ["Bug", "crash", "", 1, True])
def test_an_unknown_kind_is_400(bugs, kind):
    _, base_url, token, key = bugs
    assert post_bug(base_url, token, key, kind=kind) == (400, {"error": "invalid kind"})


def test_kind_ask_is_a_plain_ask(bugs):
    srv, base_url, token, _ = bugs
    status, data = post_ask(base_url, token, kind="ask")
    assert status == 201 and "kind" not in data["item"]
    assert len(srv.telegram.sent_messages) == 1


def test_a_bug_still_needs_an_instructable_and_the_right_context(bugs):
    _, base_url, token, key = bugs
    body = {"project": "Hook", "context": "personal", "kind": "bug", "signature": BUG_SIG, "ask": "x"}
    assert kcall(base_url, "/asks", token, method="POST", body=body, key=key) == (400, {"error": "missing fields"})
    assert post_bug(base_url, token, key, context="work") == (403, {"error": "wrong context"})
    status, data = post_bug(base_url, token, key, doc={**_bug_doc(), "steps": []})
    assert status == 400 and data["error"] == "invalid instructable"


def test_the_kind_filter_on_get_asks(bugs):
    _, base_url, token, key = bugs
    _, ask = post_ask(base_url, token)
    _, bug = post_bug(base_url, token, key)
    everything = call(base_url, "/asks", token=token)[1]["items"]
    assert {i["id"] for i in everything} == {ask["item"]["id"], bug["item"]["id"]}
    assert [i["id"] for i in call(base_url, "/asks?kind=bug", token=token)[1]["items"]] == [bug["item"]["id"]]
    assert [i["id"] for i in call(base_url, "/asks?kind=ask", token=token)[1]["items"]] == [ask["item"]["id"]]
    for bad in ("/asks?kind=bugs", "/asks?kind=", "/asks?kind=BUG"):
        assert call(base_url, bad, token=token) == (400, {"error": "invalid query"})


def test_the_plain_ask_title_dedupe_is_untouched_by_bugs(bugs):
    _, base_url, token, key = bugs
    _, bug = post_bug(base_url, token, key)
    status, ask = post_ask(base_url, token, project="Hook", ask=None, instructable=_bug_doc())
    assert status == 201 and ask["item"]["id"] != bug["item"]["id"]
    status, again = post_ask(base_url, token, project="Hook", ask=None, instructable=_bug_doc())
    assert status == 200 and again["deduped"] is True and again["item"]["id"] == ask["item"]["id"]


def test_a_bug_reads_the_same_for_protocol_4_and_5_clients(bugs):
    _, base_url, token, key = bugs
    _, bug = post_bug(base_url, token, key)
    item_id = bug["item"]["id"]
    old = kcall(base_url, f"/asks/{item_id}", token, protocol=4)[1]["item"]
    assert "revision" not in old
    assert old["payload"]["instructable"]["steps"][0]["do"] == "Read the crash report"
    assert old["payload"]["instructable"]["steps"][0]["id"] == "s1"
    new = kcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    assert new["item"]["revision"]["rev"] == 1 and [r["rev"] for r in new["revisions"]] == [1]
    assert old["kind"] == new["item"]["kind"] == "bug"


# --- messages: POST /asks/{id}/reply with kind "message" (Pond 1.2.0) ----------

def _message(base_url, token, item_id, text, *, key=None, **extra):
    return kcall(base_url, f"/asks/{item_id}/reply", token, method="POST", body={"text": text, **extra}, key=key)


def test_a_message_is_stored_as_a_message_and_reaches_the_owner_routes(server):
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    calls = len(srv.telegram.calls)
    text = "Use the staging key first.\nThen prod."
    status, data = _message(base_url, token, item_id, text, kind="message")
    assert status == 200 and data["item"]["status"] == "open"
    assert len(srv.telegram.calls) == calls
    detail = call(base_url, f"/asks/{item_id}", token=token)[1]["events"]
    stream = call(base_url, "/events", token=token)[1]["events"]
    for rows in (detail, stream):
        last = rows[-1]
        assert (last["kind"], last["actor"], last["text"], last["auth"]) == ("message", "owner", text, "token")
        assert not any(e["kind"] == "reply" for e in rows)


def test_a_message_with_the_owner_key_records_it(server):
    srv, base_url, token = server
    key = _approved_key(srv)
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    assert _message(base_url, token, item_id, "hello", kind="message", key=key)[0] == 200
    last = call(base_url, f"/asks/{item_id}", token=token)[1]["events"][-1]
    assert (last["kind"], last["auth"]) == ("message", "owner-key")


@pytest.mark.parametrize("kind", [None, "reply"])
def test_a_plain_reply_is_unchanged_with_or_without_kind(server, kind):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    extra = {} if kind is None else {"kind": kind}
    assert _message(base_url, token, item_id, " yes ", **extra)[0] == 200
    last = call(base_url, f"/asks/{item_id}", token=token)[1]["events"][-1]
    assert (last["kind"], last["text"]) == ("reply", "yes")


@pytest.mark.parametrize("kind", ["Message", "note", "", 3])
def test_an_unknown_reply_kind_is_400(server, kind):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    assert _message(base_url, token, created["item"]["id"], "x", kind=kind) == (400, {"error": "invalid kind"})


def test_a_secret_like_message_is_400_and_never_echoed(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    secret = "sk-" + "Ab1" * 16
    status, data = _message(base_url, token, created["item"]["id"], f"try {secret}", kind="message")
    assert (status, data) == (400, {"error": "invalid message", "details": ["text: secret-like value"]})
    assert secret not in json.dumps(data)


def test_message_up_to_8000_over_http(server):
    srv, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    status, data = _message(base_url, token, item_id, "m" * 8000, kind="message")
    assert status == 200
    assert [e for e in srv.service.store.events_for_item(item_id)][-1].kind == "message"
    assert _message(base_url, token, item_id, "m" * 8001, kind="message") == (
        400, {"error": "invalid message", "details": ["text: too long"]})


def test_a_message_on_a_closed_ask_and_past_the_cap_is_409(server):
    _, base_url, token = server
    _, created = post_ask(base_url, token)
    item_id = created["item"]["id"]
    for n in range(50):
        assert _message(base_url, token, item_id, f"note {n}", kind="message")[0] == 200
    assert _message(base_url, token, item_id, "51", kind="message") == (409, {"error": "too many messages"})
    resolve(base_url, token, item_id)
    assert _message(base_url, token, item_id, "late", kind="message") == (409, {"error": "ask is closed"})
    assert _message(base_url, token, "nope", "x", kind="message") == (404, {"error": "not found"})


# --- the Pond 1.2.0 compatibility pass -------------------------------------------

def test_the_bug_message_and_requirements_vectors_are_what_the_pond_answers_today():
    """bug-vectors.json, message-vectors.json and requirements-vectors.json, which
    Hook's tests copy, are recorded from this Pond; a wire change without a
    regenerated file fails here (python3 -m mailbox.tests.gen_pond_vectors)."""
    from mailbox.tests.gen_pond_vectors import BUG_OUT, MESSAGE_OUT, REQUIREMENTS_OUT, build, dump
    for kind, path in (("bug", BUG_OUT), ("message", MESSAGE_OUT), ("requirements", REQUIREMENTS_OUT)):
        assert dump(build(kind)) == path.read_text(encoding="utf-8"), path.name


def test_the_vectors_cover_both_kinds_and_their_refusals():
    from mailbox.tests.gen_pond_vectors import BUG_OUT, MESSAGE_OUT
    bugs = {c["name"]: c for c in json.loads(BUG_OUT.read_text(encoding="utf-8"))["cases"]}
    messages = {c["name"]: c for c in json.loads(MESSAGE_OUT.read_text(encoding="utf-8"))["cases"]}
    statuses = {name: c["response"]["status"] for name, c in {**bugs, **messages}.items()}
    assert {statuses[n] for n in ("bug-new", "bug-after-close")} == {201}
    assert statuses["bug-repeat"] == 200 and bugs["bug-repeat"]["response"]["body"]["occurrences"] == 2
    assert {statuses[n] for n in ("bug-no-owner-key",)} == {403}
    assert {statuses[n] for n in ("bug-bad-signature", "bug-bad-kind", "bug-list-bad-kind",
                                  "message-bad-kind", "message-secret", "message-too-long", "message-blank")} == {400}
    assert statuses["message-closed"] == 409
    kinds = [e["kind"] for e in messages["message-events"]["response"]["body"]["events"]]
    assert kinds == ["posted", "message", "reply", "reply"]
    for text in (BUG_OUT.read_text(encoding="utf-8"), MESSAGE_OUT.read_text(encoding="utf-8")):
        assert "/Users/" not in text and "/home/" not in text and ".ts.net" not in text and not re.search(r"\b(?!ISO-|SHA-|UTF-)[A-Z]{2,5}-\d+\b", text)


def test_a_1_1_0_client_reads_a_bug_as_an_ordinary_ask_with_extra_keys(bugs):
    """No X-Hook-Protocol and no kind anywhere: the keys a plain ask has, the same
    payload shape, plus `kind` and `bug`; the list without ?kind= has both."""
    _, base_url, token, key = bugs
    _, ask = post_ask(base_url, token, ask=None, instructable=_bug_doc("b" * 12) | {"title": "Rotate the key"})
    _, bug = post_bug(base_url, token, key)
    plain = call(base_url, f"/asks/{ask['item']['id']}", token=token)[1]
    filed = call(base_url, f"/asks/{bug['item']['id']}", token=token)[1]
    assert set(filed) == set(plain) == {"item", "events"}
    assert set(filed["item"]) - set(plain["item"]) == {"kind", "bug"}
    assert set(plain["item"]) <= set(filed["item"])
    assert set(filed["item"]["payload"]) == set(plain["item"]["payload"])
    assert set(filed["events"][0]) == set(plain["events"][0])
    listed = call(base_url, "/asks", token=token)[1]["items"]
    assert {i["id"] for i in listed} == {ask["item"]["id"], bug["item"]["id"]}


def test_an_older_hooks_plain_reply_and_answers_still_work_on_1_2_0(server):
    """What Hook 0.11 sends, unchanged: a reply with no kind, Done by the owner."""
    _, base_url, token = server
    _, created = post_ask(base_url, token, ask=None, instructable={
        "v": 1, "title": "Pick one", "steps": [{"do": "Pick", "question": {"prompt": "Which?", "choices": ["A", "B"]}}]})
    item_id = created["item"]["id"]
    assert call(base_url, f"/asks/{item_id}/reply", token=token, method="POST", body={"text": "ok"})[0] == 200
    assert call(base_url, f"/asks/{item_id}/answers", token=token, method="POST",
                body={"answers": [{"step": "1", "picked": ["A"]}]})[0] == 200
    assert call(base_url, f"/asks/{item_id}/resolve", token=token, method="POST",
                body={"verdict": "done", "actor": "owner"})[0] == 200
    kinds = [e["kind"] for e in call(base_url, f"/asks/{item_id}", token=token)[1]["events"]]
    assert kinds == ["posted", "reply", "answer", "resolved"]


# --- POST /asks/{id}/progress and `requirements` (Pond 1.3.0) --------------------

REQ_DOC = {"v": 1, "title": "Rotate the staging deploy key",
           "steps": [{"do": "Open the deploy settings"},
                     {"do": "Rotate the key", "required": True},
                     {"do": "Pick", "question": {"prompt": "Env?", "choices": ["Staging", "Preview"]}}],
           "reply": {"prompt": "Did the deploy pass?", "choices": []}}
STEP_2_LEFT = {"left": [{"kind": "step", "id": "s2", "path": "2"}], "ready": False}


def _req_post(base_url, token, doc=REQ_DOC, **top):
    status, data = post_ask(base_url, token, ask=None, instructable={**doc, **top}, editKeyHash=EDIT_HASH)
    assert status == 201, data
    return data["item"]["id"]


def _progress(base_url, token, item_id, *, key=None, raw=None, protocol=5, **body):
    if raw is not None:
        return hcall(base_url, f"/asks/{item_id}/progress", token, method="POST", raw=raw, protocol=protocol)
    return kcall(base_url, f"/asks/{item_id}/progress", token, method="POST", body=body, key=key,
                 protocol=protocol)


def test_progress_without_the_bearer_is_401(server):
    _, base_url, _ = server
    assert _progress(base_url, "", "nope", tick=["s1"])[0] == 401
    assert call(base_url, "/asks/nope/progress", method="POST", body={}) == (401, {"error": "unauthorized"})


def test_progress_check_order(server):
    srv, base_url, token = server
    item_id = _req_post(base_url, token)
    assert _progress(base_url, token, "nope", tick=["s1"]) == (404, {"error": "not found"})
    assert _progress(base_url, token, "nope", raw=b"{nope") == (404, {"error": "not found"})
    assert _progress(base_url, token, item_id, raw=b"{nope") == (400, {"error": "invalid json"})
    assert _progress(base_url, token, item_id) == (400, {"error": "missing fields"})
    assert _progress(base_url, token, item_id, tick=None, untick=None) == (400, {"error": "missing fields"})
    assert _progress(base_url, token, item_id, tick=["s1"], rev="1") == (400, {"error": "invalid revision"})
    assert _progress(base_url, token, item_id, tick=["s9"], key="not-a-key") == (403, {"error": "unknown owner key"})
    from mailbox.owner import SETTING_REQUIRED
    srv.service.store.set_setting(SETTING_REQUIRED, "1")
    assert _progress(base_url, token, item_id, tick=["s9"]) == (403, {"error": "owner key required"})
    srv.service.store.set_setting(SETTING_REQUIRED, "0")
    status, data = _progress(base_url, token, item_id, tick=["s9"], rev=1, hash="0" * 64)
    assert status == 409 and data["error"] == "ask was corrected"
    assert _progress(base_url, token, item_id, tick=["s1", "s9"], untick=["s1"]) == (
        400, {"error": "invalid progress", "details": ["tick[1]: unknown step", "untick[0]: also in tick"]})
    resolve(base_url, token, item_id, "declined")
    assert _progress(base_url, token, item_id, tick=["s9"], rev=1, hash="0" * 64) == (
        409, {"error": "ask is closed"})


def test_progress_records_an_event_and_answers_the_item_with_requirements(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token)
    status, data = _progress(base_url, token, item_id, tick=["s2"], rev=1)
    assert status == 200
    assert data["item"]["id"] == item_id and data["item"]["requirements"] == {"left": [], "ready": True}
    events = call(base_url, f"/asks/{item_id}", token=token)[1]["events"]
    assert [(e["kind"], e["actor"], e["text"], e["rev"], e["auth"]) for e in events[1:]] == [
        ("progress", "owner", '{"tick":["s2"],"untick":[]}', 1, "token")]
    assert _progress(base_url, token, item_id, tick=["s2"])[0] == 200          # no change: nothing stored
    assert len(call(base_url, f"/asks/{item_id}", token=token)[1]["events"]) == 2
    status, data = _progress(base_url, token, item_id, untick=["s2"])
    assert data["item"]["requirements"] == STEP_2_LEFT


def test_progress_without_requirements(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token, {**REQ_DOC, "steps": [{"do": "Open the deploy settings"}]})
    status, data = _progress(base_url, token, item_id, tick=["s1"])
    assert status == 200 and "requirements" not in data["item"]
    assert [e["kind"] for e in call(base_url, f"/asks/{item_id}", token=token)[1]["events"]] == ["posted", "progress"]


def test_requirements_on_get_ask_but_never_on_the_list(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token)
    plain_id = _req_post(base_url, token, {**REQ_DOC, "title": "Plain", "steps": [{"do": "Open it"}]})
    for protocol in (None, 5):
        got = hcall(base_url, f"/asks/{item_id}", token, protocol=protocol)[1]
        assert got["item"]["requirements"] == STEP_2_LEFT
        assert "requirements" not in hcall(base_url, f"/asks/{plain_id}", token, protocol=protocol)[1]["item"]
    listed = call(base_url, "/asks", token=token)[1]["items"]
    assert {i["id"] for i in listed} == {item_id, plain_id}
    assert not any("requirements" in i for i in listed)


def test_every_single_item_answer_carries_requirements(server):
    srv, base_url, token = server
    key = _approved_key(srv)
    item_id = _req_post(base_url, token)
    current = srv.service.store.current_revision(item_id)
    routes = [
        ("reply", {"text": "it passed"}),
        ("reply", {"text": "thanks", "kind": "message"}),
        ("answers", {"answers": [{"step": "3", "picked": ["Staging"]}]}),
        ("accept", {"rev": current.rev, "hash": current.hash}),
        ("problems", {"step": "1", "rev": current.rev, "hash": current.hash, "note": "the menu moved"}),
        ("resolve", {"verdict": "declined", "actor": "owner"}),
        ("reopen", {}),
    ]
    for route, body in routes:
        status, data = kcall(base_url, f"/asks/{item_id}/{route}", token, method="POST", body=body, key=key,
                             protocol=5)
        assert status == 200, (route, data)
        assert data["item"]["requirements"] == STEP_2_LEFT, route


def test_done_message_reads_ready_after_a_message(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token, {**REQ_DOC, "steps": [{"do": "Open the deploy settings"}]},
                        done=["message"])
    assert call(base_url, f"/asks/{item_id}", token=token)[1]["item"]["requirements"] == {
        "left": [{"kind": "message"}], "ready": False}
    status, data = call(base_url, f"/asks/{item_id}/reply", token=token, method="POST",
                        body={"text": "Rotated it.", "kind": "message"})
    assert status == 200 and data["item"]["requirements"] == {"left": [], "ready": True}


def test_done_reply_reads_ready_after_a_telegram_text_reply(server):
    srv, base_url, token = server
    item_id = _req_post(base_url, token, {**REQ_DOC, "steps": [{"do": "Open the deploy settings"}]},
                        done=["reply"])
    message_id = json.loads(srv.service.store.get_item(item_id).payload)["tg_message_id"]
    srv.service.handle_update({"update_id": 77, "message": {
        "message_id": 501, "from": {"id": OWNER_ID}, "chat": {"id": OWNER_ID, "type": "private"},
        "text": "the deploy passed", "reply_to_message": {"message_id": message_id}}})
    assert call(base_url, f"/asks/{item_id}", token=token)[1]["item"]["requirements"] == {"left": [], "ready": True}


# --- the owner's Done waits for the requirements (Pond 1.3.0) --------------------


def _resolve_body(base_url, token, item_id, **body):
    return kcall(base_url, f"/asks/{item_id}/resolve", token, method="POST", body=body, protocol=5)


def test_an_owner_done_with_something_left_is_409_requirements_unmet(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token, done=["reply", "message"])
    before = call(base_url, f"/asks/{item_id}", token=token)[1]["events"]
    assert _resolve_body(base_url, token, item_id, verdict="done", actor="owner", rev=1) == (409, {
        "error": "requirements_unmet",
        "left": [{"kind": "step", "id": "s2", "path": "2"}, {"kind": "reply"}, {"kind": "message"}],
        "details": ["step 2", "the reply", "a message"]})
    after = call(base_url, f"/asks/{item_id}", token=token)[1]
    assert after["item"]["status"] == "open" and after["events"] == before


def test_a_session_done_and_an_owner_skip_pass_the_gate(server):
    _, base_url, token = server
    first = _req_post(base_url, token)
    status, data = _resolve_body(base_url, token, first, verdict="done", actor="session")
    assert status == 200 and data["item"]["status"] == "done"
    second = _req_post(base_url, token, {**REQ_DOC, "title": "Rotate the preview deploy key"})
    status, data = _resolve_body(base_url, token, second, verdict="declined", actor="owner")
    assert status == 200 and data["item"]["status"] == "dismissed"


def test_a_stale_owner_done_gets_ask_was_corrected_first(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token)
    status, data = _resolve_body(base_url, token, item_id, verdict="done", actor="owner", rev=1, hash="0" * 64)
    assert status == 409 and data["error"] == "ask was corrected"


def test_an_owner_done_after_the_last_tick_closes(server):
    _, base_url, token = server
    item_id = _req_post(base_url, token)
    assert _progress(base_url, token, item_id, tick=["s2"], rev=1)[0] == 200
    status, data = _resolve_body(base_url, token, item_id, verdict="done", actor="owner", rev=1)
    assert status == 200 and data["item"]["status"] == "done"
    assert data["item"]["requirements"] == {"left": [], "ready": True}


# --- the Pond 1.3.0 compatibility pass ------------------------------------------

def test_the_requirements_vectors_cover_progress_the_gate_and_the_item_key():
    from mailbox.tests.gen_pond_vectors import REQUIREMENTS_OUT
    text = REQUIREMENTS_OUT.read_text(encoding="utf-8")
    doc = json.loads(text)
    assert doc["pond"] == "1.3.0"
    cases = {c["name"]: c for c in doc["cases"]}
    statuses = {name: c["response"]["status"] for name, c in cases.items()}
    body = {name: c["response"]["body"] for name, c in cases.items()}
    posted = cases["requirements-post"]["request"]["body"]["instructable"]
    assert posted["done"] == ["reply", "message"] and posted["steps"][1]["required"] is True
    assert statuses["requirements-post"] == 201
    assert body["requirements-detail-left"]["item"]["requirements"] == {"left": [
        {"kind": "step", "id": "s2", "path": "2"}, {"kind": "reply"}, {"kind": "message"}], "ready": False}
    assert statuses["requirements-progress-tick"] == 200
    assert body["requirements-progress-tick"]["item"]["requirements"]["left"] == [{"kind": "reply"},
                                                                                {"kind": "message"}]
    assert body["requirements-progress-noop"] == body["requirements-progress-tick"]
    assert statuses["requirements-progress-invalid"] == 400
    assert body["requirements-progress-invalid"] == {"error": "invalid progress", "details": [
        "tick[0]: unknown step", "untick[0]: also in tick"]}
    assert (statuses["requirements-done-unmet"], body["requirements-done-unmet"]) == (409, {
        "error": "requirements_unmet", "left": [{"kind": "reply"}, {"kind": "message"}],
        "details": ["the reply", "a message"]})
    assert body["requirements-detail-ready"]["item"]["requirements"] == {"left": [], "ready": True}
    events = body["requirements-detail-ready"]["events"]
    assert [e["kind"] for e in events] == ["posted", "progress", "reply", "message"]
    assert events[1]["text"] == '{"tick":["s2"],"untick":[]}' and events[1]["auth"] == "owner-key"
    assert statuses["requirements-done-owner"] == 200
    assert body["requirements-done-owner"]["item"]["status"] == "done"
    assert (statuses["requirements-done-session"], body["requirements-done-session"]["item"]["status"]) == (
        200, "done")
    assert (statuses["requirements-skip-owner"], body["requirements-skip-owner"]["item"]["status"]) == (
        200, "dismissed")
    assert body["requirements-done-session"]["item"]["requirements"]["ready"] is False
    assert statuses["requirements-progress-closed"] == 409
    listed = body["requirements-list"]["items"]
    assert len(listed) == 1 and "requirements" not in listed[0]
    assert "/Users/" not in text and "/home/" not in text and ".ts.net" not in text and not re.search(r"\b(?!ISO-|SHA-|UTF-)[A-Z]{2,5}-\d+\b", text)
    assert not re.search(r"ghp_|xox[abp]-|sk-[A-Za-z0-9]|AKIA|-----BEGIN", text)


HOOK_012_ASK = {"v": 1, "title": "Rotate the staging deploy key",
                "steps": [{"do": "Open the deploy settings"},
                          {"do": "Rotate the key", "required": True},
                          {"do": "Pick", "question": {"prompt": "Env?", "choices": ["Staging", "Preview"]}}],
                "reply": {"prompt": "Did the deploy pass?", "choices": []}}


def _notice(body: dict) -> str:
    """What Hook 0.12's QueueError.rejected says in its "Couldn't send Done" Notice:
    the error, then the details joined with ", "."""
    details = body.get("details") or []
    return body["error"] + (": " + ", ".join(details) if details else "")


class TestHook012OnPond130:
    """A Hook 0.12 client against Pond 1.3.0: it has no gate for `required`, never sends
    progress and reads no `requirements`. It keeps working, and its refused Done reads
    sensibly in the Notice it already has."""

    def _ask(self, server, title=HOOK_012_ASK["title"]):
        srv, base_url, token = server
        key = _approved_key(srv)
        status, data = kcall(base_url, "/asks", token, method="POST", key=key, protocol=5, body={
            "project": "Example", "context": "personal", "instructable": {**HOOK_012_ASK, "title": title},
            "editKeyHash": EDIT_HASH})
        assert status == 201, data
        return base_url, token, key, data["item"]["id"]

    def test_a_post_with_required_is_accepted(self, server):
        base_url, token, _key, item_id = self._ask(server)
        got = call(base_url, f"/asks/{item_id}", token=token)[1]
        assert got["item"]["payload"]["instructable"]["steps"][1]["required"] is True

    def test_its_owner_done_without_progress_reads_requirements_unmet_step_2(self, server):
        base_url, token, key, item_id = self._ask(server)
        status, body = kcall(base_url, f"/asks/{item_id}/resolve", token, method="POST", key=key, protocol=5,
                             body={"verdict": "done", "actor": "owner", "rev": 1})
        assert status == 409
        assert _notice(body) == "requirements_unmet: step 2"
        assert call(base_url, f"/asks/{item_id}", token=token)[1]["item"]["status"] == "open"

    def test_its_skip_reply_answers_and_message_work(self, server):
        base_url, token, key, item_id = self._ask(server)
        for route, body in (("reply", {"text": "it passed"}),
                            ("answers", {"answers": [{"step": "3", "picked": ["Staging"]}], "rev": 1}),
                            ("reply", {"text": "Rotated the staging one.", "kind": "message"}),
                            ("resolve", {"verdict": "declined", "actor": "owner"})):
            status, data = kcall(base_url, f"/asks/{item_id}/{route}", token, method="POST", key=key,
                                 protocol=5, body=body)
            assert status == 200, (route, data)
        kinds = [e["kind"] for e in call(base_url, f"/asks/{item_id}", token=token)[1]["events"]]
        assert kinds == ["posted", "reply", "answer", "message", "declined"]

    def test_the_extra_key_and_progress_events_break_no_1_2_0_reader(self, server):
        """An ask without the keys reads exactly as on 1.2.0; one with them adds only
        `requirements` on its own detail, and a progress event has an event's keys."""
        base_url, token, key, item_id = self._ask(server)
        plain_id = post_ask(base_url, token, ask=None, instructable={
            "v": 1, "title": "Plain", "steps": [{"do": "Open it"}]})[1]["item"]["id"]
        before = call(base_url, "/asks", token=token)[1]
        assert _progress(base_url, token, item_id, tick=["s2"], key=key)[0] == 200
        after = call(base_url, "/asks", token=token)[1]
        assert after == before                      # the list never carries requirements or progress
        plain = call(base_url, f"/asks/{plain_id}", token=token)[1]
        gated = call(base_url, f"/asks/{item_id}", token=token)[1]
        assert set(gated["item"]) - set(plain["item"]) == {"requirements"}
        assert "requirements" not in plain["item"]
        progress = [e for e in gated["events"] if e["kind"] == "progress"]
        assert len(progress) == 1 and set(progress[0]) == set(gated["events"][0])
        events = call(base_url, "/events?after=0&limit=100", token=token)[1]["events"]
        assert {e["kind"] for e in events} >= {"posted", "progress"}
        for e in events:
            assert set(e) == set(events[0])

    def test_the_version_meets_1_3_0_with_protocol_5(self, server):
        """Pond 1.4.0 is an additive minor: a Hook 0.12 or 0.13 range (^1.1.0) still
        matches and the protocol integer stays 5."""
        from mailbox import FEATURES
        _, base_url, token = server
        assert call(base_url, "/version", token=token)[1] == {"service": "pond", "version": "1.15.1", "protocol": 5, "features": list(FEATURES)}


# --- choice objects and clients before protocol 6 (Pond 1.4.0) ----------------------

CHOICE_DOC = {"v": 1, "title": "Roll out the change", "urgency": "today", "steps": [
    {"do": "Read the plan"},
    {"do": "Pick the rollout", "question": {"prompt": "How?", "choices": [
        "All at once", {"label": "Mix", "text": "What should change?"}, "Staff first"]}}],
    "reply": {"prompt": "Anything else?", "choices": ["No", {"label": "Yes", "text": "What else?"}]}}


def _choice_post(base_url, token, doc=CHOICE_DOC):
    data = _post_editable(base_url, token, instructable=doc)
    return data["item"]["id"], data["revision"]


def _labels(ins):
    return ins["steps"][1]["question"]["choices"], ins["reply"]["choices"]


def test_protocol5_client_gets_labels_and_no_revision(pond):
    _, base_url, token, _ = pond
    item_id, _ = _choice_post(base_url, token)
    for protocol in (5, None, 1):
        status, detail = hcall(base_url, f"/asks/{item_id}", token, protocol=protocol)
        assert status == 200
        ins = detail["item"]["payload"]["instructable"]
        assert _labels(ins) == (["All at once", "Mix", "Staff first"], ["No", "Yes"])
        assert "revision" not in detail["item"] and "revisions" not in detail
        assert "What should change?" not in json.dumps(detail)
        assert ins["steps"][1]["id"] == "s2" and ins["title"] == "Roll out the change"


def test_protocol6_client_gets_objects_and_revision(pond):
    _, base_url, token, _ = pond
    item_id, rev1 = _choice_post(base_url, token)
    status, detail = hcall(base_url, f"/asks/{item_id}", token, protocol=6)
    assert status == 200
    ins = detail["item"]["payload"]["instructable"]
    assert _labels(ins) == (CHOICE_DOC["steps"][1]["question"]["choices"], CHOICE_DOC["reply"]["choices"])
    assert detail["item"]["revision"]["hash"] == rev1["hash"]
    assert [r["rev"] for r in detail["revisions"]] == [1]
    assert json.loads(detail["item"]["revision"]["body"]) == ins


def test_protocol5_list_labels_only(pond):
    _, base_url, token, _ = pond
    item_id, _ = _choice_post(base_url, token)
    items = hcall(base_url, ALL_ASKS, token, protocol=5)[1]["items"]
    [item] = [i for i in items if i["id"] == item_id]
    assert _labels(item["payload"]["instructable"]) == (["All at once", "Mix", "Staff first"], ["No", "Yes"])
    assert "revision" not in item
    items = hcall(base_url, ALL_ASKS, token, protocol=6)[1]["items"]
    [item] = [i for i in items if i["id"] == item_id]
    assert item["payload"]["instructable"]["reply"]["choices"][1] == {"label": "Yes", "text": "What else?"}
    assert item["revision"]["rev"] == 1


def test_protocol5_corrected_ask_with_objects_gets_stub(pond):
    _, base_url, token, clock = pond
    plain = copy.deepcopy(CHOICE_DOC)
    plain["steps"][1]["question"]["choices"][1] = "Mix"
    plain["reply"]["choices"][1] = "Yes"
    item_id, rev1 = _choice_post(base_url, token, plain)
    assert "revision" in hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]["item"]
    clock.advance(61)
    body = json.loads(hcall(base_url, f"/asks/{item_id}", token, protocol=6)[1]["item"]["revision"]["body"])
    body["steps"][1]["question"]["choices"][1] = {"label": "Mix", "text": "What should change?"}
    status, amended = _amend(base_url, token, item_id, rev1, body)
    assert status == 200, amended
    detail = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    from mailbox.service import revisions_stub
    assert detail["item"]["payload"]["instructable"] == revisions_stub(body)
    assert "revision" not in detail["item"] and "revisions" not in detail
    assert hcall(base_url, f"/asks/{item_id}", token, protocol=6)[1]["item"]["revision"]["rev"] == 2


def test_protocol5_history_with_objects_is_never_served(pond):
    """rev 1 holds an object and rev 2 drops it: an old client still never reads
    a revision body with an object in it."""
    _, base_url, token, clock = pond
    item_id, rev1 = _choice_post(base_url, token)
    clock.advance(61)
    body = json.loads(hcall(base_url, f"/asks/{item_id}", token, protocol=6)[1]["item"]["revision"]["body"])
    body["steps"][1]["question"]["choices"][1] = "Mix"
    body["reply"]["choices"][1] = "Yes"
    assert _amend(base_url, token, item_id, rev1, body)[0] == 200
    detail = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    assert "revisions" not in detail and "revision" not in detail["item"]
    assert "What should change?" not in json.dumps(detail)


def test_plain_ask_unchanged_for_protocol5(pond):
    _, base_url, token, _ = pond
    plain = copy.deepcopy(CHOICE_DOC)
    plain["steps"][1]["question"]["choices"][1] = "Mix"
    plain["reply"]["choices"][1] = "Yes"
    item_id, _ = _choice_post(base_url, token, plain)
    five = hcall(base_url, f"/asks/{item_id}", token, protocol=5)[1]
    six = hcall(base_url, f"/asks/{item_id}", token, protocol=6)[1]
    assert five == six and five["item"]["revision"]["rev"] == 1


def test_protocol5_actions_still_work(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _choice_post(base_url, token)
    for path, body in (("/reply", {"text": "Fine by me"}),
                       ("/answers", {"answers": [{"step": "2", "picked": ["Mix"]}]}),
                       ("/ack", {})):
        status, data = hcall(base_url, f"/asks/{item_id}{path}", token, method="POST", body=body, protocol=5)
        assert status == 200, (path, data)
        assert "What should change?" not in json.dumps(data), path
    status, data = hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST",
                         body={"verdict": "done"}, protocol=5)
    assert status == 200 and "What should change?" not in json.dumps(data)
    kinds = [e.kind for e in srv.service.store.events_for_item(item_id)]
    assert kinds == ["posted", "reply", "answer", "acked", "resolved"]


# --- the Pond 1.4.0 compatibility pass: choice and limit vectors -------------------

def test_choice_vectors_replay():
    """choice-vectors.json, which Hook's tests copy, is what this Pond answers today;
    a wire change without a regenerated file fails here
    (python3 -m mailbox.tests.gen_choice_vectors)."""
    from mailbox.tests.gen_choice_vectors import CHOICE_OUT, build
    from mailbox.tests.gen_pond_vectors import dump
    assert dump(build("choice")) == CHOICE_OUT.read_text(encoding="utf-8")
    cases = {c["name"]: c for c in json.loads(CHOICE_OUT.read_text(encoding="utf-8"))["cases"]}
    status = {name: c["response"]["status"] for name, c in cases.items()}
    assert {status[n] for n in cases if n.startswith("choice-invalid-")} == {400}
    six = cases["choice-detail-protocol-6"]["response"]["body"]
    five = cases["choice-detail-protocol-5"]["response"]["body"]
    assert six["item"]["payload"]["instructable"]["steps"][1]["question"]["choices"][1] == {
        "label": "Mix", "text": "What should change?"}
    assert "revision" in six["item"] and "revisions" in six
    assert five["item"]["payload"]["instructable"]["steps"][1]["question"]["choices"] == [
        "All at once", "Mix", "Staff first"]
    assert "revision" not in five["item"] and "revisions" not in five
    texts = [e["text"] for e in cases["choice-events"]["response"]["body"]["events"] if e["kind"] in ("answer", "reply")]
    assert texts == ['{"step":"2","id":"s2","picked":["Mix"],"text":"Staff on Monday, the rest on Wednesday"}',
                     '{"step":"2","id":"s2","picked":["Mix"]}',
                     "choice: Yes · Tell the night shift first", "choice: No"]


def test_limits_vectors_replay():
    from mailbox.tests.gen_choice_vectors import LIMITS_OUT, TRACEBACK, build
    from mailbox.tests.gen_pond_vectors import dump
    assert dump(build("limits")) == LIMITS_OUT.read_text(encoding="utf-8")
    cases = {c["name"]: c for c in json.loads(LIMITS_OUT.read_text(encoding="utf-8"))["cases"]}
    assert [cases[n]["response"]["status"] for n in (
        "limits-message-8000", "limits-message-8001", "limits-problem-multiline", "limits-problem-8000",
        "limits-problem-8001", "limits-problem-hidden")] == [200, 400, 200, 200, 400, 400]
    events = cases["limits-detail"]["response"]["body"]["events"]
    notes = [json.loads(e["text"])["note"] for e in events if e["kind"] == "problem"]
    assert notes == [TRACEBACK, "n" * 8000] and len(TRACEBACK) == 937 and TRACEBACK.count("\n") == 13
    assert [e["text"] for e in events if e["kind"] == "message"] == ["m" * 8000]


def test_the_replayed_vectors_do_not_depend_on_todays_date():
    """The generators pin the service clock to T0; the store's own "now" (the
    14-day cutoff of GET /asks among others) must not leak the real date in.
    Here the wall clock reads T0 + 400 days, and every file still rebuilds byte for byte."""
    from unittest import mock
    from mailbox.tests import gen_choice_vectors, gen_pond_vectors
    late = gen_pond_vectors.T0 + timedelta(days=400)

    class Late(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return late.astimezone(tz) if tz is not None else late.replace(tzinfo=None)

    builds = [(gen_pond_vectors.build, kind, path) for kind, path in (
        ("bug", gen_pond_vectors.BUG_OUT), ("message", gen_pond_vectors.MESSAGE_OUT),
        ("requirements", gen_pond_vectors.REQUIREMENTS_OUT))]
    builds += [(gen_choice_vectors.build, kind, path) for kind, path in gen_choice_vectors.OUTS]
    with mock.patch("mailbox.store.datetime", Late), mock.patch("mailbox.service.datetime", Late):
        for build, kind, path in builds:
            assert gen_pond_vectors.dump(build(kind)) == path.read_text(encoding="utf-8"), path.name


def test_the_1_4_0_vectors_hold_nothing_personal():
    from mailbox.tests.gen_choice_vectors import OUTS
    for _, path in OUTS:
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/home/" not in text and "192.168." not in text and ".ts.net" not in text and not re.search(r"\b(?!ISO-|SHA-|UTF-)[A-Z]{2,5}-\d+\b", text)

# --- phone handoff and alert configuration (Pond 1.5.0) --------------------------

def test_public_phone_page_never_reads_asks(server, monkeypatch):
    import http.client
    from mailbox import link_page
    srv, base, _ = server
    item, _ = srv.service.post("Example", "private headline", why="private explanation")
    def forbidden(*args, **kwargs):
        raise AssertionError("public page must not access asks")
    for name in ("get_item", "current_revision", "events_for_item", "list_open"):
        if hasattr(srv.store, name):
            monkeypatch.setattr(srv.store, name, forbidden)
    pages = []
    for item_id in (item.id, "abcdef12-1234-5678-9abc-123456789abc"):
        conn = http.client.HTTPConnection("127.0.0.1", srv.http_server.server_port)
        conn.request("GET", "/i/" + item_id)
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Security-Policy") == link_page.CSP
        assert "default-src 'none'" in link_page.CSP and "'unsafe-inline'" not in link_page.CSP
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("Referrer-Policy") == "no-referrer"
        page = response.read().decode()
        assert f'href="hook://i/{item_id}"' in page
        assert f'href="relay://i/{item_id}"' in page
        assert "private" not in page and "Bearer" not in page and "https://" not in page
        pages.append(page.replace(item_id, "UUID"))
        conn.close()
    assert pages[0] == pages[1]


@pytest.mark.parametrize("suffix", ["not-a-uuid", "abcdef12-1234-5678-9abc-123456789abc?x=1",
                                    "abcdef12-1234-5678-9abc-123456789abc?",
                                    "abcdef12-1234-5678-9abc-123456789abc#fragment",
                                    "abcdef12-1234-5678-9abc-123456789abc#",
                                    "abcdef12-1234-5678-9abc-123456789abc/extra",
                                    "%61bcdef12-1234-5678-9abc-123456789abc",
                                    'abcdef12-1234-5678-9abc-123456789abc%22%3E'])
def test_public_phone_page_rejects_nonexact_routes(server, suffix):
    import http.client
    srv, _, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.http_server.server_port)
    conn.request("GET", "/i/" + suffix)
    response = conn.getresponse()
    assert response.status == 404
    assert json.loads(response.read()) == {"error": "not found"}
    conn.close()


def test_public_phone_page_uuid_case_and_script_hash():
    import base64
    import hashlib
    from mailbox import link_page
    item_id = "abcdef12-1234-5678-9abc-123456789abc"
    assert link_page.render("/i/" + item_id.upper()) == link_page.render("/i/" + item_id)
    assert base64.b64encode(hashlib.sha256(link_page.SCRIPT.encode()).digest()).decode() in link_page.CSP


# --- the launch page on Android (Pond 1.7.0) ------------------------------------

ANDROID_PACKAGE = "io.github.xooxoxxo.hook.android"


def test_public_phone_page_offers_android_with_the_id_lower_cased(server):
    import http.client
    srv, _, _ = server
    item_id = "abcdef12-1234-5678-9abc-123456789abc"
    conn = http.client.HTTPConnection("127.0.0.1", srv.http_server.server_port)
    conn.request("GET", "/i/" + item_id.upper())
    response = conn.getresponse()
    assert response.status == 200
    page = response.read().decode()
    conn.close()
    intent = f"intent://i/{item_id}#Intent;scheme=hook;package={ANDROID_PACKAGE};end"
    assert f'<a id="android" href="{intent}">Android</a>' in page
    assert f'<a id="iphone" href="hook://i/{item_id}">iPhone</a>' in page
    assert item_id.upper() not in page and "browser_fallback_url" not in page
    assert page.count("intent://") == 1 and "https://" not in page


def test_public_phone_page_csp_hashes_the_served_script(server):
    import base64
    import hashlib
    import http.client
    import re as _re
    srv, _, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.http_server.server_port)
    conn.request("GET", "/i/abcdef12-1234-5678-9abc-123456789abc")
    response = conn.getresponse()
    csp = response.getheader("Content-Security-Policy")
    page = response.read().decode()
    conn.close()
    scripts = _re.findall(r"<script>(.*?)</script>", page, flags=_re.S)
    assert len(scripts) == 1 and page.count("<script") == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp and "'unsafe-inline'" not in csp
    assert not _re.search(r"\son[a-z]+\s*=", page, flags=_re.I)


def test_public_phone_page_script_picks_android_by_user_agent():
    from mailbox import link_page
    assert "navigator.userAgent" in link_page.SCRIPT and '"Android"' in link_page.SCRIPT
    assert 'getElementById("android")' in link_page.SCRIPT
    assert 'getElementById("iphone")' in link_page.SCRIPT
    page = link_page.render("/i/abcdef12-1234-5678-9abc-123456789abc").decode()
    assert page.endswith(f"<script>{link_page.SCRIPT}</script></body></html>")


# --- the quiet-card launch page (Pond 1.12.1) ------------------------------------

LINK_ID = "abcdef12-1234-5678-9abc-123456789abc"


def _get_link_page(srv, item_id=LINK_ID):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", srv.http_server.server_port)
    conn.request("GET", "/i/" + item_id)
    response = conn.getresponse()
    csp, page = response.getheader("Content-Security-Policy"), response.read().decode()
    conn.close()
    return csp, page


def test_link_page_csp_hashes_script_and_style_and_nothing_else(server):
    import base64
    import hashlib
    import re as _re
    from mailbox import link_page
    srv, _, _ = server
    csp, page = _get_link_page(srv)
    sha = lambda text: base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
    styles = _re.findall(r"<style>(.*?)</style>", page, flags=_re.S)
    scripts = _re.findall(r"<script>(.*?)</script>", page, flags=_re.S)
    assert len(styles) == 1 and page.count("<style") == 1 and styles[0] == link_page.STYLE
    assert len(scripts) == 1 and scripts[0] == link_page.SCRIPT
    assert csp == ("default-src 'none'; script-src 'sha256-" + sha(scripts[0])
                   + "'; style-src 'sha256-" + sha(styles[0])
                   + "'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
    assert not _re.search(r"\sstyle\s*=", page, flags=_re.I)       # inline style attributes would be blocked
    assert not _re.search(r"\son[a-z]+\s*=", page, flags=_re.I)


def test_link_page_known_and_unknown_ids_are_byte_identical_apart_from_the_id(server):
    srv, _, _ = server
    item, _ = srv.service.post("Example", "private headline", why="private explanation")
    pages = {}
    for item_id in (item.id, LINK_ID):
        csp, page = _get_link_page(srv, item_id)
        pages[item_id] = (csp, page.encode("utf-8"))
    known, unknown = pages[item.id], pages[LINK_ID]
    assert known[0] == unknown[0]
    assert known[1].replace(item.id.encode(), b"UUID") == unknown[1].replace(LINK_ID.encode(), b"UUID")
    assert known[1].count(item.id.encode()) == unknown[1].count(LINK_ID.encode()) == 4


def test_link_page_has_no_external_urls(server):
    import re as _re
    from mailbox import link_icon
    srv, _, _ = server
    _, page = _get_link_page(srv)
    data_uri = "data:image/png;base64," + link_icon.ICON_PNG_B64
    assert page.count(data_uri) == 1
    rest = page.replace(data_uri, "DATA")
    urls = _re.findall(r"""(?:href|src|action|xlink:href|srcset)\s*=\s*["']([^"']*)""", rest, flags=_re.I)
    links = [u for u in urls if u != "DATA"]
    assert urls.count("DATA") == 1
    assert links and all(_re.match(r"(hook|intent|relay)://i/" + LINK_ID, u) for u in links), links
    assert "url(" not in rest and "@import" not in rest
    assert "//" not in rest.replace("hook://", "").replace("intent://", "").replace("relay://", "")
    assert "<link" not in rest and "<iframe" not in rest and "<svg" not in rest and rest.count("<img") == 1


def test_link_page_csp_allows_only_data_images_and_the_icon_is_the_expected_png(server):
    import hashlib
    import re as _re
    import struct
    from mailbox import link_icon, link_page
    srv, _, _ = server
    csp, page = _get_link_page(srv)
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives["img-src"] == "data:"
    assert directives["default-src"] == "'none'"
    assert [d for d in directives if d.endswith("-src")] == ["default-src", "script-src", "style-src", "img-src"]
    png = link_icon.ICON_PNG
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    assert struct.unpack(">II", png[16:24]) == (144, 144)
    assert hashlib.sha256(png).hexdigest() == link_icon.ICON_SHA256 == \
        "d2fa509e48aaaf688418c481ad3a30724a990b50d81e07111cc9fcdff24b1df5"
    assert len(png) < 16_000
    imgs = _re.findall(r"<img [^>]*>", page)
    assert imgs == ['<img class="icon" src="data:image/png;base64,' + link_icon.ICON_PNG_B64
                    + '" width="72" height="72" alt="">']
    assert link_page.ICON in page


def test_link_page_quiet_card_content():
    from mailbox import link_page
    page = link_page.render("/i/" + LINK_ID).decode()
    assert "<svg" not in page and 'alt=""' in page and "Opening Hook…" in page and 'class="spin"' in page
    assert f'<a id="open" class="btn" href="hook://i/{LINK_ID}">Open in Hook</a>' in page
    assert f'<a id="mac" href="relay://i/{LINK_ID}">Mac</a>' in page
    assert "Needs Hook installed and connected to your Pond." in page
    assert "@media (prefers-reduced-motion:reduce){.spin{animation:none}}" in link_page.STYLE
    assert "prefers-color-scheme:dark" in link_page.STYLE and ":focus-visible" in link_page.STYLE
    assert "font-face" not in link_page.STYLE


def test_link_page_script_aims_the_button_and_keeps_the_redirect():
    from mailbox import link_page
    script = link_page.SCRIPT
    assert 'getElementById("open").href=d.getElementById(p).href' in script
    assert '"android"' in script and '"iphone"' in script and '"mac"' in script
    assert "iPhone|iPad|iPod" in script and "maxTouchPoints" in script
    # The redirect is as before: Android opens the intent link, everything else hook://.
    assert script.endswith('if (p==="android") window.location.replace(d.getElementById("android").href); '
                           'else window.location.replace(d.getElementById("iphone").href);')
    assert "fetch" not in script and "XMLHttpRequest" not in script and "innerHTML" not in script


def test_1_12_1_is_described():
    """1.12.1 (the quiet-card launch page) is a patch: protocol 5, schema 5, no new
    feature. 1.13.0, 1.14.0, 1.15.0 and 1.15.1 came after: see test_the_version_is_1_15_1_with_protocol_5_and_schema_5."""
    from mailbox import FEATURES
    assert FEATURES[-4] == "ownerKeyDates"
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.12.1\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("style-src", "img-src data:", "`protocol` stays 5", "No migration"):
        assert needle in section, needle


def test_1_7_0_is_still_described():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.7.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("Android", ANDROID_PACKAGE, "^1.5.0", "`protocol` stays 5", "No migration"):
        assert needle in section, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "intent://i/<uuid>" in readme


def test_1_8_0_is_still_described():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.8.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("`features`", "`protocol` stays 5", "No migration", "askWarning"):
        assert needle in section, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert '"features": [' in readme


def test_1_9_0_is_still_described():
    from mailbox import FEATURES
    assert "appendSteps" in FEATURES
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.9.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("`protocol` stays 5", "Schema 3", "`revision_meta`", "/append", "appendSteps",
                   "appends on|off|status", "steps[0].id: used before"):
        assert needle in section, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`/asks/{id}/append`" in readme


def test_1_11_0_is_still_described():
    """1.12.0, 1.12.1, 1.13.0, 1.14.0, 1.15.0 and 1.15.1 came after: the version check is
    test_the_version_is_1_15_1_with_protocol_5_and_schema_5."""
    from mailbox import FEATURES
    assert FEATURES[-5:-3] == ("ownerRevokeRequest", "ownerKeyDates")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.11.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("`protocol` stays 5", "Schema 5", "`last_used`", "`owner_revoke_requests`",
                   "POST /owner/revoke-request", "GET /owner/revoke-request/{id}", "`maxKeys`", "`lastUsed`",
                   "ownerRevokeRequest", "ownerKeyDates", "10", "after 1.10.0"):
        assert needle in section, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`/owner/revoke-request`" in readme


def test_1_13_0_is_still_described():
    """1.13.0 (`fields`, a settings table on a step) is additive: protocol 5, no
    migration, one new feature name. 1.14.0, 1.15.0 and 1.15.1 came after: see
    test_the_version_is_1_15_1_with_protocol_5_and_schema_5."""
    from mailbox import FEATURES
    assert FEATURES[-3] == "fields"
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    flat = " ".join(changelog.split("\n## 1.13.0\n", 1)[1].split("\n## ", 1)[0].split())
    for needle in ("`protocol` stays 5", "No migration", "`fields`", "1 to 20 rows", "40 characters",
                   "500 characters", "unique within the step", "`verify.fields: unknown key`",
                   "revision hash", "fields-vectors.json", "Every vector file names 1.13.0"):
        assert needle in flat, needle
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "`steps[0].fields[1].label: duplicate label`" in readme


def test_1_14_0_is_still_described():
    """1.14.0 (`brief`, POST /asks/{id}/ask-about) is additive: protocol 5, no
    migration, one new feature name always (`brief`) and one while a key is set
    (`askAbout`). 1.15.0 and 1.15.1 came after: see
    test_the_version_is_1_15_1_with_protocol_5_and_schema_5."""
    from mailbox import FEATURES
    assert FEATURES[-2] == "brief" and "askAbout" not in FEATURES
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    flat = " ".join(changelog.split("\n## 1.14.0\n", 1)[1].split("\n## ", 1)[0].split())
    for needle in ("`protocol` stays 5", "No migration", "No new dependency", "`brief`", "8000 characters",
                   "POST /asks/{id}/ask-about", "`askAbout`", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE",
                   "claude-haiku-5-5", "ask_about_question", "ask_about_answer", "429", "502",
                   "brief-vectors.json", "Every vector file names 1.14.0", "Anthropic"):
        assert needle in flat, needle
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    for needle in ("/asks/{id}/ask-about", "ANTHROPIC_API_KEY_FILE", "`askAbout`", "Anthropic's API"):
        assert needle in readme, needle


def test_the_version_is_1_15_1_with_protocol_5_and_schema_5():
    """1.15.1 is a patch: the Central Time ack paths encode the id, a notification ref
    must match whole, and it is the first signed two-platform image from the public
    repository. Protocol 5, no migration, no new feature name."""
    from mailbox import FEATURES, PROTOCOL, VERSION, migrations
    assert (VERSION, PROTOCOL, migrations.SCHEMA_VERSION) == ("1.15.1", 5, 5)
    assert FEATURES[-1] == "longAnswers"
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    flat = " ".join(changelog.split("\n## 1.15.1\n", 1)[1].split("\n## ", 1)[0].split())
    for needle in ("`protocol` stays 5", "No migration", "No new feature name", "No new dependency",
                   "Central Time", "`.` or `..`", "percent-encoded", "trailing newline",
                   "linux/amd64", "linux/arm64", "signed", "name 1.15.1"):
        assert needle in flat, needle
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    for needle in ('"version": "1.15.1"', "scripts/verify-image.sh 1.15.1", "release.yml@refs/tags/v1.15.1"):
        assert needle in readme, needle


def test_1_15_0_is_still_described():
    """1.15.0 (`longAnswers`) is additive: an owner's answer text takes 8000 characters
    (1000 before), as a message does. Protocol 5, no migration, one new feature name.
    1.15.1 came after: see test_the_version_is_1_15_1_with_protocol_5_and_schema_5."""
    from mailbox import FEATURES
    from mailbox.answers import MAX_ANSWER_TEXT
    assert FEATURES[-1] == "longAnswers" and MAX_ANSWER_TEXT == 8000
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    flat = " ".join(changelog.split("\n## 1.15.0\n", 1)[1].split("\n## ", 1)[0].split())
    for needle in ("`protocol` stays 5", "No migration", "No new dependency", "`longAnswers`",
                   "8000 characters", "1000", "answers[0].text: longer than 8000 characters",
                   "Telegram", "answers-vectors.json", "choice-vectors.json", "the Pond's release (history, progress, strict-JSON, append, document and phone) name 1.15.0"):
        assert needle in flat, needle
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    for needle in ('"longAnswers"', "1-8,000 characters (1.15.0, feature `longAnswers`; 1-1,000 before)"):
        assert needle in readme, needle
    assert "(1-1,000 characters)" not in readme


def test_1_12_0_is_still_described():
    """1.12.0 (the total limit on stored documents) is additive: protocol 5, no
    migration, no new feature name. 1.12.1 and 1.13.0 came after: see
    test_1_12_1_is_described and test_the_version_is_1_15_1_with_protocol_5_and_schema_5."""
    from mailbox import FEATURES
    assert FEATURES[-4] == "ownerKeyDates"
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.12.0\n", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(section.split())
    for needle in ("`protocol` stays 5", "No migration", "268,435,456", "256 MiB", "once",
                   "counts once", '`409` `{"error": "document store full"}`', "Nothing is stored", "docs status",
                   "of 268435456 bytes", "Releasing", "7-day expiry", "doc-wire-vectors.json"):
        assert needle in flat, needle
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    flat_readme = " ".join(readme.split())
    assert '"version": "1.15.1"' in readme
    assert "268,435,456 bytes" in flat_readme and "`409 {\"error\": \"document store full\"}`" in flat_readme
    assert "no total cap" not in flat_readme
    # Said once and true: normally no Telegram message; a lost race edits the one sent.
    for name, flat_text in (("CHANGELOG", flat), ("README", flat_readme)):
        assert "or Telegram message" not in flat_text, name
        assert "normally no Telegram message is sent" in flat_text, name
        assert "its buttons are removed" in flat_text, name


def test_telegram_mode_config_and_server_wiring(tmp_path):
    assert load_config(BASE_ENV).get("telegram_mode", "full") == "full"
    assert load_config({**BASE_ENV, "MAILBOX_TELEGRAM_MODE": "alert"})["telegram_mode"] == "alert"
    for mode in ("", "alerts", "FULL"):
        with pytest.raises(ValueError, match="MAILBOX_TELEGRAM_MODE must be full or alert"):
            load_config({**BASE_ENV, "MAILBOX_TELEGRAM_MODE": mode})
    srv = MailboxServer(tmp_path / "data", tmp_path / "backups", "fake", OWNER_ID, "test", port=0,
                        host="127.0.0.1", telegram_mode="alert")
    try:
        assert srv.service.telegram_mode == "alert"
    finally:
        srv.http_server.server_close()
        srv.store.close()
