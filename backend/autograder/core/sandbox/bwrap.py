"""bubblewrap sandbox for Linux hosts: no network, private pid namespace, read-only system."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from ..manifest import check_permission
from .base import RunResult, check_file_name, host_kill_after, limited_command
from .proc import run_process, to_run_result


class BwrapSandbox:
    kind = "bwrap"
    # the pid namespace dies with bwrap, so leftovers are reaped after every sh()
    kill_all = ":"

    def __init__(self, workdir: Path, log=None):
        self.dir = workdir
        self.log = log

    @staticmethod
    async def available() -> bool:
        if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
            return False
        r = await run_process(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"], kill_after_s=10)
        return r.code == 0

    @classmethod
    async def create(cls, *, memory_limit_mb: int = 64, log=None, **_) -> BwrapSandbox:
        check_permission("sandbox:exec", log)
        d = Path(tempfile.mkdtemp(prefix="autograder-"))
        if log:
            log({"kind": "sandbox", "msg": f"bwrap sandbox in {d} (no network, private pid/ipc namespaces)"})
        return cls(d, log)

    async def write_file(self, name: str, data: bytes | str) -> None:
        check_file_name(name)
        (self.dir / name).write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

    def _argv(self, script: str, env: dict | None) -> list[str]:
        argv = [
            "bwrap", "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(self.dir), "/work", "--chdir", "/work",
            "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/work",
        ]
        for k, v in (env or {}).items():
            argv += ["--setenv", str(k), str(v)]
        return argv + ["sh", "-c", script]

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
        res = await run_process(self._argv(limited, env), input=input, kill_after_s=host_kill_after(timeout_s))
        return to_run_result(res, timeout_s)

    async def destroy(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
