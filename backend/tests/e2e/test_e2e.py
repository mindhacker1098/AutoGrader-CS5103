"""End-to-end run of the whole system: seed-demo, API, worker, then checks over HTTP.

The stack is started on a fresh temp DATA_DIR with Docker and the seed LLM
cache (LLM_DISABLED, so nothing reaches Ollama). Set AG_E2E_URL=http://host:port
to run the checks against a stack that is already running and seeded instead.

In DB mode the peers are the latest submission of every other student, which
for the seed data is the same set of files as the submissions/ folder, so all
15 grades and flag sets match seeds/summary.json. What differs is only the
originality evidence: peer refs are sub:<id> instead of peer:<file>, and
because ties in top_matches are broken by ref, the zero/equal-similarity tail
of that list can name different peers.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.slow, pytest.mark.sandbox]

ROOT = Path(__file__).resolve().parents[3]
SEEDS = ROOT / "seeds"
# grades of the 15 seed submissions under the current rules (regenerate with: cli batch all --no-tutor)
SUMMARY = json.loads((SEEDS / "expected_current" / "summary.json").read_text("utf-8"))
SEED_TIMEOUT_S = 1200


def _docker_ok() -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", "autograder-sandbox:1"], capture_output=True, timeout=30,
                           check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(args: list[str], env: dict, log: Path) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with open(log, "wb") as out:
        return subprocess.Popen(args, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
                                start_new_session=os.name != "nt")


def _stop(p: subprocess.Popen, timeout: float = 60) -> None:
    if p.poll() is not None:
        return
    p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(10)


def _wait_http(url: str, proc: subprocess.Popen, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"API exited with code {proc.returncode}")
        try:
            if httpx.get(url + "/api/health?quick=1", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("API did not come up")


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    if os.environ.get("AG_E2E_URL"):
        yield os.environ["AG_E2E_URL"].rstrip("/")
        return
    if not _docker_ok():
        pytest.skip("needs Docker with autograder-sandbox:1")
    tmp = tmp_path_factory.mktemp("e2e")
    shutil.copytree(SEEDS / "cache_llm", tmp / "cache_llm")
    env = {**os.environ, "DATA_DIR": str(tmp / "data"), "LLM_CACHE_DIR": str(tmp / "cache_llm"),
           "LLM_DISABLED": "true", "SANDBOX": "docker", "DATABASE_URL": "",
           "JWT_SECRET": "e2e-secret-0123456789abcdef0123456789abcdef", "PYTHONUNBUFFERED": "1",
           "PYTHONPATH": str(ROOT / "backend")}
    py = sys.executable
    subprocess.run([py, str(ROOT / "backend/tests/fixtures/seed_class.py"), "--admin-password", "admin123"], cwd=ROOT, env=env,
                   check=True, timeout=120)
    port = _free_port()
    api = _spawn([py, "-m", "uvicorn", "autograder.api.main:app", "--port", str(port)], env, tmp / "api.log")
    worker = _spawn([py, "-m", "autograder.worker", "--jobs", "2", "--sandboxes", "3"], env, tmp / "worker.log")
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_http(url, api)
        yield url
    finally:
        _stop(worker)
        _stop(api, 20)


@contextmanager
def client(base: str, roll: str, password: str = "pass1234") -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base + "/api", timeout=60) as c:
        r = c.post("/auth/login", json={"roll_no": roll, "password": password})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture(scope="module")
def staff(base_url):
    with client(base_url, "admin", "admin123") as c:
        if c.get("/auth/me").json()["user"]["must_change_pw"]:
            r = c.post("/auth/change-password", json={"old": "admin123", "new": "admin-e2e-pass"})
            assert r.status_code == 200, r.text
        yield c


def wait_idle(staff: httpx.Client, timeout: float = SEED_TIMEOUT_S) -> dict:
    end = time.monotonic() + timeout
    while True:
        counts = staff.get("/staff/queue").json()["counts"]
        if counts["queued"] == 0 and counts["running"] == 0 and counts["done"] > 0:
            return counts
        if time.monotonic() > end:
            pytest.fail(f"queue did not drain: {counts}")
        time.sleep(2)


def wait_job(c: httpx.Client, job_id: int, timeout: float = 600) -> dict:
    end = time.monotonic() + timeout
    while True:
        job = c.get(f"/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        if time.monotonic() > end:
            pytest.fail(f"job {job_id} still {job['status']}")
        time.sleep(1)


@pytest.fixture(scope="module")
def seeded(staff):
    counts = wait_idle(staff)
    assert counts["failed"] == 0, staff.get("/staff/queue").json()["recent"]
    subs = staff.get("/staff/submissions", params={"page_size": 500}).json()["items"]
    return {s["filename"]: s for s in subs}


def roll_of(seeded: dict, name: str) -> str:
    return seeded[name]["roll_no"]


def expected_report(problem: str, filename: str) -> dict:
    return json.loads((SEEDS / "expected_current" / problem / (filename[:-2] + ".json")).read_text("utf-8"))


def _flags(s: str) -> set[str]:
    return {x for x in s.split(",") if x}


def test_seed_grades_match_summary(seeded):
    rows = []
    for exp in SUMMARY:
        got = seeded[exp["submission"]]
        rows.append((exp["submission"], got["grade"], exp["grade"],
                     {f["type"] for f in got["flags"]}, _flags(exp["flags"])))
    bad = [r for r in rows if r[1] != r[2] or r[3] != r[4]]
    assert not bad, bad


def test_seed_metrics_match_expected(staff, seeded):
    for exp in SUMMARY:
        rep = staff.get(f"/submissions/{seeded[exp['submission']]['id']}/report.json").json()
        node = expected_report(exp["problem"], exp["submission"])
        assert [(m["name"], m["score"], m["status"]) for m in rep["metrics"]] == \
               [(m["name"], m["score"], m["status"]) for m in node["metrics"]], exp["submission"]
        assert rep["rules_hash"] == node["rules_hash"]
        assert rep["submission_hash"] == node["submission_hash"]
        assert rep["corpus_hash"].startswith("sha256:")
        # originality: same similarity values, only the peer refs and tie order differ
        top = next(m for m in rep["metrics"] if m["analyzer"] == "similarity_flag")["evidence"]["top_matches"]
        ntop = next(m for m in node["metrics"] if m["analyzer"] == "similarity_flag")["evidence"]["top_matches"]
        assert [m["similarity"] for m in top] == [m["similarity"] for m in ntop]
        assert top[0]["kind"] == ntop[0]["kind"]
        assert all(not m["id"].startswith("peer:") for m in top)


def _sse(c: httpx.Client, job_id: int, last_id: int | None = None) -> list[tuple[str, str | None, dict]]:
    headers = {"Last-Event-ID": str(last_id)} if last_id else {}
    events, ev, eid, data = [], None, None, []
    with c.stream("GET", f"/jobs/{job_id}/events", headers=headers, timeout=600) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("id:"):
                eid = line[3:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "" and ev:
                events.append((ev, eid, json.loads("\n".join(data) or "null")))
                if ev == "done":
                    break
                ev, eid, data = None, None, []
    return events


def test_new_submission_streams_events(base_url, seeded):
    roll = roll_of(seeded, "add.c")
    src = (SEEDS / "problems" / "add_two_integers" / "submissions" / "add.c").read_text("utf-8")
    with client(base_url, roll) as c:
        r = c.post("/problems/add_two_integers/submissions",
                   json={"filename": "add_v2.c", "source": src + "\n/* second try */\n"})
        assert r.status_code == 201, r.text
        sub_id, job_id = r.json()["submission_id"], r.json()["job_id"]
        busy = c.post("/problems/add_two_integers/submissions", json={"filename": "x.c", "source": src})
        assert busy.status_code == 409

        events = _sse(c, job_id)
        kinds = [e[0] for e in events]
        assert "log" in kinds and kinds[-1] == "done"
        assert ("status", None, {"status": "done"}) in events
        result = next(e[2] for e in events if e[0] == "result")
        assert result["submission_id"] == sub_id and result["grade"] is not None
        logs = [e for e in events if e[0] == "log"]
        # API v2: students get stage events instead of worker bookkeeping like "result N stored"
        assert not any("stored" in e[2]["msg"] for e in logs)
        stages = [e[2]["stage"] for e in events if e[0] == "stage"]
        assert stages[0] in ("queued", "compile") and stages[-1] == "done"

        # resume from the middle: only later log events come back
        mid = int(logs[len(logs) // 2][1])
        again = [e for e in _sse(c, job_id, mid) if e[0] == "log"]
        assert again and all(int(e[1]) > mid for e in again)

        detail = c.get(f"/submissions/{sub_id}").json()
        assert detail["grade"] == result["grade"]
        assert detail["report"]["submission"] == "add_v2.c"


def test_authorization(base_url, seeded, staff):
    a, b = seeded["add.c"], seeded["add_good.c"]
    with httpx.Client(base_url=base_url + "/api") as anon:
        assert anon.get("/submissions").status_code == 401
        assert anon.get("/staff/queue").status_code == 401
        assert anon.post("/auth/login", json={"roll_no": a["roll_no"], "password": "nope"}).status_code == 401
    with client(base_url, a["roll_no"]) as c:
        assert c.get("/auth/me").json()["user"]["role"] == "student"
        assert c.get(f"/submissions/{b['id']}").status_code == 404
        assert c.get(f"/submissions/{b['id']}/report.json").status_code == 404
        assert c.get(f"/jobs/{b['job']['id']}").status_code == 404
        for path in ("/staff/queue", "/staff/submissions", "/staff/gradebook.csv",
                     "/staff/problems/add_two_integers/leaderboard"):
            assert c.get(path).status_code == 403, path
        assert c.post("/staff/problems/add_two_integers/plagiarism-scan", json={}).status_code == 403
        mine = c.get("/submissions").json()["items"]
        assert mine and all(s["id"] != b["id"] for s in mine)

        # students get the redacted view: no staff keys, no peer refs
        rep = c.get(f"/submissions/{a['id']}/report.json").json()
        assert "pre_analysis" not in rep
        assert "sub:" not in json.dumps(rep)
        assert "sub:" not in c.get(f"/submissions/{a['id']}/report.txt").text
    full = staff.get(f"/submissions/{a['id']}/report.json").json()
    assert "pre_analysis" in full and "sub:" in json.dumps(full)


def _labels(scan: dict, pair: dict) -> set[str]:
    return {scan["labels"][pair["a"]]["label"], scan["labels"][pair["b"]]["label"]}


def test_plagiarism_scan(staff, seeded):
    r = staff.post("/staff/problems/add_two_integers/plagiarism-scan", json={"threshold": 0.85})
    assert r.status_code == 200, r.text
    scan_id = r.json()["scan_id"]
    out = staff.get("/staff/problems/add_two_integers/plagiarism", params={"min": 0.5}).json()
    assert out["scan"]["id"] == scan_id
    pairs = out["pairs"]

    def find(x: str, y: str) -> dict:
        for p in pairs:
            labels = _labels(out, p)
            if any(x in s for s in labels) and any(y in s for s in labels):
                return p
        raise AssertionError(f"no pair {x} / {y}")

    peer = find("add_peer_copy.c", "add_good.c")
    web = find("add_web_copy.c", "forum_safe_add.c")
    assert peer["sim"] >= 0.85 and web["sim"] >= 0.85
    members = [set(cl["members"]) for cl in out["scan"]["clusters"]]
    assert any({peer["a"], peer["b"]} <= m for m in members)
    assert any({web["a"], web["b"]} <= m for m in members)

    d = staff.get("/staff/plagiarism/pair", params={"scan": scan_id, "a": web["a"], "b": web["b"]}).json()
    assert d["regions"] and d["a"]["source"] and d["b"]["source"]

    # the same scan as a background job on the worker
    job = staff.post("/staff/problems/add_two_integers/plagiarism-scan",
                     json={"threshold": 0.85, "background": True}).json()["job_id"]
    assert wait_job(staff, job, 300)["status"] == "done"
    assert staff.get("/staff/problems/add_two_integers/plagiarism").json()["scan"]["id"] > scan_id


def test_leaderboard(staff, seeded):
    wait_idle(staff, 600)
    rows = staff.get("/staff/problems/sort_integers/leaderboard").json()
    assert len(rows) == 6
    ranked = [r for r in rows if r["rank"] is not None]
    assert [r["rank"] for r in ranked] == list(range(1, len(ranked) + 1))
    assert ranked[0]["submission_id"] == seeded["sort_good.c"]["id"]
    assert all(r["profiled"] for r in rows)
    assert all(r["ineligible_reason"] for r in rows if r["rank"] is None)


def test_gradebook_csv(staff, seeded):
    r = staff.get("/staff/gradebook.csv", params={"problem": "sort_integers"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    by_sub = {int(x["submission_id"]): x for x in rows if x["submission_id"]}
    for exp in SUMMARY:
        if exp["problem"] != "sort_integers":
            continue
        row = by_sub[seeded[exp["submission"]]["id"]]
        assert float(row["grade"]) == exp["grade"]
    everything = staff.get("/staff/gradebook.csv").text.splitlines()
    assert len(everything) == 1 + 2 * len(rows)


def test_enrich_and_profile_jobs(staff, seeded):
    wait_idle(staff, 600)
    q = staff.get("/staff/queue", params={"page_size": 500}).json()
    jobs = q["recent"]["items"]
    for kind in ("enrich", "profile"):
        mine = [j for j in jobs if j["kind"] == kind]
        assert len(mine) >= 15, kind
        assert all(j["status"] == "done" for j in mine), [j for j in mine if j["status"] != "done"]
    rep = staff.get(f"/submissions/{seeded['add_crash.c']['id']}/report.json").json()
    assert rep["enrichment"]["explanations"]


def test_finalize_uses_frozen_corpus(staff, seeded):
    r = staff.post("/staff/problems/sort_integers/finalize")
    assert r.status_code == 200, r.text
    ids = r.json()["job_ids"]
    assert len(ids) == 6
    for j in ids:
        assert wait_job(staff, j)["status"] == "done"
    subs = {s["filename"]: s for s in
            staff.get("/staff/submissions", params={"problem": "sort_integers"}).json()["items"]}
    for exp in SUMMARY:
        if exp["problem"] == "sort_integers":
            assert subs[exp["submission"]]["grade"] == exp["grade"]


def test_health(base_url, staff):
    assert httpx.get(base_url + "/api/health", timeout=60).json()["ok"] is True
    h = staff.get("/staff/health", timeout=60).json()
    assert "docker" in json.dumps(h)
