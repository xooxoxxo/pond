"""Tests of the release scripts: check-release.py, check-image-files.py, verify-image.sh.

    python3 -m pytest -q scripts/tests
"""
import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_release = _load("check-release")
check_image_files = _load("check-image-files")

INIT = '"""x"""\n__version__ = "0.1.0"\n\nVERSION = "1.15.0"\nPROTOCOL = 5\n'
CHANGELOG = "# Pond changelog\n\nIntro.\n\n## 1.15.0\n\n- Longer answers.\n\n## 1.14.0\n\n- Brief.\n"


# check-release.py

def test_matching_tag_and_changelog_pass():
    assert check_release.check("v1.15.0", INIT, CHANGELOG) == []


def test_tag_version_mismatch_is_refused():
    found = check_release.check("v1.15.1", INIT, CHANGELOG)
    assert found and "does not match mailbox.VERSION '1.15.0'" in found[0]


def test_missing_changelog_section_is_refused():
    changelog = CHANGELOG.replace("## 1.15.0\n\n- Longer answers.\n\n", "")
    assert check_release.check("v1.15.0", INIT, changelog) == ["CHANGELOG.md: no '## 1.15.0' section"]


def test_empty_changelog_section_is_refused():
    changelog = CHANGELOG.replace("- Longer answers.\n", "")
    assert check_release.check("v1.15.0", INIT, changelog) == ["CHANGELOG.md: the '## 1.15.0' section is empty"]


def test_a_prefix_heading_does_not_count():
    # "## 1.15.0" must not be satisfied by "## 1.15.01" or "## 1.15.0-rc1".
    changelog = CHANGELOG.replace("## 1.15.0\n", "## 1.15.0-rc1\n")
    assert check_release.check("v1.15.0", INIT, changelog) == ["CHANGELOG.md: no '## 1.15.0' section"]


@pytest.mark.parametrize("tag", ["1.15.0", "v1.15", "v1.15.0-rc1", "v01.15.0", "refs/tags/v1.15.0", ""])
def test_malformed_tags_are_refused(tag):
    assert check_release.check(tag, INIT, CHANGELOG)


def test_the_repository_itself_passes_for_its_version():
    version = check_release.package_version((ROOT / "mailbox" / "__init__.py").read_text())
    run = subprocess.run([sys.executable, str(SCRIPTS / "check-release.py"), f"v{version}", "--root", str(ROOT)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == version


def test_the_cli_refuses_a_mismatch_with_a_message():
    run = subprocess.run([sys.executable, str(SCRIPTS / "check-release.py"), "v0.0.1", "--root", str(ROOT)],
                         capture_output=True, text=True)
    assert run.returncode == 1
    assert "does not match mailbox.VERSION" in run.stderr
    assert "no '## 0.0.1' section" in run.stderr


# check-image-files.py

def _layer(paths: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in paths.items():
            info = tarfile.TarInfo(path)
            if path.endswith("/"):
                info.type, info.name = tarfile.DIRTYPE, path.rstrip("/")
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _image(tmp_path: Path, *layers: dict[str, bytes]) -> Path:
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as tar:
        for index, layer in enumerate(layers):
            data = _layer(layer)
            info = tarfile.TarInfo(f"blobs/sha256/{index:064x}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        index_json = b'{"schemaVersion":2,"manifests":[]}'
        info = tarfile.TarInfo("index.json")
        info.size = len(index_json)
        tar.addfile(info, io.BytesIO(index_json))
    return archive


def _allowed() -> set[str]:
    return check_image_files.copied_files((ROOT / "mailbox" / "Dockerfile").read_text())


def _runtime_layer() -> dict[str, bytes]:
    return {"app/": b"", "app/mailbox/": b"", **{f"app/mailbox/{n}": b"x" for n in _allowed()}}


def test_the_dockerfile_copy_list_is_read():
    allowed = _allowed()
    assert {"__init__.py", "__main__.py", "server.py"} <= allowed
    assert not any(n.startswith("tests") or n.endswith((".md", ".yaml", ".sh")) for n in allowed)


def test_a_clean_image_passes(tmp_path):
    base = {"usr/local/lib/python3.12/README.txt": b"x", "etc/os-release": b"x"}
    layers = check_image_files.layer_listings(_image(tmp_path, base, _runtime_layer()))
    assert check_image_files.problems(layers, _allowed()) == []


@pytest.mark.parametrize("extra", [
    "app/mailbox/tests/test_server.py", "app/README.md", "app/mailbox/compose.yaml",
    "app/mailbox/prepare-host.sh", "app/.env", "app/mailbox/Dockerfile",
])
def test_an_excluded_file_under_app_is_refused(tmp_path, extra):
    layers = check_image_files.layer_listings(_image(tmp_path, _runtime_layer(), {extra: b"x"}))
    assert any(extra in p for p in check_image_files.problems(layers, _allowed()))


def test_an_env_file_anywhere_is_refused(tmp_path):
    layers = check_image_files.layer_listings(_image(tmp_path, _runtime_layer(), {"root/.env.local": b"x"}))
    assert check_image_files.problems(layers, _allowed()) == [f"blobs/sha256/{1:064x}: env file root/.env.local"]


def test_a_missing_runtime_file_is_refused(tmp_path):
    layer = _runtime_layer()
    del layer["app/mailbox/server.py"]
    layers = check_image_files.layer_listings(_image(tmp_path, layer))
    assert check_image_files.problems(layers, _allowed()) == ["missing runtime file app/mailbox/server.py"]


# verify-image.sh

def test_verify_image_self_test_passes():
    run = subprocess.run(["bash", str(SCRIPTS / "verify-image.sh"), "--self-test"], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert ", 0 failed" in run.stdout


def test_verify_image_refuses_a_v_prefixed_version_before_any_network():
    run = subprocess.run(["bash", str(SCRIPTS / "verify-image.sh"), "v1.15.0"], capture_output=True, text=True)
    assert run.returncode != 0
    assert "is not MAJOR.MINOR.PATCH" in run.stderr
