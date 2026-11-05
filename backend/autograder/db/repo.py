"""Small data-access helpers on top of the SQLModel tables."""
from __future__ import annotations

import json
import re
import secrets
import shutil
from collections.abc import Iterable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import bcrypt
from sqlalchemy import func, update
from sqlmodel import Session, select

from ..config import get_settings
from ..core.hashing import hash_problem_inputs, hash_rules, sha256
from ..core.textfmt import round1
from .models import (
    PRIORITY,
    ROLES,
    Fingerprint,
    Flag,
    Job,
    JobEvent,
    MetricResult,
    Problem,
    ProblemVersion,
    Result,
    Submission,
    User,
    utcnow,
)

COPY_IGNORE = shutil.ignore_patterns("submissions", "__pycache__", "*.pyc")

FLAG_SEVERITY = {
    "suspected_copy_peer": "high",
    "suspected_copy_web": "high",
    "suspected_copy_llm": "high",
    "prompt_injection_attempt": "high",
    "dangerous_call": "high",
    "possible_copy_low_confidence": "low",
    "possible_ai_generated": "low",
}


# users

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("ascii"))
    except ValueError:
        return False


def generate_password(n: int = 10) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


ROLL_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
PW_MIN, PW_MAX = 8, 72
PW_POLICY = f"Password must be {PW_MIN} to {PW_MAX} characters."


def normalize_roll(roll_no: str) -> str:
    return (roll_no or "").strip().upper()


def roll_error(roll_no: str) -> str | None:
    """Why a (normalised) roll number is not acceptable, or None."""
    if not roll_no:
        return "Roll number is required."
    if not ROLL_RE.match(roll_no):
        return "Roll number must be 2 to 32 letters, digits, - or _, starting with a letter or digit."
    return None


def get_user_by_roll(session: Session, roll_no: str) -> User | None:
    return session.exec(select(User).where(func.upper(User.roll_no) == normalize_roll(roll_no))).first()


def create_user(session: Session, roll_no: str, name: str, role: str = "student",
                password: str | None = None, *, must_change_pw: bool = True) -> User:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
    roll_no = normalize_roll(roll_no)
    err = roll_error(roll_no)
    if err:
        raise ValueError(err)
    if get_user_by_roll(session, roll_no):
        raise ValueError(f"A user with roll number {roll_no} already exists.")
    user = User(roll_no=roll_no, name=name.strip(), role=role,
                password_hash=hash_password(password) if password else "", must_change_pw=must_change_pw)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def revoke_sessions(user: User) -> None:
    """Every token issued before this call stops working (checked in api/deps.py)."""
    user.token_version = (user.token_version or 0) + 1


def plan_import(session: Session, rows: Iterable[dict], *, allow_staff: bool = True) -> list[dict]:
    """Classify roster rows as create / skip / reject without writing anything."""
    out = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        roll = normalize_roll(row.get("roll_no") or "")
        name = " ".join((row.get("name") or "").split())
        role = (row.get("role") or "").strip().lower() or "student"
        if not roll and not name:
            continue
        item = {"line": row.get("_line") or i + 2, "roll_no": roll, "name": name, "role": role,
                "status": "create", "reason": ""}
        reason = roll_error(roll)
        if reason is None and not name:
            reason = "Name is required."
        if reason is None and role not in ROLES:
            reason = f"Role must be one of {', '.join(ROLES)}."
        if reason is None and role != "student" and not allow_staff:
            reason = "Only an admin can add staff accounts."
        if reason is None and roll in seen:
            reason = "This roll number appears earlier in the file."
        if reason:
            item.update(status="reject", reason=reason)
        elif get_user_by_roll(session, roll):
            item.update(status="skip", reason="Already registered.")
        seen.add(roll)
        out.append(item)
    return out


def import_roster(session: Session, rows: Iterable[dict], *, allow_staff: bool = True) -> list[dict]:
    """Create the 'create' rows of plan_import; each gets the given or a generated password."""
    rows = list(rows)
    given = {normalize_roll(r.get("roll_no") or ""): (r.get("password") or "").strip() for r in rows}
    out = plan_import(session, rows, allow_staff=allow_staff)
    for item in out:
        if item["status"] != "create":
            continue
        password = given.get(item["roll_no"]) or generate_password()
        create_user(session, item["roll_no"], item["name"], item["role"], password, must_change_pw=True)
        item["password"] = password
    return out


# problems

def _title_from_md(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _copy_problem(src_dir: Path, dest: Path) -> dict:
    src_dir = Path(src_dir)
    if not (src_dir / "rules.json").is_file():
        raise FileNotFoundError(f"no rules.json in {src_dir}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest, ignore=COPY_IGNORE)
    return json.loads((dest / "rules.json").read_text("utf-8"))


def new_version(session: Session, problem: Problem, src_dir: str | Path, *,
                problems_dir: Path | None = None) -> ProblemVersion:
    """Copy a problem folder to <problems_dir>/<slug>/v<N> and record it."""
    last = session.exec(select(func.max(ProblemVersion.version))
                        .where(ProblemVersion.problem_id == problem.id)).one()
    version = (last or 0) + 1
    dest = (problems_dir or get_settings().problems_dir) / problem.slug / f"v{version}"
    rules = _copy_problem(Path(src_dir), dest)
    pv = ProblemVersion(problem_id=problem.id, version=version, dir_path=str(dest), rules_json=rules,
                        rules_hash=hash_rules(rules), inputs_hash=hash_problem_inputs(dest, rules, sep="/"))
    session.add(pv)
    problem.current_version = version
    md = dest / "problem.md"
    if md.is_file():
        problem.statement_md = md.read_bytes().decode("utf-8", errors="replace")
    session.add(problem)
    session.commit()
    session.refresh(pv)
    return pv


def create_problem_from_dir(session: Session, slug: str, title: str | None, src_dir: str | Path, *,
                            problems_dir: Path | None = None, **fields: Any) -> tuple[Problem, ProblemVersion]:
    if get_problem_by_slug(session, slug):
        raise ValueError(f"A problem called {slug} already exists.")
    src_dir = Path(src_dir)
    md = src_dir / "problem.md"
    statement = md.read_bytes().decode("utf-8", errors="replace") if md.is_file() else ""
    problem = Problem(slug=slug, title=title or _title_from_md(statement, slug), statement_md=statement,
                      current_version=0, **fields)
    session.add(problem)
    session.flush()
    pv = new_version(session, problem, src_dir, problems_dir=problems_dir)
    session.refresh(problem)
    return problem, pv


def get_problem_by_slug(session: Session, slug: str) -> Problem | None:
    return session.exec(select(Problem).where(Problem.slug == slug)).first()


def current_version(session: Session, problem: Problem) -> ProblemVersion | None:
    return session.exec(select(ProblemVersion).where(ProblemVersion.problem_id == problem.id,
                                                     ProblemVersion.version == problem.current_version)).first()


# submissions

def create_submission(session: Session, user: User, problem: Problem, filename: str, source: str, *,
                      version: ProblemVersion | None = None, strip_bom: bool = True) -> Submission:
    if strip_bom and source.startswith("\ufeff"):
        source = source[1:]
    version = version or current_version(session, problem)
    if version is None:
        raise ValueError(f"Problem {problem.slug} has no grading version yet.")
    now = utcnow()
    sub = Submission(user_id=user.id, problem_id=problem.id, problem_version_id=version.id, filename=filename,
                     source=source, source_sha256=sha256(source), created_at=now,
                     is_late=bool(problem.due_at and now > problem.due_at))
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


def latest_submissions_per_user(session: Session, problem_id: int) -> list[Submission]:
    """Latest submission of every user for a problem, ordered by user id."""
    last = (select(Submission.user_id, func.max(Submission.id).label("sid"))
            .where(Submission.problem_id == problem_id)
            .group_by(Submission.user_id).subquery())
    stmt = (select(Submission).join(last, Submission.id == last.c.sid).order_by(Submission.user_id))
    return list(session.exec(stmt).all())


# jobs

def enqueue_job(session: Session, kind: str, priority: int | None = None, *, submission_id: int | None = None,
                problem_id: int | None = None, payload: dict | None = None, max_attempts: int = 2,
                commit: bool = True) -> Job:
    if kind not in PRIORITY:
        raise ValueError(f"unknown job kind {kind!r}")
    job = Job(kind=kind, priority=PRIORITY[kind] if priority is None else priority, submission_id=submission_id,
              problem_id=problem_id, payload=payload or {}, max_attempts=max_attempts)
    session.add(job)
    if commit:
        session.commit()
        session.refresh(job)
    else:
        session.flush()
    return job


def claim_job(session: Session, worker_id: str, kinds: Sequence[str] | None = None) -> Job | None:
    """Atomically move the best queued job to running (DESIGN section 8)."""
    kinds = list(kinds or PRIORITY)
    now = utcnow()
    pick = (select(Job.id).where(Job.status == "queued", Job.kind.in_(kinds))
            .order_by(Job.priority.desc(), Job.id).limit(1)
            .with_for_update(skip_locked=True).scalar_subquery())
    stmt = (update(Job).where(Job.id == pick, Job.status == "queued")
            .values(status="running", worker_id=worker_id, claimed_at=now, heartbeat_at=now,
                    attempts=Job.attempts + 1)
            .returning(Job.id))
    job_id = session.execute(stmt).scalar_one_or_none()
    session.commit()
    if job_id is None:
        return None
    return session.get(Job, job_id, populate_existing=True)


def heartbeat(session: Session, job_id: int, worker_id: str) -> bool:
    n = session.execute(update(Job).where(Job.id == job_id, Job.worker_id == worker_id, Job.status == "running")
                        .values(heartbeat_at=utcnow())).rowcount
    session.commit()
    return n == 1


def finish_job(session: Session, job_id: int, status: str = "done", error: str | None = None) -> None:
    session.execute(update(Job).where(Job.id == job_id)
                    .values(status=status, finished_at=utcnow(), error=error))
    session.commit()


def requeue_stale(session: Session, max_age_s: float = 60) -> tuple[int, int]:
    """Requeue running jobs with an old heartbeat, or fail them when out of attempts."""
    cutoff = utcnow() - timedelta(seconds=max_age_s)
    stale = (Job.status == "running", Job.heartbeat_at < cutoff)
    requeued = session.execute(update(Job).where(*stale, Job.attempts < Job.max_attempts)
                               .values(status="queued", worker_id=None)).rowcount
    failed = session.execute(update(Job).where(*stale, Job.attempts >= Job.max_attempts)
                             .values(status="failed", finished_at=utcnow(), error="The worker stopped responding; retry the job.")).rowcount
    session.commit()
    return requeued, failed


def add_event(session: Session, job_id: int, kind: str, msg: str, metric: str | None = None, *,
              commit: bool = True) -> JobEvent:
    ev = JobEvent(job_id=job_id, kind=kind, msg=msg, metric=metric)
    session.add(ev)
    if commit:
        session.commit()
        session.refresh(ev)
    else:
        session.flush()
    return ev


def events_after(session: Session, job_id: int, after_id: int = 0) -> list[JobEvent]:
    return list(session.exec(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > after_id)
                             .order_by(JobEvent.id)).all())


# results

COPY_FLAG_PREFIX = "suspected_copy_"
LOW_COPY_FLAG = "possible_copy_low_confidence"


# facts about the run, not accusations: shown in the report but never queued for staff review
INFO_FLAGS = ("compile_failed", "suspected_infinite_loop", "missing_main", "analyzer_error")


def needs_review(flag_type_col):
    """SQL condition: a flag a person has to look at."""
    return flag_type_col.not_in(INFO_FLAGS)


def is_copy_flag(ftype: str) -> bool:
    return ftype.startswith(COPY_FLAG_PREFIX) or ftype == LOW_COPY_FLAG


def apply_penalty(grade: float, penalty: str) -> float:
    if penalty == "zero":
        return 0.0
    if penalty == "half":
        return round1(grade / 2)
    return grade


def effective_grade(problem: Problem, sub: Submission, res: Result | None, confirmed: bool) -> float | None:
    """A staff override wins; otherwise the grade, with the integrity penalty after a confirmed copy."""
    if sub.override_grade is not None:
        return sub.override_grade
    if res is None:
        return None
    return apply_penalty(res.grade, problem.integrity_penalty) if confirmed else res.grade


def current_flags(session: Session, sub_ids: Iterable[int]) -> dict[int, list[Flag]]:
    """Flags of the current result of each submission."""
    ids = list(sub_ids)
    out: dict[int, list[Flag]] = {}
    if not ids:
        return out
    rows = session.exec(select(Flag).join(Result, Result.id == Flag.result_id)
                        .where(Flag.submission_id.in_(ids), Result.is_current == True)
                        .order_by(Flag.id)).all()
    for f in rows:
        out.setdefault(f.submission_id, []).append(f)
    return out


def confirmed_copy(flags: Iterable[Flag]) -> bool:
    return any(f.review_status == "confirmed" and is_copy_flag(f.type) for f in flags)


_SIM_DETAIL = re.compile(r"^([0-9.]+) similarity with (\S+?)(?: on a short program.*)?$")


def flag_fields(f: Flag) -> dict:
    """Structured view of a copy flag's detail ("0.93 similarity with web:a.c"): peer_ref, similarity, kind."""
    m = _SIM_DETAIL.match(f.detail or "") if is_copy_flag(f.type) else None
    if m is None:
        return {"peer_ref": None, "similarity": None, "kind": None}
    ref = m.group(2)
    prefix = ref.partition(":")[0]
    return {"peer_ref": ref, "similarity": float(m.group(1)),
            "kind": "peer" if prefix in ("sub", "peer") else prefix}


def _reviews(session: Session, submission_id: int) -> dict[tuple[str, str], Flag]:
    """Reviewed flags (not open) of the current result, keyed by (type, detail)."""
    cur = current_result(session, submission_id)
    if cur is None:
        return {}
    rows = session.exec(select(Flag).where(Flag.result_id == cur.id, Flag.review_status != "open")).all()
    return {(f.type, f.detail): f for f in rows}


def apply_flag_reviews(result: dict, dismissed: set[tuple[str, str]]) -> dict:
    """A dismissed copy flag gives the originality points back; re-opening it takes them away again."""
    flags = result.get("flags") or []
    cleared = any(f.get("type", "").startswith(COPY_FLAG_PREFIX) and (f.get("type"), f.get("detail") or "") in dismissed
                  for f in flags)
    changed = False
    metrics = []
    for m in result.get("metrics") or []:
        ev = m.get("evidence") or {}
        if m.get("analyzer") == "similarity_flag" and cleared and m.get("score", 1) < 1 and m.get("status") == "ok":
            m = {**m, "score": 1, "contribution": round1(m.get("weight", 0) * 100),
                 "evidence": {**ev, "review_override": {"score": m.get("score"),
                                                        "contribution": m.get("contribution")}},
                 "feedback": f"{m.get('feedback') or ''} The copy flag was dismissed by staff.".strip()}
            changed = True
        elif m.get("analyzer") == "similarity_flag" and not cleared and "review_override" in ev:
            old = ev["review_override"]
            m = {**m, "score": old.get("score", 0), "contribution": old.get("contribution", 0),
                 "evidence": {k: v for k, v in ev.items() if k != "review_override"},
                 "feedback": (m.get("feedback") or "").replace(" The copy flag was dismissed by staff.", "")}
            changed = True
        metrics.append(m)
    if not changed:
        return result
    total = 0
    for m in metrics:
        total += m.get("contribution") or 0
    return {**result, "metrics": metrics, "grade": round1(total)}


def store_result(session: Session, submission_id: int, job_id: int | None, result: dict,
                 report_txt: str = "", *, enrichment: dict | None = None) -> Result:
    """Insert a result with its metric and flag rows; it becomes the current one.

    Staff reviews of the previous current result's flags carry over to flags
    with the same type and detail.
    """
    reviews = _reviews(session, submission_id)
    prev_row = current_result(session, submission_id)
    raised = list(session.exec(select(Flag).where(Flag.result_id == prev_row.id, Flag.source == "staff")).all()) \
        if prev_row else []
    dismissed = {k for k, f in reviews.items() if f.review_status == "dismissed"}
    reviewed = apply_flag_reviews(result, dismissed)
    if reviewed is not result:
        from ..core.report import render_text
        result = reviewed
        try:
            report_txt = render_text(result)
        except (KeyError, TypeError, ValueError):
            pass
    session.execute(update(Result).where(Result.submission_id == submission_id, Result.is_current)
                    .values(is_current=False))
    row = Result(submission_id=submission_id, job_id=job_id, is_current=True,
                 grade=float(result.get("grade") or 0), provisional=bool(result.get("provisional")),
                 partial=bool(result.get("partial")), rules_hash=result.get("rules_hash") or "",
                 inputs_hash=result.get("inputs_hash") or "", submission_hash=result.get("submission_hash") or "",
                 corpus_hash=result.get("corpus_hash"), model_digests=result.get("model_digests"),
                 report_json=result, report_txt=report_txt, enrichment_json=enrichment)
    session.add(row)
    session.flush()
    for m in result.get("metrics") or []:
        session.add(MetricResult(result_id=row.id, name=m.get("name", ""), analyzer=m.get("analyzer") or "",
                                 weight=float(m.get("weight") or 0), score=float(m.get("score") or 0),
                                 contribution=float(m.get("contribution") or 0), status=m.get("status") or "ok"))
    seen = set()
    for f in result.get("flags") or []:
        ftype = f.get("type", "")
        detail = f.get("detail") or ""
        seen.add((ftype, detail))
        prev = reviews.get((ftype, detail))
        session.add(Flag(result_id=row.id, submission_id=submission_id, type=ftype, detail=detail,
                         severity=f.get("severity") or FLAG_SEVERITY.get(ftype, "medium"),
                         review_status=prev.review_status if prev else "open",
                         reviewed_by=prev.reviewed_by if prev else None,
                         review_note=prev.review_note if prev else None))
    for f in raised:
        if (f.type, f.detail) not in seen:
            session.add(Flag(result_id=row.id, submission_id=submission_id, type=f.type, detail=f.detail,
                             severity=f.severity, review_status=f.review_status, reviewed_by=f.reviewed_by,
                             review_note=f.review_note, source="staff"))
    session.commit()
    session.refresh(row)
    return row


def current_result(session: Session, submission_id: int) -> Result | None:
    return session.exec(select(Result).where(Result.submission_id == submission_id, Result.is_current)
                        .order_by(Result.id.desc())).first()


def find_cached_result(session: Session, submission_hash: str, rules_hash: str, inputs_hash: str,
                       corpus_hash: str | None) -> Result | None:
    """A complete earlier result for the same key. Partial results (skipped or failed metrics) and
    provisional ones (graded in a degraded environment) are never reused."""
    # the coordinator marks a result partial whenever a metric was skipped or hit an error
    return session.exec(select(Result).where(Result.submission_hash == submission_hash,
                                             Result.rules_hash == rules_hash, Result.inputs_hash == inputs_hash,
                                             Result.corpus_hash == corpus_hash, Result.provisional == False,
                                             Result.partial == False)
                        .order_by(Result.id.desc())).first()


def store_fingerprints(session: Session, problem_id: int, submission_id: int, fps: Iterable[Any]) -> int:
    """Replace the stored fingerprints of a submission; fps are similarity.Fingerprint tuples."""
    session.execute(Fingerprint.__table__.delete().where(Fingerprint.submission_id == submission_id))
    rows = [Fingerprint(problem_id=problem_id, submission_id=submission_id, hash=fp[0], tok_start=fp[1],
                        line_start=fp[2], line_end=fp[3]) for fp in fps]
    session.add_all(rows)
    session.commit()
    return len(rows)


def purge_user(session: Session, user: User) -> dict[str, int]:
    """Delete a user and everything hanging off their submissions (no commit; the caller owns the
    transaction). Staff references to the user (audit actor, flag reviewer) are kept as NULL."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import or_

    from .models import AuditLog, RankingProfile, SimilarityPair

    sub_ids = list(session.exec(select(Submission.id).where(Submission.user_id == user.id)).all())
    counts = {"submissions": len(sub_ids), "results": 0, "flags": 0, "jobs": 0}
    if sub_ids:
        res_ids = list(session.exec(select(Result.id).where(Result.submission_id.in_(sub_ids))).all())
        job_ids = list(session.exec(select(Job.id).where(Job.submission_id.in_(sub_ids))).all())
        refs = [f"sub:{i}" for i in sub_ids]
        counts["results"], counts["jobs"] = len(res_ids), len(job_ids)
        counts["flags"] = session.execute(sa_delete(Flag).where(
            or_(Flag.submission_id.in_(sub_ids), Flag.result_id.in_(res_ids or [-1])))).rowcount
        if res_ids:
            session.execute(sa_delete(MetricResult).where(MetricResult.result_id.in_(res_ids)))
            session.execute(sa_delete(Result).where(Result.id.in_(res_ids)))
        session.execute(sa_delete(Fingerprint).where(Fingerprint.submission_id.in_(sub_ids)))
        session.execute(sa_delete(RankingProfile).where(RankingProfile.submission_id.in_(sub_ids)))
        session.execute(sa_delete(SimilarityPair).where(
            or_(SimilarityPair.a_ref.in_(refs), SimilarityPair.b_ref.in_(refs))))
        if job_ids:
            session.execute(sa_delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            session.execute(sa_delete(Job).where(Job.id.in_(job_ids)))
        session.execute(sa_delete(Submission).where(Submission.id.in_(sub_ids)))
    session.execute(update(Flag).where(Flag.reviewed_by == user.id).values(reviewed_by=None))
    session.execute(update(AuditLog).where(AuditLog.actor_id == user.id).values(actor_id=None))
    session.execute(update(Submission).where(Submission.override_by == user.id).values(override_by=None))
    session.execute(update(Submission).where(Submission.comment_by == user.id).values(comment_by=None))
    session.delete(user)
    session.flush()
    return counts
