"""Native sandbox for Linux lab machines without Docker (timeout + prlimit only)."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from ..manifest import check_permission
from .base import RunResult, check_file_name, host_kill_after, limited_command, sh_quote
from .proc import merged_env, run_process, to_run_result


class NativeSandbox:
    kind = "native"
    # never kill -9 -1 on the host
    kill_all = ":"

    def __init__(self, workdir: Path, log=None):
        self.dir = workdir
        self.log = log

    @staticmethod
    async def available() -> bool:
        if not sys.platform.startswith("linux"):
            return False
        r = await run_process(["sh", "-c", "command -v gcc && command -v timeout && command -v prlimit"], kill_after_s=5)
        return r.code == 0

    @classmethod
    async def create(cls, *, memory_limit_mb: int = 64, log=None, **_) -> NativeSandbox:
        check_permission("sandbox:exec", log)
        d = Path(tempfile.mkdtemp(prefix="autograder-"))
        if log:
            log({"kind": "sandbox", "msg": f"native sandbox in {d} (weaker isolation: no network/caps isolation)"})
        return cls(d, log)

    async def write_file(self, name: str, data: bytes | str) -> None:
        check_file_name(name)
        (self.dir / name).write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

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
        limited = limited_command(cmd, timeout_s=timeout_s, mem_mb=mem_mb, cpu_s=cpu_s, fsize_mb=fsize_mb)
        full = f"cd {sh_quote(str(self.dir))} && {limited}"
        res = await run_process(["sh", "-c", full], input=input, kill_after_s=host_kill_after(timeout_s),
                                env=merged_env(env))
        return to_run_result(res, timeout_s)

    async def destroy(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
