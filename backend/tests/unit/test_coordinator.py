"""Coordinator logic with fake analyzers and no sandbox."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from autograder.config import Settings
from autograder.core import coordinator as co
from autograder.core.textfmt import dump_json

ROOT = Path(__file__).resolve().parents[3]
ADD = ROOT / "seeds" / "problems" / "add_two_integers"


class Fake:
    def __init__(self, name, kind, score=1.0, fail=0, **extra):
        self.name, self.kind, self.description = name, kind, name
        self.score, self.fail, self.extra, self.calls = score, fail, extra, 0

    async def run(self, ctx, params, log):
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("docker hiccup ")
        assert (ctx.sandbox is None) == (self.kind == "source")
        return {"score": self.score, "evidence": {}, "feedback": f"{self.name} done", **self.extra}


def first_pass(compiled=True, smoke_status="ok"):
    async def fp(ctx, log):
        return {"compiled": compiled, "compile_warnings": 0,
                "compile_errors": [] if compiled else ["prog.c:1: error: boom"],
                "smoke_ok": smoke_status == "ok", "smoke_status": smoke_status if compiled else "not run",
                "suspected_infinite_loop": smoke_status == "timeout",
                "sanity": {"hasMain": True, "hasReader": True, "hasWriter": True, "mainHasReturn": True},
                "tools": {"sandbox": "fake"}}
    return fp


@asynccontextmanager
async def fake_sandbox(*a, **k):
    yield object()


def rules_with(metrics):
    return {"problem": "p", "time_limit_s": 1, "memory_limit_mb": 64, "reference_solution": "reference.c",
            "sample_input": "sample.in", "metrics": metrics}


@pytest.fixture
def env(monkeypatch, tmp_path):
    fakes = {
        "io_tests": Fake("io_tests", "exec", 0.8, counterexamples=[{"input": "1 2", "expected": "3", "got": "4",
                                                                      "why": "w"}], suggestions=["a", "b"]),
        "compiler_warnings": Fake("compiler_warnings", "build", 1.0, suggestions=["b"]),
        "llm_review": Fake("llm_review", "source", 0.7, flags=[{"type": "prompt_injection_attempt", "detail": "x"}]),
        "sanitizer_run": Fake("sanitizer_run", "exec", 0.5, fail=1),
        "similarity_flag": Fake("similarity_flag", "source", 1.0, fail=2),
    }
    monkeypatch.setattr(co, "REGISTRY", fakes)
    monkeypatch.setattr(co, "sandbox_ctx", fake_sandbox)
    s = Settings(LLM_DISABLED=True, LLM_CACHE_DIR=tmp_path / "cache", DATA_DIR=tmp_path)
    return fakes, s


SMALL = [
    {"name": "correctness", "analyzer": "io_tests", "weight": 0.5},
    {"name": "syntax", "analyzer": "compiler_warnings", "weight": 0.3},
    {"name": "readability", "analyzer": "llm_review", "weight": 0.2},
]

METRICS = [
    {"name": "correctness", "analyzer": "io_tests", "weight": 0.3},
    {"name": "syntax", "analyzer": "compiler_warnings", "weight": 0.2},
    {"name": "readability", "analyzer": "llm_review", "weight": 0.2},
    {"name": "memory", "analyzer": "sanitizer_run", "weight": 0.2},
    {"name": "originality", "analyzer": "similarity_flag", "weight": 0.1},
]


async def test_collation_retry_and_flags(env, monkeypatch):
    fakes, s = env
    monkeypatch.setattr(co, "_first_pass", first_pass())
    logs = []
    r = await co.grade_submission(ADD, rules_with(METRICS), "int main(){}", "x.c", logs.append, settings=s)
    by = {m["name"]: m for m in r["metrics"]}
    assert by["correctness"]["contribution"] == 24
    assert by["memory"]["status"] == "ok" and fakes["sanitizer_run"].calls == 2
    assert by["originality"]["status"] == "error" and by["originality"]["score"] == 0
    assert r["grade"] == 24 + 20 + 14 + 10
    assert r["partial"] is True
    assert [f["type"] for f in r["flags"]] == ["prompt_injection_attempt", "analyzer_error"]
    assert r["flags"][1]["detail"] == "originality: docker hiccup "
    assert r["suggestions"] == ["a", "b"]
    assert len(r["counterexamples"]) == 1
    assert any("infrastructure error (docker hiccup); retrying once" in e["msg"] for e in logs)
    assert logs[-1]["msg"] == "GRADE 68.0/100 for x.c (partial); flags: prompt_injection_attempt, analyzer_error"
    assert "tutor_note" not in r
    assert r["pre_analysis"]["denylist"] == [] and r["pre_analysis"]["llm"] is None
    assert r["provisional"] is False


async def test_skip_exec_when_not_compiled(env, monkeypatch):
    fakes, s = env
    monkeypatch.setattr(co, "_first_pass", first_pass(compiled=False))
    r = await co.grade_submission(ADD, rules_with(SMALL), "x", "x.c", settings=s)
    m = r["metrics"][0]
    # API v2: sentence case, and a compile failure is a complete verdict (not partial)
    assert m["status"] == "skipped" and m["feedback"] == "Not run: the program does not compile."
    assert r["partial"] is False
    assert fakes["io_tests"].calls == 0 and fakes["compiler_warnings"].calls == 1
    assert r["flags"][0] == {"type": "compile_failed", "detail": "prog.c:1: error: boom"}


async def test_skip_on_smoke_timeout(env, monkeypatch):
    _, s = env
    monkeypatch.setattr(co, "_first_pass", first_pass(smoke_status="timeout"))
    r = await co.grade_submission(ADD, rules_with(SMALL), "x", "x.c", settings=s)
    assert r["metrics"][0]["feedback"] == "not run: the smoke run exceeded 1s (suspected infinite loop)"
    assert r["partial"] is True
    assert r["flags"][0]["detail"] == "sample input did not finish within 1s"


async def test_invalid_rules(env):
    _, s = env
    with pytest.raises(ValueError, match="invalid rules"):
        await co.grade_submission(ADD, rules_with([{"name": "a", "analyzer": "nope", "weight": 1}]), "", "x.c",
                                  settings=s)


def _result(grade):
    return {"grade": grade, "metrics": [{"name": "a", "score": 0.5, "feedback": "fa"},
                                        {"name": "b", "score": 0.2, "feedback": "fb"}],
            "counterexamples": []}


async def test_tutor_note_grade_guard(tmp_path, monkeypatch):
    replies = iter(['{"note": "You got 50/100, nice."}', '{"note": " Your grade is 82.3 out of 100. "}'])

    async def fake_call(system, user, params, **kw):
        assert system == co.TUTOR_SYSTEM and kw["max_tokens"] == 300
        assert json.loads(user)["weakest_metrics"][0]["metric"] == "b"
        return {"provider": "ollama", "model": "m", "text": next(replies), "cached": True}

    monkeypatch.setattr(co, "call_model", fake_call)
    logs = []
    rules = {"metrics": []}
    assert await co.write_tutor_note(_result(82.3), rules, logs.append) is None
    assert logs[-1]["kind"] == "security"
    note = await co.write_tutor_note(_result(82.3), rules, logs.append)
    assert note == {"text": "Your grade is 82.3 out of 100.", "model": "ollama/m", "cached": True}


def test_tutor_facts_match_node_cache():
    node = json.loads((ROOT / "seeds" / "expected_reports" / "add_two_integers" / "add.json").read_text("utf-8"))
    facts = co.tutor_facts(node)
    assert facts["grade"] == 82.3 and facts["weakest_metrics"][0]["metric"] == "modularity"
    assert dump_json(facts).startswith('{"grade":82.3,"weakest_metrics":[')
    first = node["counterexamples"][0]
    hidden = co.tutor_facts({**node, "counterexamples": [{**first, "source": "hidden test"}]},
                            reveal_hidden_tests=False)["first_failure"]
    assert hidden["input"] == hidden["expected"] == hidden["got"] == "(hidden test)"
    assert "Expected" not in hidden["why"]


def test_environment_marks_degraded_grades_provisional():
    metrics = [{"name": "readability", "analyzer": "llm_review", "status": "ok",
                "evidence": {"method": "heuristic (model unavailable)"}},
               {"name": "memory", "analyzer": "sanitizer_run", "status": "ok", "evidence": {"valgrind": False}}]
    env, why = co.environment(metrics, {"sandbox": "wsl", "valgrind": "unavailable"})
    assert env["sandbox"] == "wsl" and env["valgrind"] is False and len(why) == 2
    metrics[0]["evidence"]["method"] = "llm"
    metrics[1]["evidence"]["valgrind"] = True
    assert co.environment(metrics, {"sandbox": "docker", "valgrind": "valgrind-3.22.0"})[1] == []


def test_load_problem_and_write_reports(tmp_path):
    p = co.load_problem(ADD)
    assert p["id"] == "add_two_integers" and p["submissions"][0] == "add.c" and "add_good.c" in p["submissions"]
    out = co.write_reports({"submission": "a.c"}, "text", tmp_path)
    assert out["json"].name == "a.json" and out["txt"].read_text() == "text"
    assert json.loads(out["json"].read_text()) == {"submission": "a.c"}


QUALITY = [
    {"name": "correctness", "analyzer": "io_tests", "weight": 0.4},
    {"name": "syntax", "analyzer": "compiler_warnings", "weight": 0.2},
    {"name": "readability", "analyzer": "llm_review", "weight": 0.2},
    {"name": "originality", "analyzer": "similarity_flag", "weight": 0.2},
]


async def test_quality_metrics_score_zero_without_a_build(env, monkeypatch):
    fakes, s = env
    fakes["similarity_flag"].fail = 0
    monkeypatch.setattr(co, "_first_pass", first_pass(compiled=False))
    r = await co.grade_submission(ADD, rules_with(QUALITY), "x", "x.c", settings=s)
    by = {m["name"]: m for m in r["metrics"]}
    for name in ("readability", "originality"):
        assert by[name]["score"] == 0 and by[name]["feedback"] == co.NOT_COMPILED
    assert fakes["llm_review"].calls == 0 and fakes["similarity_flag"].calls == 0
    # compiler_warnings still runs and reports the errors
    assert by["syntax"]["status"] == "ok" and fakes["compiler_warnings"].calls == 1
    assert r["grade"] == 20 and r["partial"] is False


async def test_quality_metrics_run_when_compiled(env, monkeypatch):
    fakes, s = env
    fakes["similarity_flag"].fail = 0
    monkeypatch.setattr(co, "_first_pass", first_pass())
    r = await co.grade_submission(ADD, rules_with(QUALITY), "x", "x.c", settings=s)
    assert all(m["feedback"] != co.NOT_COMPILED for m in r["metrics"])
    assert fakes["llm_review"].calls == 1


async def test_stage_events_move_forward(env, monkeypatch):
    fakes, s = env
    fakes["similarity_flag"].fail = 0
    monkeypatch.setattr(co, "_first_pass", first_pass())
    logs = []
    await co.grade_submission(ADD, rules_with(METRICS), "x", "x.c", logs.append, settings=s)
    stages = [e["msg"] for e in logs if e["kind"] == "stage"]
    assert stages[0] == "compile" and stages[-1] == "done" and "tests" in stages
    assert [co.STAGES.index(x) for x in stages] == sorted(co.STAGES.index(x) for x in stages)
    assert len(stages) == len(set(stages))


async def test_compile_errors_use_student_filename(monkeypatch):
    from autograder.core.analyzers.common import CompileResult

    async def fake_compile(sb, source, bin, flags, **kw):
        return CompileResult(ok=False, stderr="prog.c: In function 'main':\nprog.c:3:5: error: expected ';'\n",
                             warnings=0, errors=1)

    async def fake_versions(sb):
        return {"sandbox": "fake"}

    monkeypatch.setattr(co, "sandbox_ctx", fake_sandbox)
    monkeypatch.setattr(co, "compile", fake_compile)
    monkeypatch.setattr(co, "tool_versions", fake_versions)
    ctx = co.GradeContext(problem_dir=ADD, rules={}, source="int main(){}", filename="my_add.c", reference=None,
                          starter=None, sample_input="", time_limit_s=1, memory_limit_mb=64, corpus=None, pre=None,
                          settings=Settings())
    fp = await co._first_pass(ctx, lambda e: None)
    assert fp["compile_errors"] == ["my_add.c:3:5: error: expected ';'"]
