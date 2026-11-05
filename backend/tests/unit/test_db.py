import threading
from datetime import timedelta

import pytest
from autograder.config import PROJECT_ROOT
from autograder.core.hashing import hash_problem_inputs, hash_rules
from autograder.db import repo
from autograder.db.models import Flag, Job, MetricResult, Result, utcnow
from autograder.db.session import init_db, make_engine, new_session
from sqlalchemy import text
from sqlmodel import select

SEEDS = PROJECT_ROOT / "seeds" / "problems"


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with new_session(engine) as s:
        yield s


@pytest.fixture
def problem(session, tmp_path):
    p, _ = repo.create_problem_from_dir(session, "add_two_integers", None, SEEDS / "add_two_integers",
                                        problems_dir=tmp_path / "problems")
    return p


def _student(session, roll="S01"):
    return repo.create_user(session, roll, "Student " + roll, "student", "pass123")


def test_pragmas(engine):
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_users(session):
    u = repo.create_user(session, " A1 ", "Ann", "ta", "secret")
    assert repo.get_user_by_roll(session, "A1").id == u.id
    assert repo.verify_password("secret", u.password_hash)
    assert not repo.verify_password("wrong", u.password_hash)
    with pytest.raises(ValueError):
        repo.create_user(session, "A1", "again", "student", "x")
    with pytest.raises(ValueError):
        repo.create_user(session, "B1", "Bob", "instructor", "x")
    out = repo.import_roster(session, [{"roll_no": "A1", "name": "Ann"}, {"roll_no": "C1", "name": "Cy", "role": "Student"}])
    # API v2 statuses: create / skip / reject
    assert [r["status"] for r in out] == ["skip", "create"]
    assert repo.verify_password(out[1]["password"], repo.get_user_by_roll(session, "C1").password_hash)


def test_problem_from_dir(session, problem, tmp_path):
    pv = repo.current_version(session, problem)
    dest = tmp_path / "problems" / "add_two_integers" / "v1"
    assert pv.version == 1 and pv.dir_path == str(dest)
    assert (dest / "rules.json").is_file() and (dest / "tests" / "01.in").is_file()
    assert not (dest / "submissions").exists()
    assert (dest / "corpus" / "web").is_dir()
    assert problem.title == "Add two integers"
    assert pv.rules_hash == hash_rules(pv.rules_json)
    assert pv.inputs_hash == hash_problem_inputs(SEEDS / "add_two_integers", pv.rules_json, sep="/")
    assert pv.inputs_hash.startswith("sha256v2:")
    pv2 = repo.new_version(session, problem, SEEDS / "add_two_integers", problems_dir=tmp_path / "problems")
    assert pv2.version == 2 and problem.current_version == 2
    assert repo.current_version(session, problem).id == pv2.id


def test_latest_per_user(session, problem):
    a, b = _student(session, "S01"), _student(session, "S02")
    repo.create_submission(session, a, problem, "a.c", "int main(){}")
    a2 = repo.create_submission(session, a, problem, "a.c", "\ufeffint main(){return 0;}")
    b1 = repo.create_submission(session, b, problem, "b.c", "int main(){}")
    assert a2.source.startswith("int")
    assert [s.id for s in repo.latest_submissions_per_user(session, problem.id)] == [a2.id, b1.id]


def test_claim_order_and_kinds(session, problem):
    lo = repo.enqueue_job(session, "profile", problem_id=problem.id)
    hi = repo.enqueue_job(session, "grade", problem_id=problem.id)
    hi2 = repo.enqueue_job(session, "regrade", problem_id=problem.id)
    assert lo.priority == 3 and hi.priority == 10
    assert repo.claim_job(session, "w", ["profile"]).id == lo.id
    j = repo.claim_job(session, "w")
    assert j.id == hi.id and j.status == "running" and j.attempts == 1 and j.worker_id == "w"
    assert j.claimed_at is not None and j.heartbeat_at is not None
    assert repo.claim_job(session, "w", ["grade"]) is None
    assert repo.claim_job(session, "w").id == hi2.id
    assert repo.claim_job(session, "w") is None


def test_claim_atomic_threads(engine, session, problem):
    n_jobs, n_threads = 60, 8
    for _ in range(n_jobs):
        repo.enqueue_job(session, "grade", problem_id=problem.id, commit=False)
    session.commit()
    claimed: dict[str, list[int]] = {}
    errors = []
    start = threading.Barrier(n_threads)

    def worker(wid):
        mine = claimed.setdefault(wid, [])
        try:
            start.wait()
            with new_session(engine) as s:
                while (job := repo.claim_job(s, wid, ["grade"])) is not None:
                    mine.append(job.id)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    ids = [i for v in claimed.values() for i in v]
    assert len(ids) == n_jobs and len(set(ids)) == n_jobs
    session.expire_all()
    jobs = session.exec(select(Job)).all()
    assert all(j.status == "running" and j.attempts == 1 for j in jobs)
    assert all(j.id in claimed[j.worker_id] for j in jobs)


def test_requeue_stale(session, problem):
    j = repo.enqueue_job(session, "grade", problem_id=problem.id, max_attempts=2)
    for expect in ((1, 0), (0, 1)):
        claimed = repo.claim_job(session, "w")
        assert claimed.id == j.id
        claimed.heartbeat_at = utcnow() - timedelta(seconds=120)
        session.add(claimed)
        session.commit()
        assert repo.requeue_stale(session, 60) == expect
    session.refresh(j)
    assert j.status == "failed" and j.attempts == 2


def test_events(session, problem):
    j = repo.enqueue_job(session, "grade", problem_id=problem.id)
    e1 = repo.add_event(session, j.id, "plan", "start")
    repo.add_event(session, j.id, "ok", "done", metric="correctness")
    evs = repo.events_after(session, j.id, e1.id)
    assert [(e.kind, e.metric, e.msg) for e in evs] == [("ok", "correctness", "done")]


def _result(grade, flags=()):
    return {
        "submission": "a.c", "submission_hash": "h", "rules_hash": "sha256:r", "inputs_hash": "sha256v2:i",
        "grade": grade, "partial": False, "corpus_hash": "c", "model_digests": None, "provisional": False,
        "metrics": [
            {"name": "correctness", "analyzer": "io_tests", "weight": 0.5, "score": 1, "contribution": 50, "status": "ok"},
            {"name": "speed", "analyzer": "complexity_estimator", "weight": 0.5, "score": 0, "contribution": 0,
             "status": "skipped"},
        ],
        "flags": list(flags),
    }


def test_store_result(session, problem):
    u = _student(session)
    sub = repo.create_submission(session, u, problem, "a.c", "int main(){}")
    job = repo.enqueue_job(session, "grade", submission_id=sub.id, problem_id=problem.id)
    r1 = repo.store_result(session, sub.id, job.id, _result(50, [{"type": "suspected_copy_web", "detail": "x"}]), "txt")
    r2 = repo.store_result(session, sub.id, job.id, _result(60.5, [
        {"type": "possible_copy_low_confidence", "detail": "short"}, {"type": "compile_failed", "detail": "e"}]))
    session.refresh(r1)
    assert not r1.is_current and r2.is_current
    assert repo.current_result(session, sub.id).id == r2.id
    assert r2.grade == 60.5 and r2.report_json["metrics"][1]["status"] == "skipped"
    assert r1.report_txt == "txt"
    mets = session.exec(select(MetricResult).where(MetricResult.result_id == r2.id)).all()
    assert [(m.name, m.analyzer, m.contribution, m.status) for m in mets] == [
        ("correctness", "io_tests", 50, "ok"), ("speed", "complexity_estimator", 0, "skipped")]
    flags = session.exec(select(Flag).where(Flag.submission_id == sub.id).order_by(Flag.id)).all()
    assert [(f.result_id, f.type, f.severity, f.review_status) for f in flags] == [
        (r1.id, "suspected_copy_web", "high", "open"),
        (r2.id, "possible_copy_low_confidence", "low", "open"),
        (r2.id, "compile_failed", "medium", "open")]
    assert repo.find_cached_result(session, "h", "sha256:r", "sha256v2:i", "c").id == r2.id
    assert repo.find_cached_result(session, "h", "sha256:r", "sha256v2:i", "other") is None
    assert len(session.exec(select(Result)).all()) == 2


def test_cache_skips_partial_and_reviews_carry_over(session, problem):
    u = _student(session)
    sub = repo.create_submission(session, u, problem, "a.c", "int main(){}")
    repo.store_result(session, sub.id, None, {**_result(40), "partial": True, "submission_hash": "p"})
    assert repo.find_cached_result(session, "p", "sha256:r", "sha256v2:i", "c") is None

    copy = {**_result(50, [{"type": "suspected_copy_peer", "detail": "0.9 similarity with sub:3"}]), "metrics": [
        {"name": "correctness", "analyzer": "io_tests", "weight": 0.5, "score": 1, "contribution": 50, "status": "ok"},
        {"name": "originality", "analyzer": "similarity_flag", "weight": 0.5, "score": 0, "contribution": 0,
         "status": "ok", "evidence": {}}]}
    r1 = repo.store_result(session, sub.id, None, copy)
    f = session.exec(select(Flag).where(Flag.result_id == r1.id)).one()
    f.review_status, f.review_note = "dismissed", "own work"
    session.add(f)
    session.commit()
    # a regrade gives the same flag again: the review is kept and the points come back
    r2 = repo.store_result(session, sub.id, None, copy)
    f2 = session.exec(select(Flag).where(Flag.result_id == r2.id)).one()
    assert (f2.review_status, f2.review_note) == ("dismissed", "own work")
    assert r2.grade == 100 and r2.report_json["metrics"][1]["evidence"]["review_override"]["score"] == 0
    f2.review_status = "confirmed"
    session.add(f2)
    session.commit()
    r3 = repo.store_result(session, sub.id, None, r2.report_json)
    assert r3.grade == 50 and "review_override" not in r3.report_json["metrics"][1]["evidence"]


def test_fingerprints(session, problem):
    from autograder.core.similarity import fingerprints_with_pos

    u = _student(session)
    sub = repo.create_submission(session, u, problem, "a.c", "int main(){int a,b;scanf(\"%d %d\",&a,&b);"
                                                             "printf(\"%d\\n\",a+b);return 0;}")
    fps = fingerprints_with_pos(sub.source)
    assert repo.store_fingerprints(session, problem.id, sub.id, fps) == len(fps) > 0
    assert repo.store_fingerprints(session, problem.id, sub.id, fps[:1]) == 1
