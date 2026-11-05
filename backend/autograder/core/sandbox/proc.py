"""Async subprocess runner with byte I/O and capped output.

Works on the Windows Proactor loop, which is what asyncio.run() gives by default.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

from .base import RunResult, classify_run

OUTPUT_CAP = 32 * 1024 * 1024
_CHUNK = 64 * 1024


@dataclass
class ProcResult:
    code: int
    stdout: bytes
    stderr: bytes
    ms: int
    truncated: bool = False
    host_killed: bool = False

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class _Capped:
    def __init__(self, cap: int):
        self.cap = cap
        self.buf = bytearray()
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(_CHUNK)
            if not chunk:
                return
            room = self.cap - len(self.buf)
            if room <= 0:
                # keep reading so the child never blocks on a full pipe
                self.truncated = True
                continue
            if len(chunk) > room:
                self.truncated = True
            self.buf += chunk[:room]


async def _feed(stdin: asyncio.StreamWriter, data: bytes | None) -> None:
    # the child may exit without reading its input
    with contextlib.suppress(OSError):
        if data:
            stdin.write(data)
            await stdin.drain()
    with contextlib.suppress(OSError, RuntimeError):
        stdin.close()


def _to_bytes(data: bytes | str | None) -> bytes | None:
    if data is None:
        return None
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


async def run_process(
    argv: list[str],
    *,
    input: bytes | str | None = None,
    kill_after_s: float = 0,
    env: dict | None = None,
    cap: int = OUTPUT_CAP,
) -> ProcResult:
    """Run argv, feed input on stdin, collect stdout/stderr (each capped).

    If kill_after_s > 0 the process is killed from the host once that much time
    has passed; host_killed is then set.
    """
    started = time.monotonic()
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **kwargs,
        )
    except OSError as e:
        return ProcResult(-1, b"", str(e).encode(), _ms(started))

    out, err = _Capped(cap), _Capped(cap)
    readers = [
        asyncio.ensure_future(out.drain(proc.stdout)),
        asyncio.ensure_future(err.drain(proc.stderr)),
    ]
    feeder = asyncio.ensure_future(_feed(proc.stdin, _to_bytes(input)))
    killed = False

    async def finish() -> None:
        await asyncio.gather(*readers)
        await proc.wait()

    task = asyncio.ensure_future(finish())
    try:
        done, _ = await asyncio.wait([task], timeout=kill_after_s if kill_after_s > 0 else None)
        if not done:
            killed = True
            _kill(proc)
            # grandchildren may still hold the pipes, so do not wait on them forever
            done, _ = await asyncio.wait([task], timeout=10)
            if not done:
                task.cancel()
                for t in readers:
                    t.cancel()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    pass
    except asyncio.CancelledError:
        _kill(proc)
        task.cancel()
        for t in readers:
            t.cancel()
        raise
    finally:
        if not feeder.done():
            feeder.cancel()

    code = proc.returncode
    return ProcResult(
        code if code is not None else -1,
        bytes(out.buf),
        bytes(err.buf),
        _ms(started),
        truncated=out.truncated or err.truncated,
        host_killed=killed,
    )


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except OSError:
        pass


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def merged_env(extra: dict | None) -> dict:
    env = dict(os.environ)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def to_run_result(res: ProcResult, timeout_s: float) -> RunResult:
    """Attach a status (ok, timeout, crash, ...) to a finished host process."""
    status, sig = classify_run(res.code, res.ms, timeout_s, res.host_killed)
    return RunResult(
        code=res.code,
        stdout=res.stdout,
        stderr=res.stderr,
        ms=res.ms,
        timed_out=status == "timeout",
        status=status,
        signal=sig,
        truncated=res.truncated,
        host_killed=res.host_killed,
    )


def worker_label() -> str:
    return os.environ.get("AG_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
