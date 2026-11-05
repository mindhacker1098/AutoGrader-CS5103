"""Job handlers, one per job kind."""
from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session

from ..config import Settings
from ..core.analyzers.base import LogFn
from ..core.code_feedback import compiler_issues, review_source
from ..core.coordinator import grade_submission
from ..core.cparse import normalized_tokens
from ..core.feedback_validation import verify_repair
from ..core.report import render_text
from ..core.similarity import fingerprints_with_pos
from ..db import repo
from ..db.models import (
    AuditLog,
    Job,
    Problem,
    ProblemVersion,
    RankingProfile,
    Result,
    Submission,
    utcnow,
)
from .dbcorpus import DbCorpus
from .queue import JobQueue


@dataclass
class JobContext:
    job: Job
    queue: JobQueue
    settings: Settings
    log: LogFn
    sandbox_pool: asyncio.Semaphore
    llm_pool: asyncio.Semaphore

    @property
    def payload(self) -> dict:
        return self.job.payload or {}


Handler = Callable[[JobContext], Awaitable[dict]]


class JobFailed(Exception):
    """The job finished without a usable result; it is marked failed and not retried."""

    def __init__(self, msg: str, result: dict | None = None):
        super().__init__(msg)
        self.result = result


async def _maybe_await(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call fn whether it is async or blocking (blocking ones go to a thread)."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    r = await asyncio.to_thread(fn, *args, **kwargs)
    return await r if inspect.isawaitable(r) else r


async def _with_session(jc: JobContext, fn: Callable[..., Any], *args: Any) -> Any:
    """fn(session, *args); async functions run on the loop, blocking ones in a thread."""
    if inspect.iscoroutinefunction(fn):
        with jc.queue.session() as s:
            return await fn(s, *args)
    return await jc.queue.call(fn, *args)


def _submission_id(jc: JobContext) -> int:
    sid = jc.job.submission_id or jc.payload.get("submission_id")
    if not sid:
        raise ValueError(f"job {jc.job.id} has no submission_id")
    return int(sid)


def _load_version(s: Session, problem_id: int, version_id: int | None) -> ProblemVersion:
    if version_id:
        pv = s.get(ProblemVersion, version_id)
    else:
        problem = s.get(Problem, problem_id)
        pv = repo.current_version(s, problem) if problem else None
    if pv is None:
        raise ValueError(f"no problem version for problem {problem_id}")
    return pv


# grade / regrade

def _frozen_peers(payload: dict) -> list[int] | None:
    ids = payload.get("peer_ids")
    if ids is None:
        ids = payload.get("frozen_peer_ids")
    return ids



async def handle_grade(jc: JobContext) -> dict:
    sub_id = _submission_id(jc)
    p = jc.payload
    regrade = jc.job.kind == "regrade"

    def load(s: Session):
        sub = s.get(Submission, sub_id)
        if sub is None:
            raise ValueError(f"submission {sub_id} not found")
        # a first grade uses the version the student submitted against; a regrade
        # (staff regrade, finalize) uses the problem's current rules and tests
        version_id = p.get("problem_version_id") or (None if regrade else sub.problem_version_id)
        pv = _load_version(s, sub.problem_id, version_id)
        corpus = DbCorpus(s, sub.problem_id, sub.user_id, pv.dir_path, peer_ids=_frozen_peers(p))
        problem = s.get(Problem, sub.problem_id)
        return sub, pv, corpus, problem.reveal_hidden_tests if problem else True

    sub, pv, corpus, reveal = await jc.queue.call(load)
    problem_dir = Path(pv.dir_path)
    rules = pv.rules_json
    # A four-hash match does not identify the compiler, sandbox image or grader code.
    # Always measure afresh until a complete execution identity is stored in the DB.
    # Model-response caching below the grader remains available with digest validation.

    result = await grade_submission(problem_dir, rules, sub.source, sub.filename, jc.log, corpus=corpus,
                                    settings=jc.settings, sandbox_pool=jc.sandbox_pool,
                                    reveal_hidden_tests=reveal, tutor_note=False)
    result["problem_version_id"] = pv.id
    result["code_feedback"] = {"status": "queued", "issues": compiler_issues(sub.source, result), "reviews": []}
    row = await jc.queue.call(repo.store_result, sub.id, jc.job.id, result, render_text(result))
    await jc.queue.call(repo.store_fingerprints, sub.problem_id, sub.id, fingerprints_with_pos(sub.source))
    await _enqueue_followups(jc, sub, row.id, enrich=True)
    jc.log({"kind": "stored", "msg": f"result {row.id} stored: grade {result['grade']}"})
    return {"result_id": row.id, "grade": result["grade"], "cached": False}


async def _copy_cached(jc: JobContext, sub: Submission, cached: Result) -> dict:
    if cached.submission_id == sub.id and cached.is_current:
        jc.log({"kind": "cache", "msg": f"result {cached.id} is current and up to date; nothing to do"})
        return {"result_id": cached.id, "grade": cached.grade, "cached": True}
    report = dict(cached.report_json or {})
    report["submission"] = sub.filename
    report["cached_from"] = cached.id
    row = await jc.queue.call(repo.store_result, sub.id, jc.job.id, report, render_text(report),
                              enrichment=cached.enrichment_json)
    await jc.queue.call(repo.store_fingerprints, sub.problem_id, sub.id, fingerprints_with_pos(sub.source))
    await _enqueue_followups(jc, sub, row.id, enrich=cached.enrichment_json is None)
    jc.log({"kind": "stage", "msg": "done"})
    jc.log({"kind": "cache", "msg": f"same submission, rules, inputs and corpus as result {cached.id}; "
                                    f"copied as result {row.id} (grade {cached.grade})"})
    return {"result_id": row.id, "grade": cached.grade, "cached": True}


async def _enqueue_followups(jc: JobContext, sub: Submission, result_id: int, *, enrich: bool) -> None:
    common = {"submission_id": sub.id, "problem_id": sub.problem_id}
    if enrich:
        await jc.queue.enqueue("enrich", priority=5, payload={"result_id": result_id}, **common)
    await jc.queue.enqueue("profile", priority=3, payload={"result_id": result_id}, **common)


# enrich / profile

def _result_for(s: Session, jc: JobContext) -> Result:
    rid = jc.payload.get("result_id")
    row = s.get(Result, rid) if rid else repo.current_result(s, _submission_id(jc))
    if row is None:
        raise ValueError(f"no result for job {jc.job.id}")
    return row


async def handle_enrich(jc: JobContext) -> dict:
    try:
        from ..core.preanalysis.llm_pre import explain_failures
    except ImportError as e:
        jc.log({"kind": "warn", "msg": f"enrichment not available: {e}"})
        return {"skipped": "llm_pre not available"}

    row = await jc.queue.call(_result_for, jc)
    def review_context(session):
        sub = session.get(Submission, row.submission_id)
        version = _load_version(session, sub.problem_id,
                                (row.report_json or {}).get("problem_version_id") or sub.problem_version_id)
        path = Path(version.dir_path) / "problem.md"
        return sub.source, path.read_text(encoding="utf-8") if path.exists() else "", Path(version.dir_path), version.rules_json
    source, statement, problem_dir, rules = await jc.queue.call(review_context)
    async def verify_change(candidate):
        async with jc.sandbox_pool:
            return await verify_repair(problem_dir, rules, source, candidate, jc.settings)
    async with jc.llm_pool:
        report = row.report_json or {}
        feedback = await review_source(source, statement, report, jc.settings, jc.log, verify_change=verify_change)
        def save_feedback(session):
            current = session.get(Result, row.id)
            updated = {**(current.report_json or {}), "code_feedback": feedback}
            updated.pop("tutor_note", None)  # Superseded by source-specific feedback, not generic praise.
            current.report_json = updated
            current.report_txt = render_text(updated)
            session.add(current)
            session.commit()
        await jc.queue.call(save_feedback)
        # ground the explanations in the same static findings the grade collected, if any
        kw = {"findings": report["static_findings"]} if report.get("static_findings") is not None else {}
        enrichment = await _maybe_await(explain_failures, report, jc.settings, **kw)

    def save(s: Session) -> None:
        r = s.get(Result, row.id)
        r.enrichment_json = enrichment
        r.report_json = {**(r.report_json or {}), "enrichment": enrichment}
        r.report_txt = render_text(r.report_json)
        s.add(r)
        s.commit()

    await jc.queue.call(save)
    jc.log({"kind": "stored", "msg": f"enrichment stored on result {row.id}"})
    return {"result_id": row.id}


async def handle_profile(jc: JobContext) -> dict:
    try:
        from ..core.profile import profile_submission
    except ImportError as e:
        jc.log({"kind": "warn", "msg": f"profiling not available: {e}"})
        return {"skipped": "profile not available"}

    sub_id = _submission_id(jc)

    def load(s: Session):
        sub = s.get(Submission, sub_id)
        res = repo.current_result(s, sub_id)
        report = res.report_json if res else None
        pv = _load_version(s, sub.problem_id, (report or {}).get("problem_version_id") or sub.problem_version_id)
        return sub, pv, report

    sub, pv, report = await jc.queue.call(load)
    async with jc.sandbox_pool:
        prof = await _maybe_await(profile_submission, Path(pv.dir_path), pv.rules_json, sub.source, jc.settings)
    prof = dict(prof or {})
    if prof.get("readability") is None and report:
        prof["readability"] = next((m["score"] for m in report.get("metrics") or []
                                    if m.get("analyzer") == "llm_review"), None)
    if prof.get("token_count") is None:
        prof["token_count"] = len(normalized_tokens(sub.source))

    def save(s: Session) -> None:
        rp = s.get(RankingProfile, sub_id) or RankingProfile(submission_id=sub_id)
        for k in ("instr_at_max_n", "peak_heap_bytes", "token_count", "readability"):
            setattr(rp, k, prof.get(k))
        rp.computed_at = utcnow()
        s.add(rp)
        s.commit()

    await jc.queue.call(save)
    jc.log({"kind": "stored", "msg": f"ranking profile stored for submission {sub_id}"})
    if prof.get("error"):
        # a program that crashes or hangs cannot be measured; that is a result, not a job failure
        jc.log({"kind": "warn", "msg": f"ranking profile incomplete: {prof['error']}"})
    keys = ("instr_at_max_n", "peak_heap_bytes", "token_count", "readability", "error")
    return {k: prof.get(k) for k in keys if prof.get(k) is not None or k != "error"}


# staff jobs

async def handle_plagiarism_scan(jc: JobContext) -> dict:
    from ..core.plagiarism.pairwise import run_scan

    problem_id = jc.job.problem_id or jc.payload.get("problem_id")
    threshold = float(jc.payload.get("threshold", 0.85))
    jc.log({"kind": "plan", "msg": f"plagiarism scan for problem {problem_id} at threshold {threshold}"})
    out = await _with_session(jc, run_scan, problem_id, threshold)
    scan_id = getattr(out, "id", out if isinstance(out, int) else None)

    def audit(s: Session) -> None:
        problem = s.get(Problem, problem_id)
        s.add(AuditLog(actor_id=jc.payload.get("requested_by"), action="plagiarism.scan",
                       target=problem.slug if problem else str(problem_id),
                       detail={"scan_id": scan_id, "threshold": threshold, "background": True, "job_id": jc.job.id}))
        s.commit()

    await jc.queue.call(audit)
    jc.log({"kind": "stored", "msg": f"plagiarism scan {scan_id} finished"})
    return {"scan_id": scan_id}


async def handle_corpus_gen(jc: JobContext) -> dict:
    from ..core.aidetect.corpus_gen import generate

    problem_id = jc.job.problem_id or jc.payload.get("problem_id")
    pv = await jc.queue.call(_load_version, problem_id, jc.payload.get("problem_version_id"))
    params = {k: v for k, v in jc.payload.items() if k != "problem_version_id"}
    log, granted = quiet_permissions(jc.log)
    async with jc.llm_pool, jc.sandbox_pool:
        entries = await _maybe_await(generate, Path(pv.dir_path), params, jc.settings, log)
    if granted:
        jc.log({"kind": "perm-allow", "msg": "permissions granted: " +
                ", ".join(f"{cap} x{n}" for cap, n in sorted(granted.items()))})
    out = corpus_summary(entries or [])
    jc.log({"kind": "stored", "msg": f"corpus: {out['kept']} of {out['requested']} kept, {out['failed']} failed"})
    if out["kept"] == 0 and out["failed"] > 0:
        raise JobFailed(f"No corpus file was kept: {', '.join(out['reasons']) or 'every request failed'}.", out)
    return out


def quiet_permissions(log: LogFn) -> tuple[LogFn, dict[str, int]]:
    """Counts perm-allow events instead of logging each one (corpus_gen makes hundreds)."""
    granted: dict[str, int] = {}

    def wrapped(e: dict) -> None:
        if e.get("kind") == "perm-allow":
            m = re.search(r'"([^"]+)"', str(e.get("msg") or ""))
            cap = m.group(1) if m else "?"
            granted[cap] = granted.get(cap, 0) + 1
            return
        log(e)

    return wrapped, granted


def corpus_summary(entries: list[dict]) -> dict:
    """{requested, kept, failed, reasons}; near-duplicates are dropped on purpose and do not count as failures."""
    reasons: dict[str, int] = {}
    failed = 0
    for e in entries:
        if e.get("kept"):
            continue
        why = str(e.get("reason") or "unknown error")
        why = "model error" if why.startswith("model error") else why
        reasons[why] = reasons.get(why, 0) + 1
        failed += why != "near-duplicate"
    return {"requested": len(entries), "kept": sum(1 for e in entries if e.get("kept")), "failed": failed,
            "reasons": [f"{k} ({n})" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])]}


HANDLERS: dict[str, Handler] = {
    "grade": handle_grade,
    "regrade": handle_grade,
    "enrich": handle_enrich,
    "profile": handle_profile,
    "plagiarism_scan": handle_plagiarism_scan,
    "corpus_gen": handle_corpus_gen,
}
