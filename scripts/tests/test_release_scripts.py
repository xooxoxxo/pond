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


# The publish path of verify-image.sh and registry-tag.sh, with docker, cosign and curl
# replaced by stubs on PATH. The v1.15.1 run verified the digest, printed that it was the
# signed release, and then failed with "tmp: unbound variable": a RETURN trap set inside
# a function stays set after it returns and fires again on the next function return,
# where the function's locals are gone and `set -u` stops the script.

FIXTURES = SCRIPTS / "fixtures" / "verify-image"
D1 = "sha256:" + "1" * 64


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o755)


def _stub_env(tmp_path: Path) -> dict:
    import os
    bin_dir = tmp_path / "bin"
    work = tmp_path / "tmp"
    bin_dir.mkdir()
    work.mkdir()
    _stub(bin_dir, "docker", f"""
case "$*" in
  "buildx imagetools inspect --raw "*) cat {FIXTURES / "index-ok.json"} ;;
  "buildx imagetools inspect "*) cat {FIXTURES / "inspect-ok.txt"} ;;
  *) echo "docker stub: unexpected $*" >&2; exit 99 ;;
esac
""")
    _stub(bin_dir, "cosign", f'cat {FIXTURES / "cosign-ok.json"}\n')
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=str(work))
    env.pop("IMAGE_REPO", None)
    return env


@pytest.mark.parametrize("args", [["--digest-only", D1], ["--digest", D1], []],
                         ids=["digest-only", "digest", "tag"])
def test_verify_image_succeeds_and_cleans_up_under_set_u(tmp_path, args):
    env = _stub_env(tmp_path)
    run = subprocess.run(["bash", str(SCRIPTS / "verify-image.sh"), "1.15.0", *args],
                         capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"ghcr.io/xooxoxxo/pond@{D1} is the signed 1.15.0 release" in run.stdout
    assert "unbound variable" not in run.stderr
    assert list((tmp_path / "tmp").iterdir()) == []  # its temporary folder is gone


def test_verify_image_still_fails_on_a_wrong_digest_and_cleans_up(tmp_path):
    env = _stub_env(tmp_path)
    run = subprocess.run(["bash", str(SCRIPTS / "verify-image.sh"), "1.15.0", "--digest", "sha256:" + "2" * 64],
                         capture_output=True, text=True, env=env)
    assert run.returncode == 1
    assert "the release names sha256:2222" in run.stderr
    assert "unbound variable" not in run.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


def _registry_env(tmp_path: Path, index: bytes) -> dict:
    import os
    bin_dir = tmp_path / "bin"
    work = tmp_path / "tmp"
    bin_dir.mkdir()
    work.mkdir()
    (tmp_path / "index.json").write_bytes(index)
    # curl --config - [...]: the credential comes on stdin and is dropped here.
    _stub(bin_dir, "curl", f"""
[[ "${{1:-}}" == --config ]] && cat >/dev/null
args="$*"
case "$args" in
  *"/token?"*) echo '{{"token": "t"}}' ;;
  *"-X PUT"*)
    for a in "$@"; do [[ "$a" == @* ]] && cp "${{a#@}}" {tmp_path / "put.json"}; done
    printf 201 ;;
  *" -I "*) printf 404 ;;
  *)
    out=""; prev=""
    for a in "$@"; do [[ "$prev" == -o ]] && out="$a"; prev="$a"; done
    cp {tmp_path / "index.json"} "$out" ;;
esac
""")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=str(work),
               REGISTRY_USER="someone", REGISTRY_TOKEN="not-a-real-token")
    env.pop("IMAGE_REPO", None)
    return env


def test_registry_tag_puts_the_exact_index_bytes_and_cleans_up(tmp_path):
    import hashlib
    index = (FIXTURES / "index-ok.json").read_bytes()
    digest = "sha256:" + hashlib.sha256(index).hexdigest()
    env = _registry_env(tmp_path, index)
    run = subprocess.run(["bash", str(SCRIPTS / "registry-tag.sh"), "tag", "1.15.0", digest],
                         capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"ghcr.io/xooxoxxo/pond:1.15.0 -> {digest}" in run.stderr
    assert (tmp_path / "put.json").read_bytes() == index
    assert list((tmp_path / "tmp").iterdir()) == []


def test_registry_tag_exists_reads_a_404_as_free(tmp_path):
    env = _registry_env(tmp_path, b"{}")
    run = subprocess.run(["bash", str(SCRIPTS / "registry-tag.sh"), "exists", "1.15.0"],
                         capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr
    assert "is free (404)" in run.stderr


def test_no_script_sets_a_return_trap_that_reads_a_variable_later():
    # The trap text is run when it fires, not when it is set. A single-quoted RETURN trap
    # that names a local reads it at a later function return, after the local is gone.
    import re
    for script in sorted(SCRIPTS.glob("*.sh")):
        for number, line in enumerate(script.read_text().splitlines(), 1):
            if re.search(r"\btrap\s+'[^']*\$[^']*'.*\bRETURN\b", line):
                pytest.fail(f"{script.name}:{number}: {line.strip()}")


def test_a_return_trap_outlives_its_function_in_bash():
    # The bash behaviour the guard above relies on.
    script = ("set -u; f() { local tmp=x; trap 'echo \"$tmp\"' RETURN; }; g() { f; }; g; echo reached")
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert run.returncode != 0 and "unbound variable" in run.stderr


# The workflows themselves (text checks: no YAML parser in the test tooling)

WORKFLOWS = ROOT / ".github" / "workflows"


@pytest.mark.parametrize("name", ["release.yml", "ci.yml"])
def test_workflows_run_steps_with_pipefail(name):
    # GitHub's default shell is `bash -e` without pipefail, so `pytest | tee` would pass on a
    # failing pytest. An explicit `shell: bash` is `bash -eo pipefail`.
    text = (WORKFLOWS / name).read_text()
    assert "\ndefaults:\n  run:\n    shell: bash\n" in text


def test_the_release_runs_ci_and_holds_no_hook_pin_of_its_own():
    release = (WORKFLOWS / "release.yml").read_text()
    ci = (WORKFLOWS / "ci.yml").read_text()
    assert "uses: ./.github/workflows/ci.yml" in release
    assert "workflow_call:" in ci
    assert "HOOK_REF" in ci
    assert "8115ff6" not in release and "HOOK_SHA" not in release and "HOOK_REPO" not in release
    assert release.count("needs: [ci, check]") == 2  # dry-run and publish both wait for CI


def test_pipefail_is_what_stops_a_failing_pipe():
    # The behaviour the guard above relies on.
    plain = subprocess.run(["bash", "-e", "-c", "false | tee /dev/null; echo continued"],
                           capture_output=True, text=True)
    strict = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                             "false | tee /dev/null; echo continued"], capture_output=True, text=True)
    assert plain.returncode == 0 and "continued" in plain.stdout
    assert strict.returncode != 0 and "continued" not in strict.stdout
