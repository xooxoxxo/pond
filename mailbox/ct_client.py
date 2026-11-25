"""Central Time client for the mailbox: create a captured item, acknowledge a
reminder. urllib only. The token is the capture addon's (scopes
items:write:own and reminders:ack); neither it nor any body is ever logged,
and an error carries a status and a kind, never text from the response (a
title could be in it)."""
import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Optional

TIMEOUT_S = 5.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The bearer must never follow a redirect: a 3xx surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


# The ack route's 409 vocabulary. Only one of these words is ever kept from a
# response body; anything else (a title could be in it) is dropped.
ACK_REASONS = frozenset(("closed", "recurring", "unanchored", "untimed", "stale"))
# The household routes' error vocabulary. Same rule: only these words are ever kept.
HH_REASONS = frozenset(("nonexistent_time", "invalid_time", "stale", "closed", "not_yours", "not_member",
                        "photo_too_large", "storage_full", "bad_image", "too_large", "gone",
                        "command_mismatch", "command_id_reused", "wrong_action"))


def _reason(exc: urllib.error.HTTPError, allowed: frozenset = ACK_REASONS) -> Optional[str]:
    """The whitelisted `error` word of an error body, or None."""
    try:
        out = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return None
    word = out.get("error") if isinstance(out, dict) else None
    return word if word in allowed else None


class CentralTimeError(Exception):
    """A call that did not succeed: `status` 0 for a network failure, `reason`
    one of the call's whitelist (ACK_REASONS, or HH_REASONS for the household
    calls) when the error body named one, else None."""

    def __init__(self, status: int, what: str, reason: Optional[str] = None):
        super().__init__(f"central time {what} {status}")
        self.status = status
        self.what = what
        self.reason = reason


class CentralTimeClient:
    """JSON POSTs with the bearer: the asks instance's create and ack, the household instance's hh_* calls."""

    def __init__(self, base_url: str, token: str, timeout: float = TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    HH_TIMEOUT_S = 20.0  # a capture can carry a 5 MiB photo

    def _call(self, method: str, path: str, body: dict[str, Any], what: str,
              reasons: frozenset = ACK_REASONS, timeout: Optional[float] = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with _open(req, timeout or self.timeout) as resp:
                out = json.loads(resp.read() or b"{}")
                return out if isinstance(out, dict) else {}
        except urllib.error.HTTPError as exc:
            raise CentralTimeError(exc.code, what, _reason(exc, reasons)) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            raise CentralTimeError(0, what) from None

    def create_item(self, title: str, date: Optional[str] = None, time: Optional[str] = None) -> dict[str, Any]:
        """POST /api/items with the Central Time envelope's anchor."""
        anchor: Optional[dict[str, Any]] = None
        if date:
            anchor = {"date": date, "allDay": time is None}
            if time:
                anchor["time"] = time
        return self._call("POST", "/api/items", {"title": title, "anchor": anchor}, "create")

    def ack_fire(self, fire_id: str, action: str) -> dict[str, Any]:
        """POST /api/reminders/{id}/ack with done or tomorrow."""
        return self._call("POST", f"/api/reminders/{fire_id}/ack", {"action": action}, "ack")

    def hh_command(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/hh/commands as the household relay; body carries actorTgId."""
        return self._call("POST", "/api/hh/commands", body, "hh-command", reasons=HH_REASONS, timeout=self.HH_TIMEOUT_S)

    def hh_ack(self, delivery_id: str, action: str, actor_tg_id: int) -> dict[str, Any]:
        """POST /api/hh/deliveries/{id}/ack with done, accept or decline and the tapping member."""
        return self._call("POST", f"/api/hh/deliveries/{delivery_id}/ack", {"action": action, "actorTgId": actor_tg_id}, "hh-ack", reasons=HH_REASONS)


class FakeCentralTimeClient(CentralTimeClient):
    """Test double: records every call; raises `fail` when set."""

    def __init__(self):
        super().__init__("http://ct.test", "ct_" + "0" * 48)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail: Optional[CentralTimeError] = None
        # A fixed answer for the next calls (e.g. an ack with applied False); None = the default.
        self.answer: Optional[dict[str, Any]] = None

    def _call(self, method: str, path: str, body: dict[str, Any], what: str,
              reasons: frozenset = ACK_REASONS, timeout: Optional[float] = None) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if self.fail is not None:
            raise self.fail
        if self.answer is not None:
            return self.answer
        if what == "hh-command":
            return {"recordId": "rec-1", "version": 1, "suggestion": None}
        if what == "hh-ack":
            return {"applied": True, "outcome": body["action"]}
        if what == "create":
            return {"id": "item-1", "title": body["title"], "anchor": body["anchor"]}
        return {"fire": {"id": path.split("/")[3], "ack": body["action"]}, "item": {"id": "item-1"}, "applied": True}
