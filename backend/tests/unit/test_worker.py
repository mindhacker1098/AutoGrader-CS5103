import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from autograder.config import PROJECT_ROOT, Settings
from autograder.core.corpus import read_text
from autograder.db import repo
from autograder.db.models import Fingerprint, Job, JobEvent, Result
from autograder.db.session import init_db, make_engine, new_session
from autograder.worker.dbcorpus import DbCorpus
from autograder.worker.main import Lane, Worker, parse_lanes
from autograder.worker.reaper import reap_once
from sqlmodel import select

SEEDS = PROJECT_ROOT / "seeds" / "problems"


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(eng)
    yield eng
    eng.dispose()


def _seed(engine, tmp_path, subs=("add.c",)):
    with new_session(engine) as s:
        problem, pv = repo.create_problem_from_dir(s, "add_two_integers", None, SEEDS / "add_two_integers",
                                                   problems_dir=tmp_path / "problems")
        out = []
        for i, name in enumerate(subs):
            user = repo.create_user(s, f"S{i + 1:02d}", name, "student", "pass123")
            src = read_text(SEEDS / "add_two_integers" / "submissions" / name)
            sub = repo.create_submission(s, user, problem, name, src)
            job = repo.enqueue_job(s, "grade", submission_id=sub.id, problem_id=problem.id)
            out.append((sub.id, job.id))
        return problem.id, pv, out


def _settings(tmp_path):
    return Settings(DATA_DIR=tmp_path, SANDBOX="docker", LLM_CACHE_DIR=PROJECT_ROOT / "seeds" / "cache_llm",
                    LLM_DISABLED=True)


def _docker_or_skip():
    from autograder.core.sandbox.docker import DockerSandbox

    if not asyncio.run(DockerSandbox.available(Settings().SANDBOX_IMAGE)):
        pytest.skip("Docker sandbox image not available")


def test_parse_lanes():
    default = parse_lanes(None, 2)
    assert default[0].kinds == ("grade", "regrade") and default[0].slots == 2
    assert "enrich" in default[1].kinds and default[1].slots == 1
    lanes = parse_lanes("grade+regrade=3, enrich", 2)
    assert [(lane.kinds, lane.slots) for lane in lanes] == [(("grade", "regrade"), 3), (("enrich",), 1)]
    with pytest.raises(ValueError):
        parse_lanes("grade+bogus=1", 2)


def test_dbcorpus_peers(engine, tmp_path):
    pid, pv, ids = _seed(engine, tmp_path, ("add.c", "add_good.c", "add_crash.c"))
    with new_session(engine) as s:
        # a newer submission from student 2 replaces their first one as a peer
        u2 = repo.get_user_by_roll(s, "S02")
        problem = repo.get_problem_by_slug(s, "add_two_integers")
        newer = repo.create_submission(s, u2, problem, "add_good.c", "int main(void){return 0;}\n")
        c = DbCorpus(s, pid, repo.get_user_by_roll(s, "S01").id, pv.dir_path)
    peers = [i for i in c.peers() if i.kind == "peer"]
    assert [p.ref for p in peers] == [f"sub:{newer.id}", f"sub:{ids[2][0]}"]
    kinds = [i.kind for i in c.peers()]
    assert kinds == ["peer", "peer", "web", "web", "llm", "llm", "llm"]
    assert c.hash.startswith("sha256:")
    with new_session(engine) as s:
        frozen = DbCorpus(s, pid, None, pv.dir_path, peer_ids=[ids[1][0]])
    assert frozen.peer_ids == [ids[1][0]]


def test_reaper_requeues_stale_job(engine, tmp_path):
    _, _, ids = _seed(engine, tmp_path)
    job_id = ids[0][1]
    with new_session(engine) as s:
        assert repo.claim_job(s, "dead-worker").id == job_id
        job = s.get(Job, job_id)
        job.heartbeat_at = job.heartbeat_at - timedelta(seconds=120)
        s.add(job)
        s.commit()
    assert reap_once(engine) == (1, 0)
    with new_session(engine) as s:
        job = s.get(Job, job_id)
        assert job.status == "queued" and job.worker_id is None and job.attempts == 1
        repo.claim_job(s, "dead-worker-2")
        job = s.get(Job, job_id, populate_existing=True)
        job.heartbeat_at = job.heartbeat_at - timedelta(seconds=120)
        s.add(job)
        s.commit()
    assert reap_once(engine) == (0, 1)
    with new_session(engine) as s:
        assert s.get(Job, job_id).status == "failed"


def test_handler_error_is_retried_then_failed(engine, tmp_path):
    with new_session(engine) as s:
        job = repo.enqueue_job(s, "grade", submission_id=None, payload={})

    async def go():
        w = Worker(_settings(tmp_path), engine=engine, lanes=[Lane(("grade",), 1)], worker_id="t-err",
                   poll_s=0.05, reaper=False)
        await w.run(max_jobs=2)

    asyncio.run(go())
    with new_session(engine) as s:
        j = s.get(Job, job.id)
        assert j.status == "failed" and j.attempts == 2 and "no submission_id" in j.error
        assert s.exec(select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.kind == "error")).first()


@pytest.mark.sandbox
def test_worker_grades_add_c(engine, tmp_path):
    _docker_or_skip()
    pid, _, ids = _seed(engine, tmp_path)
    sub_id, job_id = ids[0]

    async def go():
        w = Worker(_settings(tmp_path), engine=engine, lanes=[Lane(("grade", "regrade"), 1)],
                   worker_id="test-worker", poll_s=0.05, reaper=False)
        await w.run(max_jobs=1)

    asyncio.run(go())
    with new_session(engine) as s:
        job = s.get(Job, job_id)
        assert job.status == "done", job.error
        res = repo.current_result(s, sub_id)
        assert res is not None and res.grade == 86.3
        assert res.job_id == job_id and res.corpus_hash and res.report_txt
        assert res.report_json["corpus_hash"] == res.corpus_hash
        assert s.exec(select(Fingerprint).where(Fingerprint.submission_id == sub_id)).first()
        kinds = {j.kind: j.priority for j in s.exec(select(Job).where(Job.id != job_id)).all()}
        assert kinds == {"enrich": 5, "profile": 3}
        msgs = [e.msg for e in repo.events_after(s, job_id)]
        assert any("GRADE 86.3/100" in m for m in msgs)

        # Equal source/rules do not identify the grading environment: measure again.
        regrade = repo.enqueue_job(s, "grade", submission_id=sub_id, problem_id=pid)

    async def again():
        w = Worker(_settings(tmp_path), engine=engine, lanes=[Lane(("grade",), 1)], worker_id="test-worker",
                   poll_s=0.05, reaper=False)
        await w.run(max_jobs=1)

    asyncio.run(again())
    with new_session(engine) as s:
        assert s.get(Job, regrade.id).status == "done"
        msgs = [e.msg for e in repo.events_after(s, regrade.id)]
        assert any("GRADE" in m for m in msgs)
        assert repo.current_result(s, sub_id).id != res.id
        assert repo.current_result(s, sub_id).grade == res.grade
        assert len(s.exec(select(Result)).all()) == 2


def test_regrade_skips_cache_and_uses_current_version(engine, tmp_path, monkeypatch):
    import json
    import shutil

    from autograder.db.models import Problem
    from autograder.worker import handlers

    pid, pv1, ids = _seed(engine, tmp_path)
    sub_id = ids[0][0]
    calls = []

    async def fake_grade(problem_dir, rules, source, filename, log, **kw):
        calls.append((Path(problem_dir), kw.get("reveal_hidden_tests")))
        report = json.loads((PROJECT_ROOT / "seeds" / "expected_reports" / "add_two_integers" / "add.json")
                            .read_text("utf-8"))
        return {**report, "grade": 50.0 + len(calls)}

    monkeypatch.setattr(handlers, "grade_submission", fake_grade)
    v2_src = tmp_path / "v2src"
    shutil.copytree(SEEDS / "add_two_integers", v2_src)
    rules = json.loads((v2_src / "rules.json").read_text("utf-8"))
    rules["time_limit_s"] = 3
    (v2_src / "rules.json").write_text(json.dumps(rules), "utf-8")
    with new_session(engine) as s:
        problem = s.get(Problem, pid)
        problem.reveal_hidden_tests = False
        s.add(problem)
        s.commit()
        pv2 = repo.new_version(s, problem, v2_src, problems_dir=tmp_path / "problems")
        assert pv2.id != pv1.id
        regrade = repo.enqueue_job(s, "regrade", submission_id=sub_id, problem_id=pid)
        pv2_dir = Path(pv2.dir_path)

    async def go(kinds):
        w = Worker(_settings(tmp_path), engine=engine, lanes=[Lane(kinds, 1)], worker_id="t-rg",
                   poll_s=0.05, reaper=False)
        await w.run(max_jobs=1)

    asyncio.run(go(("grade",)))
    assert calls[0][0] == Path(pv1.dir_path) and calls[0][1] is False
    asyncio.run(go(("regrade",)))
    with new_session(engine) as s:
        assert s.get(Job, regrade.id).status == "done"
        assert len(calls) == 2 and calls[1][0] == pv2_dir
        res = repo.current_result(s, sub_id)
        assert res.grade == 52.0 and res.report_json["problem_version_id"] == pv2.id


def test_enrich_and_profile_store_results(engine, tmp_path, monkeypatch):
    import sys
    import types

    from autograder.db.models import RankingProfile

    _, _, ids = _seed(engine, tmp_path)
    sub_id = ids[0][0]
    import json
    report = json.loads((PROJECT_ROOT / "seeds/expected_reports/add_two_integers/add.json").read_text("utf-8"))
    report.update({"grade": 50, "metrics": [{"name": "readability", "analyzer": "llm_review", "score": 0.8,
                                           "weight": 1, "contribution": 80, "status": "ok", "feedback": "Measured readability"}],
                   "flags": [], "counterexamples": [{"input": "1 2"}], "static_findings": ["gcc: measured finding"]})
    with new_session(engine) as s:
        s.get(Job, ids[0][1]).status = "done"
        s.commit()
        row = repo.store_result(s, sub_id, None, report)
        e = repo.enqueue_job(s, "enrich", submission_id=sub_id, payload={"result_id": row.id})
        p = repo.enqueue_job(s, "profile", submission_id=sub_id)

    async def explain_failures(result, settings, *, findings):
        assert findings == ["gcc: measured finding"]
        return {"explanations": [len(result["counterexamples"])]}

    def profile_submission(problem_dir, rules, source, settings):
        assert (problem_dir / "rules.json").is_file() and "main" in source
        return {"instr_at_max_n": 1234, "peak_heap_bytes": 0}

    monkeypatch.setitem(sys.modules, "autograder.core.preanalysis.llm_pre",
                        types.SimpleNamespace(explain_failures=explain_failures))
    monkeypatch.setitem(sys.modules, "autograder.core.profile",
                        types.SimpleNamespace(profile_submission=profile_submission))

    async def go():
        w = Worker(_settings(tmp_path), engine=engine, lanes=[Lane(("enrich", "profile"), 1)], worker_id="t-bg",
                   poll_s=0.05, reaper=False)
        await w.run(max_jobs=2)

    asyncio.run(go())
    with new_session(engine) as s:
        assert s.get(Job, e.id).status == "done" and s.get(Job, p.id).status == "done"
        r = s.get(Result, row.id)
        assert r.enrichment_json == {"explanations": [1]} and r.report_json["enrichment"] == r.enrichment_json
        rp = s.get(RankingProfile, sub_id)
        assert rp.instr_at_max_n == 1234 and rp.readability == 0.8 and rp.token_count > 0
