"""Docker sandbox: one throwaway container per sandbox."""
from __future__ import annotations

import math

from ..manifest import check_permission
from .base import RunResult, check_file_name, host_kill_after, limited_command
from .proc import run_process, to_run_result, worker_label

DEFAULT_IMAGE = "autograder-sandbox:1"
LABEL = "autograder.worker"
INSTANCE_LABEL = "autograder.instance"
# A plain "sleep 3600" as pid 1 never reaps the zombies a killed fork
# bomb leaves behind, so the pids limit stays full. bash as pid 1 reaps orphans
# each time read times out (stdin is kept open by -i, so read blocks without
# forking). pid 1 is immune to kill -9 -1 and still exits after an hour.
KEEPALIVE = "trap : CHLD; while [ $SECONDS -lt 3600 ]; do read -r -t 1 _; done"


class DockerSandbox:
    kind = "docker"
    # pid 1 survives; this cleans up fork bombs and leftover children
    kill_all = "kill -9 -1 2>/dev/null"

    def __init__(self, cid: str, log=None, image: str = DEFAULT_IMAGE):
        self.id = cid
        self.log = log
        self.image = image

    @staticmethod
    async def available(image: str = DEFAULT_IMAGE) -> bool:
        r = await run_process(["docker", "image", "inspect", image, "--format", "{{.Id}}"], kill_after_s=10)
        return r.code == 0

    @classmethod
    async def create(
        cls, *, memory_limit_mb: int = 64, log=None, image: str = DEFAULT_IMAGE, worker_id: str | None = None,
        instance: str | None = None,
    ) -> DockerSandbox:
        check_permission("sandbox:exec", log)
        container_mem = max(768, math.floor((memory_limit_mb or 64) * 6))
        args = [
            "docker", "run", "-d", "-i", "--rm",
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/work:rw,exec,nosuid,size=64m,uid=10001,gid=10001",
            "--tmpfs", "/tmp:rw,exec,nosuid,size=64m,uid=10001,gid=10001",
            "--pids-limit", "64",
            "--memory", f"{container_mem}m",
            # no swap: an unlimited run (ASan, valgrind) would otherwise page the VM for seconds
            "--memory-swap", f"{container_mem}m",
            "--cpus", "1",
            "--workdir", "/work",
            "--label", f"{LABEL}={worker_id or worker_label()}",
            *(["--label", f"{INSTANCE_LABEL}={instance}"] if instance else []),
            image, "bash", "-c", KEEPALIVE,
        ]
        r = await run_process(args, kill_after_s=60)
        if r.code != 0:
            raise RuntimeError(f"could not start sandbox container: {r.err.strip() or r.out.strip()}")
        cid = r.out.strip()
        if log:
            log({
                "kind": "sandbox",
                "msg": f"sandbox container {cid[:12]} started (network none, caps dropped, read-only fs, "
                       f"pids<=64, mem<={container_mem}MB)",
            })
        return cls(cid, log, image)

    async def write_file(self, name: str, data: bytes | str) -> None:
        check_file_name(name)
        r = await run_process(["docker", "exec", "-i", self.id, "sh", "-c", f"cat > /work/{name}"],
                              input=data, kill_after_s=30)
        if r.code != 0:
            raise RuntimeError(f"write {name} failed: {r.err}")

    async def sh(
        self,
        cmd: str,
        *,
        input: bytes | None = None,
        timeout_s: float = 30,
        mem_mb: int = 0,
        cpu_s: float | None = None,
        env: dict | None = None,
        fsize_mb: int = 16,
    ) -> RunResult:
        env_args: list[str] = []
        for k, v in (env or {}).items():
            env_args += ["-e", f"{k}={v}"]
        limited = limited_command(cmd, timeout_s=timeout_s, mem_mb=mem_mb, cpu_s=cpu_s, fsize_mb=fsize_mb)
        full = f"{limited}; rc=$?; {self.kill_all}; exit $rc"
        res = await run_process(["docker", "exec", "-i", *env_args, self.id, "sh", "-c", full],
                                input=input, kill_after_s=host_kill_after(timeout_s))
        return to_run_result(res, timeout_s)

    async def destroy(self) -> None:
        await run_process(["docker", "rm", "-f", self.id], kill_after_s=30)
        if self.log:
            self.log({"kind": "sandbox", "msg": f"sandbox container {self.id[:12]} destroyed"})


async def remove_orphan_containers(alive: set[str], self_id: str, instance: str, min_age_s: float = 120) -> int:
    """Remove sandboxes of workers that died without cleaning up (killed, crashed, power loss).

    A worker only holds containers while it runs a job, and running jobs send heartbeats, so a
    labelled container whose worker is not in `alive` is an orphan. Only containers of this
    instance (same database) are considered: another installation on the same Docker host has
    its own workers that this database knows nothing about. Young containers are skipped to
    avoid racing a worker that has just claimed a job.
    """
    import time
    from datetime import datetime

    r = await run_process(["docker", "ps", "-a", "--filter", f"label={LABEL}",
                           "--filter", f"label={INSTANCE_LABEL}={instance}",
                           "--format", '{{.ID}}\t{{.Label "' + LABEL + '"}}\t{{.CreatedAt}}'], kill_after_s=20)
    if r.code != 0:
        return 0
    doomed = []
    for line in r.out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        cid, owner, created = parts
        if owner == self_id or owner in alive:
            continue
        try:  # "2026-10-22 01:12:57 +0530 IST"
            age = time.time() - datetime.strptime(" ".join(created.split()[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            age = min_age_s
        if age >= min_age_s:
            doomed.append(cid)
    if doomed:
        await run_process(["docker", "rm", "-f", *doomed], kill_after_s=60)
    return len(doomed)


async def remove_worker_containers(worker_id: str) -> int:
    """Remove containers left behind by a crashed worker (only ones carrying its label)."""
    r = await run_process(["docker", "ps", "-aq", "--filter", f"label={LABEL}={worker_id}"], kill_after_s=20)
    ids = r.out.split()
    if ids:
        await run_process(["docker", "rm", "-f", *ids], kill_after_s=60)
    return len(ids)
