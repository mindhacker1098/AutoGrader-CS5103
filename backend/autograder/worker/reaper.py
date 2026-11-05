"""Requeue jobs whose worker stopped sending heartbeats (DESIGN section 8)."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.engine import Engine

from ..db import repo
from ..db.session import new_session

STALE_AFTER_S = 60
REAP_EVERY_S = 15

log = logging.getLogger("autograder.worker")


def reap_once(engine: Engine, max_age_s: float = STALE_AFTER_S) -> tuple[int, int]:
    """Returns (requeued, failed)."""
    with new_session(engine) as s:
        return repo.requeue_stale(s, max_age_s)


def alive_workers(engine: Engine, max_age_s: float = STALE_AFTER_S) -> set[str]:
    """Workers with a running job whose heartbeat is fresh."""
    from datetime import timedelta

    from sqlmodel import select

    from ..db.models import Job, utcnow

    cutoff = utcnow() - timedelta(seconds=max_age_s)
    with new_session(engine) as s:
        rows = s.exec(select(Job.worker_id).where(Job.status == "running", Job.heartbeat_at >= cutoff).distinct()).all()
    return {w for w in rows if w}


async def reap_containers(engine: Engine, worker_id: str, instance: str) -> int:
    from ..core.sandbox.docker import remove_orphan_containers

    alive = await asyncio.to_thread(alive_workers, engine)
    return await remove_orphan_containers(alive, worker_id, instance)


async def reaper_loop(engine: Engine, stop: asyncio.Event, *, every_s: float = REAP_EVERY_S,
                      max_age_s: float = STALE_AFTER_S, worker_id: str | None = None,
                      instance: str | None = None) -> None:
    while not stop.is_set():
        try:
            requeued, failed = await asyncio.to_thread(reap_once, engine, max_age_s)
            if requeued or failed:
                log.warning("reaper: requeued %d stale job(s), failed %d", requeued, failed)
        except Exception as e:  # noqa: BLE001
            log.error("reaper: %s", e)
        if worker_id and instance:
            try:
                n = await reap_containers(engine, worker_id, instance)
                if n:
                    log.warning("reaper: removed %d orphaned sandbox container(s)", n)
            except Exception as e:  # noqa: BLE001
                log.debug("reaper: container sweep skipped: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), every_s)
        except TimeoutError:
            pass
