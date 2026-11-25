"""The secret scan builds its skeleton once (final wave, W4), and answers exactly as
the scan before it did, for every input.

`reference_looks_secret` is the implementation as it stood at Pond 1.10.0 before this
change (one skeleton per pattern), copied here unchanged. Every secret-shaped string is
built at run time from parts: no real-looking token is written in this file."""
import ast
import json
import random
from pathlib import Path
from unittest import mock

from mailbox import docs, instructable
from mailbox.instructable import SECRET_PATTERNS, _skeleton, looks_secret

HERE = Path(__file__).parent


def reference_looks_secret(text: str) -> bool:
    """True when the text, or its skeleton, holds anything shaped like a credential."""
    return any(p.search(text) for p in SECRET_PATTERNS) or any(
        p.search(_skeleton(text)) for p in SECRET_PATTERNS
    )


# --- (a) every string the vector files and the tests already hold -------------------------

def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def existing_strings() -> list[str]:
    found: set[str] = set()
    for path in sorted(HERE.glob("*.json")):
        found.update(_strings(json.loads(path.read_text(encoding="utf-8"))))
    for path in sorted(HERE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.update(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str))
    return sorted(found)


def test_the_corpus_from_the_vectors_and_tests_is_not_empty():
    strings = existing_strings()
    assert len(strings) > 1000
    assert sum(1 for s in strings if reference_looks_secret(s)) > 0      # it holds secret-like ones too


def test_every_existing_string_answers_as_before():
    differ = [s[:40] for s in existing_strings() if looks_secret(s) != reference_looks_secret(s)]
    assert differ == []


# --- (b) a generated corpus: the patterns' shapes, hidden characters, other scripts -----

def _shapes(rng: random.Random) -> list[str]:
    alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return [
        "sk" + "-" + "ant" + "-" + "api03-" + "".join(rng.choice(alnum) for _ in range(24)),
        "sk" + "-" + "".join(rng.choice(alnum) for _ in range(rng.choice((19, 20, 32)))),
        "x" + "ox" + rng.choice("abpz") + "-" + "".join(rng.choice(alnum) for _ in range(12)),
        "gh" + "p_" + "".join(rng.choice(alnum) for _ in range(36)),
        "git" + "hub_pat_" + "".join(rng.choice(alnum) for _ in range(22)),
        "-----BEGIN " + rng.choice(("RSA ", "EC ", "OPENSSH ", "")) + "PRIVATE " + "KEY-----",
        "AK" + "IA" + "".join(rng.choice(upper) for _ in range(rng.choice((15, 16, 20)))),
    ]


HIDDEN = ["‍", "​", "­", "️", "︎", " ", " ", "͏", "᠎",
          "\U000e0041", "\U000e0001", "", "\x07", "‮", "﻿", "\ud800"]
# NFKC turns these into more than one character, or into ASCII.
EXPANDING = ["ﷺ", "ﬃ", "㎒", "⑴", "㏇", "ｓ", "ｋ", "－", "Ⅰ",
             "½", "‥", "ẛ̣"]
SCRIPTS = ["ѕк-", "αβγ", "日本語", "مرحبا", "שלום", "한국어", "🙂", "👩‍💻", "é", "Ä"]
FULLWIDTH = {c: chr(ord(c) + 0xFEE0) for c in "!\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                                "[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"}


def _disguise(rng: random.Random, text: str) -> str:
    out = []
    for ch in text:
        roll = rng.random()
        if roll < 0.15:
            out.append(FULLWIDTH.get(ch, ch))
        elif roll < 0.30:
            out.append(ch + rng.choice(HIDDEN))
        elif roll < 0.35:
            out.append(ch + rng.choice(EXPANDING))
        else:
            out.append(ch)
    return "".join(out)


def generated_strings(count: int = 6000, seed: int = 1010) -> list[str]:
    rng = random.Random(seed)
    corpus = list(HIDDEN + EXPANDING + SCRIPTS) + ["", " ", "\n", "\t"]
    for _ in range(count):
        shape = rng.choice(_shapes(rng))
        kind = rng.randrange(6)
        if kind == 0:
            text = shape
        elif kind == 1:
            text = _disguise(rng, shape)
        elif kind == 2:                          # a shape cut short, or with a letter changed
            cut = shape[: rng.randrange(1, len(shape))]
            text = _disguise(rng, cut) if rng.random() < 0.5 else cut
        elif kind == 3:                          # prose around a disguised shape
            text = (rng.choice(SCRIPTS) + " see " + _disguise(rng, shape) + " " + rng.choice(EXPANDING) * 3)
        elif kind == 4:                          # only expanding and hidden characters
            text = "".join(rng.choice(EXPANDING + HIDDEN) for _ in range(rng.randrange(1, 40)))
        else:                                    # mixed scripts, no shape
            text = "".join(rng.choice(SCRIPTS + EXPANDING + [" ", "-", "_"]) for _ in range(rng.randrange(1, 30)))
        corpus.append(text)
    return corpus


def test_the_generated_corpus_holds_both_answers():
    answers = [reference_looks_secret(s) for s in generated_strings()]
    assert answers.count(True) > 1000 and answers.count(False) > 1000
    disguised = [s for s in generated_strings() if reference_looks_secret(s)
                 and not any(p.search(s) for p in SECRET_PATTERNS)]
    assert len(disguised) > 100                  # found through the skeleton only


def test_every_generated_string_answers_as_before():
    differ = [ascii(s[:40]) for s in generated_strings() if looks_secret(s) != reference_looks_secret(s)]
    assert differ == []


# --- the skeleton is built once ---------------------------------------------------------

def test_the_skeleton_is_built_at_most_once_per_call():
    plain = "A note with nothing to hide in it. " * 50
    with mock.patch.object(instructable, "_skeleton", wraps=_skeleton) as spy:
        assert looks_secret(plain) is False
    assert spy.call_count == 1
    shaped = "gh" + "p_" + "x" * 36
    with mock.patch.object(instructable, "_skeleton", wraps=_skeleton) as spy:
        assert looks_secret(shaped) is True
    assert spy.call_count == 0                   # found in the raw text: no skeleton needed


# --- docs.check scans only texts it would keep ------------------------------------------

def _ask(*texts: str) -> dict:
    steps = []
    for i, text in enumerate(texts):
        sha, size = docs.digest(text)
        steps.append({"do": "Read it", "file": f"/var/tmp/{i}.md", "doc": {"sha256": sha, "bytes": size}})
    return {"v": 1, "title": "Read the notes", "steps": steps}


def test_check_does_not_scan_a_text_no_step_or_link_uses():
    used, stray = "# Used\n", "# Stray\n" + "gh" + "p_" + "y" * 36 + "\n"
    sent = {docs.digest(t)[0]: t for t in (used, stray)}
    with mock.patch.object(docs, "looks_secret", wraps=looks_secret) as spy:
        errors = docs.check(_ask(used), sent, set())
    assert errors == [f"docs.{docs.digest(stray)[0]}: not used by any step or link"]
    assert [c.args[0] for c in spy.call_args_list] == [used]


def test_check_does_not_scan_when_the_request_is_over_its_total():
    texts = [c * instructable.MAX_DOC_BYTES for c in "abc"] + ["d"]
    sent = {docs.digest(t)[0]: t for t in texts}
    with mock.patch.object(docs, "looks_secret", wraps=looks_secret) as spy:
        errors = docs.check(_ask(*texts), sent, set())
    assert errors == [f"docs: more than {docs.MAX_DOCS_BYTES} bytes in all"]
    assert spy.call_count == 0


def test_check_still_names_a_secret_like_text_it_would_keep_in_the_same_order():
    secret = "# Setup\n" + "sk" + "-" + "ant" + "-" + "api03-" + "Q" * 32 + "\n"
    plain, stray = "# Plain\n", "# Stray\n"
    obj = _ask(plain, secret)
    obj["steps"].append({"do": "Read the last", "file": "/var/tmp/last.md",
                         "doc": {"sha256": "0" * 64, "bytes": 3}})
    sent = {docs.digest(t)[0]: t for t in (plain, secret, stray)}
    hashes = sorted([docs.digest(secret)[0], docs.digest(stray)[0]])
    expected = ["steps[2].doc: no document with this hash"] + [
        f"docs.{h}: secret-like value" if h == docs.digest(secret)[0] else f"docs.{h}: not used by any step or link"
        for h in hashes]
    assert docs.check(obj, sent, set()) == expected
