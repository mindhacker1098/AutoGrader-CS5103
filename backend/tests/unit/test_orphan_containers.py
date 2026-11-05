"""Sandboxes left by a killed worker are swept; live, just-started and foreign ones are not."""
import asyncio
import subprocess

import pytest
from autograder.config import Settings
from autograder.core.sandbox.docker import (
    DEFAULT_IMAGE,
    INSTANCE_LABEL,
    LABEL,
    remove_orphan_containers,
)

pytestmark = pytest.mark.sandbox

ME = "test-inst-a"


def _start(owner: str, instance: str | None = ME) -> str:
    labels = ["--label", f"{LABEL}={owner}"] + (["--label", f"{INSTANCE_LABEL}={instance}"] if instance else [])
    try:
        return subprocess.check_output(["docker", "run", "-d", "--rm", *labels, DEFAULT_IMAGE, "sleep", "120"],
                                       text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("docker sandbox image not available")


def _running(cid: str) -> bool:
    ids = subprocess.check_output(["docker", "ps", "-q", "--no-trunc", "--filter", f"label={LABEL}"], text=True).split()
    return cid in ids


def test_sweeps_only_dead_workers_of_this_instance():
    dead, live, mine = _start("test-dead-w1"), _start("test-live-w2"), _start("test-self-w3")
    # another installation (other database) and an unlabelled old container on the same Docker host
    foreign, legacy = _start("test-dead-w5", "test-inst-b"), _start("test-dead-w6", None)
    try:
        n = asyncio.run(remove_orphan_containers({"test-live-w2"}, "test-self-w3", ME, min_age_s=0))
        assert n == 1
        assert not _running(dead)
        assert _running(live) and _running(mine) and _running(foreign) and _running(legacy)
    finally:
        subprocess.run(["docker", "rm", "-f", dead, live, mine, foreign, legacy], capture_output=True, check=False)


def test_young_containers_survive():
    young = _start("test-dead-w4")
    try:
        asyncio.run(remove_orphan_containers(set(), "someone-else", ME))
        assert _running(young)
    finally:
        subprocess.run(["docker", "rm", "-f", young], capture_output=True, check=False)


def test_instance_id_follows_the_database(tmp_path):
    a = Settings(DATA_DIR=tmp_path / "a")
    assert a.instance_id == Settings(DATA_DIR=tmp_path / "a").instance_id
    assert a.instance_id != Settings(DATA_DIR=tmp_path / "b").instance_id and len(a.instance_id) == 12
