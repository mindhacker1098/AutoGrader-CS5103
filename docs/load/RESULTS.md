# Load and crash tests (22 Oct 2026)

Laptop: GTX 1650 (4 GB) + 24 GB RAM, Docker Desktop sandbox, SQLite, one worker with `--jobs 2 --sandboxes 3`,
Ollama 0.40.7 with qwen2.5-coder:7b on the GPU (55% CPU / 45% GPU).

Script: `backend/tests/load/load_test.py`. Each simulated student is imported through the staff API, logs in,
changes the forced password, submits a seed file with a unique trailing comment (so the result cache cannot
answer it), and follows the live SSE stream until the grade arrives. Raw data is in `docs/load/*.json`.

## A. 30 students at the same moment, LLM off (sandbox + queue)

| | before fixes | after fixes |
|---|---|---|
| Students who received a grade | 2 / 30 | **30 / 30** |
| HTTP 500 / dropped streams | 13 / 15 | **0 / 0** |
| `QueuePool limit reached` in the API log | yes | **0** |
| Login refusals (all students behind one IP) | n/a | **0** |
| Throughput | n/a | 5.0 submissions/min (about 12 s each) |
| Submit-to-grade time, median / p90 / max | n/a | 191 s / 329 s / 360 s |
| Same file, different student | n/a | identical grade in all 15 pairs |
| Duplicate submissions | n/a | all 15 pairs flagged `suspected_copy_peer` (originality 0) |

The grades differ from `seeds/summary.json` only where expected: originality is 0 because every file was
submitted twice, and readability uses the deterministic heuristic because the LLM was off.

## B. 10 students, live LLM on the GPU, worker killed mid-run

| | result |
|---|---|
| Warm-up: 15 seed submissions graded with the live LLM | 706 s (47 s each; the single LLM slot is the bottleneck) |
| Students who received a grade | **10 / 10**, 0 errors |
| Worker 1 hard-killed (taskkill /F) 90 s into the test | worker 2 started 5 s later |
| Stale jobs requeued by the reaper | 3 (2 at +56 s, 1 at +71 s); none failed |
| Orphaned sandbox of the dead worker | removed by worker 2's reaper (+119 s) |
| Throughput with the live LLM | 1.25 submissions/min (about 48 s each) |
| Submit-to-grade time, median / p90 / max | 287 s / 448 s / 480 s |
| API errors | 0 |

## Bugs the load tests found (all fixed, with tests)
1. **SSE held a database connection for the whole stream.** With 15 or more students watching their grading live,
   the SQLAlchemy pool (5 + 10) ran out, and later requests got HTTP 500 or dropped streams.
   - Fix: the request session is closed before streaming (`api/sse.py`), and the SQLite pool was raised to
     20 + 40 (`db/session.py`).
2. **The login rate limit counted successful logins per IP (30/min).** A class behind one campus NAT would have
   been locked out.
   - Fix: only failed logins count per IP. There is a high cap on all attempts, a per-roll limit, and
     `TRUSTED_PROXIES` for X-Forwarded-For.
   - Test: `test_class_behind_one_nat_can_log_in`.
3. **A hard-killed worker left its sandbox containers running for up to an hour.**
   - Fix: each worker's reaper removes containers whose owner has no fresh heartbeat (older than 2 minutes).
   - Tests: `test_orphan_containers.py`.

## Capacity planning
- Without the LLM, one laptop worker grades about 5 submissions per minute, so a class of 60 is done in about 12 minutes.
- With the live 7B LLM, the laptop does about 1.25 per minute (60 students in about 48 minutes). The LLM is the
  bottleneck, not the sandbox.
- For a live lab test on the lab server:
  - run 2-4 workers or `--jobs 4`
  - serve the model with batching (vLLM, or Ollama with `OLLAMA_NUM_PARALLEL>1`)
  - move the readability review to the low-priority enrich lane, so students see the execution results first
