"""Analyzer registry: rules.json analyzer name -> analyzer class.

Each module defines ANALYZER. A module that is missing or fails to import is
left out of REGISTRY and its error is kept in IMPORT_ERRORS, so a broken
analyzer only shows up as "unknown analyzer" when a rules file names it.
"""
from __future__ import annotations

import importlib

from .base import Analyzer, AnalyzerResult, GradeContext, Kind, LogFn

MODULES = [
    "io_tests",
    "compiler_warnings",
    "static_analysis",
    "function_split",
    "edge_case_runner",
    "complexity_estimator",
    "sanitizer_run",
    "llm_review",
    "similarity_flag",
    "ai_detector",
    "codacy_review",
]

REGISTRY: dict[str, Analyzer] = {}
IMPORT_ERRORS: dict[str, str] = {}


def _load() -> None:
    REGISTRY.clear()
    IMPORT_ERRORS.clear()
    for mod in MODULES:
        try:
            a = importlib.import_module(f"{__name__}.{mod}").ANALYZER
        except Exception as e:  # noqa: BLE001
            IMPORT_ERRORS[mod] = f"{type(e).__name__}: {e}"
            continue
        REGISTRY[a.name] = a


def reload_registry() -> dict[str, Analyzer]:
    importlib.invalidate_caches()
    _load()
    return REGISTRY


def analyzer_names() -> list[str]:
    return list(REGISTRY)


_load()

__all__ = [
    "IMPORT_ERRORS",
    "MODULES",
    "REGISTRY",
    "Analyzer",
    "AnalyzerResult",
    "GradeContext",
    "Kind",
    "LogFn",
    "analyzer_names",
    "reload_registry",
]
