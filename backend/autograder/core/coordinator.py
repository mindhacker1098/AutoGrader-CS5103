"""Grading coordinator: the agent that runs every analyzer and adds up the grade.

First pass (compile, smoke run, static sanity) in one sandbox, then every
metric's analyzer in parallel. exec analyzers are skipped when the program
does not compile or the smoke run times out. The grade is fixed before the
optional tutor note is written.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, get_settings
from . import findings as static_findings
from . import preanalysis
from .analyzers import REGISTRY
from .analyzers.base import GradeContext, LogFn
from .analyzers.codacy_review import codacy_params
from .analyzers.common import compile, describe_status, run_one
from .analyzers.static_analysis import parse_cppcheck, run_cppcheck
from .codacy import run_codacy
from .corpus import CorpusProvider, FolderCorpus, corpus_hash, read_text
from .cparse import static_sanity
from .textfmt import (
    dump_json,
    number_text,
    round_half_up,
    text_sort_key,
    trim_ws,
    round1,
    to_fixed,
)
from .llm import call_model, extract_json, resolve_model
from .manifest import check_permission
from .redact import hidden_inputs, hide_case
from .report import render_text
from .rules import hash_problem_inputs, hash_rules, sha256, validate_rules
from .sandbox.select import SandboxUnavailable, pin_settings, sandbox_ctx, tool_versions
from .usefulness import usefulness_limitations

FIRST_PASS_FLAGS = "-O2 -Wall -Wextra -std=c11"

TUTOR_SYSTEM = """You write a short note (3-4 sentences) to a student about their graded C program, using ONLY the facts given.
Be specific and encouraging. Do not invent test results. Do not state any grade other than the one given.
Respond with ONLY this JSON: {"note": "<3-4 sentences>"}"""

_CLAIMED_GRADE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:/\s*100|out of 100)")
_PROG_C = re.compile(r"\bprog\.c:")

# source-only metrics that mean nothing for a program that does not build (SR3)
QUALITY_ANALYZERS = {"static_analysis", "function_split", "similarity_flag", "codacy_review", "llm_review"}
NOT_COMPILED = "Not graded: the program does not compile."
NOT_RUN_COMPILE = "Not run: the program does not compile."
TEST_ANALYZERS = {"io_tests", "edge_case_runner", "complexity_estimator"}
STAGES = ("compile", "tests", "memory", "quality", "review", "done")


class StageLog:
    """Emits {"kind": "stage"} events, only ever moving forward."""

    def __init__(self, log: LogFn):
        self.log = log
        self.at = -1

    def __call__(self, stage: str) -> None:
        i = STAGES.index(stage)
        if i > self.at:
            self.at = i
            self.log({"kind": "stage", "msg": stage})


def _noop(_e: dict) -> None:
    pass


def _err(e: BaseException) -> str:
    return str(e) or type(e).__name__


def _now_ms() -> int:
    return int(time.perf_counter() * 1000)


def _js_slice(s: Any, n: int) -> str:
    """String.prototype.slice(0, n): counts UTF-16 code units."""
    s = "" if s is None else str(s)
    if all(ord(c) <= 0xFFFF for c in s):
        return s[:n]
    raw = s.encode("utf-16-le", "surrogatepass")[: 2 * n]
    return raw.decode("utf-16-le", "surrogatepass")


def _js_string(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return number_text(v)
    if isinstance(v, list):
        return ",".join("" if x is None else _js_string(x) for x in v)
    return "[object Object]"


def _read_opt(problem_dir: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    p = problem_dir / rel
    return read_text(p) if p.exists() else None


def _default_corpus(problem_dir: Path, rules: dict, filename: str) -> FolderCorpus:
    corpus_dir = None
    for m in rules.get("metrics") or []:
        if m.get("analyzer") == "similarity_flag":
            corpus_dir = (m.get("params") or {}).get("corpus_dir")
            break
    return FolderCorpus(problem_dir, filename, corpus_dir)


# Hook points for the v2 components. They are no-ops until those modules land.

async def run_pre_analysis(ctx: GradeContext, log: LogFn, defer_llm: bool = False) -> dict | None:
    """Denylist scan and LLM pre-read (core.preanalysis). Runs before the first pass; carries no weight."""
    return await preanalysis.run(ctx, log, defer_llm=defer_llm)


async def model_digests(rules: dict, settings: Settings) -> dict | None:
    """Ollama digests of the models the LLM metrics name, so a result can be tied to exact weights."""
    if settings.LLM_DISABLED:
        return None
    wanted = {resolve_model(m.get("params") or {}, settings)["model"]
              for m in rules.get("metrics") or [] if m.get("analyzer") == "llm_review"}
    wanted = {w for w in wanted if w}
    if not wanted:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(f"{settings.OLLAMA_HOST.rstrip('/')}/api/tags")
        tags = {m.get("name"): m.get("digest") for m in (r.json().get("models") or [])} if r.status_code < 400 else {}
    except (httpx.HTTPError, ValueError):
        tags = {}
    return {w: tags.get(w) for w in sorted(wanted)}


def environment(metrics: list[dict], tools: dict) -> tuple[dict, list[str]]:
    """What the grade depended on beyond the rules, and why it may differ in a full environment."""
    env: dict[str, Any] = {"sandbox": tools.get("sandbox"),
                           "valgrind": not str(tools.get("valgrind") or "unavailable").startswith("unavailable")}
    reasons = []
    for m in metrics:
        ev = m.get("evidence") or {}
        if m["status"] != "ok":
            continue
        if m["analyzer"] == "llm_review":
            env.setdefault("llm_review", ev.get("method"))
            if ev.get("method") == "heuristic (model unavailable)":
                reasons.append(f"{m['name']}: the model was unavailable, heuristic used")
        elif m["analyzer"] == "sanitizer_run" and ev.get("valgrind") is False:
            reasons.append(f"{m['name']}: valgrind not available in the {env['sandbox']} sandbox")
        elif m["analyzer"] == "complexity_estimator" and "wall-clock" in str(ev.get("method") or ""):
            reasons.append(f"{m['name']}: wall-clock timing instead of callgrind")
        elif m["analyzer"] == "codacy_review":
            env.setdefault("codacy", ev.get("mode") if ev.get("available") else "unavailable")
            if ev.get("available") is False:
                reasons.append(f"{m['name']}: Codacy unavailable ({ev.get('reason')}), metric not applied")
    return env, reasons


def compute_corpus_hash(ctx: GradeContext) -> str | None:
    try:
        return corpus_hash(ctx.corpus.peers(ctx.rules.get("problem"), ctx.source))
    except Exception:  # noqa: BLE001
        return None


async def _pinned(settings: Settings) -> Settings:
    """Decide the sandbox backend once per grade; pre-analysis and every analyzer then use the same one."""
    try:
        return await pin_settings(settings)
    except SandboxUnavailable:
        raise
    except RuntimeError:
        return settings


async def _first_pass(ctx: GradeContext, log: LogFn, smoke: bool = True, cppcheck: bool = False) -> dict:
    async with sandbox_ctx(ctx.settings, ctx.limits, log) as sb:
        tools = await tool_versions(sb)
        c = await compile(sb, ctx.source, "prog", FIRST_PASS_FLAGS)
        fp: dict[str, Any] = {
            "compile_output": c.stderr,
            "cppcheck_out": await run_cppcheck(sb, ctx.source) if cppcheck else None,
            "compiled": c.ok,
            "compile_warnings": c.warnings,
            "compile_errors": [] if c.ok else [_PROG_C.sub(f"{ctx.filename}:", ln)
                                               for ln in c.stderr.split("\n") if "error:" in ln][:5],
            "smoke_ok": False,
            "smoke_status": "not run",
            "suspected_infinite_loop": False,
            "sanity": static_sanity(ctx.source),
            "tools": tools,
        }
        if c.ok and not smoke:
            fp["smoke_status"] = "not run (blocked by pre-analysis)"
        elif c.ok:
            r = await run_one(sb, "prog", ctx.sample_input, ctx.limits)
            fp["smoke_status"] = describe_status(r)
            fp["smoke_ok"] = r.status == "ok"
            fp["smoke_ms"] = r.ms
            fp["suspected_infinite_loop"] = r.status == "timeout"
            fp["smoke_output"] = _js_slice(r.stdout, 500)
        return fp


async def _collect_findings(ctx: GradeContext, fp: dict, codacy: dict, share_codacy: bool, log: LogFn) -> None:
    """Compact, sorted findings for the LLM steps; cppcheck and Codacy output is shared with their analyzers."""
    cppcheck = parse_cppcheck(fp.get("cppcheck_out") or "")
    if fp.get("cppcheck_out") is not None:
        ctx.extra["cppcheck_out"] = fp["cppcheck_out"]
    if share_codacy:
        ctx.extra["codacy"] = codacy
    ctx.extra["codacy_versions"] = codacy.get("tool_versions") if codacy["available"] else \
        f"unavailable: {codacy.get('error')}"
    parts = {
        "codacy": static_findings.from_codacy(codacy),
        "cppcheck": static_findings.from_cppcheck(cppcheck),
        "gcc": static_findings.from_gcc(fp.get("compile_output") or ""),
    }
    ctx.extra["static_findings"] = static_findings.finding_lines([f for p in parts.values() for f in p])
    counts = ", ".join(f"{k} {len(v)}" for k, v in parts.items())
    log({"kind": "plan", "msg": f"static findings for the LLM: {len(ctx.extra['static_findings'])} line(s) "
                                f"({counts}{'' if codacy['available'] else ', codacy unavailable'})"})


async def grade_submission(problem_dir: str | Path, rules: dict, source: str, filename: str,
                           log: LogFn | None = None, *, corpus: CorpusProvider | None = None,
                           tutor_note: bool | None = None, settings: Settings | None = None,
                           inputs_hash_sep: str = "/", sandbox_pool: asyncio.Semaphore | None = None,
                           reveal_hidden_tests: bool = True) -> dict:
    """Grade one submission.

    tutor_note=None follows rules.feedback.llm_tutor_note. inputs_hash_sep="\\"
    gives the older Windows-style inputs_hash. sandbox_pool lets a worker share
    one sandbox limit across concurrent jobs. reveal_hidden_tests=False keeps
    hidden test data out of the tutor note.
    """
    log = log or _noop
    settings = await _pinned(settings or get_settings())
    problem_dir = Path(problem_dir)

    check_permission("fs:read", log)
    errors = validate_rules(rules, list(REGISTRY))
    if errors:
        raise ValueError("invalid rules:\n  - " + "\n  - ".join(errors))

    ctx = GradeContext(
        problem_dir=problem_dir,
        rules=rules,
        source=source,
        filename=filename,
        reference=_read_opt(problem_dir, rules.get("reference_solution")),
        starter=_read_opt(problem_dir, rules.get("starter_code")),
        sample_input=_read_opt(problem_dir, rules.get("sample_input")) or "",
        time_limit_s=rules["time_limit_s"],
        memory_limit_mb=rules["memory_limit_mb"],
        corpus=corpus or _default_corpus(problem_dir, rules, filename),
        pre=None,
        settings=settings,
    )
    time_limit = number_text(ctx.time_limit_s)
    hashes = {
        "submission_hash": f"sha256:{sha256(source)}",
        "rules_hash": hash_rules(rules),
        "inputs_hash": hash_problem_inputs(problem_dir, rules, inputs_hash_sep),
    }
    log({"kind": "plan", "msg": f'grading {filename} for "{rules["problem"]}" with {len(rules["metrics"])} metrics '
                                f'(rules {hashes["rules_hash"][:19]}...)'})

    # LLM steps with use_static_findings get Codacy, cppcheck and gcc findings collected before they run
    want = static_findings.wanted(rules)
    want["pre"] = want["pre"] and preanalysis.llm_enabled(rules, settings)
    need_findings = want["review"] or want["pre"]
    codacy_metric = next((m for m in rules["metrics"] if m.get("analyzer") == "codacy_review"), None)

    ctx.pre = await run_pre_analysis(ctx, log, defer_llm=True) if want["pre"] else await run_pre_analysis(ctx, log)

    stage = StageLog(log)
    stage("compile")
    log({"kind": "plan", "msg": "first pass: compile check -> smoke run on sample input -> static sanity"})
    pool = sandbox_pool or asyncio.Semaphore(max(1, int(settings.MAX_SANDBOXES or 3)))
    blocked = preanalysis.block_reason(ctx.pre)
    codacy_task = None
    if need_findings:
        cp = codacy_params((codacy_metric or {}).get("params") or {})
        codacy_task = asyncio.create_task(run_codacy(source, settings, log, mode=cp["mode"],
                                                     cloud_file_path=cp["cloud_file_path"]))
    try:
        async with pool:
            kw = {"cppcheck": True} if need_findings else {}
            fp = await (_first_pass(ctx, log, smoke=False, **kw) if blocked else _first_pass(ctx, log, **kw))
    except BaseException:
        if codacy_task:
            codacy_task.cancel()
        raise
    if codacy_task:
        await _collect_findings(ctx, fp, await codacy_task, codacy_metric is not None, log)
        if want["pre"]:
            await preanalysis.read(ctx, ctx.pre, log, ctx.extra["static_findings"])
    failed = ", ".join(k for k, v in fp["sanity"].items() if not v)
    log({
        "kind": "ok" if fp["compiled"] and fp["smoke_ok"] else "warn",
        "msg": f"first pass: compiled={'true' if fp['compiled'] else 'false'} ({fp['compile_warnings']} warning(s)), "
               f"smoke run: {fp['smoke_status']}, sanity: {failed or 'all checks pass'}",
    })

    skip_reason = None
    if blocked:
        skip_reason = blocked
    elif not fp["compiled"]:
        skip_reason = "the program does not compile"
    elif fp["suspected_infinite_loop"]:
        skip_reason = f"the smoke run exceeded {time_limit}s (suspected infinite loop)"
    if fp["compiled"] and not skip_reason and ((ctx.pre or {}).get("llm") or {}).get("extra_inputs"):
        async with pool:
            await preanalysis.verify(ctx, ctx.pre, log)
    llm_cx = ((ctx.pre or {}).get("verified") or {}).get("counterexamples") or []
    plan = []
    for m in rules["metrics"]:
        a = REGISTRY[m["analyzer"]]
        skip = bool(skip_reason) and a.kind == "exec"
        plan.append((m, a, "not_compiled" if not fp["compiled"] and m["analyzer"] in QUALITY_ANALYZERS else skip))
    msg = "dispatch plan: run " + ", ".join(m["name"] for m, _, skip in plan if not skip)
    if skip_reason:
        msg += f"; SKIP {', '.join(m['name'] for m, _, skip in plan if skip)} because {skip_reason}"
    log({"kind": "plan", "msg": msg})
    pending = {m["name"]: m["analyzer"] for m, _, skip in plan if not skip}

    def advance() -> None:
        left = set(pending.values())
        if left & TEST_ANALYZERS:
            stage("tests")
        elif "sanitizer_run" in left:
            stage("memory")
        else:
            stage("quality")

    advance()

    async def run_analyzer(analyzer, metric: dict, mlog: LogFn) -> dict:
        params = metric.get("params") or {}
        actx = dataclasses.replace(ctx, extra=dict(ctx.extra))
        if analyzer.kind == "source":
            return await analyzer.run(actx, params, mlog)
        async with pool, sandbox_ctx(settings, ctx.limits, mlog) as sb:
            actx.sandbox = sb
            return await analyzer.run(actx, params, mlog)

    async def run_metric(metric: dict, analyzer, skip: bool | str) -> dict:
        def mlog(e: dict) -> None:
            log({**e, "metric": metric["name"]})

        if skip == "not_compiled":
            return {"status": "skipped", "score": 0, "evidence": {}, "feedback": NOT_COMPILED}
        if skip:
            # other skip reasons keep their original wording
            feedback = NOT_RUN_COMPILE if not fp["compiled"] and not blocked else f"not run: {skip_reason}"
            return {"status": "skipped", "score": 0, "evidence": {}, "feedback": feedback}
        try:
            return await attempt_metric(metric, analyzer, mlog)
        finally:
            pending.pop(metric["name"], None)
            advance()

    async def attempt_metric(metric: dict, analyzer, mlog: LogFn) -> dict:
        started = _now_ms()
        for attempt in (1, 2):
            try:
                r = await run_analyzer(analyzer, metric, mlog)
                verdict = "review only" if r.get("informational") else number_text(round_half_up(r["score"] * 100)) + "%"
                mlog({"kind": "result", "msg": f"{metric['name']}: {verdict} - {r.get('feedback')}"})
                return {"status": "ok", **r, "ms": _now_ms() - started}
            except SandboxUnavailable:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    mlog({"kind": "warn",
                          "msg": f"{metric['name']}: infrastructure error ({trim_ws(_err(e))}); retrying once"})
                    continue
                mlog({"kind": "error", "msg": f"{metric['name']}: analyzer error: {_err(e)}"})
                return {"status": "error", "score": 0, "evidence": {"error": _err(e)},
                        "feedback": f"analyzer error: {_err(e)}"}
        raise AssertionError("unreachable")

    results = await asyncio.gather(*(run_metric(m, a, skip) for m, a, skip in plan))

    metrics = []
    for (metric, _, _), r in zip(plan, results):
        mparams = metric.get("params") or {}
        if llm_cx and metric["analyzer"] == "edge_case_runner" and r["status"] == "ok" \
                and mparams.get("include_llm_cases"):
            r = _with_llm_cases(r, mparams, llm_cx)
        metrics.append({
            "name": metric["name"],
            "analyzer": metric["analyzer"],
            "weight": metric["weight"],
            "status": r["status"],
            "score": r["score"],
            "informational": bool(r.get("informational")),
            "contribution": 0 if r.get("informational") else round1(r["score"] * metric["weight"] * 100),
            "evidence": r.get("evidence"),
            "feedback": r.get("feedback"),
            "duration_ms": r.get("ms") or 0,
        })
    # speed only means something for a program that produces correct answers
    if any(m["analyzer"] == "io_tests" and m["status"] == "ok" and m["score"] == 0 for m in metrics):
        for m in metrics:
            if m["analyzer"] == "complexity_estimator" and m["status"] == "ok" and m["score"] > 0:
                m["score"] = 0
                m["contribution"] = 0
                m["feedback"] = ("Not credited: the program passes none of the hidden tests, so its running time "
                                 "is not meaningful. " + (m["feedback"] or ""))

    # review-only metrics (no comparison data, AI-origin signal) add nothing; the remaining
    # weights are rescaled so the grade stays out of 100
    scored = [m for m in metrics if not m["informational"]]
    scored_weight = sum(m["weight"] for m in scored)
    total = sum(m["contribution"] for m in scored)
    if scored_weight and abs(scored_weight - 1) > 1e-9:
        total = total / scored_weight
        for m in scored:
            m["contribution"] = round1(m["contribution"] / scored_weight)
    grade = round1(total)

    flags: list[dict] = []
    if not fp["compiled"]:
        flags.append({"type": "compile_failed", "detail": (fp["compile_errors"] or ["compilation failed"])[0]})
    if fp["suspected_infinite_loop"]:
        flags.append({"type": "suspected_infinite_loop", "detail": f"sample input did not finish within {time_limit}s"})
    if not fp["sanity"]["hasMain"]:
        flags.append({"type": "missing_main", "detail": "no main() function found"})
    flags.extend(preanalysis.flags(ctx.pre))
    for (metric, _, _), r in zip(plan, results):
        flags.extend(r.get("flags") or [])
        if r["status"] == "error":
            flags.append({"type": "analyzer_error", "detail": f"{metric['name']}: {r['evidence']['error']}"})

    seen: set = set()
    counterexamples = []
    for r in [*results, {"counterexamples": llm_cx}]:
        for c in r.get("counterexamples") or []:
            key = c.get("input")
            if key in seen:
                continue
            seen.add(key)
            counterexamples.append(c)
    suggestions = list(dict.fromkeys(s for r in results for s in (r.get("suggestions") or [])))

    result: dict[str, Any] = {
        "submission": filename,
        "problem": rules["problem"],
        **hashes,
        "grade": grade,
        # a compile failure is a complete verdict, not an incomplete grading
        "partial": bool(skip_reason and fp["compiled"]) or bool(blocked)
                   or any(r["status"] == "error" for r in results),
        "first_pass": {
            "compiled": fp["compiled"],
            "compile_warnings": fp["compile_warnings"],
            "compile_errors": fp["compile_errors"],
            "smoke_ok": fp["smoke_ok"],
            "smoke_status": fp["smoke_status"],
            "suspected_infinite_loop": fp["suspected_infinite_loop"],
            "static_sanity": fp["sanity"],
        },
        "metrics": metrics,
        "flags": flags,
        "counterexamples": counterexamples,
        "suggestions": suggestions,
        "usefulness_limitations": usefulness_limitations(),
        "tool_versions": fp["tools"],
        "corpus_hash": compute_corpus_hash(ctx),
        "model_digests": await model_digests(rules, settings),
        "pre_analysis": ctx.pre,
        "provisional": False,
    }
    codacy_versions = ctx.extra.get("codacy_versions")
    for m in metrics:
        if m["analyzer"] == "codacy_review" and m["status"] == "ok":
            ev = m.get("evidence") or {}
            codacy_versions = ev.get("tool_versions") if ev.get("available") else f"unavailable: {ev.get('reason')}"
    if codacy_versions is not None:
        result["tool_versions"] = {**result["tool_versions"], "codacy": codacy_versions}
    if "static_findings" in ctx.extra:
        result["static_findings"] = ctx.extra["static_findings"]
    result["environment"], degraded = environment(metrics, fp["tools"])
    if degraded:
        # not reused by the result cache; a regrade in a full environment replaces it
        result["provisional"] = True
        result["provisional_reasons"] = degraded
        log({"kind": "warn", "msg": "provisional grade: " + "; ".join(degraded)})

    if ctx.pre:
        preanalysis.compare_complexity(ctx.pre, metrics)
        stage("review")
        if counterexamples and preanalysis.llm_enabled(rules, settings) and not ctx.pre["blocked"]:
            kw = {"findings": ctx.extra["static_findings"]} if "static_findings" in ctx.extra else {}
            ctx.pre["explanations"] = (await preanalysis.explain_failures(result, settings, log=log,
                                                                          **kw))["explanations"]

    want_note = tutor_note if tutor_note is not None else (rules.get("feedback") or {}).get("llm_tutor_note")
    if want_note:
        stage("review")
        result["tutor_note"] = await write_tutor_note(result, rules, log, settings,
                                                      reveal_hidden_tests=reveal_hidden_tests)

    verdict = f"GRADE {to_fixed(grade, 1)}/100 for {filename}"
    if result["partial"]:
        verdict += " (partial)"
    if flags:
        verdict += "; flags: " + ", ".join(f["type"] for f in flags)
    stage("done")
    log({"kind": "verdict", "msg": verdict})
    return result


def _with_llm_cases(r: dict, params: dict, cases: list[dict]) -> dict:
    """edge_case_runner with include_llm_cases: confirmed LLM inputs count like a failed random trial."""
    factor = params.get("random_failure_factor")
    factor = 0.8 if factor is None else factor
    score = round_half_up(r["score"] * factor * 1000) / 1000
    note = f"{len(cases)} LLM-suggested input(s) failed (execution-confirmed)"
    return {**r, "score": score, "feedback": f"{r.get('feedback') or ''} {note}.".strip(),
            "evidence": {**(r.get("evidence") or {}), "llm_cases_failed": len(cases)}}


def tutor_facts(result: dict, *, reveal_hidden_tests: bool = True) -> dict:
    weakest = sorted(result["metrics"], key=lambda m: m["score"])[:3]
    first = result["counterexamples"][0] if result["counterexamples"] else None
    if first and not reveal_hidden_tests and (first.get("source") == "hidden test"
                                              or first.get("input") in hidden_inputs(result)):
        first = hide_case(first)
    return {
        "grade": result["grade"],
        "weakest_metrics": [{"metric": m["name"], "feedback": m["feedback"]} for m in weakest],
        "first_failure": {
            "input": _js_slice(first.get("input"), 200),
            "expected": _js_slice(first.get("expected"), 200),
            "got": _js_slice(first.get("got"), 200),
            "why": _js_slice(first.get("why"), 300),
        } if first else None,
    }


async def write_tutor_note(result: dict, rules: dict, log: LogFn, settings: Settings | None = None, *,
                           reveal_hidden_tests: bool = True) -> dict | None:
    """Short LLM note for the student. It cannot change the grade; a note quoting another grade is dropped."""
    params = next((m.get("params") or {} for m in rules["metrics"] if m.get("analyzer") == "llm_review"), {})
    facts = tutor_facts(result, reveal_hidden_tests=reveal_hidden_tests)
    try:
        res = await call_model(TUTOR_SYSTEM, dump_json(facts), params, json=True, max_tokens=300, log=log,
                               settings=settings)
        parsed = extract_json(res["text"])
        if parsed is None:
            raise TypeError("Cannot read properties of null (reading 'note')")
        raw = parsed.get("note") if isinstance(parsed, dict) else None
        note = trim_ws(_js_string(raw) if raw else "")
        claimed = [float(m.group(1)) for m in _CLAIMED_GRADE.finditer(note)]
        if not note or any(abs(g - result["grade"]) > 0.5 for g in claimed):
            why = (f"claims {', '.join(number_text(g) for g in claimed)}/100 but the grade is {number_text(result['grade'])}"
                   if note else "empty")
            log({"kind": "security", "msg": f'tutor note rejected ({why}): "{_js_slice(note, 160)}"'})
            return None
        return {"text": note, "model": f"{res['provider']}/{res['model']}", "cached": res["cached"]}
    except Exception as e:  # noqa: BLE001
        log({"kind": "warn", "msg": f"tutor note skipped: {_err(e)}"})
        return None


def write_reports(result: dict, text: str | None, out_dir: str | Path, log: LogFn | None = None) -> dict:
    """Write <name>.json and <name>.txt; returns their paths."""
    check_permission("fs:write", log)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = re.sub(r"\.c$", "", result["submission"])
    json_path = out_dir / f"{base}.json"
    txt_path = out_dir / f"{base}.txt"
    json_path.write_text(dump_json(result, indent=2), "utf-8", newline="")
    txt_path.write_text(render_text(result) if text is None else text, "utf-8", newline="")
    return {"json": json_path, "txt": txt_path}


def load_problem(problem_dir: str | Path) -> dict:
    problem_dir = Path(problem_dir)
    rules = json.loads((problem_dir / "rules.json").read_text("utf-8"))
    statement_path = problem_dir / "problem.md"
    statement = read_text(statement_path) if statement_path.exists() else ""
    sub_dir = problem_dir / "submissions"
    subs = sorted((p.name for p in sub_dir.iterdir() if p.name.endswith(".c")), key=text_sort_key) if sub_dir.is_dir() else []
    return {"id": problem_dir.name, "dir": problem_dir, "rules": rules, "statement": statement, "submissions": subs}
