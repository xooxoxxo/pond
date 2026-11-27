"""Exercise native access policy through the same HTTP routes used by the clients."""
from contextlib import contextmanager
import time

import pytest

from mailbox.tests.test_admission import claim, member, mint
from mailbox.tests.test_server import _running, call, post_ask


@pytest.fixture
def pond(tmp_path):
    with contextmanager(_running)(tmp_path, native=True) as server:
        now = int(time.time())
        admission = server.srv.service.admission
        claim(admission, mint(admission, now=now), now=now)
        for key in ["agent-a", "agent-b", "pending"]:
            candidate = member(admission, "agent", key, now)
            if key != "pending":
                admission.decide("desktop", candidate["id"], "approve", candidate["code"], now)
        yield server


def test_agents_see_only_their_own_asks_and_answers(pond):
    url = pond.base_url
    code, a = post_ask(url, "agent-a", ask="Same title")
    assert code == 201
    code, b = post_ask(url, "agent-b", ask="Same title", agent="agent-a", role="admin")
    assert code == 201 and a["item"]["id"] != b["item"]["id"]
    aid, bid = a["item"]["id"], b["item"]["id"]
    assert post_ask(url, "agent-a", ask="Same title")[1]["item"]["id"] == aid
    assert call(url, f"/asks/{aid}/reply", token="desktop", method="POST", body={"text":"Use the blue one"})[0] == 200
    for key, visible, hidden in (("agent-a", aid, bid), ("agent-b", bid, aid)):
        assert [i["id"] for i in call(url, "/asks", token=key)[1]["items"]] == [visible]
        assert call(url, "/asks/" + hidden, token=key)[0] == 404
        assert call(url, "/asks/" + hidden + "/docs/" + "a"*64, token=key)[0] == 404
        assert call(url, "/asks/" + hidden + "/append", token=key, method="POST",
                    body={"editKey": "same", "actor": "owner"})[0] == 404
        page = call(url, "/events?limit=1", token=key)[1]
        assert [e["itemId"] for e in page["events"]] == [visible]
        events = call(url, "/events", token=key)[1]["events"]
        assert all(e["itemId"] == visible for e in events)
        assert any(e["text"] == "Use the blue one" for e in events) == (key == "agent-a")
    assert call(url, f"/asks/{aid}/resolve", token="agent-a", method="POST",
                body={"verdict":"done", "actor":"owner"})[0] == 200
    events = call(url, f"/asks/{aid}", token="desktop")[1]["events"]
    assert events[-1]["actor"] == "agent"


def test_owner_writes_require_a_device(pond):
    code, ask = post_ask(pond.base_url, "agent-a")
    assert code == 201
    path = "/asks/" + ask["item"]["id"]
    for act in ("reply", "answers", "ask-about", "progress", "accept", "problems", "reopen"):
        assert call(pond.base_url, f"{path}/{act}", token="agent-a", method="POST", body={"actor":"owner"})[0] == 403
    for path in ("/owner/enroll", "/owner/revoke", "/owner/require", "/owner/revoke-request",
                 "/admission/invites", "/admission/requests/missing/decision", "/admission/principals/missing/revoke"):
        assert call(pond.base_url, path, token="agent-a", method="POST", body={})[0] == 403
    assert post_ask(pond.base_url, "agent-a", kind="bug")[0] == 403
    assert call(pond.base_url, "/notify", token="agent-a", method="POST", body={})[0] == 403


def test_revocation_takes_effect_on_the_next_request(pond):
    a = pond.srv.service.admission
    now = int(time.time())
    pid = a.me("agent-a", now)["id"]
    assert call(pond.base_url, "/asks", token="agent-a")[0] == 200
    a.revoke("desktop", pid, now)
    for path in ("/version", "/asks", "/events", "/admission/me"):
        assert call(pond.base_url, path, token="agent-a")[0] == 401
    for path in ("/version", "/asks", "/events"):
        assert call(pond.base_url, path, token="pending")[0] == 403
    assert call(pond.base_url, "/admission/me", token="pending")[1]["status"] == "pending"
    assert call(pond.base_url, "/asks", token="agent-b")[0] == 200


def test_agent_responses_hide_owner_conversations_and_delivery_ids(pond):
    url = pond.base_url
    code, posted = post_ask(url, "agent-a")
    assert code == 201
    pid = posted["item"]["id"]
    for kind in ("ask_about_question", "ask_about_answer"):
        pond.srv.store.append_event(pid, kind, "owner", "Private conversation")
    ack = call(url, f"/asks/{pid}/ack", method="POST", token="agent-a")[1]
    for item in (posted["item"], ack["item"]):
        assert not {"tg_chat_id", "tg_message_id"}.intersection(item["payload"])
    for route in (f"/asks/{pid}", "/events"):
        events = call(url, route, token="agent-a")[1]["events"]
        assert all(e["text"] != "Private conversation" for e in events)
        assert any(e["text"] == "Private conversation" for e in call(url, route, token="desktop")[1]["events"])


def test_native_post_uses_pond_context(pond):
    pond.srv.service.context = "team"
    code, posted = post_ask(pond.base_url, "agent-a", context="personal")
    assert code == 201
    assert posted["item"]["lane"] == "team"
