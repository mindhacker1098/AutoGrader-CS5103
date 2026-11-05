"""Sandbox protocol and the helpers every backend shares."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, Protocol

Status = Literal["ok", "timeout", "crash", "nonzero_exit"]

FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$", re.ASCII)
_FLAG_RE = re.compile(r"^[-A-Za-z0-9=,_.+:/]+$", re.ASCII)

SIGNAL_NAMES = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 11: "SIGSEGV", 24: "SIGXCPU"}


@dataclass
class RunResult:
    code: int
    stdout: bytes
    stderr: bytes
    ms: int
    timed_out: bool = False
    status: Status = "ok"
    signal: int | None = None
    truncated: bool = False
    host_killed: bool = False

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class Sandbox(Protocol):
    kind: str

    async def write_file(self, name: str, data: bytes | str) -> None: ...

    async def sh(
        self,
        cmd: str,
        *,
        input: bytes | None = None,
        timeout_s: float,
        mem_mb: int = 0,
        cpu_s: float | None = None,
        env: dict | None = None,
        fsize_mb: int = 16,
    ) -> RunResult: ...

    async def destroy(self) -> None: ...


def check_file_name(name: str) -> str:
    if not FILE_NAME_RE.match(name):
        raise ValueError(f"illegal sandbox file name: {name}")
    return name


def safe_flags(flags: str | None) -> str:
    """Compiler flags from rules.json may only contain plain flag characters."""
    tokens = str(flags or "").split()
    for t in tokens:
        if not _FLAG_RE.match(t):
            raise ValueError(f"rejected unsafe compiler flag token: {t}")
    return " ".join(tokens)


def sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def limited_command(
    cmd: str, *, timeout_s: float, mem_mb: float = 0, cpu_s: float | None = None, fsize_mb: int = 16
) -> str:
    t = max(1, math.ceil(timeout_s))
    pl = [f"--fsize={fsize_mb * 1024 * 1024}"]
    if mem_mb:
        pl.append(f"--as={math.floor(mem_mb * 1024 * 1024)}")
    if cpu_s:
        pl.append(f"--cpu={max(1, math.ceil(cpu_s))}")
    return f"timeout -s KILL {t} prlimit {' '.join(pl)} -- {cmd}"


def host_kill_after(timeout_s: float) -> float:
    """Seconds after which the host side gives up on a command."""
    return math.ceil(timeout_s) + 15


def classify_run(code: int, ms: float, timeout_s: float, host_killed: bool = False) -> tuple[Status, int | None]:
    timed_out = code == 137 and ms >= timeout_s * 1000 * 0.8
    if host_killed or timed_out:
        status: Status = "timeout"
    elif code >= 128:
        status = "crash"
    elif code != 0:
        status = "nonzero_exit"
    else:
        status = "ok"
    signal = code - 128 if code >= 128 and status == "crash" else None
    return status, signal


def signal_name(sig: int | None) -> str:
    if not sig:
        return ""
    return SIGNAL_NAMES.get(sig, f"signal {sig}")
