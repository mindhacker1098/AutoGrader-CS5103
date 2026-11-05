"""Analyzer interface shared by every metric."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from ...config import Settings
    from ..corpus import CorpusProvider
    from ..sandbox.base import Sandbox

# exec: skipped if compile fails or the smoke run times out; build and exec get a sandbox
Kind = Literal["exec", "build", "source"]

LogFn = Callable[[dict], None]


class AnalyzerResult(TypedDict, total=False):
    score: float
    informational: bool
    evidence: dict
    feedback: str
    suggestions: list[str]
    counterexamples: list[dict]
    flags: list[dict]


@dataclass
class GradeContext:
    problem_dir: Path
    rules: dict
    source: str
    filename: str
    reference: str | None
    starter: str | None
    sample_input: str
    time_limit_s: float
    memory_limit_mb: int
    corpus: CorpusProvider
    pre: dict | None
    settings: Settings
    sandbox: Sandbox | None = None
    extra: dict = field(default_factory=dict)

    @property
    def limits(self) -> dict:
        return {"time_limit_s": self.time_limit_s, "memory_limit_mb": self.memory_limit_mb}


class Analyzer(Protocol):
    name: str
    kind: Kind
    description: str

    async def run(self, ctx: GradeContext, params: dict, log: LogFn) -> AnalyzerResult: ...
