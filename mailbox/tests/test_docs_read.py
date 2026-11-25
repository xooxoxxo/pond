"""Reading and releasing an ask's documents (Pond 1.10.0): GET /asks/{id}/docs/{sha256}
and POST /asks/{id}/docs/release, through the real HTTP server, and the wire vectors
the phones copy (doc-wire-vectors.json).

Every text here is synthetic."""
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from mailbox.tests import gen_doc_wire
from mailbox.tests import gen_phone_vectors as phone
from mailbox.tests.test_docs_post import (  # noqa: F401 — `pond` is a fixture
    NOTES_TEXT, PLAN_TEXT, _amend, _body, _counts, _post, _posted, ask, pond, read, sha,
)
from mailbox.tests.test_server import hcall

REPO = Path(__file__).resolve().parents[2]
MISSING_ID = "00000000-0000-4000-8000-000000000000"
STANDARD_HEADERS = {"Server", "Date"}


def get_doc(base_url, item_id, digest, token):
    """GET /asks/{id}/docs/{sha256} → (status, headers, raw body bytes)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"{base_url}/asks/{item_id}/docs/{digest}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def read_json(base_url, item_id, digest, token):
    status, headers, raw = get_doc(base_url, item_id, digest, token)
    assert headers["Content-Type"] == "application/json; charset=utf-8", (status, raw)
    return status, json.loads(raw)


def release(base_url, item_id, token, body=None, raw=None):
    return hcall(base_url, f"/asks/{item_id}/docs/release", token, method="POST", body=body, raw=raw)


def close(base_url, token, item_id, verdict="done"):
    status, data = hcall(base_url, f"/asks/{item_id}/resolve", token, method="POST", body={"verdict": verdict})
    assert status == 200, data


def _events(srv):
    return srv.service.store.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()


def _items(srv):
    return srv.service.store.conn.execute("SELECT * FROM items ORDER BY id").fetchall()


GONE = (410, {"error": "document no longer kept"})
NOT_FOUND = (404, {"error": "not found"})


# --- reading ---------------------------------------------------------------------------

def test_a_kept_document_reads_as_its_exact_bytes(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token, ask(read(NOTES_TEXT)), {sha(NOTES_TEXT): NOTES_TEXT})
    status, headers, body = get_doc(base_url, item_id, sha(NOTES_TEXT), token)
    assert status == 200
    assert body == NOTES_TEXT.encode("utf-8") == srv.service.store.get_doc(sha(NOTES_TEXT))
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Length"] == str(len(body)) == str(len(NOTES_TEXT) + 2)
    assert set(headers.keys()) == STANDARD_HEADERS | {"Content-Type", "Cache-Control", "X-Content-Type-Options",
                                                      "Content-Length"}


def test_a_document_keeps_its_bytes_exactly(pond):
    """No newline added, none taken away, CRLF and a BOM kept as sent."""
    srv, base_url, token, _ = pond
    text = "﻿# Plan\r\n\r\nno newline at the end"
    item_id, _ = _posted(base_url, token, ask(read(text)), {sha(text): text})
    status, _, body = get_doc(base_url, item_id, sha(text), token)
    assert (status, body) == (200, text.encode("utf-8"))


@pytest.mark.parametrize("segment", [
    "A" * 64, "0" * 63, "0" * 65, "g" * 64, "release", "%2e%2e", "0" * 64 + "%00",
], ids=["uppercase", "short", "long", "not-hex", "word", "dots", "nul"])
def test_a_malformed_hash_is_404_and_never_echoed(pond, segment):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    status, data = read_json(base_url, item_id, segment, token)
    assert (status, data) == NOT_FOUND


def test_a_hash_the_ask_never_linked_is_404(pond):
    """Also when another ask keeps that very text: reading goes through the ask."""
    srv, base_url, token, _ = pond
    first, _ = _posted(base_url, token)
    other, _ = _posted(base_url, token, ask(read(NOTES_TEXT), title="Other ask"), {sha(NOTES_TEXT): NOTES_TEXT})
    assert read_json(base_url, first, sha(NOTES_TEXT), token) == NOT_FOUND
    assert read_json(base_url, first, "0" * 64, token) == NOT_FOUND
    assert get_doc(base_url, other, sha(NOTES_TEXT), token)[0] == 200


def test_an_unknown_ask_is_404(pond):
    srv, base_url, token, _ = pond
    _posted(base_url, token)
    assert read_json(base_url, MISSING_ID, sha(PLAN_TEXT), token) == NOT_FOUND
    assert read_json(base_url, "nope", sha(PLAN_TEXT), token) == NOT_FOUND


@pytest.mark.parametrize("token_kind", ["none", "wrong"])
def test_no_token_is_401_before_anything_else(pond, token_kind):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    bad = None if token_kind == "none" else "not-the-token"
    unauthorized = (401, {"error": "unauthorized"})
    assert read_json(base_url, item_id, sha(PLAN_TEXT), bad) == unauthorized
    assert read_json(base_url, MISSING_ID, "nope", bad) == unauthorized
    assert release(base_url, item_id, bad) == unauthorized
    assert release(base_url, MISSING_ID, bad) == unauthorized


def test_other_paths_under_docs_are_404(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    for path in (f"/asks/{item_id}/docs", f"/asks/{item_id}/docs/", f"/asks/{item_id}/docs/{sha(PLAN_TEXT)}/x"):
        assert hcall(base_url, path, token) == NOT_FOUND, path
    assert hcall(base_url, f"/asks/{item_id}/docs/{sha(PLAN_TEXT)}", token, method="POST", body={}) == NOT_FOUND
    assert hcall(base_url, f"/asks/{item_id}/docs/release", token) == NOT_FOUND       # GET: not a hash


def test_with_the_switch_off_a_stored_document_still_reads_and_releases(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    srv.service.store.delete_setting("docs_enabled")
    status, _, body = get_doc(base_url, item_id, sha(PLAN_TEXT), token)
    assert (status, body) == (200, PLAN_TEXT.encode("utf-8"))
    close(base_url, token, item_id)
    assert release(base_url, item_id, token) == (200, {"released": 1})
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


# --- never shown anywhere else -----------------------------------------------------------

def test_the_link_page_health_and_telegram_never_hold_document_text(pond, caplog):
    srv, base_url, token, _ = pond
    marker = "Synthetic marker for the read check"
    text = f"# Plan\n\n{marker}\n"
    caplog.set_level(logging.DEBUG)
    item_id, _ = _posted(base_url, token, ask(read(text)), {sha(text): text})
    assert get_doc(base_url, item_id, sha(text), token)[0] == 200
    with urllib.request.urlopen(f"{base_url}/i/{item_id}", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    health = json.dumps(hcall(base_url, "/health", None)[1])
    message = srv.service.telegram.sent_messages[-1]["text"]
    assert "   📄 plan.md (on the Mac)" in message.splitlines()
    close(base_url, token, item_id)
    released = json.dumps(release(base_url, item_id, token)[1])
    gone = json.dumps(read_json(base_url, item_id, sha(text), token)[1])
    seen = [page, health, message, released, gone, json.dumps(srv.service.telegram.calls, default=str)]
    seen += [r.getMessage() for r in caplog.records]
    for path in ("/asks?status=done", f"/asks/{item_id}", "/events"):
        for protocol in (None, 5, 6):
            seen.append(json.dumps(hcall(base_url, path, token, protocol=protocol)[1]))
    assert not [s for s in seen if marker in s]


# --- releasing ---------------------------------------------------------------------------

def test_release_on_an_open_ask_is_409_and_deletes_nothing(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    before, events = _counts(srv), _events(srv)
    assert release(base_url, item_id, token) == (409, {"error": "ask is open"})
    assert _counts(srv) == before and _events(srv) == events
    assert srv.service.store.doc_status(item_id, sha(PLAN_TEXT)) == "kept"
    assert get_doc(base_url, item_id, sha(PLAN_TEXT), token)[0] == 200


def test_release_on_an_unknown_ask_is_404(pond):
    srv, base_url, token, _ = pond
    assert release(base_url, MISSING_ID, token) == NOT_FOUND
    assert release(base_url, "nope", token) == NOT_FOUND


def test_release_deletes_the_asks_text_and_keeps_text_another_ask_keeps(pond):
    srv, base_url, token, _ = pond
    store = srv.service.store
    mine, _ = _posted(base_url, token, ask(read(), read(NOTES_TEXT, path="/var/tmp/notes.md", do="Read the notes")),
                      {sha(PLAN_TEXT): PLAN_TEXT, sha(NOTES_TEXT): NOTES_TEXT})
    theirs, _ = _posted(base_url, token, ask(read(), title="Another ask"), {sha(PLAN_TEXT): PLAN_TEXT})
    close(base_url, token, mine)
    events, items = _events(srv), _items(srv)

    assert release(base_url, mine, token) == (200, {"released": 2})
    assert store.get_doc(sha(NOTES_TEXT)) is None                       # only this ask kept it
    assert store.get_doc(sha(PLAN_TEXT)) == PLAN_TEXT.encode("utf-8")   # the other ask still keeps it
    status, _, body = get_doc(base_url, theirs, sha(PLAN_TEXT), token)
    assert (status, body) == (200, PLAN_TEXT.encode("utf-8"))
    assert read_json(base_url, mine, sha(PLAN_TEXT), token) == GONE      # released here, kept there
    assert read_json(base_url, mine, sha(NOTES_TEXT), token) == GONE
    assert _events(srv) == events and _items(srv) == items              # no event, no change to the ask

    assert release(base_url, mine, token) == (200, {"released": 0})
    assert read_json(base_url, mine, sha(PLAN_TEXT), token) == GONE


def test_a_dismissed_ask_releases_too(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id, verdict="declined")
    assert release(base_url, item_id, token) == (200, {"released": 1})
    assert srv.service.store.get_doc(sha(PLAN_TEXT)) is None


def test_an_ask_without_documents_releases_nothing(pond):
    srv, base_url, token, _ = pond
    status, data = _post(base_url, token, ask({"do": "Plain"}))
    assert status == 201, data
    item_id = data["item"]["id"]
    close(base_url, token, item_id)
    assert release(base_url, item_id, token) == (200, {"released": 0})


def test_release_ignores_its_body_but_refuses_a_malformed_one(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    assert release(base_url, item_id, token, raw=b"not json") == (400, {"error": "invalid json"})
    assert srv.service.store.doc_status(item_id, sha(PLAN_TEXT)) == "kept"
    assert release(base_url, item_id, token, body={"anything": 1}) == (200, {"released": 1})
    assert release(base_url, item_id, token, body={}) == (200, {"released": 0})
    assert release(base_url, item_id, token) == (200, {"released": 0})


def test_release_needs_no_owner_key_even_when_the_owner_key_is_required(pond):
    srv, base_url, token, _ = pond
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    srv.service.store.set_setting("owner_key_required", "1")
    assert srv.service.owner_key_required()
    assert release(base_url, item_id, token) == (200, {"released": 1})


# --- a released ask reopened and corrected -------------------------------------------------

def _released_and_reopened(srv, base_url, token, clock):
    item_id, base = _posted(base_url, token)
    close(base_url, token, item_id)
    assert release(base_url, item_id, token) == (200, {"released": 1})
    status, data = hcall(base_url, f"/asks/{item_id}/reopen", token, method="POST", body={"actor": "session"})
    assert status == 200 and data["item"]["status"] == "open", data
    clock.advance(61)
    return item_id, base


def test_a_reopened_ask_keeps_a_released_doc_without_its_text_and_reads_410(pond):
    srv, base_url, token, clock = pond
    item_id, base = _released_and_reopened(srv, base_url, token, clock)
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Read the plan again"
    status, data = _amend(base_url, token, item_id, base, new)
    assert status == 200, data
    assert srv.service.store.doc_status(item_id, sha(PLAN_TEXT)) == "released"
    assert read_json(base_url, item_id, sha(PLAN_TEXT), token) == GONE


def test_a_reopened_ask_that_resends_the_text_keeps_it_again(pond):
    srv, base_url, token, clock = pond
    item_id, base = _released_and_reopened(srv, base_url, token, clock)
    new = _body(srv, item_id)
    new["steps"][0]["do"] = "Read the plan again"
    status, data = _amend(base_url, token, item_id, base, new, {sha(PLAN_TEXT): PLAN_TEXT})
    assert status == 200, data
    assert srv.service.store.doc_status(item_id, sha(PLAN_TEXT)) == "kept"
    status, _, body = get_doc(base_url, item_id, sha(PLAN_TEXT), token)
    assert (status, body) == (200, PLAN_TEXT.encode("utf-8"))


# --- the wire vectors ----------------------------------------------------------------------

def test_doc_wire_replays():
    env = {k: v for k, v in os.environ.items() if k != gen_doc_wire.RECORD_ENV}
    done = subprocess.run([sys.executable, "-m", "mailbox.tests.gen_doc_wire"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def test_the_doc_transcript_has_its_cases_in_order_and_nothing_private():
    text = gen_doc_wire.dump(gen_doc_wire.build())
    assert text == gen_doc_wire.OUT.read_text(encoding="utf-8")
    assert phone.TOKENS and not any(token in text for token in phone.TOKENS)
    for needle in ("/Users/", "/home/", "192.168.", "tailnet", ".ts.net"):
        assert needle not in text, needle
    assert not re.search(r"\b(?!ISO-|SHA-|UTF-)[A-Z]{2,5}-\d+\b", text)   # no ticket ids
    doc = json.loads(text)
    assert doc["pond"] == gen_doc_wire.VERSION
    got = [(c["name"], c["response"]["status"]) for c in doc["cases"]]
    assert got == [
        ("doc-post", 201), ("doc-read", 200), ("doc-read-not-linked", 404), ("doc-read-unknown-ask", 404),
        ("doc-read-no-token", 401), ("doc-release-open", 409), ("doc-close", 200), ("doc-release", 200),
        ("doc-release-again", 200), ("doc-read-released", 410), ("doc-release-unknown-ask", 404),
        # the final wave (W9)
        ("doc-post-errors", 400), ("doc-post-bom-crlf", 201), ("doc-read-bom-crlf", 200),
        ("doc-amend-keeps-held", 200), ("doc-amend-unknown-hash", 400), ("doc-post-deduped", 200),
        ("doc-read-deduped-not-linked", 404), ("doc-version-on", 200), ("doc-version-off", 200),
        ("doc-post-switch-off", 409), ("doc-post-empty-docs-switch-off", 409),
        # Pond 1.12.0: the total limit
        ("doc-post-store-full", 409),
    ]
    cases = {c["name"]: c for c in doc["cases"]}
    read_ok = cases["doc-read"]["response"]
    assert read_ok["headers"] == {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store",
                                  "X-Content-Type-Options": "nosniff"}
    assert read_ok["body"] == gen_doc_wire.PLAN_TEXT
    assert base64.b64decode(read_ok["bodyBase64"]) == gen_doc_wire.PLAN_TEXT.encode("utf-8")

    # W9: what the clients need pinned.
    sha_plan = gen_doc_wire.PLAN_SHA
    errors = cases["doc-post-errors"]
    assert errors["response"]["body"]["error"] == "invalid documents"
    details = errors["response"]["body"]["details"]
    assert {d.split(": ", 1)[1] for d in details[2:]} == {"does not match its hash", "not used by any step or link"}
    assert details[:2] == ["steps[0].doc.bytes: does not match the document", "steps[1].doc: no document with this hash"]
    sent_order = [d.split(":", 1)[0].removeprefix("docs.") for d in details[2:]]
    assert sent_order == sorted(sent_order) and len(set(sent_order)) == 2
    assert sha_plan in errors["request"]["body"]["docs"]

    bom = gen_doc_wire.BOM_TEXT
    assert bom.startswith("\ufeff") and "\r\n" in bom and not bom.isascii()
    posted = cases["doc-post-bom-crlf"]["request"]["body"]
    step = posted["instructable"]["steps"][0]
    assert step["doc"] == {"sha256": hashlib.sha256(bom.encode("utf-8")).hexdigest(),
                           "bytes": len(bom.encode("utf-8"))}
    assert step["doc"]["bytes"] > len(bom)                     # bytes counts UTF-8 bytes, not characters
    assert posted["docs"] == {step["doc"]["sha256"]: bom}
    read_bom = cases["doc-read-bom-crlf"]["response"]
    assert read_bom["body"] == bom
    assert base64.b64decode(read_bom["bodyBase64"]) == bom.encode("utf-8")

    kept = cases["doc-amend-keeps-held"]
    assert "docs" not in kept["request"]["body"]
    assert kept["request"]["body"]["instructable"]["steps"][0]["doc"] == step["doc"]
    unknown = cases["doc-amend-unknown-hash"]
    assert "docs" not in unknown["request"]["body"]
    assert unknown["response"]["body"] == {"error": "invalid documents",
                                           "details": ["steps[0].doc: no document with this hash"]}

    deduped = cases["doc-post-deduped"]
    assert deduped["response"]["body"]["deduped"] is True and deduped["request"]["body"]["docs"]
    assert deduped["response"]["body"]["item"]["id"] == "{ask2}"
    assert cases["doc-read-deduped-not-linked"]["request"]["path"].endswith(
        next(iter(deduped["request"]["body"]["docs"])))

    assert "docs" in cases["doc-version-on"]["response"]["body"]["features"]
    assert "docs" not in cases["doc-version-off"]["response"]["body"]["features"]
    assert cases["doc-post-switch-off"]["request"]["body"]["docs"]
    assert cases["doc-post-empty-docs-switch-off"]["request"]["body"]["docs"] == {}
    for name in ("doc-post-switch-off", "doc-post-empty-docs-switch-off"):
        assert cases[name]["response"]["body"] == {"error": "documents not enabled"}
    assert cases["doc-release"]["response"]["body"] == {"released": 1}
    assert cases["doc-release-again"]["response"]["body"] == {"released": 0}
    assert cases["doc-read-released"]["response"]["body"] == {"error": "document no longer kept"}
    assert cases["doc-read-no-token"]["request"]["token"] is False
    full = cases["doc-post-store-full"]
    assert full["response"]["body"] == {"error": "document store full"}
    assert full["request"]["body"]["docs"] and full["request"]["body"]["instructable"]["steps"][0]["doc"]
    assert not any(text in json.dumps(full["response"]) for text in full["request"]["body"]["docs"].values())


def test_the_doc_transcript_does_not_depend_on_todays_date():
    """The wall clock reads the pinned time + 400 days, and the file still rebuilds byte for byte."""
    late = phone.T0 + timedelta(days=400)

    class Late(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return late.astimezone(tz) if tz is not None else late.replace(tzinfo=None)

    with mock.patch("mailbox.store.datetime", Late), mock.patch("mailbox.service.datetime", Late), \
            mock.patch("mailbox.server.datetime", Late):
        assert gen_doc_wire.dump(gen_doc_wire.build()) == gen_doc_wire.OUT.read_text(encoding="utf-8")


# --- version and release notes -------------------------------------------------------------

def test_1_10_0_is_still_described():
    """1.11.0 came after: the version and schema checks live in test_server.py."""
    from mailbox import migrations
    assert dict(migrations._STEPS)[4] is migrations._DDL_4
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("\n## 1.10.0\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("`protocol` stays 5", "Schema 4", "`docs`", "`ask_docs`", "Pond 1.9.0", "`doc`",
                   "262,144", "786,432", "docs on|off|status", "off by default",
                   "GET /asks/{id}/docs/{sha256}", "POST /asks/{id}/docs/release",
                   "410", "ask is open", "nightly backups until they rotate out (30 days)",
                   "about 0.1 s of CPU per 256 KB of", "doc-wire-vectors.json"):
        assert needle in section, needle
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "`/asks/{id}/docs/{sha256}`" in readme and "`/asks/{id}/docs/release`" in readme
    assert "Each text's key is the SHA-256 of its UTF-8 bytes" in readme
    from mailbox import admin
    assert "bug" in admin.__doc__.split("`docs` opens", 1)[1]
    flat_readme = " ".join(readme.split())
    for needle in ("comes back only with a new revision that resends its text",
                   "a correction that changes nothing is refused as `no change` before documents are looked at",
                   "On a Pond rolled back to 1.9.0 after documents were used",
                   "refuses added steps (`…doc: unknown key`) and can only be corrected by dropping `doc`",
                   "every document read answers `404`",
                   "the `docs_enabled` setting survives, so the switch is on again after the next update"):
        assert needle in flat_readme, needle
    keep = ("until 7 days after the ask is closed, whichever comes first",
            "a reopened ask keeps its documents while it is open")
    flat_section = " ".join(section.split())
    for needle in ("There is no total cap on stored documents", "an open ask keeps its documents as long as it is open",
                   "`docs status` shows how many are stored and their bytes in all"):
        assert needle in flat_section, needle                # 1.10.0 as it was released
    # 1.12.0 added the limit: the README and the admin help describe the Pond of today.
    assert "no total cap" not in flat_readme
    assert "an open ask keeps its documents as long as it is open" in flat_readme
    assert "no total cap" not in " ".join(admin.__doc__.split())
    for name, text in (("CHANGELOG", section), ("README", readme), ("admin", admin.__doc__)):
        flat = " ".join(text.split())
        assert "nothing deletes" not in flat.lower(), name
        for needle in keep:
            assert needle in flat, (name, needle)


# --- a reopen racing a release (fix round 1) -------------------------------------------------

def test_no_reopen_lands_between_the_closed_check_and_the_release(pond):
    """Another thread reopens the ask while the release is inside its write. The release
    holds the store across its check and its write, so the reopen waits until the
    release is done: the two land one after the other, never interleaved."""
    import threading
    srv, base_url, token, _ = pond
    store = srv.service.store
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    real = store._release_docs
    seen = {}

    def racing(rid):
        rival = threading.Thread(target=store.reopen_item, args=(item_id,))
        rival.start()
        rival.join(timeout=0.5)
        seen["reopen landed first"] = not rival.is_alive()
        seen["thread"] = rival
        seen["status when released"] = store.conn.execute(
            "SELECT status FROM items WHERE id = ?", (rid,)).fetchone()[0]
        return real(rid)

    store._release_docs = racing
    status, data = release(base_url, item_id, token)
    seen["thread"].join(timeout=10)
    assert (seen["status when released"], seen["reopen landed first"]) == ("done", False)
    assert (status, data) == (200, {"released": 1})          # closed when it was checked
    assert store.get_item(item_id).status == "open"           # the reopen landed after it


def test_a_reopen_just_before_the_release_is_409_and_deletes_nothing(pond):
    """The reopen lands after the route was called and before the release's one atomic
    step: the release sees an open ask."""
    srv, base_url, token, _ = pond
    store = srv.service.store
    item_id, _ = _posted(base_url, token)
    close(base_url, token, item_id)
    real = store.release_closed_docs

    def racing(rid):
        store.reopen_item(rid)
        return real(rid)

    store.release_closed_docs = racing
    before = _counts(srv)
    assert release(base_url, item_id, token) == (409, {"error": "ask is open"})
    assert _counts(srv) == before
    assert store.doc_status(item_id, sha(PLAN_TEXT)) == "kept"
    assert get_doc(base_url, item_id, sha(PLAN_TEXT), token)[0] == 200
