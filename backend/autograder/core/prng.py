"""mulberry32: a small seeded random generator, so generated tests are the same every run."""
from __future__ import annotations

from collections.abc import Callable

from .textfmt import MASK32, u32


def _imul32(a: int, b: int) -> int:
    return (a * b) & MASK32


def mulberry32(seed: float) -> Callable[[], float]:
    a = u32(seed)

    def rng() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & MASK32
        t = a
        t = _imul32(t ^ (t >> 15), t | 1)
        t = (t ^ ((t + _imul32(t ^ (t >> 7), t | 61)) & MASK32)) & MASK32
        return ((t ^ (t >> 14)) & MASK32) / 4294967296

    return rng
