"""Worker process: claims jobs from the DB and runs them.

    python -m autograder.worker --jobs 2 --sandboxes 3

Each lane is a group of job kinds with its own number of slots, so slow
background work (enrich, profile, scans) never takes a grading slot. The
default is one lane of --jobs slots for grade/regrade and one slot for the
rest; --lanes "grade+regrade=2,enrich+profile=1,plagiarism_scan+corpus_gen=1"
overrides it.

Ctrl+C once stops claiming and lets running jobs finish; a second Ctrl+C
cancels them and puts them back in the queue.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import traceback
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from ..config import Settings, get_settings
from ..core.sandbox.proc import worker_label
from ..db.models import JOB_KINDS, Job
from ..db.session import get_engine, init_db
from .handlers import HANDLERS, JobContext, JobFailed
from .queue import EventSink, JobQueue
from .reaper import reaper_loop

HEARTBEAT_S = 5
POLL_S = 1.0

log = logging.getLogger("autograder.worker")


def human_error(e: BaseException) -> str:
    """One sentence for the UI; the traceback goes to the log and the staff-only traceback field."""
    text = (str(e).strip().splitlines() or [""])[0][:300] or type(e).__name__
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


@dataclass
class Lane:
    kinds: tuple[str, ...]
    slots: int

    @property
    def name(self) -> str:
        return "+".join(self.kinds)


def parse_lanes(spec: str | None, jobs: int) -> list[Lane]:
    if not spec:
        return [Lane(("grade", "regrade"), jobs),
                Lane(("enrich", "profile", "plagiarism_scan", "corpus_gen"), 1)]
    lanes = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        kinds_s, _, n = part.partition("=")
        kinds = tuple(k.strip() for k in kinds_s.split("+") if k.strip())
        bad = [k for k in kinds if k not in JOB_KINDS]
        if bad or not kinds:
            raise ValueError(f"bad lane {part!r}: unknown kind(s) {', '.join(bad) or '(none)'}")
        lanes.append(Lane(kinds, int(n) if n.strip() else 1))
    return lanes


class Worker:
    def __init__(self, settings: Settings | None = None, *, engine: Engine | None = None, jobs: int | None = None,
                 sandboxes: int | None = None, lanes: list[Lane] | None = None, worker_id: str | None = None,
                 poll_s: float = POLL_S, heartbeat_s: float = HEARTBEAT_S, reaper: bool = True):
        self.settings = settings or get_settings()
        self.engine = engine or get_engine(self.settings)
        jobs = jobs or self.settings.WORKER_JOBS
        self.lanes = lanes or parse_lanes(None, jobs)
        if worker_id:
            # sandbox containers are labelled from AG_WORKER_ID, so cleanup finds them
            os.environ["AG_WORKER_ID"] = worker_id
        self.worker_id = worker_id or worker_label()
        self.queue = JobQueue(self.engine, self.worker_id)
        self.sandbox_pool = asyncio.Semaphore(max(1, sandboxes or self.settings.MAX_SANDBOXES))
        self.llm_pool = asyncio.Semaphore(1 if self.settings.LLM_PROFILE == "laptop" else max(1, jobs))
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        self.reaper = reaper
        self.stop = asyncio.Event()
        self.running: dict[int, asyncio.Task] = {}
        self.done_count = 0
        self._max_jobs: int | None = None

    # one job

    async def _heartbeat(self, job_id: int, task: asyncio.Task) -> None:
        while not task.done():
            await asyncio.sleep(self.heartbeat_s)
            try:
                ok = await self.queue.heartbeat(job_id)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat for job %d failed: %s", job_id, e)
                continue
            if not ok:
                status = await self.queue.status(job_id)
                log.warning("job %d is now %s; stopping it", job_id, status)
                task.cancel()
                return

    async def run_job(self, job: Job) -> str:
        """Run a claimed job to the end; returns the final status."""
        handler = HANDLERS.get(job.kind)
        async with EventSink(self.queue, job.id) as sink:
            sink({"kind": "status", "msg": f"running {job.kind} (attempt {job.attempts}) on {self.worker_id}"})
            if handler is None:
                sink({"kind": "error", "msg": f"no handler for job kind {job.kind!r}"})
                await sink.flush()
                await self.queue.finish(job.id, "failed", f"This worker cannot run {job.kind} jobs.")
                return "failed"
            jc = JobContext(job, self.queue, self.settings, sink, self.sandbox_pool, self.llm_pool)
            task = asyncio.create_task(handler(jc))
            beat = asyncio.create_task(self._heartbeat(job.id, task))
            try:
                out = await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if asyncio.current_task().cancelling():
                    raise
                sink({"kind": "warn", "msg": "job stopped: it is no longer assigned to this worker"})
                return "cancelled"
            except JobFailed as e:
                # a definite failure (bad input, nothing produced): retrying would not help
                sink({"kind": "error", "msg": str(e)})
                await sink.flush()
                await self.queue.finish(job.id, "failed", str(e), result=e.result)
                return "failed"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log.exception("job %d (%s) failed: %s", job.id, job.kind, err)
                sink({"kind": "error", "msg": err})
                await sink.flush()
                status = await self.queue.fail_or_retry(job.id, human_error(e), traceback.format_exc()[-4000:])
                sink({"kind": "status", "msg": f"job {status}"})
                return status
            finally:
                beat.cancel()
            sink({"kind": "done", "msg": f"{job.kind} done: {out}"})
        await self.queue.finish(job.id, "done", result=out if isinstance(out, dict) else None)
        return "done"

    # loops

    async def _slot(self, lane: Lane, n: int) -> None:
        while not self.stop.is_set():
            if self._max_jobs is not None and self.done_count >= self._max_jobs:
                self.stop.set()
                break
            try:
                job = await self.queue.claim(lane.kinds)
            except Exception as e:  # noqa: BLE001
                log.error("claim failed on lane %s: %s", lane.name, e)
                job = None
            if job is None:
                try:
                    await asyncio.wait_for(self.stop.wait(), self.poll_s)
                except TimeoutError:
                    pass
                continue
            log.info("[%s/%d] job %d %s", lane.name, n, job.id, job.kind)
            task = asyncio.create_task(self.run_job(job))
            self.running[job.id] = task
            try:
                status = await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                status = "released"
            finally:
                self.running.pop(job.id, None)
                self.done_count += 1
            log.info("[%s/%d] job %d %s", lane.name, n, job.id, status)

    async def _beat_loop(self) -> None:
        """Keep this worker's row in worker_beat fresh so the UI can tell idle from dead."""
        lanes = ", ".join(f"{lane.name}={lane.slots}" for lane in self.lanes)
        host = socket.gethostname()
        while True:
            try:
                await self.queue.beat(host, os.getpid(), lanes, len(self.running))
            except Exception as e:  # noqa: BLE001
                log.warning("worker heartbeat failed: %s", e)
            try:
                await asyncio.wait_for(self.stop.wait(), self.heartbeat_s)
                return
            except TimeoutError:
                pass

    async def run(self, *, max_jobs: int | None = None) -> None:
        """Serve jobs until stop is set (or max_jobs jobs have run)."""
        self._max_jobs = max_jobs
        init_db(self.engine)
        bg = [asyncio.create_task(reaper_loop(self.engine, self.stop, worker_id=self.worker_id,
                                              instance=self.settings.instance_id))] if self.reaper else []
        bg.append(asyncio.create_task(self._beat_loop()))
        slots = [asyncio.create_task(self._slot(lane, i + 1)) for lane in self.lanes for i in range(lane.slots)]
        log.info("worker %s up: lanes %s, %d sandbox(es)", self.worker_id,
                 ", ".join(f"{lane.name}={lane.slots}" for lane in self.lanes), self.sandbox_pool._value)
        try:
            await asyncio.gather(*slots)
        finally:
            for t in slots:
                t.cancel()
            await asyncio.gather(*slots, return_exceptions=True)
            self.stop.set()
            await asyncio.gather(*bg, return_exceptions=True)
            await self._cleanup()
            try:
                await self.queue.unregister()
            except Exception as e:  # noqa: BLE001
                log.debug("could not remove the heartbeat row: %s", e)

    async def cancel_running(self) -> None:
        held = list(self.running)
        for t in list(self.running.values()):
            t.cancel()
        n = await self.queue.release(held)
        if n:
            log.warning("released %d running job(s) back to the queue", n)

    async def _cleanup(self) -> None:
        if self.running:
            await self.cancel_running()
        try:
            from ..core.sandbox.docker import remove_worker_containers
            n = await remove_worker_containers(self.worker_id)
            if n:
                log.info("removed %d leftover sandbox container(s)", n)
        except Exception as e:  # noqa: BLE001
            log.debug("container cleanup skipped: %s", e)


def _install_signals(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    state = {"hits": 0}

    def on_signal(signum, _frame) -> None:
        state["hits"] += 1
        if state["hits"] == 1:
            log.warning("stopping: no new jobs; waiting for %d running job(s) (Ctrl+C again to cancel them)",
                        len(worker.running))
            loop.call_soon_threadsafe(worker.stop.set)
        else:
            log.warning("cancelling running jobs")
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(worker.cancel_running()))

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass


async def amain(args: argparse.Namespace) -> None:
    settings = get_settings()
    worker = Worker(settings, jobs=args.jobs, sandboxes=args.sandboxes,
                    lanes=parse_lanes(args.lanes, args.jobs or settings.WORKER_JOBS), worker_id=args.worker_id,
                    reaper=not args.no_reaper)
    _install_signals(worker)
    await worker.run(max_jobs=args.max_jobs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m autograder.worker", description="AutoGrader job worker")
    ap.add_argument("--jobs", type=int, default=None, help="grading slots (default WORKER_JOBS)")
    ap.add_argument("--sandboxes", type=int, default=None, help="sandboxes alive at once (default MAX_SANDBOXES)")
    ap.add_argument("--lanes", default=None, help='e.g. "grade+regrade=2,enrich+profile=1,plagiarism_scan+corpus_gen=1"')
    ap.add_argument("--worker-id", default=None)
    ap.add_argument("--max-jobs", type=int, default=None, help="exit after this many jobs")
    ap.add_argument("--no-reaper", action="store_true",
                    help="do not requeue stale jobs or sweep orphaned sandboxes (another worker does it)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        parse_lanes(args.lanes, args.jobs or 1)
    except ValueError as e:
        ap.error(str(e))
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130
    return 0
