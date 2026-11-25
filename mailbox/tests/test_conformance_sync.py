"""The Pond's vector files and Hook's canonical conformance suite hold the same bytes.

Set HOOK_CONFORMANCE_DIR to Hook's conformance folder (the one with manifest.json).
Every file the manifest lists whose basename is also a file in mailbox/tests/ must be
byte-identical to the canonical one; a file the manifest doesn't list is not compared.
Unset, the test skips: Hook's checkout isn't part of this repo.
"""
import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).parent
_DIR = os.environ.get("HOOK_CONFORMANCE_DIR")


_UNSET = "HOOK_CONFORMANCE_DIR is not set: no Hook conformance folder to compare with"


def _shared() -> list:
    """(manifest path, Hook's file, ours) for each listed file the Pond also has."""
    if not _DIR:
        return [pytest.param(None, None, None, id="unset", marks=pytest.mark.skip(reason=_UNSET))]
    root = Path(_DIR).expanduser()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files") or []
    return [pytest.param(entry["path"], root / entry["path"], HERE / Path(entry["path"]).name, id=entry["path"])
            for entry in files if (HERE / Path(entry["path"]).name).is_file()]


def test_the_manifest_is_readable():
    if not _DIR:
        pytest.skip(_UNSET)
    root = Path(_DIR).expanduser()
    assert (root / "manifest.json").is_file(), f"no manifest.json in {root}"
    assert _shared(), "the manifest lists none of the Pond's vector files"


@pytest.mark.parametrize("path,canonical,ours", _shared())
def test_a_shared_file_is_byte_identical(path, canonical, ours):
    assert canonical.is_file(), f"the manifest lists {path}, but Hook has no such file"
    assert ours.read_bytes() == canonical.read_bytes(), (
        f"mailbox/tests/{ours.name} differs from Hook's conformance/{path}: regenerate or copy it over")
