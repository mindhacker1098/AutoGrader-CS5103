import json
from pathlib import Path

import pytest
from autograder.core.corpus import FolderCorpus, corpus_hash, stripped_hash
from autograder.core.hashing import canonical_json, fnv1a32, hash_problem_inputs, hash_rules, sha256
from autograder.core.prng import mulberry32
from autograder.core.sandbox.base import classify_run, limited_command, safe_flags, signal_name

ROOT = Path(__file__).parents[3]
FIX = Path(__file__).parents[1] / "fixtures" / "golden"
PROBLEMS = ROOT / "seeds" / "problems"


def golden(name):
    return json.loads((FIX / name).read_text("utf-8"))


PRNG = golden("prng.json")
HASHES = golden("hashes.json")
SIM = golden("similarity.json")


@pytest.mark.parametrize("seed", list(PRNG))
def test_mulberry32_first_1000(seed):
    rng = mulberry32(float(seed) if "." in seed else int(seed))
    assert [rng() for _ in range(1000)] == PRNG[seed]


def test_fnv1a_utf16():
    for row in SIM["fnv1a"]:
        assert fnv1a32(row["input"]) == row["hash"], row["input"]


def test_sha256_strings():
    for row in HASHES["_sha256"]:
        assert sha256(row["input"]) == row["hex"]
    assert sha256(b"abc") == sha256("abc")


def test_canonical_json():
    for row in HASHES["_canonical"]:
        assert canonical_json(row["value"]) == row["canonical"]


@pytest.mark.parametrize("problem", ["add_two_integers", "sort_integers"])
def test_rules_and_inputs_hash(problem):
    rules = json.loads((PROBLEMS / problem / "rules.json").read_text("utf-8"))
    want = HASHES[problem]
    assert canonical_json(rules) == want["canonical_json"]
    assert hash_rules(rules) == want["rules_hash"]
    assert hash_problem_inputs(PROBLEMS / problem, rules, sep="\\") == want["inputs_hash"]
    v2 = hash_problem_inputs(PROBLEMS / problem, rules)
    assert v2.startswith("sha256v2:") and v2[9:] != want["inputs_hash"][7:]


def test_hashes_match_expected_reports():
    for problem in ("add_two_integers", "sort_integers"):
        for rep in (ROOT / "seeds" / "expected_reports" / problem).glob("*.json"):
            r = json.loads(rep.read_text("utf-8"))
            assert r["rules_hash"] == HASHES[problem]["rules_hash"]
            assert r["inputs_hash"] == HASHES[problem]["inputs_hash"]


@pytest.mark.parametrize("problem", ["add_two_integers", "sort_integers"])
def test_folder_corpus_order_and_exclusion(problem):
    for fname, row in SIM["compare_to_corpus"][problem].items():
        source = (PROBLEMS / problem / "submissions" / fname).read_bytes().decode("utf-8")
        items = FolderCorpus(PROBLEMS / problem, fname, "submissions/").peers(problem, source)
        assert [i.ref for i in items] == row["corpus_ids"], fname
        assert all(i.file != fname and stripped_hash(i.source) != stripped_hash(source) for i in items)
    assert corpus_hash(items).startswith("sha256:")


def test_limited_command_and_classify():
    assert limited_command("./prog", timeout_s=1, mem_mb=64) == (
        "timeout -s KILL 1 prlimit --fsize=16777216 --as=67108864 -- ./prog")
    assert limited_command("x", timeout_s=2.2, cpu_s=0.5, fsize_mb=2) == (
        "timeout -s KILL 3 prlimit --fsize=2097152 --cpu=1 -- x")
    assert classify_run(0, 10, 1) == ("ok", None)
    assert classify_run(137, 900, 1) == ("timeout", None)
    assert classify_run(137, 100, 1) == ("crash", 9)
    assert classify_run(139, 5, 1) == ("crash", 11)
    assert classify_run(1, 5, 1) == ("nonzero_exit", None)
    assert classify_run(0, 5, 1, host_killed=True) == ("timeout", None)
    assert signal_name(11) == "SIGSEGV" and signal_name(31) == "signal 31" and signal_name(None) == ""


def test_safe_flags():
    assert safe_flags("  -Wall   -Wextra -std=c11 ") == "-Wall -Wextra -std=c11"
    assert safe_flags(None) == ""
    with pytest.raises(ValueError):
        safe_flags("-O2; rm -rf /")
    with pytest.raises(ValueError):
        safe_flags("$(id)")
