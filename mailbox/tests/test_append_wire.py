"""append-wire-vectors.json is what gen_append_wire records from this Pond today."""
import os
import subprocess
import sys
from pathlib import Path

from mailbox.tests import gen_append_wire as gen
from mailbox.tests import gen_phone_vectors as phone

REPO = Path(__file__).resolve().parents[2]


def test_append_wire_replays():
    env = {k: v for k, v in os.environ.items() if k != gen.RECORD_ENV}
    done = subprocess.run([sys.executable, "mailbox/tests/gen_append_wire.py"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def test_the_transcript_has_its_cases_in_order_and_no_secret():
    text = gen.dump(gen.build())
    assert text == gen.OUT.read_text(encoding="utf-8")
    assert phone.TOKENS and not any(token in text for token in phone.TOKENS)
    import json
    doc = json.loads(text)
    assert doc["pond"] == gen.VERSION
    got = [(c["name"], c["response"]["status"], c["response"]["body"].get("error")) for c in doc["cases"]]
    assert got == [
        ("append-post", 201, None), ("append-answer", 200, None),
        ("append-done-waiting", 409, "waiting_for_steps"), ("append-detail-waiting", 200, None),
        ("append-steps", 200, None), ("append-detail", 200, None),
        ("append-done-old-rev", 409, "steps_added"), ("append-answer-old-rev", 200, None),
        ("append-done", 200, None),
    ]
    detail = doc["cases"][5]["response"]["body"]["item"]["revision"]
    assert (detail["rev"], detail["kind"], detail["acceptedRev"]) == (2, "append", 2)
    assert "stepWait" in doc["cases"][3]["response"]["body"]["item"]
