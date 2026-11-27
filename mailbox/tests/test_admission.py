"""Admission uses the same SQLite database across the daemon and local operator."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from urllib.parse import urlsplit

import pytest

from mailbox import owner
from mailbox.store import Store
from mailbox.tests.test_migrations import _old_database


@contextmanager
def opened(path):
    from mailbox.admission import Admission
    store = Store(path)
    store.open()
    try:
        yield Admission(store)
    finally:
        store.close()


ORIGIN = "https://pond.example"
T = 1000


def mint(a, purpose="first", now=T):
    return urlsplit(a.operator_invite(purpose, ORIGIN, now)["link"]).fragment


def claim(a, invite, key="desktop", kind="device", now=T):
    return a.claim(invite, owner.key_hash(key), kind, "Studio", now)


def status(work):
    from mailbox.admission import AdmissionError
    try:
        work()
        return 201
    except AdmissionError as e:
        return e.status


def member(a, kind, key, now=T):
    invite = a.invite("desktop", kind, now)
    return claim(a, urlsplit(invite["link"]).fragment, key, kind, now)


def test_first_claim_is_atomic(tmp_path):
    path = tmp_path / "test.db"
    with opened(path) as a:
        token = mint(a)
    barrier = Barrier(2)

    def compete(key):
        with opened(path) as connection:
            barrier.wait(5)
            return status(lambda: claim(connection, token, key))
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(compete, ["one", "two"]))
    assert sorted(statuses) == [201, 410]


@pytest.mark.parametrize("age,expected", [(899, 201), (900, 410)])
def test_expiry_boundary(tmp_path, age, expected):
    with opened(tmp_path / "test.db") as a:
        token = mint(a)
        assert status(lambda: claim(a, token, now=T + age)) == expected


@pytest.mark.parametrize("version", [3, 5])
def test_empty_upgraded_database_cannot_bootstrap(tmp_path, version):
    from mailbox import migrations
    path = tmp_path / "old.db"
    conn = _old_database(path)
    for step, statements in migrations._STEPS:
        if step <= version:
            for stmt in statements:
                stmt(conn) if callable(stmt) else conn.execute(stmt)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    for _ in range(2):
        with opened(path) as a:
            assert a.mode == "legacy"
            assert status(lambda: mint(a)) == 409


def test_restart_decision_and_revocation(tmp_path):
    path = tmp_path / "test.db"
    with opened(path) as a:
        old = mint(a)
        first = mint(a)
        assert status(lambda: claim(a, old)) == 410
        assert status(lambda: claim(a, first, kind="agent")) == 400
        admin = claim(a, first)
        assert a.me("desktop", T)["id"] == admin["id"]
        assert status(lambda: a.me(owner.key_hash("desktop"), T)) == 401
        assert status(lambda: mint(a)) == 409
        assert status(lambda: a.revoke("desktop", admin["id"], T)) == 409
        pending = member(a, "agent", "builder")
    with opened(path) as a:
        assert a.me("builder", T)["status"] == "pending"
        assert a.pending("desktop", T)[0]["code"] == pending["code"]
        assert len(pending["code"]) == 6
        assert status(lambda: a.decide("desktop", pending["id"], "approve", "wrong", T)) == 403
        assert status(lambda: a.pending("builder", T)) == 403
        a.decide("desktop", pending["id"], "approve", pending["code"], T)
        assert status(lambda: a.decide("desktop", pending["id"], "approve", pending["code"], T)) == 409
        assert a.me("builder", T)["role"] == "agent"
        assert status(lambda: a.pending("builder", T)) == 403
        listing = a.principals("desktop", T)
        assert listing["maxDevices"] == 10
        assert set(listing["principals"][0]) == {"id","kind","role","label","status","created","lastUsed"}
        a.revoke("desktop", pending["id"], T)
        assert status(lambda: a.me("builder", T)) == 401
        assert a.me("desktop", T)["status"] == "approved"


def test_rejection_expiration_and_recovery(tmp_path):
    with opened(tmp_path / "test.db") as a:
        old = claim(a, mint(a))
        denied = member(a, "device", "other")
        a.decide("desktop", denied["id"], "reject", denied["code"], T)
        assert status(lambda: a.me("other", T)) == 401
        invitation = a.invite("desktop", "agent", T)
        late = claim(a, urlsplit(invitation["link"]).fragment, "late", "agent", T+800)
        assert late["expires"] == T+900
        assert status(lambda: a.decide("desktop", late["id"], "approve", late["code"], T+900)) == 410
        token = mint(a, "recovery", T+901)
        new = claim(a, token, "new", now=T+902)
        assert new["role"] == "admin"
        assert status(lambda: claim(a, token, "later", now=T+903)) == 410
        assert a.me("new", T+903)["id"] == new["id"]
        a.revoke("new", old["id"], T+903)
        assert status(lambda: a.me("desktop", T+903)) == 401


def test_agent_does_not_take_a_device_slot(tmp_path):
    with opened(tmp_path / "test.db") as a:
        claim(a, mint(a))
        for i in range(9):
            p = member(a, "device", f"phone-{i}")
            a.decide("desktop", p["id"], "approve", p["code"], T)
        agent = member(a, "agent", "agent")
        a.decide("desktop", agent["id"], "approve", agent["code"], T)
        p = member(a, "device", "extra")
        assert status(lambda: a.decide("desktop", p["id"], "approve", p["code"], T)) == 409


def test_join_over_http(tmp_path, monkeypatch, capsys):
    from contextlib import closing
    from mailbox import admin
    from mailbox.tests.test_server import _running, call
    monkeypatch.setenv("MAILBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAILBOX_PUBLIC_ORIGIN", ORIGIN)
    with closing(_running(tmp_path, native=True)) as servers:
        server = next(servers)
        assert admin.main(["admission", "invite", "--first"]) == 0
        link = urlsplit(capsys.readouterr().out.strip())
        assert (link.scheme, link.netloc) == ("hook", "join")
        def post(path, body, token=None):
            return call(server.base_url, path, method="POST", body=body, token=token)
        def get(path, token):
            return call(server.base_url, path, token=token)
        body = {"invite": link.fragment, "kind": "device", "keyHash": owner.key_hash("desktop"), "label": "Studio"}
        code, first = post("/admission/claim", body)
        assert code == 201 and first["role"] == "admin"
        assert post("/admission/claim", body)[0] == 410
        assert get("/admission/me", "desktop")[1]["id"] == first["id"]
        assert get("/admission/me", body["keyHash"])[0] == 401
        assert "admission" in get("/version", "desktop")[1]["features"]
        for device, decision in [("tablet", "reject"), ("phone", "approve")]:
            code, invite = post("/admission/invites", {"kind":"device"}, "desktop")
            assert code == 201
            code, p = post("/admission/claim", {**body, "invite":urlsplit(invite["link"]).fragment,
                                                "keyHash":owner.key_hash(device)})
            assert code == 201 and p["status"] == "pending"
            assert get("/admission/pending", "desktop")[1]["pending"][0]["code"] == p["code"]
            assert post(f"/admission/requests/{p['id']}/decision", {"decision":decision, "code":p["code"]},
                        "desktop")[0] == 200
            if decision == "approve":
                assert get("/admission/principals", device)[0] == 200
                assert post(f"/admission/principals/{p['id']}/revoke", {}, "desktop")[0] == 200
            assert get("/admission/me", device)[0] == 401
        assert post("/admission/invite", {"purpose":"recovery"}, "desktop")[0] == 404


def test_admission_vectors_reproduce():
    import json
    from pathlib import Path
    from mailbox.tests.gen_admission_vectors import generate
    vectors = json.loads(Path(__file__).with_name("admission-vectors.json").read_text())
    assert generate() == vectors
    assert [step["response"]["status"] for step in vectors["cases"]] == [
        201, 201, 201, 200, 401, 201, 201, 200, 200, 200, 401]


def test_fresh_server_creates_no_shared_credential(tmp_path):
    from mailbox.tests.test_server import _running, call
    with contextmanager(_running)(tmp_path, native=True) as server:
        assert not (tmp_path / "data" / "mailbox-token").exists()
        assert not (tmp_path / "backups" / "mailbox-token").exists()
        assert call(server.base_url, "/asks", token="")[0] == 401


def test_recovery_must_prove_its_key_before_the_old_admin_retires(tmp_path):
    with opened(tmp_path / "test.db") as a:
        old = claim(a, mint(a))
        claim(a, mint(a, "recovery"), "rescuer")
        assert status(lambda: a.revoke("desktop", old["id"], T)) == 409
        assert a.me("rescuer", T)["status"] == "approved"
        assert a.revoke("desktop", old["id"], T)["status"] == "revoked"


def test_pending_capacity_and_invite_rate_limit(tmp_path):
    from mailbox.admission import MAX_OUTSTANDING, INVITES_PER_WINDOW
    with opened(tmp_path / "test.db") as a:
        claim(a, mint(a))
        for i in range(MAX_OUTSTANDING):
            member(a, "agent", f"pending-{i}")
        assert status(lambda: member(a, "agent", "extra")) == 429
        for _ in range(INVITES_PER_WINDOW - MAX_OUTSTANDING - 1):
            a.invite("desktop", "agent", T)
        assert status(lambda: a.invite("desktop", "agent", T)) == 429
        assert member(a, "agent", "later", now=T+900)["status"] == "pending"


def test_rate_limit_anonymous_claims(tmp_path):
    from mailbox.tests.test_server import _running, call
    with contextmanager(_running)(tmp_path, native=True) as server:
        statuses = [call(server.base_url, "/admission/claim", method="POST", body={})[0] for _ in range(65)]
        assert statuses[:64] == [410] * 64
        assert statuses[-1] == 429


@pytest.mark.parametrize("origin", ["http://pond.example", "https://user@pond.example",
                                  "https://[::1]extra", "https://pond.example/?key",
                                  "https://pond.example/path", "\x00https://pond.example"])
def test_invalid_operator_origin_cannot_mint(tmp_path, origin):
    with opened(tmp_path / "test.db") as a:
        assert status(lambda: a.operator_invite("first", origin, T)) == 400


def test_full_capacity_recovery_retires_only_the_named_device_after_proof(tmp_path):
    path = tmp_path / "test.db"
    with opened(path) as a:
        old = claim(a, mint(a))
        for i in range(9):
            p = member(a, "device", f"phone-{i}")
            a.decide("desktop", p["id"], "approve", p["code"], T)
        assert status(lambda: mint(a, "recovery")) == 409
        token = a.operator_invite("recovery", ORIGIN, T, replace_id=old["id"])
        fragment = urlsplit(token["link"]).fragment
        claim(a, fragment, "replacement")  # the response is lost
        assert a.me("desktop", T)["id"] == old["id"]
        assert status(lambda: claim(a, fragment, "duplicate")) == 410
    with opened(path) as a:
        assert a.me("replacement", T+1)["status"] == "approved"
        assert status(lambda: a.me("desktop", T+1)) == 401
        rows = a.principals("replacement", T+1)["principals"]
        assert len([row for row in rows if row["status"] == "approved"]) == 10
        assert status(lambda: a.me("phone-0", T+1)) == 201


def test_interrupted_fresh_initialization_retries_as_native(tmp_path, monkeypatch):
    from mailbox import migrations
    path = tmp_path / "test.db"

    def stop(_):
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_STEPS", migrations._STEPS + ((7, (stop,)),))
        store = Store(path)
        with pytest.raises(RuntimeError):
            store.open()
        store.close()
    with opened(path) as admission:
        assert admission.mode == "native"
        assert claim(admission, mint(admission))["role"] == "admin"


def test_expiry_checked_after_the_request_obtains_the_database_lock(tmp_path, monkeypatch):
    from mailbox.tests.test_server import _running, call
    with contextmanager(_running)(tmp_path, native=True) as server:
        a = server.srv.service.admission
        token = mint(a)
        clock = [T + 899]
        monkeypatch.setattr("time.time", lambda: clock[0])
        transaction = a.store.event_transaction
        @contextmanager
        def after_wait():
            with transaction():
                clock[0] = T + 900
                yield
        monkeypatch.setattr(a.store, "event_transaction", after_wait)
        code, _ = call(server.base_url, "/admission/claim", method="POST", body={
            "kind": "device", "keyHash": owner.key_hash("desktop"), "label": "Desktop", "invite": token})
        assert code == 410


def test_repeat_revocation_preserves_the_original_actor(tmp_path):
    with opened(tmp_path / "test.db") as a:
        admin = claim(a, mint(a))
        other = member(a, "device", "other")
        a.decide("desktop", other["id"], "approve", other["code"], T)
        agent = member(a, "agent", "agent")
        a.decide("desktop", agent["id"], "approve", agent["code"], T)
        a.revoke("desktop", agent["id"], T)
        a.revoke("other", agent["id"], T+1)
        assert a.db.execute("SELECT revoked_by FROM principals WHERE id=?",
                            (agent["id"],)).fetchone() == (admin["id"],)
