"""Write doc-wire-vectors.json: the wire transcript of an ask's document read and
released (Pond 1.10.0), recorded from this Pond over loopback.

    python3 -m mailbox.tests.gen_doc_wire                            # check: exit 1 on a difference
    HOOK_CONFORMANCE_RECORD=1 python3 -m mailbox.tests.gen_doc_wire  # record the file again

The cases run in order against one fresh Pond with the `docs` switch on: the Telegram
fake, the pinned clocks (the store's and the service's) and the counting ids of
gen_phone_vectors, so two runs give the same bytes whatever today's date is. A case is

    {name, note, request: {method, path, token, protocol, body}, response: {status, headers, body}, binds?}

`token` false sends no Authorization header; true sends the Pond token, which no file
holds. `protocol` is the X-Hook-Protocol sent. `headers` are the response's
Content-Type, and Cache-Control and X-Content-Type-Options when it has them. `body`
is the parsed JSON for an application/json response and the exact text for a
text/plain one (a document read); a text/plain response also carries `bodyBase64`,
the exact bytes it sent in standard base64, which no JSON escaping can alter. `binds`
names the item id the response carries ({"ask1": "item.id"}), and later paths use it
as {ask1}. In JSON bodies every item id is its {name} and every ISO-8601 timestamp is
{time}, except a revision's `at`, kept as recorded because its hash covers it.
`baseRev` and `baseHash` in a request body are the real values the Pond accepts; the
edit key is a synthetic one. A `doc`'s `bytes` is the UTF-8 byte count of its text.
The switch is on until the case doc-version-off, and on again for the last case,
doc-post-store-full (Pond 1.12.0), recorded with docs.MAX_STORE_BYTES patched, for
that case only, to the bytes already stored. Every text and path is synthetic.
test_docs_read holds the file to what this generator records today.
"""
import base64
import copy
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from mailbox import VERSION, owner
from mailbox import docs as documents
from mailbox.docs import digest
from mailbox.tests import gen_phone_vectors as phone

HERE = Path(__file__).parent
OUT = HERE / "doc-wire-vectors.json"
SOURCE = "mailbox/tests/gen_doc_wire.py, recorded from the Pond over loopback"
RECORD_ENV = "HOOK_CONFORMANCE_RECORD"
PROTOCOL = 6
RECORDED_HEADERS = ("Content-Type", "Cache-Control", "X-Content-Type-Options")

PLAN_PATH = "~/notes/plan.md"
PLAN_TEXT = "# Release plan\n\n1. Tag the release.\n2. Publish the notes on https://pond.example.test/notes.\n"
OTHER_TEXT = "# Some other document\n"
PLAN_SHA, PLAN_BYTES = digest(PLAN_TEXT)
OTHER_SHA = digest(OTHER_TEXT)[0]

ASK = {
    "v": 1, "title": "Review the release plan",
    "steps": [
        {"do": "Read the plan", "file": PLAN_PATH, "doc": {"sha256": PLAN_SHA, "bytes": PLAN_BYTES}},
        {"do": "Say whether it can ship", "question": {"prompt": "Ship it?", "choices": ["Yes", "No"]}},
    ],
}

# The final wave (W9): a text that is not ASCII, with a leading BOM and CRLF line ends.
BOM_PATH = "~/notes/handover.md"
BOM_TEXT = "\ufeff# Übergabe – notes\r\n\r\nCafé, naïve, 日本語.\r\nNo line end at the end"
BOM_SHA, BOM_BYTES = digest(BOM_TEXT)
HANDOVER = {
    "v": 1, "title": "Hand over the notes",
    "steps": [
        {"do": "Read the notes", "file": BOM_PATH, "doc": {"sha256": BOM_SHA, "bytes": BOM_BYTES}},
        {"do": "Say when the handover is done"},
    ],
}
NOTES_PATH = "~/notes/notes.md"
NOTES_TEXT = "# Notes\n\nNothing new.\n"
NOTES_SHA, NOTES_BYTES = digest(NOTES_TEXT)
CHECKLIST_PATH = "/var/tmp/release-checklist.md"
CHECKLIST_TEXT = "# Checklist\n\n- tests pass\n"
CHECKLIST_SHA, CHECKLIST_BYTES = digest(CHECKLIST_TEXT)
STRAY_TEXT = "# A stray document\n"
STRAY_SHA = digest(STRAY_TEXT)[0]
# Four errors: two per `doc` in document order, then two per sent text in hash order.
ERRORS_ASK = {
    "v": 1, "title": "Review the release checklist",
    "steps": [
        {"do": "Read the plan", "file": PLAN_PATH, "doc": {"sha256": PLAN_SHA, "bytes": PLAN_BYTES + 1}},
        {"do": "Read the notes", "file": NOTES_PATH, "doc": {"sha256": NOTES_SHA, "bytes": NOTES_BYTES}},
    ],
    "links": [{"label": "Checklist", "path": CHECKLIST_PATH,
               "doc": {"sha256": CHECKLIST_SHA, "bytes": CHECKLIST_BYTES}}],
}
ERRORS_DOCS = {PLAN_SHA: PLAN_TEXT, CHECKLIST_SHA: "# Not the checklist\n", STRAY_SHA: STRAY_TEXT}
# Pond 1.12.0: a text that does not fit a full store.
NEXT_TEXT = "# Next plan\n\n1. Tag the next release.\n"
NEXT_SHA, NEXT_BYTES = digest(NEXT_TEXT)


def _format() -> str:
    return __doc__.split("A case is", 1)[1].split("test_docs_read holds", 1)[0].strip()


def _send(base_url: str, token: Optional[str], method: str, path: str, body: Any) -> tuple[int, Any, Any, bytes]:
    """One request → (status, recorded headers, parsed body: JSON or the text, raw bytes)."""
    headers = {"X-Hook-Protocol": str(PROTOCOL)}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            status, got, raw = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        status, got, raw = exc.code, exc.headers, exc.read()
    recorded = {name: got[name] for name in RECORDED_HEADERS if got[name] is not None}
    if recorded["Content-Type"].startswith("text/plain"):
        if got["Content-Length"] != str(len(raw)):
            raise SystemExit(f"gen_doc_wire: {path} sent a wrong Content-Length")
        return status, recorded, raw.decode("utf-8"), raw
    return status, recorded, json.loads(raw), raw


class _Recorder:
    """Sends each case's request to the Pond and records it with its response."""

    def __init__(self, pond: Any) -> None:
        self.pond = pond
        self.ids: dict[str, str] = {}
        self.cases: list[dict] = []

    def case(self, name: str, note: str, method: str, path: str, body: Any = None, *, token: bool = True,
             expect: int = 200, binds: Optional[dict] = None) -> Any:
        request = {"method": method, "path": path, "token": token, "protocol": PROTOCOL, "body": body}
        sent = phone._fill(request, self.ids)
        status, headers, answer, raw = _send(self.pond.base_url, self.pond.token if token else None,
                                             sent["method"], sent["path"], sent["body"])
        if status != expect:
            raise SystemExit(f"gen_doc_wire: {name} answered {status} {answer}")
        for bound, dotted in (binds or {}).items():
            self.ids[bound] = phone._at(answer, dotted)
        names = {item_id: bound for bound, item_id in self.ids.items()}
        recorded: dict[str, Any] = {
            "name": name, "note": note, "request": request,
            "response": {"status": status, "headers": headers,
                         "body": answer if isinstance(answer, str) else phone._normal(answer, names)}}
        if isinstance(answer, str):
            recorded["response"]["bodyBase64"] = base64.b64encode(raw).decode("ascii")
        if binds:
            recorded["binds"] = binds
        self.cases.append(recorded)
        return answer


def _cases() -> list[dict]:
    with phone._pond() as pond:
        pond.store.set_setting("docs_enabled", "1")
        t = _Recorder(pond)
        pond.clock.advance(1)
        t.case("doc-post", "an ask whose step 1 file carries a doc; the text travels in `docs`, keyed by its hash",
               "POST", "/asks", {"project": "Example", "context": "personal", "instructable": ASK,
                                 "docs": {PLAN_SHA: PLAN_TEXT}},
               expect=201, binds={"ask1": "item.id"})
        doc_path = "/asks/{ask1}/docs/" + PLAN_SHA
        t.case("doc-read", "the ask keeps the document: 200, its exact bytes as text/plain", "GET", doc_path)
        t.case("doc-read-not-linked", "a hash this ask never linked: 404", "GET",
               "/asks/{ask1}/docs/" + OTHER_SHA, expect=404)
        t.case("doc-read-unknown-ask", "an ask the Pond does not have: 404", "GET",
               f"/asks/{phone.MISSING_ID}/docs/{PLAN_SHA}", expect=404)
        t.case("doc-read-no-token", "no Authorization header: 401", "GET", doc_path, token=False, expect=401)
        t.case("doc-release-open", "release while the ask is open: 409, nothing deleted", "POST",
               "/asks/{ask1}/docs/release", expect=409)
        pond.clock.advance(5)
        t.case("doc-close", "the session marks the ask done", "POST", "/asks/{ask1}/resolve", {"verdict": "done"})
        t.case("doc-release", "release on the closed ask: 200 with the documents this call released", "POST",
               "/asks/{ask1}/docs/release")
        t.case("doc-release-again", "a second release: 200 with 0", "POST", "/asks/{ask1}/docs/release")
        t.case("doc-read-released", "the ask released the document: 410", "GET", doc_path, expect=410)
        t.case("doc-release-unknown-ask", "release on an ask the Pond does not have: 404", "POST",
               f"/asks/{phone.MISSING_ID}/docs/release", expect=404)
        _final_wave(t, pond)
        return t.cases


def _final_wave(t: _Recorder, pond: Any) -> None:
    """The cases the clients need beside the first ones (W9)."""
    t.case("doc-post-errors", "every document error at once: one per `doc` in document order, then one per "
           "sent text in hash order (then the total, not shown: it needs over 768 KB of text); nothing stored",
           "POST", "/asks", {"project": "Example", "context": "personal", "instructable": ERRORS_ASK,
                             "docs": ERRORS_DOCS}, expect=400)
    pond.clock.advance(1)
    posted = t.case("doc-post-bom-crlf", "a text that is not ASCII, with a leading BOM and CRLF line ends: its "
                    "key and `bytes` are of its UTF-8 bytes, BOM included; an edit key makes the ask editable",
                    "POST", "/asks", {"project": "Example", "context": "personal", "instructable": HANDOVER,
                                      "docs": {BOM_SHA: BOM_TEXT}, "editKeyHash": owner.key_hash(phone.EDIT_KEY)},
                    expect=201, binds={"ask2": "item.id"})
    t.case("doc-read-bom-crlf", "the read gives back the exact bytes: BOM and CRLF kept, nothing added",
           "GET", "/asks/{ask2}/docs/" + BOM_SHA)
    rev1 = posted["revision"]
    body = json.loads(posted["item"]["revision"]["body"])
    kept = copy.deepcopy(body)
    kept["steps"][1]["do"] = "Say when the handover is done and filed"
    pond.clock.advance(phone.CORRECTION_GAP_S)
    amended = t.case("doc-amend-keeps-held", "a correction that keeps the document the ask holds: no `docs` "
                     "member, the text is not sent again", "POST", "/asks/{ask2}/amend",
                     {"editKey": phone.EDIT_KEY, "baseRev": rev1["rev"], "baseHash": rev1["hash"],
                      "reason": "Say where it is filed", "instructable": kept})
    unknown = copy.deepcopy(kept)
    unknown["steps"][0]["doc"] = {"sha256": OTHER_SHA, "bytes": len(OTHER_TEXT)}
    pond.clock.advance(phone.CORRECTION_GAP_S)
    t.case("doc-amend-unknown-hash", "a correction whose `doc` hash was never sent or held: 400, nothing stored",
           "POST", "/asks/{ask2}/amend",
           {"editKey": phone.EDIT_KEY, "baseRev": amended["revision"]["rev"], "baseHash": amended["revision"]["hash"],
            "reason": "Point at the other notes", "instructable": unknown}, expect=400)
    again = copy.deepcopy(HANDOVER)
    again["steps"][0]["doc"] = {"sha256": NOTES_SHA, "bytes": NOTES_BYTES}
    pond.clock.advance(1)
    t.case("doc-post-deduped", "the same title again while the ask is open: answered as a dedupe, and its "
           "documents are neither stored nor linked", "POST", "/asks",
           {"project": "Example", "context": "personal", "instructable": again, "docs": {NOTES_SHA: NOTES_TEXT}})
    t.case("doc-read-deduped-not-linked", "the dedupe linked nothing: 404", "GET",
           "/asks/{ask2}/docs/" + NOTES_SHA, expect=404)
    t.case("doc-version-on", "the switch is on: `docs` is in `features`", "GET", "/version")
    pond.store.delete_setting("docs_enabled")
    t.case("doc-version-off", "the owner turned the switch off: `docs` is not in `features`", "GET", "/version")
    t.case("doc-post-switch-off", "a post with a document while the switch is off: 409, nothing stored", "POST",
           "/asks", {"project": "Example", "context": "personal",
                     "instructable": {**ASK, "title": "Review the next plan"}, "docs": {PLAN_SHA: PLAN_TEXT}},
           expect=409)
    t.case("doc-post-empty-docs-switch-off", "an empty `docs` member counts too while the switch is off: 409",
           "POST", "/asks", {"project": "Example", "context": "personal",
                             "instructable": {"v": 1, "title": "A plain ask", "steps": [{"do": "Plain"}]},
                             "docs": {}}, expect=409)
    _store_full(t, pond)


def _store_full(t: _Recorder, pond: Any) -> None:
    """Pond 1.12.0: a post whose new text would take the stored total over the limit.
    The limit is patched small for this case only: the file cannot hold 256 MiB."""
    pond.store.set_setting("docs_enabled", "1")
    stored = pond.store.doc_totals()[1]
    pond.clock.advance(1)
    with mock.patch.object(documents, "MAX_STORE_BYTES", stored):
        t.case("doc-post-store-full", "a post whose new text would take the stored total over the limit "
               "(patched here to the bytes already stored): 409, nothing stored, nothing sent", "POST", "/asks",
               {"project": "Example", "context": "personal",
                "instructable": {"v": 1, "title": "Review the next plan",
                                 "steps": [{"do": "Read the next plan", "file": PLAN_PATH,
                                            "doc": {"sha256": NEXT_SHA, "bytes": NEXT_BYTES}}]},
                "docs": {NEXT_SHA: NEXT_TEXT}}, expect=409)


def build() -> dict:
    return {"version": 1, "pond": VERSION, "source": SOURCE, "format": _format(), "cases": _cases()}


def dump(doc: dict) -> str:
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if any(token in text for token in phone.TOKENS) or any(secret in text for secret in phone.SECRETS):
        raise SystemExit("gen_doc_wire: the transcript holds a token or an owner key")
    return text


def main() -> int:
    text = dump(build())
    if os.environ.get(RECORD_ENV) == "1":
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT}")
        return 0
    if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
        print(f"gen_doc_wire: {OUT.name} differs from what the Pond answers today; "
              f"record it with {RECORD_ENV}=1", file=sys.stderr)
        return 1
    print(f"{OUT.name}: matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
