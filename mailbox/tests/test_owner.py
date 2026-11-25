"""Tests for the owner key: enrollment on Telegram, the owner check on the
HTTP routes, and the enforcement switch (Pond 1.1.0)."""
import json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from mailbox import owner
from mailbox.owner import enroll_code, key_hash, revoke_code
from mailbox.service import EnrollRefused, RevokeRefused, Service
from mailbox.store import Store
from mailbox.telegram import FakeTelegramClient, TelegramError
from mailbox.tests.test_server import _running

OWNER_ID = 12345
T0 = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def svc():
    store = Store(":memory:")
    store.open()
    telegram = FakeTelegramClient(owner_id=OWNER_ID)
    service = Service(store, telegram, "personal")
    clock = Clock()
    service.utcnow = clock
    yield service, store, telegram, clock
    store.close()


def tap(service, data, *, from_id=OWNER_ID, message_id=1):
    service.handle_update({"callback_query": {
        "id": "cb", "from": {"id": from_id}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": OWNER_ID}},
    }})
    return service.telegram.callback_answers[-1]["text"] if from_id == OWNER_ID else None


def new_key():
    secret = secrets.token_urlsafe(32)
    return secret, key_hash(secret)


def approved(service, label="Studio"):
    secret, digest = new_key()
    k = service.enroll(digest, label)
    tap(service, f"e:{k.id}:y")
    return secret, service.store.get_owner_key(k.id)


# --- pure helpers ----------------------------------------------------------


def test_key_hash_and_code():
    assert key_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    digest = "ab" * 32
    expected = int(__import__("hashlib").sha256(("hook-enroll-v1\nid-1\n" + digest).encode()).hexdigest()[:12], 16)
    assert enroll_code("id-1", digest) == f"{expected % 1_000_000:06d}"
    assert len(enroll_code("x", digest)) == 6


# --- enrollment --------------------------------------------------------------


def test_enroll_sends_the_code_and_the_escaped_label(svc):
    service, store, telegram, _ = svc
    _, digest = new_key()
    k = service.enroll(digest, "Mac <b>mini</b>")
    assert k.status == "pending" and k.code == enroll_code(k.id, digest)
    sent = telegram.sent_messages[-1]
    assert k.code in sent["text"]
    assert "&lt;b&gt;mini&lt;/b&gt;" in sent["text"] and "<b>mini</b>" not in sent["text"]
    assert sent["reply_markup"] == {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"e:{k.id}:y"},
        {"text": "🚫 Reject", "callback_data": f"e:{k.id}:n"},
    ]]}
    assert store.get_owner_key(k.id).tg_message_id == 1
    assert k.expires == (T0 + timedelta(seconds=600)).isoformat(timespec="seconds")


def test_approve_tap_from_the_owner_and_from_someone_else(svc):
    service, store, telegram, _ = svc
    _, digest = new_key()
    k = service.enroll(digest, "Studio")
    tap(service, f"e:{k.id}:y", from_id=999)
    assert service.dropped_updates == 1
    assert store.get_owner_key(k.id).status == "pending"
    assert tap(service, f"e:{k.id}:y") == "Approved"
    assert store.get_owner_key(k.id).status == "approved"
    assert telegram.edited_messages[-1]["text"] == "✅ Hook on Studio can now act as you."
    assert tap(service, f"e:{k.id}:y") == "Already handled"


def test_reject_tap(svc):
    service, store, _, _ = svc
    _, digest = new_key()
    k = service.enroll(digest, "Studio")
    assert tap(service, f"e:{k.id}:n") == "Rejected"
    assert store.get_owner_key(k.id).status == "rejected"


def test_a_second_enroll_while_one_is_pending_refuses_both(svc):
    service, store, telegram, _ = svc
    first = service.enroll(new_key()[1], "Studio")
    second = service.enroll(new_key()[1], "Studio")
    assert second.status == "rejected"
    assert store.get_owner_key(first.id).status == "rejected"
    two = [m for m in telegram.sent_messages if m["text"].startswith("Two devices asked to enroll at once")]
    assert len(two) == 1
    assert tap(service, f"e:{first.id}:y") == "Already handled"


def test_an_expired_request_is_expired_on_read_and_on_tap(svc):
    service, store, _, clock = svc
    k = service.enroll(new_key()[1], "Studio")
    clock.now = T0 + timedelta(seconds=601)
    assert tap(service, f"e:{k.id}:y") == "Expired: start again in Hook"
    assert service.enrollment(k.id).status == "expired"
    # An expired request no longer blocks a new one.
    assert service.enroll(new_key()[1], "Studio").status == "pending"


def test_enroll_refusals(svc):
    service, _, _, _ = svc
    assert owner.MAX_KEYS == 10
    for _ in range(owner.MAX_KEYS):
        approved(service)
    with pytest.raises(EnrollRefused) as caught:
        service.enroll(new_key()[1], "Eleventh")
    assert caught.value.error == "too many keys"
    known = service.store.owner_keys()[0].key_hash
    with pytest.raises(EnrollRefused) as caught:
        service.enroll(known, "Again")
    assert caught.value.error in ("too many keys", "already enrolled")
    for label in ("", "a" * 41, "two\nlines", "sk-ant-xyz"):
        with pytest.raises(EnrollRefused) as caught:
            service.enroll(new_key()[1], label)
        assert caught.value.error == "invalid label"


def test_already_enrolled(svc):
    service, _, _, _ = svc
    _, k = approved(service)
    with pytest.raises(EnrollRefused) as caught:
        service.enroll(k.key_hash, "Studio")
    assert caught.value.error == "already enrolled"


def test_owner_for(svc):
    service, _, _, _ = svc
    secret, k = approved(service)
    assert service.owner_for(secret).id == k.id
    assert service.owner_for(None) is None
    assert service.owner_for("nope") is None
    service.revoke(k.id, by=k)
    assert service.owner_for(secret) is None


# --- enforcement -------------------------------------------------------------


def test_turning_enforcement_on_and_off(svc):
    service, store, telegram, clock = svc
    _, k = approved(service)
    assert service.owner_key_required() is False
    assert service.request_enforcement(True, by=k) == "on"
    assert store.get_setting(owner.SETTING_REQUIRED) == "1"
    assert telegram.sent_messages[-1]["text"] == "🔒 Owner key now required (turned on from Hook on Studio)."
    assert service.request_enforcement(False, by=k) == "pending"
    assert service.owner_key_required() is True
    request = store.get_setting(owner.SETTING_OFF_REQUEST)
    assert telegram.sent_messages[-1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Turn off", "callback_data": f"k:{request}:off"}]]}
    assert tap(service, f"k:{request}:off") == "Owner key no longer required"
    assert service.owner_key_required() is False
    assert store.get_setting(owner.SETTING_OFF_REQUEST) is None
    assert store.get_setting(owner.SETTING_OFF_EXPIRES) is None
    assert telegram.edited_messages[-1]["text"] == "Owner key no longer required."


def test_a_stale_off_tap_changes_nothing(svc):
    service, store, _, clock = svc
    _, k = approved(service)
    service.request_enforcement(True, by=k)
    service.request_enforcement(False, by=k)
    old = store.get_setting(owner.SETTING_OFF_REQUEST)
    service.request_enforcement(False, by=k)                  # replaces the request
    assert tap(service, f"k:{old}:off") == "This request expired. Ask again from Hook."
    assert store.get_setting(owner.SETTING_REQUIRED) == "1"
    current = store.get_setting(owner.SETTING_OFF_REQUEST)
    clock.now = T0 + timedelta(seconds=601)
    assert tap(service, f"k:{current}:off") == "This request expired. Ask again from Hook."
    assert store.get_setting(owner.SETTING_REQUIRED) == "1"
    assert tap(service, "k:garbage") == "This request expired. Ask again from Hook."


def test_turning_on_drops_a_pending_off_request(svc):
    service, store, _, _ = svc
    _, k = approved(service)
    service.request_enforcement(True, by=k)
    service.request_enforcement(False, by=k)
    request = store.get_setting(owner.SETTING_OFF_REQUEST)
    service.request_enforcement(True, by=k)
    assert store.get_setting(owner.SETTING_OFF_REQUEST) is None
    assert tap(service, f"k:{request}:off") == "This request expired. Ask again from Hook."
    assert service.owner_key_required() is True


# --- HTTP ----------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    yield from _running(tmp_path)


def req(base_url, path, token, *, method="GET", body=None, key=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if key is not None:
        headers[owner.KEY_HEADER] = key
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def http_enroll(srv, base_url, token, label="Studio"):
    secret, digest = new_key()
    status, data = req(base_url, "/owner/enroll", token, method="POST", body={"keyHash": digest, "label": label})
    assert status == 201, data
    return secret, data


def http_approved(srv, base_url, token, label="Studio"):
    secret, data = http_enroll(srv, base_url, token, label)
    tap(srv.service, f"e:{data['id']}:y")
    return secret, data["id"]


def test_enroll_route_and_its_status(server):
    srv, base_url, token = server
    _, data = http_enroll(srv, base_url, token)
    assert set(data) >= {"id", "code", "expires"}
    assert req(base_url, f"/owner/enroll/{data['id']}", token) == (200, {"id": data["id"], "status": "pending"})
    assert req(base_url, "/owner/enroll/nope", token)[0] == 404
    assert req(base_url, "/owner/enroll", None, method="POST", body={})[0] == 401


def test_enroll_route_refuses_bad_input(server):
    _, base_url, token = server
    for body in ({"keyHash": "AB" * 32, "label": "x"}, {"keyHash": "ab" * 31, "label": "x"},
                 {"keyHash": "ab" * 32}, {"keyHash": "ab" * 32, "label": "two\nlines"}):
        status, data = req(base_url, "/owner/enroll", token, method="POST", body=body)
        assert status == 400, body


def test_enrollment_expires_after_601_seconds(server):
    srv, base_url, token = server
    clock = Clock()
    srv.service.utcnow = clock
    _, data = http_enroll(srv, base_url, token)
    clock.now = T0 + timedelta(seconds=601)
    assert req(base_url, f"/owner/enroll/{data['id']}", token)[1]["status"] == "expired"


def test_get_owner_lists_keys_and_self_without_hashes(server):
    srv, base_url, token = server
    secret, key_id = http_approved(srv, base_url, token)
    pending_secret, pending = http_enroll(srv, base_url, token, label="Laptop")
    status, data = req(base_url, "/owner", token, key=secret)
    assert status == 200
    assert data["required"] is False
    assert [(k["label"], k["status"]) for k in data["keys"]] == [("Studio", "approved"), ("Laptop", "pending")]
    assert set(data["keys"][0]) == {"id", "label", "status", "created", "approvedAt", "lastUsed"}
    assert data["maxKeys"] == 10
    assert data["self"] == {"id": key_id, "status": "approved"}
    assert req(base_url, "/owner", token, key=pending_secret)[1]["self"] == {"id": pending["id"], "status": "pending"}
    assert req(base_url, "/owner", token)[1]["self"] is None
    assert "keyHash" not in json.dumps(data) and "key_hash" not in json.dumps(data)


def test_the_owner_check(server):
    """_owner(always=False) through /reply: token alone works while enforcement is off."""
    srv, base_url, token = server
    secret, key_id = http_approved(srv, base_url, token)
    pending_secret, _ = http_enroll(srv, base_url, token, label="Laptop")
    item_id = req(base_url, "/asks", token, method="POST",
                  body={"project": "Orbit", "ask": "x", "context": "personal"})[1]["item"]["id"]
    path = f"/asks/{item_id}/reply"
    assert req(base_url, path, token, method="POST", body={"text": "ok"})[0] == 200
    assert req(base_url, path, token, method="POST", body={"text": "ok"}, key=secret)[0] == 200
    assert req(base_url, path, token, method="POST", body={"text": "ok"}, key=pending_secret) == (
        403, {"error": "unknown owner key"})
    srv.service.revoke(key_id, by=srv.service.store.get_owner_key(key_id))
    assert req(base_url, path, token, method="POST", body={"text": "ok"}, key=secret) == (
        403, {"error": "unknown owner key"})


def test_with_enforcement_on_a_missing_key_is_refused(server):
    srv, base_url, token = server
    secret, _ = http_approved(srv, base_url, token)
    assert req(base_url, "/owner/require", token, method="POST", body={"required": True}, key=secret) == (
        200, {"required": True})
    item_id = req(base_url, "/asks", token, method="POST",
                  body={"project": "Orbit", "ask": "x", "context": "personal"})[1]["item"]["id"]
    assert req(base_url, f"/asks/{item_id}/reply", token, method="POST", body={"text": "ok"}) == (
        403, {"error": "owner key required"})
    assert req(base_url, f"/asks/{item_id}/reply", token, method="POST", body={"text": "ok"}, key=secret)[0] == 200


def test_require_off_waits_for_the_telegram_tap(server):
    srv, base_url, token = server
    secret, _ = http_approved(srv, base_url, token)
    req(base_url, "/owner/require", token, method="POST", body={"required": True}, key=secret)
    assert req(base_url, "/owner/require", token, method="POST", body={"required": False}, key=secret) == (
        202, {"pending": True})
    assert srv.service.store.get_setting(owner.SETTING_REQUIRED) == "1"
    request = srv.service.store.get_setting(owner.SETTING_OFF_REQUEST)
    tap(srv.service, f"k:{request}:off")
    assert srv.service.store.get_setting(owner.SETTING_REQUIRED) == "0"


def test_require_and_revoke_need_the_key_even_with_enforcement_off(server):
    srv, base_url, token = server
    secret, key_id = http_approved(srv, base_url, token)
    assert req(base_url, "/owner/require", token, method="POST", body={"required": True}) == (
        403, {"error": "owner key required"})
    assert req(base_url, "/owner/revoke", token, method="POST", body={"id": key_id}) == (
        403, {"error": "owner key required"})
    assert req(base_url, "/owner/require", token, method="POST", body={"required": "yes"}, key=secret)[0] == 400
    assert req(base_url, "/owner/revoke", token, method="POST", body={"id": "nope"}, key=secret)[0] == 404
    assert req(base_url, "/owner/revoke", token, method="POST", body={"id": key_id}, key=secret) == (
        200, {"id": key_id, "status": "revoked"})
    assert req(base_url, "/owner", token, key=secret)[1]["keys"] == []


def test_an_eleventh_enroll_after_ten_approvals(server):
    srv, base_url, token = server
    for n in range(10):
        http_approved(srv, base_url, token, label=f"Mac {n}")
    status, data = req(base_url, "/owner/enroll", token, method="POST",
                       body={"keyHash": new_key()[1], "label": "Eleventh"})
    assert (status, data) == (409, {"error": "too many keys"})


# --- last used (Pond 1.11.0) ---------------------------------------------------


def test_last_used_is_stamped_on_authentication_at_most_hourly(svc):
    service, store, _, clock = svc
    secret, k = approved(service)
    assert store.get_owner_key(k.id).last_used is None
    service.owner_for(secret)
    assert store.get_owner_key(k.id).last_used == T0.isoformat(timespec="seconds")
    clock.now = T0 + timedelta(minutes=59)
    service.owner_for(secret)
    assert store.get_owner_key(k.id).last_used == T0.isoformat(timespec="seconds")
    clock.now = T0 + timedelta(minutes=60)
    service.owner_for(secret)
    assert store.get_owner_key(k.id).last_used == clock.now.isoformat(timespec="seconds")


def test_a_failed_authentication_stamps_nothing(svc):
    service, store, _, _ = svc
    pending = service.enroll(new_key()[1], "Laptop")
    service.owner_for("nope")
    assert store.get_owner_key(pending.id).last_used is None


def test_get_owner_shows_created_and_last_used_with_the_token_alone(server):
    srv, base_url, token = server
    secret, key_id = http_approved(srv, base_url, token)
    item_id = req(base_url, "/asks", token, method="POST",
                  body={"project": "Orbit", "ask": "x", "context": "personal"})[1]["item"]["id"]
    assert req(base_url, f"/asks/{item_id}/reply", token, method="POST", body={"text": "ok"}, key=secret)[0] == 200
    status, data = req(base_url, "/owner", token)                  # no owner key at all
    assert status == 200 and data["self"] is None and data["maxKeys"] == 10
    [k] = data["keys"]
    assert k["id"] == key_id and k["lastUsed"] is not None
    assert datetime.fromisoformat(k["created"]) and datetime.fromisoformat(k["lastUsed"])
    assert req(base_url, "/owner", None)[0] == 401


# --- revoke requests (Pond 1.11.0) ----------------------------------------------


def test_revoke_code():
    expected = int(__import__("hashlib").sha256(b"hook-revoke-v1\nr-1\nk-1").hexdigest()[:12], 16)
    assert revoke_code("r-1", "k-1") == f"{expected % 1_000_000:06d}"


def test_a_revoke_request_asks_on_telegram_and_revokes_on_the_tap(svc):
    service, store, telegram, _ = svc
    secret, k = approved(service, label="M4te <local>")
    r = service.request_revoke(k.id)
    assert r.status == "pending" and r.code == revoke_code(r.id, k.id)
    assert r.expires == (T0 + timedelta(seconds=600)).isoformat(timespec="seconds")
    sent = telegram.sent_messages[-1]
    assert "Revoke Hook on M4te &lt;local&gt;</b> (set up 29 Oct 2026)?" in sent["text"]
    assert r.code in sent["text"]
    assert sent["reply_markup"] == {"inline_keyboard": [[
        {"text": "Revoke", "callback_data": f"v:{r.id}:y"},
        {"text": "Keep", "callback_data": f"v:{r.id}:n"},
    ]]}
    assert store.get_owner_key(k.id).status == "approved"           # nothing yet
    tap(service, f"v:{r.id}:y", from_id=999)
    assert store.get_owner_key(k.id).status == "approved"
    assert tap(service, f"v:{r.id}:y") == "Revoked"
    assert store.get_owner_key(k.id).status == "revoked"
    assert service.revoke_request(r.id).status == "approved"
    assert service.owner_for(secret) is None
    assert telegram.sent_messages[-1]["text"] == (
        "🔑 Hook on M4te &lt;local&gt; can no longer act as you (revoked on Telegram).")
    assert tap(service, f"v:{r.id}:y") == "Already handled"


def test_keep_rejects_the_request(svc):
    service, store, telegram, _ = svc
    _, k = approved(service)
    r = service.request_revoke(k.id)
    assert tap(service, f"v:{r.id}:n") == "Kept"
    assert service.revoke_request(r.id).status == "rejected"
    assert store.get_owner_key(k.id).status == "approved"
    assert telegram.edited_messages[-1]["text"] == "Kept: Hook on Studio."


def test_a_revoke_request_expires_after_ten_minutes(svc):
    service, store, _, clock = svc
    _, k = approved(service)
    r = service.request_revoke(k.id)
    clock.now = T0 + timedelta(seconds=601)
    assert tap(service, f"v:{r.id}:y") == "Expired: ask again in Hook"
    assert service.revoke_request(r.id).status == "expired"
    assert store.get_owner_key(k.id).status == "approved"
    # An expired request no longer blocks a new one.
    assert service.request_revoke(k.id).status == "pending"


def test_revoke_request_refusals(svc):
    service, _, _, _ = svc
    _, k = approved(service)
    _, other = approved(service, label="Laptop")
    pending = service.enroll(new_key()[1], "Phone")
    with pytest.raises(RevokeRefused) as caught:
        service.request_revoke("nope")
    assert (caught.value.status, caught.value.error) == (404, "not found")
    with pytest.raises(RevokeRefused) as caught:
        service.request_revoke(pending.id)
    assert (caught.value.status, caught.value.error) == (409, "not approved")
    first = service.request_revoke(k.id)
    with pytest.raises(RevokeRefused) as caught:
        service.request_revoke(other.id)                            # one pending at a time
    assert (caught.value.status, caught.value.error) == (409, "revoke request pending")
    assert service.revoke_request(first.id).status == "pending"
    tap(service, f"v:{first.id}:y")
    with pytest.raises(RevokeRefused) as caught:
        service.request_revoke(k.id)
    assert (caught.value.status, caught.value.error) == (409, "already revoked")


def test_a_key_revoked_from_a_mac_meanwhile_is_not_revoked_twice(svc):
    service, store, telegram, _ = svc
    _, k = approved(service)
    _, mac = approved(service, label="Laptop")
    r = service.request_revoke(k.id)
    service.revoke(k.id, by=mac)
    sent = len(telegram.sent_messages)
    assert tap(service, f"v:{r.id}:y") == "Already revoked"
    assert len(telegram.sent_messages) == sent
    assert service.revoke_request(r.id).status == "approved"


def test_a_failed_telegram_send_stores_no_request(svc):
    service, store, telegram, _ = svc
    _, k = approved(service)

    def refuse(*args, **kwargs):
        raise TelegramError("down")
    telegram.send_message = refuse
    with pytest.raises(TelegramError):
        service.request_revoke(k.id)
    assert store.pending_revoke_requests() == []


def test_revoke_request_routes(server):
    srv, base_url, token = server
    secret, key_id = http_approved(srv, base_url, token)
    assert req(base_url, "/owner/revoke-request", None, method="POST", body={"id": key_id}) == (
        401, {"error": "unauthorized"})
    assert req(base_url, "/owner/revoke-request", token, method="POST", body={}) == (
        400, {"error": "missing fields"})
    assert req(base_url, "/owner/revoke-request", token, method="POST", body={"id": "nope"}) == (
        404, {"error": "not found"})
    status, data = req(base_url, "/owner/revoke-request", token, method="POST", body={"id": key_id})
    assert status == 201, data
    assert set(data) == {"id", "code", "expires", "status"} and data["status"] == "pending"
    assert len(data["code"]) == 6 and data["code"].isdigit()
    assert data["code"] in srv.service.telegram.sent_messages[-1]["text"]
    assert req(base_url, "/owner/revoke-request", token, method="POST", body={"id": key_id}) == (
        409, {"error": "revoke request pending"})
    assert req(base_url, f"/owner/revoke-request/{data['id']}", token) == (
        200, {"id": data["id"], "status": "pending"})
    assert req(base_url, f"/owner/revoke-request/{data['id']}", None)[0] == 401
    assert req(base_url, "/owner/revoke-request/nope", token) == (404, {"error": "not found"})
    tap(srv.service, f"v:{data['id']}:y")
    assert req(base_url, f"/owner/revoke-request/{data['id']}", token) == (
        200, {"id": data["id"], "status": "approved"})
    assert req(base_url, "/owner", token)[1]["keys"] == []
    assert req(base_url, "/owner/revoke-request", token, method="POST", body={"id": key_id}) == (
        409, {"error": "already revoked"})


def test_a_revoke_request_works_with_enforcement_on_and_ignores_the_owner_key(server):
    srv, base_url, token = server
    secret, key_id = http_approved(srv, base_url, token)
    req(base_url, "/owner/require", token, method="POST", body={"required": True}, key=secret)
    status, data = req(base_url, "/owner/revoke-request", token, method="POST", body={"id": key_id},
                       key="not-a-key")
    assert status == 201, data


def test_a_revoke_request_expires_over_http(server):
    srv, base_url, token = server
    clock = Clock()
    srv.service.utcnow = clock
    _, key_id = http_approved(srv, base_url, token)
    data = req(base_url, "/owner/revoke-request", token, method="POST", body={"id": key_id})[1]
    clock.now = T0 + timedelta(seconds=601)
    assert req(base_url, f"/owner/revoke-request/{data['id']}", token)[1]["status"] == "expired"
    assert srv.service.telegram.edited_messages[-1]["text"] == "⌛ Expired: revoke Hook on Studio."
