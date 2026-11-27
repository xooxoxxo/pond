"""Release gate run inside each platform image with no Telegram credentials or network."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time


def admin(*args):
    return json.loads(subprocess.check_output([sys.executable, "-m", "mailbox.admin", *args],
                                              stderr=subprocess.DEVNULL))


def main(version):
    os.environ["POND_INSTALL_ID"] = secrets.token_hex(16)
    assert admin("maintenance", "initialize")["installation"] == os.environ["POND_INSTALL_ID"]
    with subprocess.Popen([sys.executable, "-m", "mailbox"], stdout=subprocess.DEVNULL) as server:
        try:
            deadline = time.monotonic() + 60
            while server.poll() is None:
                try:
                    report = admin("status", "--json")
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.25)
                    continue
                assert report["installation"] == os.environ["POND_INSTALL_ID"]
                assert report["admission"] == "native" and report["claimed"] is False
                assert report["telegram"] is False and report["health"]["ok"] is True
                assert report["version"]["version"] == version
                assert {"managedInstall", "admission", "longAnswers"} <= set(report["version"]["features"])
                assert not any(Path(root, "mailbox-token").exists() for root in ("/data", "/backups"))
                return
            raise RuntimeError("Packaged Pond did not start in managed mode without Telegram")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    main(sys.argv[1])
