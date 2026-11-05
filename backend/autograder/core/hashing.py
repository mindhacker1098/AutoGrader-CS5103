"""Hashes shared by the grader: sha256, FNV-1a and the rules/inputs hashes."""
from __future__ import annotations

import hashlib
import posixpath
from pathlib import Path
from typing import Any

from .textfmt import MASK32, dump_json, text_sort_key


def sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()


def fnv1a32(s: str) -> int:
    """FNV-1a hash over UTF-16 code units."""
    h = 0x811C9DC5
    raw = s.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(raw), 2):
        h ^= raw[i] | (raw[i + 1] << 8)
        h = (h * 0x01000193) & MASK32
    return h


def canonical_json(value: Any) -> str:
    """JSON with object keys sorted, so the same rules always give the same hash."""
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: text_sort_key(str(k)))
        return "{" + ",".join(dump_json(str(k)) + ":" + canonical_json(value[k]) for k in keys) + "}"
    return dump_json(value)


def hash_rules(rules: dict) -> str:
    return "sha256:" + sha256(canonical_json(rules))


def _join(*parts: str) -> str:
    return posixpath.normpath("/".join(p.replace("\\", "/") for p in parts))


def hash_problem_inputs(problem_dir: str | Path, rules: dict, sep: str = "/") -> str:
    """Hash of the reference solution and the test files.

    sep="\\" reproduces the older hash computed on Windows (it used
    backslashes); the default "/" gives the portable v2 hash.
    """
    problem_dir = Path(problem_dir)
    windows_style = sep == "\\"
    files: list[str] = []
    if rules.get("reference_solution"):
        files.append(rules["reference_solution"])
    if not windows_style:
        # v2 also covers the files that change a grade without being named in params
        files.extend(rules[k] for k in ("sample_input", "starter_code") if rules.get(k))
    dirs: list[str] = []
    for m in rules.get("metrics") or []:
        d = (m.get("params") or {}).get("testset_dir")
        if not d and not windows_style and m.get("analyzer") == "io_tests":
            d = "tests"
        if not d or (not windows_style and _join(d) in dirs):
            continue
        dirs.append(_join(d))
        if (problem_dir / d).exists():
            names = sorted((p.name for p in (problem_dir / d).iterdir()), key=text_sort_key)
            files.extend(_join(d, f).replace("/", sep) for f in names)
    h = hashlib.sha256()
    for rel in files:
        p = problem_dir / rel.replace("\\", "/")
        if p.is_file():
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
    prefix = "sha256:" if sep == "\\" else "sha256v2:"
    return prefix + h.hexdigest()
