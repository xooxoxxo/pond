"""Tests for the Pond admin command: `python3 -m mailbox.admin corrections|appends|docs on|off|status`."""
import hashlib
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from mailbox import admin
from mailbox.service import AmendError, Service
from mailbox.store import Store
from mailbox.telegram import FakeTelegramClient

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILBOX_DATA_DIR", str(tmp_path))
    return tmp_path


def _amend_error(data_dir):
    """The error an amend of a real ask gets: the gate's while corrections are off,
    a later check's (the wrong edit key) once they are on."""
    store = Store(data_dir / "mailbox.db")
    store.open()
    try:
        service = Service(store, FakeTelegramClient(), "personal")
        doc = {"v": 1, "title": "t", "urgency": "today", "steps": [{"do": "x"}]}
        item, _ = service.post("Orbit", "t", instructable=doc, edit_key_hash="0" * 64)
        try:
            service.amend(item.id, "k", 1, "h", "r", doc)
        except AmendError as exc:
            return exc.body["error"]
    finally:
        store.close()


def test_corrections_status_on_off(data_dir, capsys):
    assert admin.main(["corrections", "status"]) == 0
    assert capsys.readouterr().out == "corrections: off\n"
    assert _amend_error(data_dir) == "corrections not enabled"
    assert admin.main(["corrections", "on"]) == 0
    assert capsys.readouterr().out == "corrections: on\n"
    assert admin.main(["corrections", "status"]) == 0
    assert capsys.readouterr().out == "corrections: on\n"
    assert _amend_error(data_dir) != "corrections not enabled"
    assert admin.main(["corrections", "off"]) == 0
    assert capsys.readouterr().out == "corrections: off\n"
    assert _amend_error(data_dir) == "corrections not enabled"


def test_bad_arguments_are_a_usage_error(data_dir, capsys):
    for argv in ([], ["corrections"], ["corrections", "maybe"], ["other", "on"]):
        assert admin.main(argv) == 2
    assert "usage: python3 -m mailbox.admin corrections on|off|status" in capsys.readouterr().err


def test_it_runs_as_a_module(data_dir):
    result = subprocess.run([sys.executable, "-m", "mailbox.admin", "corrections", "status"],
                            cwd=REPO_ROOT, capture_output=True, text=True,
                            env={"MAILBOX_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"})
    assert (result.returncode, result.stdout) == (0, "corrections: off\n")


def _append_error(data_dir):
    """The error an append of a real ask gets with a wrong edit key: the switch's when off."""
    store = Store(data_dir / "mailbox.db")
    store.open()
    try:
        service = Service(store, FakeTelegramClient(), "personal")
        doc = {"v": 1, "title": "t", "urgency": "today", "steps": [{"do": "x"}]}
        item, _ = service.post("Orbit", "t", instructable=doc, edit_key_hash="0" * 64)
        try:
            service.append(item.id, "k", 1, "h", "n", [{"do": "y"}])
        except AmendError as exc:
            return exc.body["error"]
    finally:
        store.close()


def test_appends_status_on_off(data_dir, capsys):
    assert admin.main(["appends", "status"]) == 0
    assert capsys.readouterr().out == "appends: on\n"
    assert _append_error(data_dir) == "wrong edit key"
    assert admin.main(["appends", "off"]) == 0
    assert capsys.readouterr().out == "appends: off\n"
    assert _append_error(data_dir) == "appends not enabled"
    assert admin.main(["appends", "on"]) == 0
    assert capsys.readouterr().out == "appends: on\n"
    assert _append_error(data_dir) == "wrong edit key"


def test_appends_bad_arguments(data_dir, capsys):
    for argv in (["appends"], ["appends", "maybe"]):
        assert admin.main(argv) == 2
    assert "appends on|off|status" in capsys.readouterr().err


def _docs_error(data_dir):
    """The error a post of an ask with a document gets: the switch's when off, none when on."""
    store = Store(data_dir / "mailbox.db")
    store.open()
    try:
        service = Service(store, FakeTelegramClient(), "personal")
        text = "# Plan\n\nShip on Friday.\n"
        sha = hashlib.sha256(text.encode()).hexdigest()
        doc = {"v": 1, "title": f"t{uuid4().hex}", "urgency": "today",
               "steps": [{"do": "Read it", "file": "~/notes/plan.md", "doc": {"sha256": sha, "bytes": len(text)}}]}
        try:
            service.post("Orbit", doc["title"], instructable=doc, docs={sha: text})
        except AmendError as exc:
            return exc.body["error"]
        return None
    finally:
        store.close()


KEPT = "docs kept: until released, or 7 days after the ask is closed\n"


def _status(state: str, count: int, size: int) -> str:
    return f"docs: {state}\ndocs stored: {count} ({size} of 268435456 bytes)\n" + KEPT


def test_docs_status_on_off(data_dir, capsys):
    assert admin.main(["docs", "status"]) == 0
    assert capsys.readouterr().out == _status("off", 0, 0)
    assert _docs_error(data_dir) == "documents not enabled"
    assert admin.main(["docs", "on"]) == 0
    assert capsys.readouterr().out == "docs: on\n"
    assert admin.main(["docs", "status"]) == 0
    assert capsys.readouterr().out == _status("on", 0, 0)
    assert _docs_error(data_dir) is None
    assert admin.main(["docs", "off"]) == 0
    assert capsys.readouterr().out == "docs: off\n"
    assert _docs_error(data_dir) == "documents not enabled"


def test_docs_status_shows_how_much_is_stored(data_dir, capsys):
    assert admin.main(["docs", "on"]) == 0
    assert _docs_error(data_dir) is None                    # one 25-byte document
    assert _docs_error(data_dir) is None                    # the same text again: stored once
    capsys.readouterr()
    assert admin.main(["docs", "status"]) == 0
    assert capsys.readouterr().out == _status("on", 1, len("# Plan\n\nShip on Friday.\n"))


def test_docs_status_prints_the_limit(data_dir, capsys, monkeypatch):
    from mailbox import docs as documents
    assert admin.main(["docs", "status"]) == 0
    assert "(0 of 268435456 bytes)" in capsys.readouterr().out
    monkeypatch.setattr(documents, "MAX_STORE_BYTES", 4096)
    assert admin.main(["docs", "status"]) == 0
    assert "docs stored: 0 (0 of 4096 bytes)\n" in capsys.readouterr().out


def test_docs_bad_arguments(data_dir, capsys):
    for argv in (["docs"], ["docs", "maybe"], ["docs", "on", "now"]):
        assert admin.main(argv) == 2
    assert "python3 -m mailbox.admin docs on|off|status" in capsys.readouterr().err


def test_docs_runs_as_a_module(data_dir):
    result = subprocess.run([sys.executable, "-m", "mailbox.admin", "docs", "status"],
                            cwd=REPO_ROOT, capture_output=True, text=True,
                            env={"MAILBOX_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"})
    assert (result.returncode, result.stdout) == (0, _status("off", 0, 0))
