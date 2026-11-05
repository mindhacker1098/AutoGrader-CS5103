"""SQLModel tables (DESIGN section 6)."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Column, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

ROLES = ("student", "ta", "admin")
PROBLEM_STATUSES = ("draft", "published", "archived")
PENALTIES = ("none", "zero", "half")
JOB_KINDS = ("grade", "regrade", "enrich", "profile", "plagiarism_scan", "corpus_gen")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
PRIORITY = {"grade": 10, "regrade": 10, "enrich": 5, "profile": 3, "plagiarism_scan": 1, "corpus_gen": 1}


def utcnow() -> datetime:
    """Aware UTC timestamp (SQLModel stores UTC and returns aware values, also on SQLite)."""
    return datetime.now(UTC)


def _json(nullable: bool = True) -> Any:
    return Field(default=None, sa_column=Column(JSON, nullable=nullable))


class User(SQLModel, table=True):
    __tablename__ = "user"

    id: int | None = Field(default=None, primary_key=True)
    roll_no: str = Field(index=True, unique=True)
    name: str = ""
    role: str = Field(default="student", index=True)
    password_hash: str = ""
    must_change_pw: bool = True
    is_active: bool = True
    token_version: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None


class Problem(SQLModel, table=True):
    __tablename__ = "problem"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    title: str = ""
    statement_md: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    current_version: int = 1
    status: str = "published"
    grade_policy: str = "latest"
    opens_at: datetime | None = None
    due_at: datetime | None = None
    finalized_at: datetime | None = None
    show_best_after_deadline: bool = False
    reveal_hidden_tests: bool = False
    max_submissions_per_hour: int = 20
    integrity_penalty: str = "none"
    created_at: datetime = Field(default_factory=utcnow)


class ProblemVersion(SQLModel, table=True):
    __tablename__ = "problem_version"
    __table_args__ = (UniqueConstraint("problem_id", "version", name="uq_problem_version"),)

    id: int | None = Field(default=None, primary_key=True)
    problem_id: int = Field(foreign_key="problem.id", index=True)
    version: int = 1
    dir_path: str
    rules_json: dict | None = _json(nullable=False)
    rules_hash: str = ""
    inputs_hash: str = ""
    sample_output: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class Submission(SQLModel, table=True):
    __tablename__ = "submission"
    __table_args__ = (Index("ix_submission_problem_user", "problem_id", "user_id", "id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    problem_id: int = Field(foreign_key="problem.id")
    problem_version_id: int = Field(foreign_key="problem_version.id")
    filename: str
    source: str = Field(sa_column=Column(Text, nullable=False))
    source_sha256: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    is_late: bool = False
    share_as_example: bool = True
    override_grade: float | None = None
    override_reason: str | None = None
    override_by: int | None = None
    override_at: datetime | None = None
    staff_comment: str | None = Field(default=None, sa_column=Column(Text))
    comment_by: int | None = None
    comment_at: datetime | None = None


class Job(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_claim", "status", "kind", "priority", "id"),
        Index("ix_job_status_heartbeat", "status", "heartbeat_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    kind: str = "grade"
    submission_id: int | None = Field(default=None, foreign_key="submission.id", index=True)
    problem_id: int | None = Field(default=None, foreign_key="problem.id", index=True)
    payload: dict | None = _json()
    status: str = "queued"
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 2
    worker_id: str | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))
    traceback: str | None = Field(default=None, sa_column=Column(Text))
    result: dict | None = _json()


class JobEvent(SQLModel, table=True):
    __tablename__ = "job_event"
    __table_args__ = (Index("ix_job_event_job", "job_id", "id"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    ts: datetime = Field(default_factory=utcnow)
    kind: str = "info"
    metric: str | None = None
    msg: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))


class Result(SQLModel, table=True):
    __tablename__ = "result"
    __table_args__ = (
        Index("ix_result_current", "submission_id", "is_current"),
        Index("ix_result_cache", "submission_hash", "rules_hash", "inputs_hash", "corpus_hash"),
    )

    id: int | None = Field(default=None, primary_key=True)
    submission_id: int = Field(foreign_key="submission.id")
    job_id: int | None = Field(default=None, foreign_key="job.id")
    is_current: bool = True
    grade: float = 0.0
    provisional: bool = False
    partial: bool = False
    rules_hash: str = ""
    inputs_hash: str = ""
    submission_hash: str = ""
    corpus_hash: str | None = None
    model_digests: dict | None = _json()
    report_json: dict | None = _json()
    report_txt: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    enrichment_json: dict | None = _json()
    created_at: datetime = Field(default_factory=utcnow)


class MetricResult(SQLModel, table=True):
    __tablename__ = "metric_result"

    id: int | None = Field(default=None, primary_key=True)
    result_id: int = Field(foreign_key="result.id", index=True)
    name: str
    analyzer: str = ""
    weight: float = 0.0
    score: float = 0.0
    contribution: float = 0.0
    status: str = "ok"


class Flag(SQLModel, table=True):
    __tablename__ = "flag"
    __table_args__ = (Index("ix_flag_type_status", "type", "review_status"),)

    id: int | None = Field(default=None, primary_key=True)
    result_id: int = Field(foreign_key="result.id", index=True)
    submission_id: int = Field(foreign_key="submission.id", index=True)
    type: str
    detail: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    severity: str = "medium"
    review_status: str = "open"
    reviewed_by: int | None = Field(default=None, foreign_key="user.id")
    review_note: str | None = None
    source: str = "grader"  # grader | staff (raised from a plagiarism pair; survives regrades)


class Fingerprint(SQLModel, table=True):
    __tablename__ = "fingerprint"
    __table_args__ = (Index("ix_fingerprint_problem_hash", "problem_id", "hash"),)

    id: int | None = Field(default=None, primary_key=True)
    problem_id: int = Field(foreign_key="problem.id")
    submission_id: int = Field(foreign_key="submission.id", index=True)
    hash: int = Field(sa_type=BigInteger)
    tok_start: int = 0
    line_start: int = 0
    line_end: int = 0


class PlagiarismScan(SQLModel, table=True):
    __tablename__ = "plagiarism_scan"

    id: int | None = Field(default=None, primary_key=True)
    problem_id: int = Field(foreign_key="problem.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    threshold: float = 0.85
    params: dict | None = _json()
    clusters_json: list | None = _json()
    n_submissions: int = 0


class SimilarityPair(SQLModel, table=True):
    __tablename__ = "similarity_pair"
    __table_args__ = (Index("ix_similarity_pair_scan", "scan_id", "a_ref", "b_ref"),)

    id: int | None = Field(default=None, primary_key=True)
    scan_id: int = Field(foreign_key="plagiarism_scan.id")
    a_ref: str
    b_ref: str
    sim: float = 0.0
    ast_sim: float | None = None
    shared: int = 0
    regions_json: list | None = _json()


class CorpusItemRow(SQLModel, table=True):
    __tablename__ = "corpus_item"

    id: int | None = Field(default=None, primary_key=True)
    problem_id: int = Field(foreign_key="problem.id", index=True)
    kind: str = "web"
    model: str | None = None
    prompt_id: str | None = None
    temperature: float | None = None
    seed: int | None = None
    path: str
    sha256: str = Field(index=True)
    compiles: bool | None = None


class RankingProfile(SQLModel, table=True):
    __tablename__ = "ranking_profile"

    submission_id: int = Field(foreign_key="submission.id", primary_key=True)
    instr_at_max_n: int | None = Field(default=None, sa_type=BigInteger)
    peak_heap_bytes: int | None = Field(default=None, sa_type=BigInteger)
    token_count: int | None = None
    readability: float | None = None
    computed_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    actor_id: int | None = Field(default=None, foreign_key="user.id", index=True)
    action: str
    target: str | None = None
    detail: dict | None = _json()
    ts: datetime = Field(default_factory=utcnow, index=True)


class WorkerBeat(SQLModel, table=True):
    __tablename__ = "worker_beat"

    worker_id: str = Field(primary_key=True)
    host: str = ""
    pid: int = 0
    started_at: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    lanes: str = ""
    busy: int = 0
