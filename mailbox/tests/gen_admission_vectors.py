"""Record reproducible local HTTP exchanges using only synthetic test credentials."""
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID

from mailbox import VERSION, owner
from mailbox.tests.test_server import _running, call


def generate():
    now = 1700000000
    cases = []
    with TemporaryDirectory() as directory, contextmanager(_running)(Path(directory), native=True) as server:
        admission = server.srv.service.admission
        def request(name, method, path, bearer=None, body=None):
            status, response = call(server.base_url, path, method=method, token=bearer, body=body)
            cases.append({"name": name, "request": {"method": method, "path": path,
                          "bearer": bearer, "body": body}, "response": {"status": status, "body": response}})
            return response

        # Only these predictable values appear in the published fixture.
        with patch("mailbox.admission.secrets.token_urlsafe", side_effect=[c * 43 for c in "abc"]), \
                patch("mailbox.admission.uuid.uuid4", side_effect=[UUID(int=n) for n in (1, 2, 3)]), \
                patch("mailbox.server.time.time", return_value=now):
            link = admission.operator_invite("first", "https://pond.example", now)["link"]
            def claim(label, kind, link, secret):
                return request(label, "POST", "/admission/claim", body={"kind": kind, "label": label,
                    "invite": urlsplit(link).fragment, "keyHash": owner.key_hash(secret)})
            first = "fixture-desktop"
            claim("Desktop", "device", link, first)
            for decision in ("reject", "approve"):
                key = f"fixture-{decision}"
                invite = request("Invite agent", "POST", "/admission/invites", first, {"kind": "agent"})
                candidate = claim("Builder", "agent", invite["link"], key)
                pid = candidate["id"]
                request(decision, "POST", f"/admission/requests/{pid}/decision", first,
                        {"code": candidate["code"], "decision": decision})
                request("Check own state", "GET", "/admission/me", key)
                if decision == "approve":
                    request("Revoke", "POST", f"/admission/principals/{pid}/revoke", first, {})
                    request("Check revoked credential", "GET", "/admission/me", key)
    return {"pond": VERSION, "firstInvite": link, "cases": cases}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))
