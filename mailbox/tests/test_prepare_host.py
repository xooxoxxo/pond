"""Host preparation uses only a temporary deployment directory."""
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MAILBOX = Path(__file__).resolve().parents[1]
BASE = "MAILBOX_CONTEXT=personal\nTZ=Etc/UTC\n"


class PrepareHost(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        shutil.copy(MAILBOX / "prepare-host.sh", self.root)

    def run_prepare(self, contents):
        env = self.root / ".env"
        env.write_text(contents)
        env.chmod(0o600)
        return subprocess.run(["bash", self.root / "prepare-host.sh"], capture_output=True, text=True)

    def test_native_requires_no_telegram_keys(self):
        for values in ("", "TELEGRAM_BOT_TOKEN=\nMAILBOX_OWNER_ID=\n",
                       "TELEGRAM_BOT_TOKEN='' # optional\nMAILBOX_OWNER_ID=\"\"\n"):
            with self.subTest(values=values):
                result = self.run_prepare(BASE + values)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / "backups").stat().st_mode & 0o777, 0o700)

    def test_one_telegram_setting_explains_pair_without_echoing_values(self):
        for values in ("TELEGRAM_BOT_TOKEN=fixture-token\n", "MAILBOX_OWNER_ID=12345\n",
                       "TELEGRAM_BOT_TOKEN=fixture-token\nMAILBOX_OWNER_ID= # absent\n"):
            with self.subTest(values=values):
                result = self.run_prepare(BASE + values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TELEGRAM_BOT_TOKEN", result.stderr)
                self.assertIn("MAILBOX_OWNER_ID", result.stderr)
                self.assertNotIn("fixture-token", result.stdout + result.stderr)
                self.assertNotIn("12345", result.stdout + result.stderr)
                self.assertFalse((self.root / "backups").exists())

    def test_full_and_alert_still_prepare(self):
        for mode in ("full", "alert"):
            result = self.run_prepare(BASE + "TELEGRAM_BOT_TOKEN=fixture-token\nMAILBOX_OWNER_ID=12345\n"
                                      + f"MAILBOX_TELEGRAM_MODE={mode}\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("fixture-token", result.stdout + result.stderr)

    def test_example_prepares_without_a_bot(self):
        contents = (MAILBOX / ".env.example").read_text()
        self.assertNotRegex(contents, r"(?m)^(TELEGRAM_BOT_TOKEN|MAILBOX_OWNER_ID)=\S")
        self.assertEqual(self.run_prepare(contents).returncode, 0)

    def test_empty_required_host_settings_fail_before_files_are_created(self):
        for line in ("MAILBOX_CONTEXT= # missing\nTZ=Etc/UTC\n", "TZ=''\nMAILBOX_CONTEXT=personal\n"):
            with self.subTest(line=line):
                result = self.run_prepare(line)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "backups").exists())
