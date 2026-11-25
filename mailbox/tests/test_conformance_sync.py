"""The Pond's vector files and Hook's canonical conformance suite hold the same bytes.

Set HOOK_CONFORMANCE_DIR to Hook's conformance folder (the one with manifest.json).
Every path in conformance_sync.SHARED must be in Hook's manifest and byte-identical
to the Pond's copy in mailbox/tests/; conformance_sync.py says the full rule.

Unset, the comparison with a real Hook tree skips, so a plain local run works
without a Hook checkout. With HOOK_CONFORMANCE_REQUIRED=1 (CI sets it) an unset
folder fails instead. The synthetic cases below never skip.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mailbox.tests import conformance_sync as sync

HERE = Path(__file__).parent
SCRIPT = HERE / "conformance_sync.py"
_DIR = os.environ.get("HOOK_CONFORMANCE_DIR")
_REQUIRED = os.environ.get("HOOK_CONFORMANCE_REQUIRED") == "1"
_UNSET = "HOOK_CONFORMANCE_DIR is not set: no Hook conformance folder to compare with"


def _hook_dir():
    if not _DIR:
        if _REQUIRED:
            pytest.fail(_UNSET + " (HOOK_CONFORMANCE_REQUIRED=1)")
        pytest.skip(_UNSET)
    return Path(_DIR).expanduser()


def _run(args, env=None):
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                            env=env)
    return result.returncode, result.stdout + result.stderr


# --- the pinned lists ----------------------------------------------------------------

def test_the_pinned_lists_are_well_formed():
    assert len(sync.SHARED) == 25
    assert len(set(sync.SHARED)) == len(sync.SHARED)
    names = [Path(p).name for p in sync.SHARED]
    assert len(set(names)) == len(names), "two shared paths share a basename"
    assert not set(names) & set(sync.POND_ONLY)


def test_every_pond_vector_is_classified():
    shared = {Path(p).name for p in sync.SHARED}
    for vec in HERE.glob("*.json"):
        assert vec.name in shared or vec.name in sync.POND_ONLY, f"{vec.name} is not classified"
    for name in sorted(shared) + list(sync.POND_ONLY):
        assert (HERE / name).is_file(), f"{name} is classified but missing from mailbox/tests/"


# --- against the real Hook tree --------------------------------------------------------

def test_the_shared_files_match_hook():
    report = sync.compare(_hook_dir(), HERE)
    assert report.errors == []
    assert sorted(report.matched) == sorted(sync.SHARED)
    assert len(report.matched) == 25


@pytest.mark.parametrize("path", sync.SHARED)
def test_a_shared_file_is_byte_identical(path):
    root = _hook_dir()
    canonical, ours = root / path, HERE / Path(path).name
    assert canonical.is_file(), f"Hook has no conformance/{path}"
    assert ours.read_bytes() == canonical.read_bytes(), (
        f"mailbox/tests/{ours.name} differs from Hook's conformance/{path}: regenerate or copy it over")


def test_the_command_passes_on_the_real_trees():
    code, out = _run(["--hook", str(_hook_dir()), "--tests", str(HERE)])
    assert code == 0, out
    assert "25 shared files match" in out


# --- synthetic trees: every failure is loud --------------------------------------------

@pytest.fixture
def trees(tmp_path):
    """A synthetic Hook suite and Pond tests folder that agree, built from the Pond's copies."""
    hook, pond = tmp_path / "hook-conformance", tmp_path / "pond-tests"
    pond.mkdir()
    for path in sync.SHARED:
        src = HERE / Path(path).name
        (hook / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, hook / path)
        shutil.copyfile(src, pond / src.name)
    for name in sync.POND_ONLY:
        shutil.copyfile(HERE / name, pond / name)
    (hook / "data/hook-only-vectors.json").write_text("{}\n", encoding="utf-8")
    _write_manifest(hook, list(sync.SHARED) + ["data/hook-only-vectors.json"])
    return hook, pond


def _write_manifest(hook, paths):
    files = [{"path": p} for p in paths]
    (hook / "manifest.json").write_text(json.dumps({"suite": 1, "files": files}), encoding="utf-8")


def _fails(hook, pond):
    report = sync.compare(hook, pond)
    code, out = _run(["--hook", str(hook), "--tests", str(pond)])
    assert not report.ok
    assert code != 0, out
    return report, out


def test_synthetic_trees_that_agree_pass(trees):
    hook, pond = trees
    report = sync.compare(hook, pond)
    assert report.errors == []
    assert len(report.matched) == 25
    code, out = _run(["--hook", str(hook), "--tests", str(pond)])
    assert code == 0, out


def test_a_missing_copy_fails(trees):
    hook, pond = trees
    (pond / "bug-vectors.json").unlink()
    report, out = _fails(hook, pond)
    assert report.missing_expected != set()
    assert report.missing_expected == {"wire/bug-vectors.json"}
    assert "bug-vectors.json" in out


def test_a_one_byte_drift_fails(trees):
    hook, pond = trees
    target = pond / "revision-vectors.json"
    data = bytearray(target.read_bytes())
    data[-2] ^= 0x01
    target.write_bytes(bytes(data))
    report, out = _fails(hook, pond)
    assert report.mismatched == {"data/revision-vectors.json"}
    assert "revision-vectors.json" in out


def test_a_manifest_path_with_no_file_fails(trees):
    hook, pond = trees
    (hook / "wire/limits-vectors.json").unlink()
    report, _ = _fails(hook, pond)
    assert report.missing_in_hook == {"wire/limits-vectors.json"}


def test_an_empty_intersection_fails(trees, tmp_path):
    hook, _ = trees
    empty = tmp_path / "empty-tests"
    empty.mkdir()
    report, out = _fails(hook, empty)
    assert "empty intersection" in out
    assert report.matched == []


def test_a_shared_path_dropped_from_the_manifest_fails(trees):
    hook, pond = trees
    _write_manifest(hook, [p for p in sync.SHARED if p != "data/lint-vectors.json"])
    report, _ = _fails(hook, pond)
    assert report.not_in_manifest == {"data/lint-vectors.json"}


def test_an_unpinned_shared_file_fails(trees):
    hook, pond = trees
    (pond / "hook-only-vectors.json").write_text("{}\n", encoding="utf-8")
    report, _ = _fails(hook, pond)
    assert report.unpinned == {"data/hook-only-vectors.json"}


def test_an_unclassified_pond_vector_fails(trees):
    hook, pond = trees
    (pond / "new-vectors.json").write_text("{}\n", encoding="utf-8")
    report, _ = _fails(hook, pond)
    assert report.unclassified == {"new-vectors.json"}


def test_a_pond_only_file_in_the_manifest_fails(trees):
    hook, pond = trees
    shutil.copyfile(HERE / "then-instructable-vectors.json", hook / "data/then-instructable-vectors.json")
    _write_manifest(hook, list(sync.SHARED) + ["data/then-instructable-vectors.json"])
    report, _ = _fails(hook, pond)
    assert report.misclassified == {"then-instructable-vectors.json"}


def test_no_manifest_fails(trees):
    hook, pond = trees
    (hook / "manifest.json").unlink()
    _fails(hook, pond)


def test_the_command_without_a_hook_folder_fails():
    env = {k: v for k, v in os.environ.items() if k != "HOOK_CONFORMANCE_DIR"}
    code, out = _run([], env=env)
    assert code != 0
    assert "HOOK_CONFORMANCE_DIR" in out
