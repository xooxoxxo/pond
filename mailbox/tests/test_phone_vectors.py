"""The phone conformance vectors: each of the six files is what gen_phone_vectors
writes today, byte for byte, so a Pond change without regenerated vectors fails
here before a client is held to stale answers."""
import json

import pytest

from mailbox.tests import gen_phone_vectors as gen

WIRE = ("phone-setup", "phone-acts", "phone-errors")


@pytest.fixture(scope="module")
def built():
    """Every file as the generator writes it now (one fresh Pond per scenario)."""
    return {kind: gen.dump(gen.build(kind)) for kind in gen.KINDS}


def _doc(kind):
    return json.loads(gen.out_path(kind).read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", gen.KINDS)
def test_the_file_is_what_the_generator_writes(built, kind):
    assert built[kind] == gen.out_path(kind).read_text(encoding="utf-8")   # rerun the generator


@pytest.mark.parametrize("kind", gen.KINDS)
def test_the_file_is_whole(kind):
    doc = _doc(kind)
    assert doc["version"] == 1 and doc["source"] == gen.SOURCE and doc["format"]
    from mailbox import VERSION
    assert doc["pond"] == VERSION
    names = [c["name"] for c in doc["cases"]]
    assert len(names) >= gen.MIN_CASES[kind]
    assert len(set(names)) == len(names)


def test_every_outcome_appears_in_the_transcripts():
    seen = {c["outcome"] for kind in WIRE for c in _doc(kind)["cases"]}
    assert seen == set(gen.OUTCOMES)


@pytest.mark.parametrize("kind", WIRE)
def test_a_transcript_case_has_the_phone_shape(kind):
    for case in _doc(kind)["cases"]:
        assert case["act"]["kind"] in gen.ACT_KINDS, case["name"]
        assert set(case["act"]["args"]) == set(gen.ACT_ARGS[case["act"]["kind"]]), case["name"]
        assert case["request"]["protocol"] == 6, case["name"]
        assert case["request"]["ownerKey"] in (True, False, "unknown"), case["name"]
        assert case["request"].get("token", "ok") in ("ok", "wrong"), case["name"]
        assert case["outcome"] == gen.outcome(case["response"]["status"], case["response"]["body"]), case["name"]
        if case["act"]["kind"] == "skip":
            assert "rev" not in case["request"]["body"] and "hash" not in case["request"]["body"]


def test_no_token_or_owner_key_reaches_a_file(built):
    assert gen.TOKENS, "the generator recorded no Pond token"
    for text in built.values():
        for token in gen.TOKENS:
            assert token not in text
        for secret in gen.SECRETS:
            assert secret not in text


@pytest.mark.parametrize("case", _doc("history")["cases"], ids=lambda c: c["name"])
def test_a_history_case_reads_as_the_clients_check_reads_it(case):
    assert gen.client_verify(case["detail"], case["pins"]) == case["expect"]


@pytest.mark.parametrize("case", _doc("strictjson")["cases"], ids=lambda c: c["name"])
def test_a_strict_json_case_is_the_ponds_strict_loader(case):
    raw = bytes.fromhex(case["hex"]) if "hex" in case else case["text"].encode("utf-8")
    pond = gen.pond_strict(raw)
    expected = case.get("pond", {"parses": case["parses"], "body": case["body"]})
    assert {"parses": pond["parses"], "body": pond["body"]} == expected


def test_the_verify_and_legacy_tick_cases_are_there():
    names = {c["name"] for kind in gen.KINDS for c in _doc(kind)["cases"]}
    assert set(gen.SHARED_ID_CASES) == {"progress-verify", "progress-verify-changed", "progress-legacy-substep",
                                        "acts-tick-verify"}
    assert set(gen.SHARED_ID_CASES) <= names
