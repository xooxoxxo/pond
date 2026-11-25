"""The Central Time client: exact requests, errors without bodies."""
import http.client
import io
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mailbox import ct_client
from mailbox.ct_client import CentralTimeClient, CentralTimeError

TOKEN = "ct_" + "a" * 48


class _Resp(io.BytesIO):
    pass


def _capture_urlopen(monkeypatch, answer: dict | Exception):
    seen = {}

    def fake_open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        if isinstance(answer, Exception):
            raise answer
        return _Resp(json.dumps(answer).encode("utf-8"))

    monkeypatch.setattr(ct_client, "_open", fake_open)
    return seen


def test_create_item_posts_the_envelope(monkeypatch):
    seen = _capture_urlopen(monkeypatch, {"id": "i1", "title": "call mum"})
    client = CentralTimeClient("http://ct.test/", TOKEN)
    out = client.create_item("call mum", "2026-10-18", "09:00")
    assert out == {"id": "i1", "title": "call mum"}
    assert seen["url"] == "http://ct.test/api/items"
    assert seen["method"] == "POST"
    assert seen["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["body"] == {"title": "call mum", "anchor": {"date": "2026-10-18", "allDay": False, "time": "09:00"}}
    assert seen["timeout"] == 5.0


def test_create_item_without_date_sends_a_null_anchor(monkeypatch):
    seen = _capture_urlopen(monkeypatch, {"id": "i1"})
    CentralTimeClient("http://ct.test", TOKEN).create_item("buy milk")
    assert seen["body"] == {"title": "buy milk", "anchor": None}


def test_ack_fire_posts_the_action(monkeypatch):
    seen = _capture_urlopen(monkeypatch, {"applied": True})
    CentralTimeClient("http://ct.test", TOKEN).ack_fire("f-1", "tomorrow")
    assert seen["url"] == "http://ct.test/api/reminders/f-1/ack"
    assert seen["body"] == {"action": "tomorrow"}


def test_http_error_carries_status_and_kind_only(monkeypatch):
    err = urllib.error.HTTPError("http://ct.test/api/items", 502, "bad", {}, io.BytesIO(b'{"error":"call mum leaked"}'))
    _capture_urlopen(monkeypatch, err)
    with pytest.raises(CentralTimeError) as exc:
        CentralTimeClient("http://ct.test", TOKEN).create_item("call mum")
    assert (exc.value.status, exc.value.what) == (502, "create")
    assert str(exc.value) == "central time create 502"


def test_network_error_is_status_zero(monkeypatch):
    _capture_urlopen(monkeypatch, urllib.error.URLError("down"))
    with pytest.raises(CentralTimeError) as exc:
        CentralTimeClient("http://ct.test", TOKEN).ack_fire("f-1", "done")
    assert (exc.value.status, exc.value.what) == (0, "ack")


def test_http_exception_is_status_zero(monkeypatch):
    _capture_urlopen(monkeypatch, http.client.RemoteDisconnected("gone"))
    with pytest.raises(CentralTimeError) as exc:
        CentralTimeClient("http://ct.test", TOKEN).create_item("x")
    assert exc.value.status == 0


def test_non_object_json_answer_becomes_empty_dict(monkeypatch):
    _capture_urlopen(monkeypatch, ["not", "an", "object"])
    assert CentralTimeClient("http://ct.test", TOKEN).create_item("x") == {}


def test_a_redirect_is_an_error_and_the_bearer_is_not_resent():
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(CentralTimeError) as exc:
            CentralTimeClient(f"http://127.0.0.1:{srv.server_port}", TOKEN).create_item("x")
    finally:
        srv.shutdown()
        srv.server_close()
    assert (exc.value.status, exc.value.what) == (302, "create")
    assert hits == [("/api/items", f"Bearer {TOKEN}")]


def test_a_409_carries_a_whitelisted_reason_only(monkeypatch):
    """The ack route answers 409 with error in a fixed vocabulary; that word is
    kept so a tap can say why. Anything else in the body is dropped."""
    for body, reason in ((b'{"error":"stale"}', "stale"), (b'{"error":"closed"}', "closed"),
                         (b'{"error":"recurring"}', "recurring"), (b'{"error":"untimed"}', "untimed"),
                         (b'{"error":"unanchored"}', "unanchored"),
                         (b'{"error":"call mum leaked"}', None), (b'not json', None), (b"", None)):
        err = urllib.error.HTTPError("http://ct.test/api/reminders/f-1/ack", 409, "conflict", {}, io.BytesIO(body))
        _capture_urlopen(monkeypatch, err)
        with pytest.raises(CentralTimeError) as exc:
            CentralTimeClient("http://ct.test", TOKEN).ack_fire("f-1", "tomorrow")
        assert (exc.value.status, exc.value.what, exc.value.reason) == (409, "ack", reason)
        assert str(exc.value) == "central time ack 409"


# --- the id in an ack path (1.15.1) --------------------------------------------------

def _no_http(monkeypatch):
    def fake_open(req, timeout=None):
        raise AssertionError(f"no request may be sent: {req.full_url}")
    monkeypatch.setattr(ct_client, "_open", fake_open)


@pytest.mark.parametrize("bad", ["", ".", ".."])
def test_an_empty_or_dot_id_is_refused_before_any_request(monkeypatch, bad):
    _no_http(monkeypatch)
    client = CentralTimeClient("http://ct.test", TOKEN)
    with pytest.raises(CentralTimeError) as exc:
        client.ack_fire(bad, "done")
    assert (exc.value.status, exc.value.what) == (0, "ack")
    with pytest.raises(CentralTimeError) as exc:
        client.hh_ack(bad, "done", 1)
    assert (exc.value.status, exc.value.what) == (0, "hh-ack")


@pytest.mark.parametrize("bad, encoded", [("../items", "..%2Fitems"), ("a/b", "a%2Fb"), ("a%2Fb", "a%252Fb"),
                                          ("a?x=1", "a%3Fx%3D1"), ("a#b", "a%23b")])
def test_an_id_cannot_change_the_path(monkeypatch, bad, encoded):
    seen = _capture_urlopen(monkeypatch, {"applied": True})
    CentralTimeClient("http://ct.test", TOKEN).ack_fire(bad, "done")
    assert seen["url"] == f"http://ct.test/api/reminders/{encoded}/ack"
    CentralTimeClient("http://ct.test", TOKEN).hh_ack(bad, "done", 1)
    assert seen["url"] == f"http://ct.test/api/hh/deliveries/{encoded}/ack"


@pytest.mark.parametrize("good", ["f-1", "fire-1", "7c1b2e0e-0000-4000-8000-000000000001",
                                  "brief:2026-10-17", "a.b_c:D-9", "..x", "x.."])
def test_a_valid_id_gives_the_same_path_as_before(monkeypatch, good):
    seen = _capture_urlopen(monkeypatch, {"applied": True})
    CentralTimeClient("http://ct.test", TOKEN).ack_fire(good, "done")
    assert seen["url"] == f"http://ct.test/api/reminders/{good}/ack"
    CentralTimeClient("http://ct.test", TOKEN).hh_ack(good, "accept", 7)
    assert seen["url"] == f"http://ct.test/api/hh/deliveries/{good}/ack"
    assert seen["body"] == {"action": "accept", "actorTgId": 7}


def test_the_fake_client_refuses_a_dot_id_too():
    fake = ct_client.FakeCentralTimeClient()
    with pytest.raises(CentralTimeError):
        fake.ack_fire("..", "done")
    assert fake.calls == []
