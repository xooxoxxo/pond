"""Write the phone conformance vectors: history, progress and strict-JSON data, and
three phone transcripts recorded from this Pond over loopback.

    python3 -m mailbox.tests.gen_phone_vectors                         # from the repo root
    python3 -m mailbox.tests.gen_phone_vectors --hook <conformance dir>  # also data/… and wire/… there

Every Pond is a fresh one on 127.0.0.1 with the Telegram fake and a pinned clock:
the store's and the service's clocks read the same pinned time, which moves forward
61 seconds before each correction, and new ids count up from
00000000-0000-4000-8000-000000000001, so two runs write the same bytes. Every ask is
synthetic. No file holds the Pond token or an owner key: a transcript says
`ownerKey: true` (the approved key), false or "unknown" (a key the Pond never
approved), and `token: "wrong"` where it sent a wrong token.
test_phone_vectors holds every file to what this generator writes today.
"""
import argparse
import copy
import io
import itertools
import json
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from unittest import mock

from mailbox import VERSION, owner
from mailbox import requirements as req
from mailbox.instructable import no_duplicates, validate
from mailbox.revisions import revision_hash

HERE = Path(__file__).parent
SOURCE = "mailbox/tests/gen_phone_vectors.py"
# kind -> the path in Hook's conformance folder; the copy here is flat, by basename.
FILES = {
    "history": "data/history-vectors.json",
    "progress": "data/progress-vectors.json",
    "strictjson": "data/strictjson-vectors.json",
    "phone-setup": "wire/phone-setup-vectors.json",
    "phone-acts": "wire/phone-acts-vectors.json",
    "phone-errors": "wire/phone-errors-vectors.json",
}
KINDS = tuple(FILES)
MIN_CASES = {"history": 14, "progress": 10, "strictjson": 14, "phone-setup": 7, "phone-acts": 13,
             "phone-errors": 10}
OUTCOMES = ("ok", "unauthorized", "notFound", "ownerKeyRequired", "ownerKeyRefused", "corrected",
            "requirementsUnmet", "askClosed", "invalid", "tooManyMessages")
ACT_ARGS = {
    "version": (), "enroll": ("keyHash", "label"), "enrollment": ("id",), "ownerStatus": (), "list": (),
    "detail": ("id",), "progress": ("id", "tick", "untick", "rev", "hash"),
    "answers": ("id", "answers", "rev", "hash"), "reply": ("id", "text"), "message": ("id", "text"),
    "done": ("id", "rev", "hash"), "skip": ("id",),
}
ACT_KINDS = tuple(ACT_ARGS)

# The cases of Pond 1.5's shared progress identities (@verify, @legacy:<path>). A Pond
# without them refuses those ticks, and the generator stops.
SHARED_ID_CASES = ("progress-verify", "progress-verify-changed", "progress-legacy-substep", "acts-tick-verify")

T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
CORRECTION_GAP_S = 61
PHONE_KEY = "android-conformance-key-phone"          # synthetic owner keys, never written
MAC_KEY = "android-conformance-key-example-mac"
SPARE_KEY = "android-conformance-key-spare"
UNKNOWN_KEY = "android-conformance-key-never-approved"
SECRETS = (PHONE_KEY, MAC_KEY, SPARE_KEY, UNKNOWN_KEY)
EDIT_KEY = "android-conformance-edit-key"
MISSING_ID = "00000000-0000-4000-8000-000000000000"   # never handed out: the counter starts at 1
TOKENS: list[str] = []                                 # every Pond token of this run, for the test

FORMATS = {
    "history": (
        "{name, note, detail: {item, events, revisions}, pins: {\"<rev>\": \"<hash>\"}, expect: {ok, problem?, "
        "current?: {rev, hash}, acceptedOnPond?}}. `detail` is GET /asks/{id} as the Pond answers a client "
        "sending X-Hook-Protocol: 6, ids and timestamps as recorded (the clock was pinned; hashes cover `at`). "
        "A tampered case edits that JSON and recomputes exactly the hashes it must, so the client's check "
        "(every rev 1..N, each hash, each prev, each body strict, title/urgency/from unchanged, the newest "
        "the item's revision, then the pins) fails for `problem` and no earlier reason. problem: "
        "badHash:<rev>, badChain:<rev>, unreadable:<rev>, missing, fixedFieldChanged:<rev>, rolledBack. "
        "acceptedOnPond is min(max(item.revision.acceptedRev, 1), current rev)."),
    "progress": (
        "{name, note, detail, ticked}. `detail` is GET /asks/{id} as served to X-Hook-Protocol: 6 (a case "
        "whose note says so adds or edits an event the Pond never serves, to test a client's filter). "
        "`ticked` is the sorted set of ticked step identities the Pond's requirements.ticked folds from the "
        "progress/owner events of this item against the accepted revision (no revisions: the posted "
        "instructable)."),
    "strictjson": (
        "{name, note, text | hex, parses, body, problem?, pond?}. `text` is the exact UTF-8 text; `hex` "
        "the raw bytes when they are not UTF-8. `parses`: a strict JSON parser accepts it (a key named twice "
        "in one object, after escapes, and a lone surrogate escape are refused). `body`: it is a revision "
        "body: it parses strictly, is a top-level object and is a valid instructable. `problem` "
        "(when body is false): duplicateKey when the text is otherwise JSON but names a key twice, else "
        "invalid. `pond`, only where present, is the Pond's own answer where it differs from "
        "docs/mailbox-protocol.md."),
}
_WIRE_FORMAT = (
    "{name, note, act: {kind, args}, request: {method, path, ownerKey, token?, protocol, body}, "
    "response: {status, body}, outcome, binds?}. The cases run in order against one fresh Pond (the "
    "Telegram fake, a pinned clock). Asks, keys and session acts a case does not show were made through "
    "the Pond's own API before the first case, unless a case's note says they happened since. `act` is what the phone was asked to do; `request` is exactly what "
    "it sends: `ownerKey` true sends the approved X-Hook-Owner-Key, false none, \"unknown\" a key the Pond "
    "never approved; `token` \"wrong\" sends a wrong Pond token (absent: the right one); `protocol` is "
    "the X-Hook-Protocol sent. `outcome` maps the response: 2xx ok, 401 unauthorized, 404 notFound, 403 "
    "\"owner key required\" ownerKeyRequired, 403 \"unknown owner key\" ownerKeyRefused, 409 \"ask was "
    "corrected\" corrected, 409 \"requirements_unmet\" requirementsUnmet, 409 \"ask is closed\" "
    "askClosed, 409 \"too many messages\" tooManyMessages, any other 4xx invalid. `binds` names an id the "
    "response carries ({\"ask1\": \"items.0.id\"}, a number indexes a list); later paths and args use it "
    "as {ask1}. In responses every bound id is its {name} and every ISO-8601 timestamp is {time}, except "
    "a revision's `at`, kept as recorded because its hash covers it. `rev` and `hash` in a request "
    "body and in act.args are the real values the Pond accepts.")


def out_path(kind: str) -> Path:
    return HERE / Path(FILES[kind]).name


# --- a pinned Pond ---------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@contextmanager
def _pinned(clock: _Clock) -> Iterator[None]:
    """The store's and the service's datetime.now read the pinned clock, and uuid4
    counts up, while a scenario runs. Nothing in the Pond's behaviour changes."""

    class Pinned(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return clock.now.astimezone(tz) if tz is not None else clock.now.replace(tzinfo=None)

    counter = itertools.count(1)

    def uuid4() -> uuid.UUID:
        return uuid.UUID(f"00000000-0000-4000-8000-{next(counter):012x}")

    with mock.patch("mailbox.store.datetime", Pinned), mock.patch("mailbox.service.datetime", Pinned), \
            mock.patch("mailbox.service.uuid4", uuid4):
        yield


class _Pond:
    def __init__(self, srv: Any, base_url: str, token: str, clock: _Clock) -> None:
        self.srv, self.base_url, self.token, self.clock = srv, base_url, token, clock
        self.service = srv.service
        self.store = srv.service.store

    def send(self, method: str, path: str, body: Any = None, *, key: Optional[str] = None,
             protocol: Optional[int] = 6, token: Optional[str] = None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token if token is None else token}"}
        if key is not None:
            headers[owner.KEY_HEADER] = key
        if protocol is not None:
            headers["X-Hook-Protocol"] = str(protocol)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def ok(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        status, answer = self.send(method, path, body, **kw)
        if not 200 <= status < 300:
            raise SystemExit(f"gen_phone_vectors: setup {method} {path} answered {status} {answer}")
        return answer

    def approve(self, secret: str, label: str) -> str:
        """Enroll a key and tap Approve on the Telegram fake, as the owner would."""
        k = self.service.enroll(owner.key_hash(secret), label)
        self.tap(f"e:{k.id}:y")
        if self.store.get_owner_key(k.id).status != "approved":
            raise SystemExit(f"gen_phone_vectors: the {label} key was not approved")
        return k.id

    def tap(self, data: str) -> None:
        from mailbox.tests.test_server import OWNER_ID
        self.service.handle_update({"callback_query": {
            "id": "cb", "from": {"id": OWNER_ID}, "data": data,
            "message": {"message_id": 1, "chat": {"id": OWNER_ID}},
        }})

    def post(self, ins: dict, *, editable: bool = False) -> tuple[str, str]:
        """POST /asks → (id, rev 1 hash). The clock moves a second, so asks list in order."""
        self.clock.advance(1)
        body: dict = {"project": "Example", "context": "personal", "instructable": ins}
        if editable:
            body["editKeyHash"] = owner.key_hash(EDIT_KEY)
        answer = self.ok("POST", "/asks", body)
        return answer["item"]["id"], answer["revision"]["hash"]

    def amend(self, item_id: str, base_rev: int, base_hash: str, reason: str, ins: dict) -> str:
        """A correction, 61 seconds after the last revision → its hash."""
        self.clock.advance(CORRECTION_GAP_S)
        answer = self.ok("POST", f"/asks/{item_id}/amend", {
            "editKey": EDIT_KEY, "baseRev": base_rev, "baseHash": base_hash, "reason": reason,
            "instructable": ins})
        return answer["revision"]["hash"]

    def detail(self, item_id: str) -> dict:
        return self.ok("GET", f"/asks/{item_id}")


@contextmanager
def _pond() -> Iterator[_Pond]:
    from mailbox.tests.test_server import _running
    clock = _Clock()
    with tempfile.TemporaryDirectory(prefix="phonetest-") as tmp, _pinned(clock):
        running = _running(Path(tmp))
        srv, base_url, token = next(running)
        TOKENS.append(token)
        try:
            srv.service.utcnow = lambda: clock.now
            srv.service.store.set_setting("corrections_enabled", "1")
            yield _Pond(srv, base_url, token, clock)
        finally:
            next(running, None)


# --- synthetic asks --------------------------------------------------------------------

VERIFY = {"do": "The deploy passes", "code": "curl -sI https://deploy.example.test/health"}


def _ask(title: str, **extra: Any) -> dict:
    """A plain synthetic ask: three steps, the last with two sub-steps, and Verify."""
    return {"v": 1, "title": title, "urgency": "today", "why": "The deploy key is 90 days old.",
            "steps": [{"do": "Open the deploy settings", "where": "deploy.example.test"},
                      {"do": "Rotate the key", "code": "deploy-key rotate --env staging"},
                      {"do": "Restart the services", "substeps": [{"do": "Restart the web service"},
                                                                  {"do": "Restart the worker"}]}],
            "verify": dict(VERIFY), **extra}


def _with_ids(ins: dict) -> dict:
    out = copy.deepcopy(ins)
    for i, step in enumerate(out["steps"]):
        step["id"] = f"s{i + 1}"
        for j, sub in enumerate(step.get("substeps") or []):
            sub["id"] = f"s{i + 1}.{j + 1}"
    return out


# --- history ---------------------------------------------------------------------------


def _hash_of(r: dict) -> str:
    return revision_hash(r["rev"], r["prev"], r["at"], r["reason"], r["body"])


def _body_or_none(text: str) -> Optional[dict]:
    """A revision body read as a strict client reads it: strict JSON, an object, no key
    named twice, a valid instructable. None when it is unreadable."""
    raw = text.encode("utf-8", "surrogatepass")
    if not pond_strict(raw)["body"]:
        return None
    return json.loads(raw)


def client_verify(detail: dict, pins: dict) -> dict:
    """The client's history check (Hook's RevisionCheck.verify, then checkPins), in its
    order, as `expect`."""
    served = detail["item"].get("revision")
    if served is None:
        return {"ok": True}
    if _hash_of(served) != served["hash"]:
        return {"ok": False, "problem": f"badHash:{served['rev']}"}
    revs = sorted(detail.get("revisions") or [], key=lambda r: r["rev"])
    if not revs or [r["rev"] for r in revs] != list(range(1, len(revs) + 1)):
        return {"ok": False, "problem": "missing"}
    previous, first, stamps = "", None, {}
    for r in revs:
        if _hash_of(r) != r["hash"]:
            return {"ok": False, "problem": f"badHash:{r['rev']}"}
        if r["prev"] != previous:
            return {"ok": False, "problem": f"badChain:{r['rev']}"}
        body = _body_or_none(r["body"])
        if body is None:
            return {"ok": False, "problem": f"unreadable:{r['rev']}"}
        if first is not None and any(first.get(k) != body.get(k) for k in ("title", "urgency", "from")):
            return {"ok": False, "problem": f"fixedFieldChanged:{r['rev']}"}
        if r["rev"] == 1:
            first = body
        stamps[r["rev"]] = r["hash"]
        previous = r["hash"]
    newest = revs[-1]
    if newest["rev"] != served["rev"]:
        return {"ok": False, "problem": "missing"}
    if newest["hash"] != served["hash"]:
        return {"ok": False, "problem": f"badHash:{served['rev']}"}
    if pins and (newest["rev"] < max(int(k) for k in pins)
                 or any(int(k) in stamps and stamps[int(k)] != h for k, h in pins.items())):
        return {"ok": False, "problem": "rolledBack"}
    accepted = min(max(served.get("acceptedRev") or 1, 1), served["rev"])
    return {"ok": True, "current": {"rev": newest["rev"], "hash": newest["hash"]}, "acceptedOnPond": accepted}


def _rev(detail: dict, rev: int) -> dict:
    return next(r for r in detail["revisions"] if r["rev"] == rev)


def _rehash(detail: dict, rev: int) -> None:
    r = _rev(detail, rev)
    r["hash"] = _hash_of(r)


def _rechain(detail: dict, start: int) -> None:
    """Recompute rev `start`'s hash and every later rev's prev and hash, and the item's
    revision from the newest one: the chain is whole again around a changed field."""
    revs = sorted(detail["revisions"], key=lambda r: r["rev"])
    for r in revs:
        if r["rev"] > start:
            r["prev"] = _rev(detail, r["rev"] - 1)["hash"]
        if r["rev"] >= start:
            r["hash"] = _hash_of(r)
    newest = revs[-1]
    served = detail["item"]["revision"]
    served.update({k: newest[k] for k in ("rev", "hash", "prev", "at", "reason", "body")})


def _edit_body(detail: dict, rev: int, change) -> None:
    r = _rev(detail, rev)
    body = json.loads(r["body"])
    change(body)
    r["body"] = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _history_cases() -> list[dict]:
    with _pond() as pond:
        pond.approve(PHONE_KEY, "Android")
        one, _ = pond.post(_ask("Rotate the staging deploy key"))
        three, h1 = pond.post(_ask("Rotate the preview deploy key"), editable=True)
        rev2 = _with_ids(_ask("Rotate the preview deploy key"))
        rev2["steps"][1]["code"] = "deploy-key rotate --env preview"
        h2 = pond.amend(three, 1, h1, "The rotate command named the wrong environment", rev2)
        rev3 = copy.deepcopy(rev2)
        rev3["steps"].append({"do": "Tell the team the key changed"})
        # It also changes a step, so it stays a correction under the append rule (1.9.0).
        rev3["steps"][0]["do"] = "Open the deploy settings page"
        h3 = pond.amend(three, 2, h2, "A step was missing", rev3)
        pond.ok("POST", f"/asks/{three}/accept", {"rev": 2, "hash": h2}, key=PHONE_KEY)
        two, g1 = pond.post(_ask("Rotate the production deploy key"), editable=True)
        later = _with_ids(_ask("Rotate the production deploy key"))
        later["why"] = "The production key is 90 days old."
        pond.amend(two, 1, g1, "Name the environment", later)
        d1, d3, d2 = pond.detail(one), pond.detail(three), pond.detail(two)

    def tampered(base: dict, change) -> dict:
        out = copy.deepcopy(base)
        change(out)
        return out

    def set_(path: list, value: Any):
        def change(d: dict) -> None:
            target = d
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
        return change

    def fourth(d: dict) -> None:
        r3 = _rev(d, 3)
        extra = {"rev": 4, "prev": r3["hash"], "at": "2026-11-03T09:10:00+00:00", "reason": "Another fix",
                 "body": r3["body"]}
        extra["hash"] = _hash_of(extra)
        d["item"]["revision"].update(extra)

    def other_served(d: dict) -> None:
        served = d["item"]["revision"]
        served["at"] = "2026-11-03T09:10:00+00:00"
        served["hash"] = _hash_of(served)

    def unreadable(rev: int, text: str):
        def change(d: dict) -> None:
            _rev(d, rev)["body"] = text
            _rehash(d, rev)
        return change

    body2 = _rev(d3, 2)["body"]
    dup2 = body2.replace('"v":1', '"v":1,"title":"Rotate a different key"', 1)
    lone1 = _rev(d1, 1)["body"].replace("90 days old.", "90 days old \\ud800.", 1)
    assert dup2 != body2 and "\\ud800" in lone1

    def chain_broken(d: dict) -> None:
        _rev(d, 2)["prev"] = "0" * 64
        _rehash(d, 2)

    def chain_rev1(d: dict) -> None:
        _rev(d, 1)["prev"] = "f" * 64
        _rechain(d, 1)

    def title2(d: dict) -> None:
        _edit_body(d, 2, lambda b: b.update(title="Rotate a different key"))
        _rechain(d, 2)

    def urgency3(d: dict) -> None:
        _edit_body(d, 3, lambda b: b.update(urgency="now"))
        _rechain(d, 3)

    def from1(d: dict) -> None:
        _edit_body(d, 2, lambda b: b.update({"from": {"host": "deploy.example.test"}}))
        _rechain(d, 2)

    specs: list[tuple[str, str, dict, dict, dict]] = [
        ("history-rev1-only", "an ask never corrected: rev 1 is current and in force", d1, {}, {"ok": True}),
        ("history-three-revisions", "two corrections, the owner accepted rev 2: current 3, in force 2", d3, {},
         {"ok": True}),
        ("history-correction-not-accepted", "one correction, not accepted yet: in force stays 1", d2, {},
         {"ok": True}),
        ("history-pins-match", "every pin is the served hash", d3,
         {"1": h1, "2": h2, "3": h3}, {"ok": True}),
        ("history-bad-hash-served", "the item's revision names a reason its hash doesn't cover",
         tampered(d3, set_(["item", "revision", "reason"], "Another reason")), {},
         {"ok": False, "problem": "badHash:3"}),
        ("history-bad-hash-rev1", "rev 1's body edited, its hash left as it was",
         tampered(d1, lambda d: _rev(d, 1).update(body=_rev(d, 1)["body"].replace("90 days", "30 days"))), {},
         {"ok": False, "problem": "badHash:1"}),
        ("history-bad-hash-rev2", "rev 2's reason edited, its hash left as it was",
         tampered(d3, lambda d: _rev(d, 2).update(reason="Another reason")), {},
         {"ok": False, "problem": "badHash:2"}),
        ("history-bad-hash-newest", "the item's revision is self-consistent but not the newest served rev",
         tampered(d3, other_served), {}, {"ok": False, "problem": "badHash:3"}),
        ("history-bad-chain-rev2", "rev 2's prev is not rev 1's hash; rev 2's hash recomputed",
         tampered(d3, chain_broken), {}, {"ok": False, "problem": "badChain:2"}),
        ("history-bad-chain-rev1", "rev 1 names a prev; the chain recomputed from rev 1",
         tampered(d1, chain_rev1), {}, {"ok": False, "problem": "badChain:1"}),
        ("history-unreadable-duplicate-key", "rev 2's body names title twice; rev 2's hash recomputed",
         tampered(d3, unreadable(2, dup2)), {}, {"ok": False, "problem": "unreadable:2"}),
        ("history-unreadable-lone-surrogate", "rev 1's body holds a lone \\ud800 escape; its hash recomputed",
         tampered(d1, unreadable(1, lone1)), {}, {"ok": False, "problem": "unreadable:1"}),
        ("history-unreadable-not-object", "rev 2's body is a JSON array; rev 2's hash recomputed",
         tampered(d3, unreadable(2, "[1,2]")), {}, {"ok": False, "problem": "unreadable:2"}),
        ("history-missing-gap", "rev 2 left out of revisions",
         tampered(d3, lambda d: d.update(revisions=[r for r in d["revisions"] if r["rev"] != 2])), {},
         {"ok": False, "problem": "missing"}),
        ("history-missing-none-served", "the item has a revision but no revisions are served",
         tampered(d1, lambda d: d.update(revisions=[])), {}, {"ok": False, "problem": "missing"}),
        ("history-missing-newer-named", "the item names a self-consistent rev 4 the history lacks",
         tampered(d3, fourth), {}, {"ok": False, "problem": "missing"}),
        ("history-fixed-title-changed", "rev 2 changes the title; the chain recomputed from rev 2",
         tampered(d3, title2), {}, {"ok": False, "problem": "fixedFieldChanged:2"}),
        ("history-fixed-urgency-changed", "rev 3 changes the urgency; rev 3 and the item recomputed",
         tampered(d3, urgency3), {}, {"ok": False, "problem": "fixedFieldChanged:3"}),
        ("history-fixed-from-changed", "rev 2 adds a `from` rev 1 lacks; the chain recomputed from rev 2",
         tampered(d3, from1), {}, {"ok": False, "problem": "fixedFieldChanged:2"}),
        ("history-rolled-back-other-hash", "a pin of rev 2 holds another hash", d3, {"2": h1},
         {"ok": False, "problem": "rolledBack"}),
        ("history-rolled-back-older-current", "a pin of rev 4 while the Pond serves rev 3", d3, {"4": "a" * 64},
         {"ok": False, "problem": "rolledBack"}),
    ]
    cases = []
    for name, note, detail, pins, expect in specs:
        found = client_verify(detail, pins)
        if expect["ok"]:
            expect = found
        if found != expect:
            raise SystemExit(f"gen_phone_vectors: {name} reads as {found}, not {expect}")
        cases.append({"name": name, "note": note, "detail": detail, "pins": pins, "expect": expect})
    oks = {c["name"]: c["expect"] for c in cases if c["expect"]["ok"]}
    if oks["history-three-revisions"] != {"ok": True, "current": {"rev": 3, "hash": h3}, "acceptedOnPond": 2}:
        raise SystemExit("gen_phone_vectors: the three-revision ask is not current 3, in force 2")
    return cases


# --- progress --------------------------------------------------------------------------


def pond_ticked(detail: dict) -> list[str]:
    """requirements.ticked over a served detail: this item's events (the store serves
    only those), the revision bodies, the accepted body."""
    item = detail["item"]
    events = [e for e in detail["events"] if e["itemId"] == item["id"]]
    revisions = detail.get("revisions") or []
    bodies = {r["rev"]: json.loads(r["body"]) for r in revisions}
    body = bodies[item["revision"]["acceptedRev"]] if revisions else item["payload"]["instructable"]
    return sorted(req.ticked(events, bodies, body))


def _legacy_item(pond: _Pond, title: str) -> str:
    """An ask stored as a Pond before 1.1.0 stored it: no step ids, no revisions."""
    from mailbox import service as service_module
    from mailbox.store import Item
    pond.clock.advance(1)
    item_id = str(service_module.uuid4())             # the pinned counter
    ins = {"v": 1, "title": title, "urgency": "today", "why": "The deploy key is 90 days old.",
           "steps": [{"do": "Open the deploy settings"},
                     {"do": "Restart the services", "substeps": [{"do": "Restart the web service"},
                                                                 {"do": "Restart the worker"}]}]}
    stamp = pond.clock.now.isoformat(timespec="seconds")
    payload = {"key": "legacy-" + item_id, "why": ins["why"], "cmd": None, "link": None,
               "tg_chat_id": pond.service.telegram.owner_id, "tg_message_id": None, "note": None,
               "instructable": ins}
    pond.store.insert_item(Item(id=item_id, source="xo-mailbox", title=title, project_id="Example",
                                lane="personal", payload=json.dumps(payload), created=stamp, updated=stamp))
    pond.store.append_event(item_id, "posted", "session", None)
    return item_id


def _progress_cases() -> list[dict]:
    cases: list[dict] = []
    with _pond() as pond:
        pond.approve(PHONE_KEY, "Android")

        def tick(item_id: str, rev: Optional[int], digest: Optional[str], tick: list, untick: list = ()) -> None:
            body: dict = {"tick": list(tick), "untick": list(untick)}
            if rev is not None:
                body.update(rev=rev, hash=digest)
            pond.ok("POST", f"/asks/{item_id}/progress", body, key=PHONE_KEY)

        def add(name: str, note: str, item_id: str, detail: Optional[dict] = None, *, served: bool = True) -> None:
            detail = detail if detail is not None else pond.detail(item_id)
            found = pond_ticked(detail)
            if served:
                _, body = pond.service.in_force(item_id)
                own = sorted(pond.service._ticked(item_id, body, pond.service._event_dicts(item_id)))
                if own != found:
                    raise SystemExit(f"gen_phone_vectors: {name}: the served detail folds to {found}, the Pond {own}")
            cases.append({"name": name, "note": note, "detail": detail, "ticked": found})

        a, ha = pond.post(_ask("Rotate the staging deploy key"))
        tick(a, 1, ha, ["s1"])
        add("progress-tick", "one tick of step 1", a)

        b, hb = pond.post(_ask("Rotate the preview deploy key"))
        tick(b, 1, hb, ["s1", "s2"])
        tick(b, 1, hb, [], ["s1"])
        add("progress-untick", "steps 1 and 2 ticked, then step 1 unticked", b)

        c, hc = pond.post(_ask("Rotate the review deploy key"))
        tick(c, 1, hc, ["s3.1"])
        add("progress-substep", "a sub-step ticked by its id", c)

        def corrected(title: str, reason: str, change, ticks: list, accept: bool) -> str:
            item_id, h1 = pond.post(_ask(title), editable=True)
            tick(item_id, 1, h1, ticks)
            new = _with_ids(_ask(title))
            change(new)
            h2 = pond.amend(item_id, 1, h1, reason, new)
            if accept:
                pond.ok("POST", f"/asks/{item_id}/accept", {"rev": 2, "hash": h2}, key=PHONE_KEY)
            return item_id

        d = corrected("Rotate the demo deploy key", "The rotate command was wrong",
                      lambda n: n["steps"][1].update(code="deploy-key rotate --env demo"), ["s2"], True)
        add("progress-dropped-by-correction", "step 2 ticked at rev 1; accepted rev 2 changed it: dropped", d)

        e = corrected("Rotate the test deploy key", "A first step was missing",
                      lambda n: n["steps"].insert(0, {"do": "Tell the team first"}), ["s2"], True)
        add("progress-kept-when-moved", "step s2 ticked at rev 1; accepted rev 2 only moved it: kept", e)

        f = corrected("Rotate the batch deploy key", "Step 1 must be done before Done",
                      lambda n: n["steps"][0].update(required=True), ["s1"], True)
        add("progress-kept-when-required-toggled", "rev 2 only marks the step required: the tick is kept", f)

        g = corrected("Rotate the mirror deploy key", "The rotate command was wrong",
                      lambda n: n["steps"][1].update(code="deploy-key rotate --env mirror"), ["s2"], False)
        add("progress-correction-not-accepted", "rev 2 changed step 2 but is not accepted: rev 1 is in force", g)

        m, hm = pond.post(_ask("Rotate the cache deploy key"))
        tick(m, 1, hm, ["s1"])
        pond.store.append_event(m, "progress", "owner", '{"tick":"s2"}', rev=1, auth="owner-key")
        pond.store.append_event(m, "progress", "owner", "not json", rev=1, auth="owner-key")
        add("progress-malformed-event", "two unreadable progress events are skipped", m)

        s, _ = pond.post(_ask("Rotate the search deploy key"))
        pond.store.append_event(s, "progress", "session", '{"tick":["s1"],"untick":[]}', rev=1)
        add("progress-session-event-ignored", "a progress event whose actor is not the owner counts for nothing", s)

        o, _ = pond.post(_ask("Rotate the queue deploy key"))
        other = copy.deepcopy(pond.detail(o))
        foreign = next(ev for ev in pond.detail(a)["events"] if ev["kind"] == "progress")
        other["events"].append({**foreign, "seq": other["events"][-1]["seq"] + 100})
        add("progress-other-item-event",
            "edited: a tick of another item's step s1 added to the events; the Pond never serves it, and "
            "it counts for nothing", o, other, served=False)

        v, hv = pond.post(_ask("Rotate the backup deploy key"))
        tick(v, 1, hv, ["@verify"])
        add("progress-verify", "Verify ticked as @verify", v)
        w = corrected("Rotate the archive deploy key", "The check URL was wrong",
                      lambda n: n["verify"].update(code="curl -sI https://deploy.example.test/ready"),
                      ["@verify"], True)
        add("progress-verify-changed", "@verify ticked at rev 1; accepted rev 2 changed Verify: dropped", w)
        legacy = _legacy_item(pond, "Rotate the legacy deploy key")
        tick(legacy, None, None, ["@legacy:2.1"])
        add("progress-legacy-substep",
            "an ask stored without step ids or revisions (before Pond 1.1.0): sub-step 2.1 as @legacy:2.1",
            legacy)
    return cases


# --- strict JSON -----------------------------------------------------------------------


def pond_strict(raw: bytes) -> dict:
    """The Pond's own strict reading of `raw`. parses: json.loads with no_duplicates and
    the UTF-8 round trip MailboxHTTPHandler._read_json makes (it refuses a lone
    surrogate). body: _read_json itself accepts it (also: an object) and
    instructable.validate(new_ids=True), the amend check, finds nothing."""
    from mailbox.server import MailboxHTTPHandler
    try:
        value = json.loads(raw, object_pairs_hook=no_duplicates)
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        parses = True
    except ValueError:
        parses = False
    body = False
    if raw:
        try:
            stub = SimpleNamespace(headers={"Content-Length": str(len(raw))}, rfile=io.BytesIO(raw))
            obj = MailboxHTTPHandler._read_json(stub)
            body = not validate(obj, new_ids=True)[1]
        except ValueError:
            body = False
    problem = None
    if not body:
        problem = "invalid"
        try:
            json.dumps(json.loads(raw), ensure_ascii=False).encode("utf-8")
            json.loads(raw, object_pairs_hook=no_duplicates)
        except ValueError as exc:
            if str(exc) == "duplicate key":
                problem = "duplicateKey"
    return {"parses": parses, "body": body, "problem": problem}


_INS = '{"v":1,"title":"Rotate the staging deploy key","why":%s,"steps":[{"do":"Open the deploy settings","id":"s1"}]}'


def _ins(why: str) -> str:
    return _INS % why


# (name, note, text or bytes, what docs/mailbox-protocol.md says: parses, body)
STRICT: list[tuple[str, str, Any, bool, bool]] = [
    ("strict-valid-body", "a plain instructable", _ins('"The key is 90 days old."'), True, True),
    ("strict-valid-pair", "a surrogate pair escape is one character", _ins('"Done \\ud83d\\ude00"'), True, True),
    ("strict-lone-high-surrogate", "a high surrogate escape alone", _ins('"Done \\ud83d"'), False, False),
    ("strict-lone-low-surrogate", "a low surrogate escape alone", _ins('"Done \\ude00"'), False, False),
    ("strict-reversed-pair", "low then high: two lone surrogates", _ins('"Done \\ude00\\ud83d"'), False, False),
    ("strict-lone-surrogate-key", "a lone surrogate in a key", '{"\\ud800":1}', False, False),
    ("strict-duplicate-top-level", "title named twice at the top",
     '{"v":1,"title":"Rotate the staging deploy key","title":"Rotate the production deploy key",'
     '"steps":[{"do":"Open the deploy settings","id":"s1"}]}', False, False),
    ("strict-duplicate-nested", "a key named twice in a nested object",
     '{"v":1,"title":"Rotate the staging deploy key","steps":[{"do":"Open the deploy settings","id":"s1"}],'
     '"verify":{"do":"The deploy passes","do":"The deploy fails"}}', False, False),
    ("strict-duplicate-in-array", "a key named twice in an object inside an array",
     '{"v":1,"title":"Rotate the staging deploy key","steps":[{"do":"Open the deploy settings","id":"s1",'
     '"code":"deploy-key show","code":"deploy-key delete"}]}', False, False),
    ("strict-duplicate-escaped", "\"a\" and \"\\u0061\" are the same key", '{"a":1,"\\u0061":2}', False, False),
    ("strict-duplicate-escaped-title", "title spelled once with an escape",
     '{"v":1,"title":"Rotate the staging deploy key","\\u0074itle":"Rotate the production deploy key",'
     '"steps":[{"do":"Open the deploy settings","id":"s1"}]}', False, False),
    ("strict-same-key-different-objects", "one key in two objects is no duplicate",
     '{"v":1,"title":"Rotate the staging deploy key","steps":[{"do":"Open the deploy settings","id":"s1"},'
     '{"do":"Rotate the key","id":"s2"}]}', True, True),
    ("strict-trailing-text", "text after the value", _ins('"The key is 90 days old."') + " x", False, False),
    ("strict-trailing-space", "white space after the value is fine", _ins('"The key is 90 days old."') + " \n",
     True, True),
    ("strict-broken", "the object never closes", '{"v":1,"title":"Rotate the staging deploy key"', False, False),
    ("strict-not-utf8", "raw bytes that are not UTF-8: {\"a\":\"<FF>\"}", b'{"a":"\xff"}', False, False),
    ("strict-top-level-array", "JSON, but not an object", '[{"v":1}]', True, False),
    ("strict-top-level-string", "JSON, but not an object", '"Rotate the staging deploy key"', True, False),
    ("strict-not-instructable", "an object, but no instructable", '{"a":1}', True, False),
]


def _strict_cases() -> list[dict]:
    cases = []
    for name, note, text, parses, body in STRICT:
        raw = text if isinstance(text, bytes) else text.encode("utf-8")
        case: dict[str, Any] = {"name": name, "note": note}
        if isinstance(text, bytes):
            case["hex"] = raw.hex()
        else:
            case["text"] = text
        pond = pond_strict(raw)
        case.update(parses=parses, body=body)
        if not body:
            case["problem"] = pond["problem"]
        if (pond["parses"], pond["body"]) != (parses, body):
            case["pond"] = {"parses": pond["parses"], "body": pond["body"]}
        cases.append(case)
    return cases


# --- phone transcripts -----------------------------------------------------------------

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)")
_REVISION_KEYS = {"rev", "hash", "prev", "at", "reason", "body"}


def _normal(value: Any, names: dict[str, str], *, keep_time: bool = False) -> Any:
    """Bound ids as {name}, timestamps as {time}, except a revision's `at`."""
    if isinstance(value, str):
        if value in names:
            return "{" + names[value] + "}"
        if _ISO.fullmatch(value) and not keep_time:
            return "{time}"
        return value
    if isinstance(value, list):
        return [_normal(v, names) for v in value]
    if isinstance(value, dict):
        revision = _REVISION_KEYS <= set(value)
        return {k: _normal(v, names, keep_time=revision and k == "at") for k, v in value.items()}
    return value


def _fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        for name, item_id in ids.items():
            value = value.replace("{" + name + "}", item_id)
        return value
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def _at(body: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        body = body[int(part)] if isinstance(body, list) else body[part]
    return body


def outcome(status: int, body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    if 200 <= status < 300:
        return "ok"
    named = {(401, None): "unauthorized", (404, None): "notFound",
             (403, "owner key required"): "ownerKeyRequired", (403, "unknown owner key"): "ownerKeyRefused",
             (409, "ask was corrected"): "corrected", (409, "requirements_unmet"): "requirementsUnmet",
             (409, "ask is closed"): "askClosed", (409, "too many messages"): "tooManyMessages"}
    if status in (401, 404):
        return named[(status, None)]
    if (status, error) in named:
        return named[(status, error)]
    if 400 <= status < 500:
        return "invalid"
    raise SystemExit(f"gen_phone_vectors: no outcome for {status} {error}")


def _request(act: dict, owner_key: Any, token: str) -> dict:
    """The request the phone must send for `act` (transcript-act-args)."""
    kind, a = act["kind"], act["args"]
    stamp = {k: a[k] for k in ("rev", "hash") if a.get(k) is not None}
    method, path, body = {
        "version": lambda: ("GET", "/version", None),
        "enroll": lambda: ("POST", "/owner/enroll", {"keyHash": a["keyHash"], "label": a["label"]}),
        "enrollment": lambda: ("GET", f"/owner/enroll/{a['id']}", None),
        "ownerStatus": lambda: ("GET", "/owner", None),
        "list": lambda: ("GET", "/asks?status=open&days=36500", None),
        "detail": lambda: ("GET", f"/asks/{a['id']}", None),
        "progress": lambda: ("POST", f"/asks/{a['id']}/progress", {"tick": a["tick"], "untick": a["untick"], **stamp}),
        "answers": lambda: ("POST", f"/asks/{a['id']}/answers", {"answers": a["answers"], **stamp}),
        "reply": lambda: ("POST", f"/asks/{a['id']}/reply", {"text": a["text"]}),
        "message": lambda: ("POST", f"/asks/{a['id']}/reply", {"text": a["text"], "kind": "message"}),
        "done": lambda: ("POST", f"/asks/{a['id']}/resolve", {"verdict": "done", "actor": "owner", **stamp}),
        "skip": lambda: ("POST", f"/asks/{a['id']}/resolve", {"verdict": "declined", "actor": "owner"}),
    }[kind]()
    request: dict[str, Any] = {"method": method, "path": path, "ownerKey": owner_key}
    if token != "ok":
        request["token"] = token
    request.update(protocol=6, body=body)
    return request


class _Transcript:
    """Runs phone acts in order against one Pond and records them."""

    def __init__(self, pond: _Pond, key: str) -> None:
        self.pond, self.key = pond, key
        self.ids: dict[str, str] = {}
        self.cases: list[dict] = []

    def act(self, name: str, note: str, kind: str, args: dict, expect: str, *, owner_key: Any = True,
            token: str = "ok", binds: Optional[dict] = None, known: Optional[dict] = None) -> Any:
        if set(args) != set(ACT_ARGS[kind]):
            raise SystemExit(f"gen_phone_vectors: {name}: args {sorted(args)} for {kind}")
        act = {"kind": kind, "args": args}
        request = _request(act, owner_key, token)
        sent = _fill(request, self.ids)
        key = {True: self.key, False: None, "unknown": UNKNOWN_KEY}[owner_key]
        status, body = self.pond.send(sent["method"], sent["path"], sent["body"], key=key,
                                      token="wrong-token" if token == "wrong" else None)
        for bound, dotted in (binds or {}).items():
            self.ids[bound] = _at(body, dotted)
            if known and known.get(bound) != self.ids[bound]:
                raise SystemExit(f"gen_phone_vectors: {name}: {dotted} is not {bound}")
        found = outcome(status, body)
        if found != expect:
            raise SystemExit(f"gen_phone_vectors: {name} answered {status} {body}: {found}, not {expect}")
        names = {item_id: bound for bound, item_id in self.ids.items()}
        case: dict[str, Any] = {"name": name, "note": note, "act": act, "request": request,
                                "response": {"status": status, "body": _normal(body, names)}, "outcome": found}
        if binds:
            case["binds"] = binds
        self.cases.append(case)
        return body


def _setup_cases() -> list[dict]:
    with _pond() as pond:
        t = _Transcript(pond, PHONE_KEY)
        phone = owner.key_hash(PHONE_KEY)
        t.act("setup-version", "the Pond's release, before anything else", "version", {}, "ok", owner_key=False)
        t.act("setup-owner-status-before", "no key yet: self is null", "ownerStatus", {}, "ok", owner_key=False)
        t.act("setup-enroll", "the phone sends its key's hash: pending, with the code to compare",
              "enroll", {"keyHash": phone, "label": "Android"}, "ok", owner_key=False,
              binds={"enroll1": "id"})
        t.act("setup-enrollment-pending", "polled before the owner answers", "enrollment",
              {"id": "{enroll1}"}, "ok", owner_key=False)
        pond.tap(f"e:{t.ids['enroll1']}:y")
        t.act("setup-enrollment-approved", "polled after the owner tapped Approve on Telegram", "enrollment",
              {"id": "{enroll1}"}, "ok", owner_key=False)
        t.act("setup-owner-status", "with the approved key: self names it", "ownerStatus", {}, "ok")
        t.act("setup-enroll-bad-label", "a label on two lines", "enroll",
              {"keyHash": owner.key_hash(SPARE_KEY), "label": "Android\nPhone"}, "invalid", owner_key=False)
        t.act("setup-version-wrong-token", "a wrong Pond token", "version", {}, "unauthorized",
              owner_key=False, token="wrong")
        return t.cases


ACTS_ASK = {
    "v": 1, "title": "Rotate the staging deploy key", "urgency": "today", "why": "The staging key is 90 days old.",
    "steps": [
        {"do": "Open the deploy settings", "where": "deploy.example.test"},
        {"do": "Pick the target", "question": {"prompt": "Which environment?", "choices": ["Staging", "Production"]}},
        {"do": "Set the expiry", "question": {"prompt": "Keep the expiry?",
                                              "choices": ["Keep", {"label": "Change it", "text": "What should change?"}]}},
        {"do": "Note the rotation time", "question": {"prompt": "When did you rotate it?", "choices": []}},
    ],
    "verify": dict(VERIFY),
    "reply": {"prompt": "Did the deploy pass?", "choices": ["Yes", "No"]},
}


def _acts_cases() -> list[dict]:
    with _pond() as pond:
        pond.approve(PHONE_KEY, "Android")
        ask1, h1 = pond.post(ACTS_ASK)
        ask2, _ = pond.post({"v": 1, "title": "Rotate the preview deploy key", "urgency": "whenever",
                             "steps": [{"do": "Rotate the key", "code": "deploy-key rotate --env preview"}]})
        t = _Transcript(pond, PHONE_KEY)
        t.act("acts-list", "the open asks, newest first", "list", {}, "ok", owner_key=False,
              binds={"ask2": "items.0.id", "ask1": "items.1.id"}, known={"ask1": ask1, "ask2": ask2})
        t.act("acts-detail", "the ask with its events and revisions", "detail", {"id": "{ask1}"}, "ok",
              owner_key=False)
        t.act("acts-tick-s1", "tick step 1", "progress",
              {"id": "{ask1}", "tick": ["s1"], "untick": [], "rev": 1, "hash": h1}, "ok")
        t.act("acts-tick-verify", "tick Verify", "progress",
              {"id": "{ask1}", "tick": ["@verify"], "untick": [], "rev": 1, "hash": h1}, "ok")
        t.act("acts-untick-s1", "untick step 1", "progress",
              {"id": "{ask1}", "tick": [], "untick": ["s1"], "rev": 1, "hash": h1}, "ok")
        t.act("acts-pick", "a pick", "answers",
              {"id": "{ask1}", "answers": [{"step": "2", "picked": ["Staging"]}], "rev": 1, "hash": h1}, "ok")
        t.act("acts-pick-with-words", "a pick of a choice that takes words, with the words", "answers",
              {"id": "{ask1}", "answers": [{"step": "3", "picked": ["Change it"], "text": "Use a 30 day expiry"}],
               "rev": 1, "hash": h1}, "ok")
        t.act("acts-free-text", "a free-text answer", "answers",
              {"id": "{ask1}", "answers": [{"step": "4", "text": "Rotated at nine"}], "rev": 1, "hash": h1}, "ok")
        t.act("acts-reply-text", "a text reply", "reply", {"id": "{ask1}", "text": "Rotated, the old key is revoked."},
              "ok")
        t.act("acts-reply-choice", "a choice of the reply prompt", "reply", {"id": "{ask1}", "text": "choice: Yes"},
              "ok")
        t.act("acts-message", "a message to the session, on two lines", "message",
              {"id": "{ask1}", "text": "Use the staging key first.\nThen production."}, "ok")
        t.act("acts-done", "Done, bound to the revision shown", "done", {"id": "{ask1}", "rev": 1, "hash": h1}, "ok")
        t.act("acts-skip", "Skip carries no rev or hash", "skip", {"id": "{ask2}"}, "ok")
        t.act("acts-detail-after", "the closed ask with every act above as an event", "detail", {"id": "{ask1}"},
              "ok", owner_key=False)
        return t.cases


def _errors_cases() -> list[dict]:
    with _pond() as pond:
        pond.approve(PHONE_KEY, "Android")
        pond.approve(MAC_KEY, "Example Mac")
        if pond.service.request_enforcement(True, by=pond.service.owner_for(PHONE_KEY)) != "on":
            raise SystemExit("gen_phone_vectors: Require Owner Key did not turn on")
        plain = {"v": 1, "title": "Rotate the staging deploy key", "urgency": "today",
                 "steps": [{"do": "Open the deploy settings"}, {"do": "Rotate the key", "code": "deploy-key rotate"}]}
        e1, h1 = pond.post(plain)
        e2, g1 = pond.post({**plain, "title": "Rotate the preview deploy key"}, editable=True)
        fixed = _with_ids({**plain, "title": "Rotate the preview deploy key"})
        fixed["steps"][1]["code"] = "deploy-key rotate --env preview"
        pond.amend(e2, 1, g1, "The rotate command named no environment", fixed)
        e3, h3 = pond.post({**plain, "title": "Rotate the production deploy key",
                            "steps": [{"do": "Open the deploy settings"},
                                      {"do": "Rotate the key", "code": "deploy-key rotate", "required": True}]})
        e4, h4 = pond.post({**plain, "title": "Rotate the review deploy key"})
        e5, _ = pond.post({**plain, "title": "Rotate the demo deploy key"})
        for n in range(50):
            pond.service.reply(e5, f"Message {n + 1}", kind="message", auth="owner-key")
        t = _Transcript(pond, PHONE_KEY)
        t.act("errors-list", "the open asks, newest first", "list", {}, "ok", owner_key=False,
              binds={"ask5": "items.0.id", "ask4": "items.1.id", "ask3": "items.2.id", "ask2": "items.3.id",
                     "ask1": "items.4.id"},
              known={"ask1": e1, "ask2": e2, "ask3": e3, "ask4": e4, "ask5": e5})
        pond.ok("POST", f"/asks/{e4}/resolve", {"verdict": "done"})       # the session closes ask4
        tick = {"id": "{ask1}", "tick": ["s1"], "untick": [], "rev": 1, "hash": h1}
        t.act("errors-owner-key-required", "Require Owner Key is on and no key was sent", "progress", tick,
              "ownerKeyRequired", owner_key=False)
        t.act("errors-unknown-owner-key", "a key the Pond never approved", "progress", tick, "ownerKeyRefused",
              owner_key="unknown")
        t.act("errors-corrected", "bound to rev 1 while rev 2 waits for review", "progress",
              {"id": "{ask2}", "tick": ["s1"], "untick": [], "rev": 1, "hash": g1}, "corrected")
        t.act("errors-requirements-unmet", "Done while a required step is left", "done",
              {"id": "{ask3}", "rev": 1, "hash": h3}, "requirementsUnmet")
        t.act("errors-ask-closed", "a tick on ask4, which its session closed after the list", "progress",
              {"id": "{ask4}", "tick": ["s1"], "untick": [], "rev": 1, "hash": h4}, "askClosed")
        t.act("errors-invalid-answers", "an answer to a step without a question", "answers",
              {"id": "{ask1}", "answers": [{"step": "1", "picked": ["Yes"]}], "rev": 1, "hash": h1}, "invalid")
        t.act("errors-invalid-progress", "a tick of a step the ask lacks", "progress",
              {"id": "{ask1}", "tick": ["s9"], "untick": [], "rev": 1, "hash": h1}, "invalid")
        t.act("errors-invalid-message", "a secret-like message: field paths only, never echoed", "message",
              {"id": "{ask1}", "text": "the token is ghp_example"}, "invalid")
        t.act("errors-too-many-messages", "the 51st message to one ask", "message",
              {"id": "{ask5}", "text": "One more thing."}, "tooManyMessages")
        t.act("errors-not-found", "an ask the Pond doesn't hold", "detail", {"id": MISSING_ID}, "notFound",
              owner_key=False)
        t.act("errors-unauthorized", "a wrong Pond token", "list", {}, "unauthorized", owner_key=False,
              token="wrong")
        return t.cases


# --- files -----------------------------------------------------------------------------

_BUILDERS = {"history": _history_cases, "progress": _progress_cases, "strictjson": _strict_cases,
             "phone-setup": _setup_cases, "phone-acts": _acts_cases, "phone-errors": _errors_cases}


def build(kind: str) -> dict:
    cases = _BUILDERS[kind]()
    names = [c["name"] for c in cases]
    if len(cases) < MIN_CASES[kind] or len(set(names)) != len(names):
        raise SystemExit(f"gen_phone_vectors: {kind}: {len(cases)} cases, or a duplicate case name")
    return {"version": 1, "pond": VERSION, "source": SOURCE, "format": FORMATS.get(kind, _WIRE_FORMAT),
            "cases": cases}


def dump(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", type=Path, help="Hook's conformance folder: also write data/… and wire/… there")
    args = parser.parse_args()
    if args.hook and not args.hook.is_dir():
        raise SystemExit(f"gen_phone_vectors: {args.hook} is not a folder")
    docs = {kind: build(kind) for kind in KINDS}
    seen = {c["outcome"] for kind in ("phone-setup", "phone-acts", "phone-errors") for c in docs[kind]["cases"]}
    if seen != set(OUTCOMES):
        raise SystemExit(f"gen_phone_vectors: outcomes missing: {sorted(set(OUTCOMES) - seen)}")
    for kind, doc in docs.items():
        text = dump(doc)
        if any(token in text for token in TOKENS) or any(secret in text for secret in SECRETS):
            raise SystemExit(f"gen_phone_vectors: {kind} holds a token or an owner key")
        out_path(kind).write_text(text, encoding="utf-8")
        print(f"wrote {out_path(kind)}")
        if args.hook:
            target = args.hook / FILES[kind]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target}")


if __name__ == "__main__":
    main()
