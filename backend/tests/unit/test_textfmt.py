import json
import math
from pathlib import Path

import pytest
from autograder.core.textfmt import (
    i32,
    imul,
    dump_json,
    number_text,
    round_half_up,
    text_sort_key,
    trim_ws,
    locale_key,
    round1,
    round3,
    to_fixed,
    u32,
)

GOLDEN = json.loads((Path(__file__).parents[1] / "fixtures" / "golden" / "textfmt.json").read_text("utf-8"))


def _num(x):
    return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(x, x) if isinstance(x, str) else float(x)


@pytest.mark.parametrize("row", GOLDEN["round"], ids=lambda r: repr(r["x"]))
def test_round_matches_golden(row):
    x = float(row["x"])
    assert round_half_up(x) == float(row["round"])
    assert round1(x) == float(row["round1"])
    assert round3(x) == float(row["round3"])


def test_round_half_up_not_bankers():
    assert round_half_up(2.5) == 3 and round_half_up(-2.5) == -2 and round_half_up(0.5) == 1
    assert round_half_up(0.49999999999999994) == 0
    assert round1(82.25) == 82.3


@pytest.mark.parametrize("row", GOLDEN["to_fixed"], ids=lambda r: repr(r["x"]))
def test_to_fixed_matches_golden(row):
    for d, want in enumerate(row["fixed"]):
        assert to_fixed(float(row["x"]), d) == want, (row["x"], d)


def test_to_fixed_specials():
    assert to_fixed(1.005, 2) == "1.00"
    assert to_fixed(0.0625, 3) == "0.063"
    assert to_fixed(-0.0, 2) == "0.00"
    assert to_fixed(1e21, 2) == "1e+21"


@pytest.mark.parametrize("row", GOLDEN["num_str"], ids=lambda r: str(r["str"]))
def test_num_str_matches_golden(row):
    assert number_text(_num(row["x"])) == row["str"]


def test_num_str_ints_and_floats():
    assert number_text(99.0) == "99" and number_text(99) == "99"
    assert number_text(-0.0) == "0"
    assert number_text(1e-7) == "1e-7" and number_text(1e21) == "1e+21"
    assert number_text(123.456e30) == "1.23456e+32"


@pytest.mark.parametrize("i", range(len(GOLDEN["json"])))
def test_json_stringify_matches_golden(i):
    row = GOLDEN["json"][i]
    value = row["value"]
    assert dump_json(value) == row["json"]
    assert dump_json(value, indent=2) == row["json_indent2"]


def test_json_floats_and_strings():
    assert dump_json({"a": 18.0, "b": [0.1 + 0.2, None, True]}) == '{"a":18,"b":[0.30000000000000004,null,true]}'
    assert dump_json(math.nan) == "null"
    assert dump_json("\u00e9\u2028\x7f\x01") == '"\u00e9\u2028\x7f\\u0001"'


def test_locale_sort_matches_golden():
    ls = GOLDEN["locale_sort"]
    for key in ("corpus_names", "ascii", "mixed"):
        assert sorted(ls[key], key=locale_key) == ls[key], key
    assert sorted(ls["mixed"], key=text_sort_key) == ls["default_sort_mixed"]
    assert sorted(["add.c", "add_good.c"], key=locale_key) == ["add_good.c", "add.c"]


def test_imul_and_u32():
    for row in GOLDEN["imul"]:
        assert imul(row["a"], row["b"]) == row["imul"]
    assert u32(-1) == 0xFFFFFFFF and u32(4294967297) == 1 and u32(3.7) == 3
    assert i32(0xFFFFFFFF) == -1 and i32(0x7FFFFFFF) == 0x7FFFFFFF


def test_trim_strips_bom():
    assert trim_ws("\ufeff  int x;\u00a0 \n") == "int x;"
