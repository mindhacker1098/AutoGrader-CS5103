"""Pre-publish check of a problem: the rules validate, the reference compiles and runs every test cleanly."""
from __future__ import annotations

from pathlib import Path

from .analyzers import REGISTRY
from .analyzers.common import BASE_FLAGS, compile, outputs_match, run_batch, run_one
from .corpus import read_text
from .textfmt import text_sort_key
from .rules import validate_rules
from .sandbox.select import sandbox_ctx

SAMPLE_MAX = 4000


def _limits(rules: dict) -> dict:
    return {"time_limit_s": rules.get("time_limit_s") or 1, "memory_limit_mb": rules.get("memory_limit_mb") or 64}


def tests_dir(problem_dir: Path, rules: dict) -> Path:
    rel = "tests"
    for m in rules.get("metrics") or []:
        if isinstance(m, dict) and m.get("analyzer") == "io_tests":
            rel = (m.get("params") or {}).get("testset_dir") or rel
    return problem_dir / rel.strip("/\\")


async def sample_output(problem_dir: str | Path, rules: dict, settings=None) -> str | None:
    """The reference's output on the sample input, or None when it cannot be produced."""
    problem_dir = Path(problem_dir)
    ref = problem_dir / (rules.get("reference_solution") or "")
    sample = problem_dir / (rules.get("sample_input") or "")
    if not rules.get("reference_solution") or not ref.is_file() or not rules.get("sample_input") or not sample.is_file():
        return None
    async with sandbox_ctx(settings, _limits(rules)) as sb:
        c = await compile(sb, read_text(ref), "ref", BASE_FLAGS)
        if not c.ok:
            return None
        r = await run_one(sb, "ref", read_text(sample), _limits(rules))
    return r.stdout[:SAMPLE_MAX] if r.status == "ok" else None


async def check_problem(problem_dir: str | Path, rules: dict, settings=None) -> dict:
    """{ok, rules_errors, compile: {ok, stderr}, tests: [{name, ok, ms, error}], sample_output}."""
    problem_dir = Path(problem_dir)
    out: dict = {"ok": False, "rules_errors": validate_rules(rules, list(REGISTRY)),
                 "compile": {"ok": False, "stderr": ""}, "tests": [], "sample_output": None}
    ref_name = rules.get("reference_solution") or ""
    ref = problem_dir / ref_name
    if not ref_name or not ref.is_file():
        out["compile"]["stderr"] = "The reference solution file is missing."
        return out
    tdir = tests_dir(problem_dir, rules)
    names = sorted((p.name for p in tdir.glob("*.in")), key=text_sort_key) if tdir.is_dir() else []
    limits = _limits(rules)
    async with sandbox_ctx(settings, limits) as sb:
        c = await compile(sb, read_text(ref), "ref", BASE_FLAGS)
        out["compile"] = {"ok": c.ok, "stderr": c.stderr.replace("ref.c:", f"{Path(ref_name).name}:")[:4000]}
        if not c.ok:
            return out
        sample = problem_dir / (rules.get("sample_input") or "")
        if rules.get("sample_input") and sample.is_file():
            r = await run_one(sb, "ref", read_text(sample), limits)
            out["sample_output"] = r.stdout[:SAMPLE_MAX] if r.status == "ok" else None
        runs = await run_batch(sb, "ref", [read_text(tdir / n) for n in names], limits)
    for name, r in zip(names, runs):
        error = None if r.status == "ok" else r.label
        expected = tdir / (name[:-3] + ".out")
        if error is None and expected.is_file() and not outputs_match(read_text(expected), r.stdout):
            error = f"output differs from {expected.name}"
        out["tests"].append({"name": name, "ok": error is None, "ms": r.ms, "error": error})
    if not names:
        out["tests"].append({"name": "(none)", "ok": False, "ms": 0, "error": "the problem has no test inputs"})
    out["ok"] = not out["rules_errors"] and all(t["ok"] for t in out["tests"])
    return out
