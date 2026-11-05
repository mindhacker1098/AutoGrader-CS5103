import asyncio
import sys

import pytest
from autograder.config import Settings
from autograder.core.analyzers.common import compile, run_batch, run_one
from autograder.core.sandbox.docker import DockerSandbox
from autograder.core.sandbox.proc import run_process
from autograder.core.sandbox.select import (
    create_sandbox,
    sandbox_ctx,
    sandbox_status,
    tool_versions,
)
from autograder.core.sandbox.wsl import WslSandbox, list_distros

LIMITS = {"time_limit_s": 2, "memory_limit_mb": 64}

HELLO = '#include <stdio.h>\nint main(void){ int a,b; if(scanf("%d %d",&a,&b)!=2) return 1; printf("%d\\n",a+b); return 0; }\n'
LOOP = "int main(void){ volatile int x = 0; for(;;) x++; }\n"
CRASH = "int main(void){ volatile int *p = 0; *p = 1; return 0; }\n"
HOG = r"""#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void){
  size_t n = (size_t)512 << 20;
  char *p = malloc(n);
  if (!p) { puts("nomem"); return 3; }
  memset(p, 1, n);
  printf("ok %d\n", p[n - 1] + p[n / 2]);
  return 0;
}
"""
FORK_BOMB = "#include <unistd.h>\nint main(void){ for(;;) fork(); }\n"


def test_run_process_host_kill():
    async def go():
        return await run_process([sys.executable, "-c", "import time; time.sleep(30)"], kill_after_s=1)

    r = asyncio.run(go())
    assert r.host_killed and r.ms < 15000


def test_run_process_caps_output_and_takes_bytes():
    code = "import sys; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(d*1000); sys.stderr.write('e')"
    r = asyncio.run(run_process([sys.executable, "-c", code], input=b"\xff\x00ab", cap=1000))
    assert r.code == 0 and len(r.stdout) == 1000 and r.truncated
    assert r.stdout[:4] == b"\xff\x00ab" and r.stderr == b"e"


def test_run_process_missing_binary():
    r = asyncio.run(run_process(["definitely-not-a-binary-xyz"]))
    assert r.code == -1 and r.stderr


def _backends():
    out = [pytest.param("docker", marks=pytest.mark.sandbox)]
    if sys.platform == "win32":
        out.append(pytest.param("wsl", marks=pytest.mark.sandbox))
    return out


@pytest.fixture(params=_backends())
async def sb(request):
    kind = request.param
    if kind == "docker":
        if not await DockerSandbox.available():
            pytest.skip("docker sandbox image not available")
        box = await DockerSandbox.create(memory_limit_mb=64, worker_id="pytest")
    else:
        if "Ubuntu" not in await list_distros() or not await WslSandbox.available("Ubuntu"):
            pytest.skip("WSL Ubuntu not available")
        box = await WslSandbox.create(memory_limit_mb=64, distro="Ubuntu")
    try:
        yield box
    finally:
        await box.destroy()


@pytest.mark.sandbox
async def test_hello_world(sb):
    c = await compile(sb, HELLO, "prog")
    assert c.ok, c.stderr
    r = await run_one(sb, "prog", "2 3\n", LIMITS)
    assert r.status == "ok" and r.stdout == "5\n" and r.label == "ok"


@pytest.mark.sandbox
async def test_compile_error(sb):
    c = await compile(sb, "int main(void){ return x; }\n", "bad")
    assert not c.ok and c.errors >= 1


@pytest.mark.sandbox
async def test_infinite_loop_times_out(sb):
    assert (await compile(sb, LOOP, "loop")).ok
    r = await run_one(sb, "loop", "", {"time_limit_s": 1, "memory_limit_mb": 64})
    assert r.status == "timeout" and r.timed_out
    assert r.ms < 10000


@pytest.mark.sandbox
async def test_crash(sb):
    assert (await compile(sb, CRASH, "crash", "-O0 -std=c11")).ok
    r = await run_one(sb, "crash", "", LIMITS)
    assert r.status == "crash" and r.signal == 11
    assert r.label == "crashed with SIGSEGV"


@pytest.mark.sandbox
async def test_memory_hog_hits_as_limit(sb):
    # -O0 so gcc cannot drop the malloc/memset pair
    assert (await compile(sb, HOG, "hog", "-O0 -std=c11")).ok
    r = await run_one(sb, "hog", "", LIMITS)
    assert r.stdout.strip() == "nomem" and r.status == "nonzero_exit" and r.code == 3
    # without --as the same program gets its memory
    free = await run_one(sb, "hog", "", {"time_limit_s": 5, "memory_limit_mb": 0})
    assert free.stdout.strip() in ("ok 2", "nomem")


@pytest.mark.sandbox
async def test_fork_bomb_contained(sb):
    if getattr(sb, "weak", False):
        pytest.skip("weak WSL mode cannot clean up a fork bomb")
    assert (await compile(sb, FORK_BOMB, "bomb")).ok
    r = await run_one(sb, "bomb", "", {"time_limit_s": 2, "memory_limit_mb": 64})
    assert r.status in ("timeout", "crash")
    await asyncio.sleep(1.5)
    left = await sb.sh("ps -e -o pid= | wc -l", timeout_s=10)
    assert int(left.out.strip()) <= 6
    again = await run_one(sb, "bomb", "", {"time_limit_s": 1, "memory_limit_mb": 64})
    assert again.status in ("timeout", "crash")


@pytest.mark.sandbox
async def test_run_batch_three_cases(sb):
    assert (await compile(sb, HELLO, "prog")).ok
    assert (await compile(sb, CRASH, "crash", "-O0 -std=c11")).ok
    r = await run_batch(sb, "prog", ["1 2", "-5 5\n", "x"], LIMITS)
    assert [x.status for x in r] == ["ok", "ok", "nonzero_exit"]
    assert [x.stdout for x in r] == ["3", "0", ""]
    assert all(x.ms >= 0 for x in r)
    c = await run_batch(sb, "crash", ["", ""], LIMITS)
    assert [x.status for x in c] == ["crash", "crash"] and c[0].signal == 11


@pytest.mark.sandbox
async def test_batch_timeout_case(sb):
    assert (await compile(sb, LOOP, "loop")).ok
    r = await run_batch(sb, "loop", ["", ""], {"time_limit_s": 1, "memory_limit_mb": 64})
    assert [x.status for x in r] == ["timeout", "timeout"]


@pytest.mark.sandbox
async def test_env_and_tool_versions(sb):
    r = await sb.sh('echo "$AG_TEST"', timeout_s=5, env={"AG_TEST": "a b'c"})
    assert r.out == "a b'c\n"
    tv = await tool_versions(sb)
    assert tv["sandbox"].startswith(sb.kind) and "gcc" in tv["gcc"]


@pytest.mark.sandbox
async def test_select_docker():
    s = Settings(SANDBOX="docker")
    if not (await sandbox_status(s))["docker"]:
        pytest.skip("docker not available")
    async with sandbox_ctx(s, {"memory_limit_mb": 64}) as box:
        assert box.kind == "docker"
        r = await box.sh("echo hi", timeout_s=5)
        assert r.out == "hi\n" and r.status == "ok"


async def test_select_unknown_backend_raises():
    s = Settings(SANDBOX="native")
    if sys.platform.startswith("linux"):
        pytest.skip("native exists on linux")
    with pytest.raises(RuntimeError, match="no sandbox available"):
        await create_sandbox(s, 64)


async def test_resolve_kind_never_falls_back_silently(monkeypatch):
    from autograder.core.sandbox import select as sel

    state = {"docker": True, "wsl": True, "ag_run": False}

    async def flag(name):
        return state[name]

    monkeypatch.setattr(sel, "_docker_ok", lambda s: flag("docker"))
    monkeypatch.setattr(sel, "_wsl_ok", lambda s: flag("wsl"))
    monkeypatch.setattr(sel, "_ag_run_ok", lambda s: flag("ag_run"))
    monkeypatch.setattr(sel, "_bwrap_ok", lambda: _false())
    monkeypatch.setattr(sel, "_native_ok", lambda: _false())
    monkeypatch.setattr(sel.sys, "platform", "win32")
    monkeypatch.setattr(sel, "_docker_seen", set())
    auto = Settings(SANDBOX="auto")
    assert await sel.resolve_kind(auto) == "docker"
    pinned = await sel.pin_settings(auto)
    assert pinned.SANDBOX == "docker" and auto.SANDBOX == "auto"
    state["docker"] = False
    with pytest.raises(sel.SandboxUnavailable):
        await sel.resolve_kind(auto)
    with pytest.raises(sel.SandboxUnavailable):
        await sel.resolve_kind(pinned)
    sel._docker_seen.clear()
    # docker never worked here: weak WSL only with the opt-in
    with pytest.raises(RuntimeError, match="no sandbox available"):
        await sel.resolve_kind(auto)
    with pytest.raises(sel.SandboxUnavailable, match="ALLOW_WEAK_SANDBOX"):
        await sel.resolve_kind(Settings(SANDBOX="wsl"))
    assert await sel.resolve_kind(Settings(SANDBOX="auto", ALLOW_WEAK_SANDBOX=True)) == "wsl"
    state["ag_run"] = True
    assert await sel.resolve_kind(auto) == "wsl"


async def _false():
    return False
