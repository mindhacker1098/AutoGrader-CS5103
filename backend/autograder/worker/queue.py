"""Async wrappers around the job table.

Every call opens its own short session in a worker thread, so slow SQLite
locks never stall the event loop that is driving the sandboxes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Self, TypeVar

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session

from ..db import repo
from ..db.models import Job, JobEvent, WorkerBeat, utcnow
from ..db.session import new_session

T = TypeVar("T")


class JobQueue:
    def __init__(self, engine: Engine, worker_id: str):
        self.engine = engine
        self.worker_id = worker_id

    def session(self) -> Session:
        return new_session(self.engine)

    def _with_session(self, fn: Callable[..., T], *args: Any, **kw: Any) -> T:
        with self.session() as s:
            return fn(s, *args, **kw)

    async def call(self, fn: Callable[..., T], *args: Any, **kw: Any) -> T:
        """Run fn(session, *args) in a thread with a fresh session."""
        return await asyncio.to_thread(self._with_session, fn, *args, **kw)

    async def claim(self, kinds: Sequence[str] | None = None) -> Job | None:
        return await self.call(repo.claim_job, self.worker_id, kinds)

    async def heartbeat(self, job_id: int) -> bool:
        return await self.call(repo.heartbeat, job_id, self.worker_id)

    async def status(self, job_id: int) -> str | None:
        def get(s: Session) -> str | None:
            job = s.get(Job, job_id)
            return job.status if job else None
        return await self.call(get)

    async def finish(self, job_id: int, status: str = "done", error: str | None = None, *,
                     result: dict | None = None) -> None:
        def fin(s: Session) -> None:
            s.execute(update(Job).where(Job.id == job_id, Job.worker_id == self.worker_id, Job.status == "running")
                      .values(status=status, finished_at=utcnow(), error=error, result=result))
            s.commit()
        await self.call(fin)

    async def beat(self, host: str, pid: int, lanes: str, busy: int) -> None:
        def go(s: Session) -> None:
            row = s.get(WorkerBeat, self.worker_id) or WorkerBeat(worker_id=self.worker_id, started_at=utcnow())
            row.host, row.pid, row.lanes, row.busy, row.last_seen = host, pid, lanes, busy, utcnow()
            s.add(row)
            s.commit()
        await self.call(go)

    async def unregister(self) -> None:
        def go(s: Session) -> None:
            row = s.get(WorkerBeat, self.worker_id)
            if row is not None:
                s.delete(row)
                s.commit()
        await self.call(go)

    async def fail_or_retry(self, job_id: int, error: str, trace: str | None = None) -> str:
        """After a handler crash: requeue while attempts remain, otherwise fail."""
        def go(s: Session) -> str:
            job = s.get(Job, job_id)
            if job is None or job.status != "running" or job.worker_id != self.worker_id:
                return job.status if job else "missing"
            status = "queued" if job.attempts < job.max_attempts else "failed"
            vals: dict[str, Any] = {"status": status, "error": error, "traceback": trace}
            if status == "queued":
                vals["worker_id"] = None
            else:
                vals["finished_at"] = utcnow()
            s.execute(update(Job).where(Job.id == job_id).values(**vals))
            s.commit()
            return status
        return await self.call(go)

    async def release(self, job_ids: Sequence[int]) -> int:
        """Put jobs this worker is still holding back in the queue (shutdown)."""
        if not job_ids:
            return 0

        def go(s: Session) -> int:
            n = s.execute(update(Job).where(Job.id.in_(list(job_ids)), Job.worker_id == self.worker_id,
                                            Job.status == "running")
                          .values(status="queued", worker_id=None, attempts=Job.attempts - 1)).rowcount
            s.commit()
            return n
        return await self.call(go)

    async def enqueue(self, kind: str, **kw: Any) -> Job:
        return await self.call(repo.enqueue_job, kind, **kw)

    async def add_events(self, job_id: int, events: Sequence[dict]) -> None:
        if not events:
            return

        def go(s: Session) -> None:
            s.add_all(JobEvent(job_id=job_id, ts=e.get("ts") or utcnow(), kind=str(e.get("kind") or "info"),
                               metric=e.get("metric"), msg=str(e.get("msg") or "")) for e in events)
            s.commit()
        await self.call(go)


class EventSink:
    """Collects coordinator log events and writes them to job_event in batches."""

    def __init__(self, queue: JobQueue, job_id: int, interval_s: float = 0.5):
        self.queue = queue
        self.job_id = job_id
        self.interval_s = interval_s
        self.pending: list[dict] = []
        self._task: asyncio.Task | None = None

    def __call__(self, e: dict) -> None:
        self.pending.append({**e, "ts": utcnow()})

    async def flush(self) -> None:
        batch, self.pending = self.pending, []
        try:
            await self.queue.add_events(self.job_id, batch)
        except Exception:  # noqa: BLE001
            self.pending[:0] = batch

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            await self.flush()

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()
