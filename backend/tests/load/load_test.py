"""Load test: N students submit at (almost) the same moment against a running API + worker(s).

Each simulated student is imported through the staff API, logs in, changes the forced password, uploads
one of the seed submissions with a unique trailing comment (so the result cache cannot answer it), and
follows the SSE stream until the job is done. Timings and outcomes go to a JSON file.

    .venv/Scripts/python.exe backend/tests/load/load_test.py --base http://127.0.0.1:8000 \
        --admin admin --admin-password <pw> --n 30 --out docs/load/run1.json

Not collected by pytest (no test_ prefix); it needs a live server.
"""
import argparse
import asyncio
import csv
import io
import json
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
SEEDS = ROOT / "seeds" / "problems"


def pick_files(n: int) -> list[tuple[str, Path]]:
    files = [("add_two_integers", p) for p in sorted((SEEDS / "add_two_integers" / "submissions").glob("*.c"))]
    files += [("sort_integers", p) for p in sorted((SEEDS / "sort_integers" / "submissions").glob("*.c"))]
    return [files[i % len(files)] for i in range(n)]


async def admin_client(base: str, roll: str, pw: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(base_url=base, timeout=60)
    r = await c.post("/api/auth/login", json={"roll_no": roll, "password": pw})
    if r.status_code == 401:  # changed by an earlier run
        r = await c.post("/api/auth/login", json={"roll_no": roll, "password": pw + "-load"})
    r.raise_for_status()
    if r.json()["user"].get("must_change_pw"):
        (await c.post("/api/auth/change-password", json={"old": pw, "new": pw + "-load"})).raise_for_status()
        print(f"admin password changed to {pw}-load (first login)")
    return c


async def import_students(admin: httpx.AsyncClient, n: int, prefix: str) -> dict[str, str]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["roll_no", "name", "role"])
    for i in range(1, n + 1):
        w.writerow([f"{prefix}{i:03d}", f"Load Student {i}", "student"])
    r = await admin.post("/api/staff/users/import", files={"file": ("roster.csv", buf.getvalue(), "text/csv")})
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return {row["roll_no"]: row["password"] for row in rows}


async def follow(c: httpx.AsyncClient, job_id: int, rec: dict, t0: float, timeout_s: float) -> None:
    event = None
    async with c.stream("GET", f"/api/jobs/{job_id}/events", timeout=httpx.Timeout(timeout_s, read=timeout_s)) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
                if "t_first_event" not in rec:
                    rec["t_first_event"] = round(time.perf_counter() - t0, 2)
            elif line.startswith("data:") and event:
                if event == "status":
                    st = json.loads(line[5:]).get("status")
                    if st == "running" and "t_running" not in rec:
                        rec["t_running"] = round(time.perf_counter() - t0, 2)
                    rec["status"] = st
                elif event == "result":
                    rec["grade"] = json.loads(line[5:]).get("grade")
                elif event == "done":
                    rec["t_done"] = round(time.perf_counter() - t0, 2)
                    return


async def student(base: str, roll: str, pw: str, problem: str, path: Path, start: asyncio.Event,
                  timeout_s: float) -> dict:
    rec = {"roll": roll, "problem": problem, "file": path.name}
    async with httpx.AsyncClient(base_url=base, timeout=60) as c:
        try:
            for attempt in range(20):
                r = await c.post("/api/auth/login", json={"roll_no": roll, "password": pw})
                if r.status_code != 429:
                    break
                rec["login_429"] = rec.get("login_429", 0) + 1
                await asyncio.sleep(5)
            r.raise_for_status()
            new_pw = pw + "x9"
            r = await c.post("/api/auth/change-password", json={"old": pw, "new": new_pw})
            r.raise_for_status()
            src = path.read_text(encoding="utf-8") + f"\n/* load test {roll} */\n"
            await start.wait()
            t0 = time.perf_counter()
            r = await c.post(f"/api/problems/{problem}/submissions", json={"filename": path.name, "source": src})
            rec["submit_status"] = r.status_code
            r.raise_for_status()
            rec["t_submit"] = round(time.perf_counter() - t0, 2)
            await follow(c, r.json()["job_id"], rec, t0, timeout_s)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
    return rec


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--admin", default="admin")
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--prefix", default="L")
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--out", default="docs/load/load_run.json")
    a = ap.parse_args()

    admin = await admin_client(a.base, a.admin, a.admin_password)
    creds = await import_students(admin, a.n, a.prefix)
    plan = pick_files(a.n)
    start = asyncio.Event()
    tasks = [asyncio.create_task(student(a.base, roll, creds[roll], prob, path, start, a.timeout))
             for roll, (prob, path) in zip(sorted(creds), plan)]
    await asyncio.sleep(3)  # let everyone log in first
    t_wall = time.perf_counter()
    start.set()
    recs = await asyncio.gather(*tasks)
    wall = time.perf_counter() - t_wall
    queue = (await admin.get("/api/staff/queue")).json()
    await admin.aclose()

    done = [r for r in recs if "t_done" in r]
    lat = [r["t_done"] for r in done]
    wait = [r["t_running"] for r in done if "t_running" in r]
    summary = {
        "n": a.n, "completed": len(done), "errors": [r for r in recs if "error" in r],
        "wall_s": round(wall, 1), "throughput_per_min": round(len(done) / wall * 60, 2) if wall else None,
        "latency_s": {"p50": pct(lat, 50), "p90": pct(lat, 90), "max": max(lat) if lat else None,
                      "mean": round(statistics.mean(lat), 1) if lat else None},
        "queue_wait_s": {"p50": pct(wait, 50), "p90": pct(wait, 90), "max": max(wait) if wait else None},
        "login_429_total": sum(r.get("login_429", 0) for r in recs),
        "queue_after": queue.get("counts"),
    }
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "students": recs}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
