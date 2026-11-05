"""WSL sandbox for Windows hosts without Docker.

Strong mode goes through /usr/local/bin/ag-run (sandbox/ag-run, installed by
sandbox/wsl_setup.sh): separate net/pid namespaces and the agrun user. Without
ag-run it falls back to plain sh + timeout + prlimit as the default WSL user,
which is marked weak.
"""
from __future__ import annotations

import base64
import sys
import uuid

from ..manifest import check_permission
from .base import RunResult, check_file_name, host_kill_after, limited_command, sh_quote
from .proc import run_process, to_run_result

AG_RUN = "/usr/local/bin/ag-run"
_EVAL = 'eval "$(printf %s "$1" | base64 -d)"'


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _exports(env: dict | None) -> str:
    return "".join(f"export {k}={sh_quote(str(v))}; " for k, v in (env or {}).items())


async def list_distros() -> list[str]:
    r = await run_process(["wsl.exe", "-l", "-q"], kill_after_s=15)
    if r.code != 0:
        return []
    # wsl.exe prints UTF-16LE
    text = r.stdout.decode("utf-16-le", errors="replace") if b"\x00" in r.stdout else r.out
    return [ln.strip().strip("\x00") for ln in text.splitlines() if ln.strip().strip("\x00")]


async def ag_run_installed(distro: str) -> bool:
    r = await run_process(["wsl.exe", "-d", distro, "-u", "root", "-e", AG_RUN, "check"], kill_after_s=20)
    return r.code == 0 and r.out.startswith("ag-run")


class WslSandbox:
    kind = "wsl"

    def __init__(self, distro: str, sid: str, weak: bool, log=None):
        self.distro = distro
        self.id = sid
        self.weak = weak
        self.dir = f"/tmp/ag-{sid}"
        self.log = log
        # inside ag-run the shell is pid 1 of its own namespace, so kill -9 -1 is
        # contained; in weak mode it would hit every process of the WSL user
        self.kill_all = ":" if weak else "kill -9 -1 2>/dev/null"

    @staticmethod
    async def available(distro: str = "Ubuntu") -> bool:
        if sys.platform != "win32":
            return False
        r = await run_process(["wsl.exe", "-d", distro, "-e", "sh", "-c", "command -v gcc && command -v prlimit"],
                              kill_after_s=30)
        return r.code == 0

    @classmethod
    async def create(cls, *, memory_limit_mb: int = 64, log=None, distro: str = "Ubuntu",
                     force_weak: bool = False, **_) -> WslSandbox:
        check_permission("sandbox:exec", log)
        weak = force_weak or not await ag_run_installed(distro)
        sb = cls(distro, uuid.uuid4().hex, weak, log)
        if weak:
            r = await sb._wsl(["sh", "-c", f"mkdir -m 700 {sb.dir}"], kill_after_s=30)
        else:
            r = await sb._wsl([AG_RUN, "init", sb.id], kill_after_s=30, root=True)
        if r.code != 0:
            raise RuntimeError(f"could not create WSL sandbox: {r.err.strip()}")
        if log:
            how = ("weak isolation: timeout+prlimit only, ag-run not installed" if weak
                   else "ag-run: no network, own pid namespace, user agrun")
            log({"kind": "sandbox", "msg": f"WSL sandbox {sb.dir} in {distro} ({how})"})
        return sb

    def _wsl(self, argv: list[str], *, input=None, kill_after_s: float = 0, root: bool = False):
        pre = ["wsl.exe", "-d", self.distro]
        if root:
            pre += ["-u", "root"]
        return run_process(pre + ["-e", *argv], input=input, kill_after_s=kill_after_s)

    async def write_file(self, name: str, data: bytes | str) -> None:
        check_file_name(name)
        if self.weak:
            r = await self._wsl(["sh", "-c", 'cat > "$1"', "ag", f"{self.dir}/{name}"], input=data, kill_after_s=30)
        else:
            r = await self._wsl([AG_RUN, "write", self.id, name], input=data, kill_after_s=30, root=True)
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
        limited = limited_command(cmd, timeout_s=timeout_s, mem_mb=mem_mb, cpu_s=cpu_s, fsize_mb=fsize_mb)
        kill_after = host_kill_after(timeout_s)
        if self.weak:
            # nproc keeps a fork bomb from taking the whole WSL VM down
            script = f"cd {self.dir} || exit 125; prlimit --pid $$ --nproc=256:256; {_exports(env)}{limited}"
            res = await self._wsl(["sh", "-c", _EVAL, "ag", _b64(script)], input=input, kill_after_s=kill_after)
        else:
            script = f"{_exports(env)}{limited}"
            res = await self._wsl([AG_RUN, "sh", self.id, _b64(script)], input=input,
                                  kill_after_s=kill_after, root=True)
        return to_run_result(res, timeout_s)

    async def destroy(self) -> None:
        if self.weak:
            await self._wsl(["rm", "-rf", self.dir], kill_after_s=30)
        else:
            await self._wsl([AG_RUN, "destroy", self.id], kill_after_s=30, root=True)
        if self.log:
            self.log({"kind": "sandbox", "msg": f"WSL sandbox {self.dir} destroyed"})
