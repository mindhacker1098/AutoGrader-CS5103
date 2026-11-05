"""Pick a sandbox backend (DESIGN section 5) and small helpers around it."""
from __future__ import annotations

import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .base import Sandbox
from .bwrap import BwrapSandbox
from .docker import DockerSandbox
from .native import NativeSandbox
from .wsl import WslSandbox, ag_run_installed

_CACHE_S = 60.0
_avail: dict[tuple, tuple[float, bool]] = {}


def _settings(settings):
    if settings is None:
        from ...config import get_settings
        settings = get_settings()
    return settings


async def _cached(key: tuple, check) -> bool:
    hit = _avail.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_S:
        return hit[1]
    ok = await check()
    _avail[key] = (now, ok)
    return ok


async def _docker_ok(s) -> bool:
    return await _cached(("docker", s.SANDBOX_IMAGE), lambda: DockerSandbox.available(s.SANDBOX_IMAGE))


async def _wsl_ok(s) -> bool:
    return await _cached(("wsl", s.WSL_DISTRO), lambda: WslSandbox.available(s.WSL_DISTRO))


async def _ag_run_ok(s) -> bool:
    return await _cached(("ag-run", s.WSL_DISTRO), lambda: ag_run_installed(s.WSL_DISTRO))


async def _bwrap_ok() -> bool:
    return await _cached(("bwrap",), BwrapSandbox.available)


async def _native_ok() -> bool:
    return await _cached(("native",), NativeSandbox.available)


def _mem(limits) -> int:
    if isinstance(limits, dict):
        return limits.get("memory_limit_mb") or 64
    return int(limits or 64)


class SandboxUnavailable(RuntimeError):
    """The expected sandbox is not usable right now; the job should fail rather than run weaker."""


_docker_seen: set[str] = set()


async def resolve_kind(settings=None) -> str:
    """The backend SANDBOX selects: docker, wsl, bwrap or native.

    Never falls back silently: once Docker has worked in this process, auto
    mode raises instead of moving to another backend, and the weak backends
    (WSL without ag-run, native) need ALLOW_WEAK_SANDBOX or an explicit SANDBOX.
    """
    s = _settings(settings)
    want = (s.SANDBOX or "auto").lower()
    weak_ok = bool(getattr(s, "ALLOW_WEAK_SANDBOX", False))
    if want in ("auto", "docker"):
        if await _docker_ok(s):
            _docker_seen.add(s.SANDBOX_IMAGE)
            return "docker"
        if want == "docker" or s.SANDBOX_IMAGE in _docker_seen:
            _avail.pop(("docker", s.SANDBOX_IMAGE), None)
            if await _docker_ok(s):
                return "docker"
            raise SandboxUnavailable(f"the Docker sandbox ({s.SANDBOX_IMAGE}) is not responding; "
                                     "not falling back to a weaker sandbox")
    if want in ("auto", "wsl") and sys.platform == "win32" and await _wsl_ok(s):
        if await _ag_run_ok(s) or weak_ok:
            return "wsl"
        if want == "wsl":
            raise SandboxUnavailable("WSL has no ag-run (run sandbox/wsl_setup.sh); "
                                     "set ALLOW_WEAK_SANDBOX=1 to use the weak WSL mode anyway")
    if want in ("auto", "bwrap") and await _bwrap_ok():
        return "bwrap"
    if want in ("auto", "native") and (want == "native" or weak_ok) and await _native_ok():
        return "native"
    raise RuntimeError(
        f"no sandbox available (SANDBOX={want}). Build the Docker image with "
        f"\"docker build -t {s.SANDBOX_IMAGE} sandbox\", or on Windows set up WSL with sandbox/wsl_setup.sh, "
        "or on Linux install bubblewrap (ALLOW_WEAK_SANDBOX=1 allows weak WSL or native timeout+prlimit)."
    )


async def pin_settings(settings=None):
    """Settings with SANDBOX fixed to the backend chosen now, so one job uses one backend."""
    s = _settings(settings)
    kind = await resolve_kind(s)
    return s if s.SANDBOX == kind else s.model_copy(update={"SANDBOX": kind})


async def create_sandbox(settings=None, limits=None, log=None) -> Sandbox:
    """limits is a dict with memory_limit_mb (GradeContext.limits) or a plain number of MB."""
    s = _settings(settings)
    mem = _mem(limits)
    kind = await resolve_kind(s)
    if kind == "docker":
        return await DockerSandbox.create(memory_limit_mb=mem, log=log, image=s.SANDBOX_IMAGE,
                                          instance=s.instance_id)
    if kind == "wsl":
        sb = await WslSandbox.create(memory_limit_mb=mem, log=log, distro=s.WSL_DISTRO)
        if sb.weak and not getattr(s, "ALLOW_WEAK_SANDBOX", False):
            await sb.destroy()
            raise SandboxUnavailable("ag-run did not answer; refusing the weak WSL sandbox")
        return sb
    if kind == "bwrap":
        return await BwrapSandbox.create(memory_limit_mb=mem, log=log)
    return await NativeSandbox.create(memory_limit_mb=mem, log=log)


@asynccontextmanager
async def sandbox_ctx(settings=None, limits=None, log=None) -> AsyncIterator[Sandbox]:
    sb = await create_sandbox(settings, limits, log)
    try:
        yield sb
    finally:
        await sb.destroy()


async def with_sandbox(settings, limits, log, fn):
    async with sandbox_ctx(settings, limits, log) as sb:
        return await fn(sb)


async def sandbox_status(settings=None) -> dict:
    s = _settings(settings)
    docker = await DockerSandbox.available(s.SANDBOX_IMAGE)
    wsl = await WslSandbox.available(s.WSL_DISTRO)
    status = {
        "mode": s.SANDBOX,
        "docker": docker,
        "wsl": wsl,
        "wsl_ag_run": wsl and await ag_run_installed(s.WSL_DISTRO),
        "bwrap": await BwrapSandbox.available(),
        "native": await NativeSandbox.available(),
        "image": s.SANDBOX_IMAGE,
    }
    status["allow_weak"] = bool(getattr(s, "ALLOW_WEAK_SANDBOX", False))
    try:
        status["selected"] = await resolve_kind(s)
    except RuntimeError as e:
        status["selected"] = None
        status["error"] = str(e)
    if status["selected"] == "wsl" and not status["wsl_ag_run"]:
        status["selected"] = "wsl (weak)"
    return status


def sandbox_label(sb) -> str:
    return f"{sb.kind} (weak)" if getattr(sb, "weak", False) else sb.kind


async def tool_versions(sb) -> dict:
    async def pick(cmd: str) -> str:
        r = await sb.sh(cmd, timeout_s=10)
        text = r.out or r.err
        return text.split("\n")[0].strip() or "unavailable"

    return {
        "sandbox": sandbox_label(sb),
        "gcc": await pick("gcc --version"),
        "valgrind": await pick("valgrind --version 2>&1 || echo unavailable"),
        "cppcheck": await pick("cppcheck --version 2>&1 || echo unavailable"),
    }
