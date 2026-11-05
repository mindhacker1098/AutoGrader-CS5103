"""rules.json validation, loading and hashing."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .hashing import canonical_json, hash_problem_inputs, hash_rules, sha256
from .textfmt import number_text, trim_ws, to_fixed

__all__ = [
    "RulesError",
    "canonical_json",
    "hash_problem_inputs",
    "hash_rules",
    "load_rules",
    "sha256",
    "validate_rules",
]

_MISSING = object()


class RulesError(ValueError):
    pass


def _get(obj: Any, key: str) -> Any:
    return obj.get(key, _MISSING) if isinstance(obj, dict) else _MISSING


def _truthy(v: Any) -> bool:
    if v is _MISSING or v is None:
        return False
    if isinstance(v, float) and math.isnan(v):
        return False
    if isinstance(v, (dict, list)):
        return True
    return bool(v)


def _to_number(v: Any) -> float:
    """ToNumber, enough for the `x > 0` checks in validateRules."""
    if v is _MISSING or isinstance(v, dict):
        return math.nan
    if v is None:
        return 0.0
    if isinstance(v, (bool, int, float)):
        return float(v)
    if isinstance(v, list):
        if len(v) == 0:
            return 0.0
        return _to_number(v[0]) if len(v) == 1 else math.nan
    s = trim_ws(str(v))
    if not s:
        return 0.0
    try:
        return float(int(s, 16)) if s.lower().startswith("0x") else float(s)
    except ValueError:
        return math.nan


def _js_str(v: Any) -> str:
    if v is _MISSING:
        return "undefined"
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return number_text(v)
    if isinstance(v, list):
        return ",".join("" if x is None else _js_str(x) for x in v)
    if isinstance(v, dict):
        return "[object Object]"
    return str(v)


def _set_key(v: Any) -> tuple:
    if isinstance(v, (dict, list)):
        return ("obj", id(v))
    return (type(v).__name__ if not isinstance(v, (int, float)) or isinstance(v, bool) else "num", v)


def validate_rules(rules: Any, known_analyzers: list[str]) -> list[str]:
    """Check a rules dict and return a list of error messages (empty means valid)."""
    errors: list[str] = []
    if not isinstance(rules, (dict, list)):
        return ["rules must be a JSON object"]
    if not _truthy(_get(rules, "problem")):
        errors.append('missing "problem"')
    if not _to_number(_get(rules, "time_limit_s")) > 0:
        errors.append('"time_limit_s" must be a positive number')
    if not _to_number(_get(rules, "memory_limit_mb")) > 0:
        errors.append('"memory_limit_mb" must be a positive number')
    metrics = _get(rules, "metrics")
    if not isinstance(metrics, list) or len(metrics) == 0:
        errors.append('"metrics" must be a non-empty array')
        return errors

    names: set = set()
    total = 0.0
    for i, m in enumerate(metrics):
        name = _get(m, "name")
        where = f"metrics[{i}]" + (f" ({_js_str(name)})" if _truthy(name) else "")
        if not _truthy(name):
            errors.append(f'{where}: missing "name"')
        key = _set_key(name)
        if key in names:
            errors.append(f"{where}: duplicate metric name")
        names.add(key)
        analyzer = _get(m, "analyzer")
        if not (isinstance(analyzer, str) and analyzer in known_analyzers):
            errors.append(f'{where}: unknown analyzer "{_js_str(analyzer)}" (known: {", ".join(known_analyzers)})')
        w = _get(m, "weight")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
            errors.append(f'{where}: "weight" must be a number >= 0')
        else:
            total += w
    if abs(total - 1) > 1e-6:
        errors.append(f"weights must sum to 1.00 (they sum to {to_fixed(total, 4)})")
    generated = [m for m in metrics if isinstance(m, dict)
                 and m.get("analyzer") in ("edge_case_runner", "complexity_estimator")]
    if generated:
        from .testgen import get_generator
        try:
            gen = get_generator(rules.get("input_spec"))
            for metric in generated:
                params = metric.get("params") or {}
                if metric["analyzer"] == "complexity_estimator":
                    sizes = params.get("sizes", [100, 200, 400, 800])
                    if (not isinstance(sizes, list) or len(sizes) < 3 or len(sizes) > 12
                            or any(type(n) is not int or not 1 <= n <= 100000 for n in sizes)
                            or sizes != sorted(set(sizes))):
                        raise ValueError("Efficiency needs 3-12 distinct increasing positive integer sizes (at most 100000)")
                    for n in sizes:
                        if not gen.validate(gen.of_size(n)):
                            raise ValueError(f"Invalid efficiency input at n={n}")
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append("input_spec: " + str(exc))
    return errors


def load_rules(rules_path: str | Path, known_analyzers: list[str]) -> dict:
    text = Path(rules_path).read_text("utf-8")
    try:
        rules = json.loads(text)
    except json.JSONDecodeError as e:
        raise RulesError(f"rules file {rules_path} is not valid JSON: {e}") from None
    errors = validate_rules(rules, known_analyzers)
    if errors:
        raise RulesError(f"invalid rules ({rules_path}):\n  - " + "\n  - ".join(errors))
    return rules
