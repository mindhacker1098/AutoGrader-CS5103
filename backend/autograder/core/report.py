"""Plain-text grading report that the student reads."""
from __future__ import annotations

import math
from typing import Any

from .textfmt import WS_RE, number_text, round_half_up, trim_ws, to_fixed


def _ulen(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _str(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return number_text(v)
    return str(v)


def _tool(v: Any) -> str:
    """Tool version; Codacy reports a dict of versions (nested "reported" ones are left out)."""
    if isinstance(v, dict):
        return ", ".join(f"{k} {_str(x)}" for k, x in v.items() if not isinstance(x, dict))
    return _str(v)


def _pad(s: Any, n: int) -> str:
    s = _str(s)
    return s + " " * max(0, n - _ulen(s))


def _num(x: float) -> str:
    if isinstance(x, int) or (isinstance(x, float) and math.isfinite(x) and x.is_integer()):
        return number_text(x)
    return to_fixed(x, 1)


def _indent_block(label: str, value: Any, width: int = 10) -> str:
    lines = ("" if value is None else _str(value)).split("\n")
    return "\n".join(
        f"        {_pad(label, width)}{line}" if i == 0 else f"        {' ' * width}{line}"
        for i, line in enumerate(lines)
    )


def wrap(text: Any, width: int, indent: str = " " * 18) -> str:
    words = [w for w in WS_RE.split(_str(text) if text else "") if w]
    lines: list[str] = []
    cur = ""
    for w in words:
        if _ulen(trim_ws(cur + " " + w)) > width:
            lines.append(trim_ws(cur))
            cur = w
        else:
            cur += " " + w
    if trim_ws(cur):
        lines.append(trim_ws(cur))
    return f"\n{indent}".join(lines)


def _cx_title(c: dict) -> str:
    src = c.get("source")
    if src == "hidden test":
        return f"Test {_str(c.get('test'))}"
    if src == "edge case":
        return f'Edge case "{_str(c.get("case"))}"'
    return f"Counter-example ({_str(c.get('case'))})"


def render_text(r: dict) -> str:
    out: list[str] = []
    out.append(f"===== Grading report for {r['submission']} =====")
    out.append(f"Problem: {r['problem']}")
    partial = "   (PARTIAL REPORT: some metrics could not run)" if r.get("partial") else ""
    out.append(f"Overall grade: {to_fixed(r['grade'], 1)} / 100{partial}")
    if r["flags"]:
        out.append("")
        out.append("FLAGS:")
        for f in r["flags"]:
            out.append(f"  ! {f['type']}: {_str(f.get('detail'))}")
    out.append("")
    out.append(f"{_pad('Metric', 15)}{_pad('Weight', 7)}{_pad('Score', 10)}{_pad('Contrib.', 9)}Verdict")
    for m in r["metrics"]:
        mx = m["weight"] * 100
        got = m["score"] * mx
        verdict = m["feedback"] if m["status"] == "ok" else f"[{m['status']}] {m['feedback']}"
        # only results marked informational are review-only; older reports counted every metric in the grade
        if m.get("informational"):
            assessment = (m.get("evidence") or {}).get("assessment")
            label = "Not assessed" if assessment == "not_assessed" else "Review only"
            out.append(_pad(m["name"], 15) + _pad("-", 7) + _pad(label, 14) + _pad("-", 9) + verdict)
            continue
        out.append(
            _pad(m["name"], 15) + _pad(_num(mx), 7)
            + _pad(f"{_num(round_half_up(got * 10) / 10)}/{_num(mx)}", 10)
            + _pad(to_fixed(m["contribution"], 1), 9) + verdict
        )

    cx = r.get("counterexamples") or []
    if cx:
        out.append("")
        out.append("--- Where you failed ---")
        for c in cx[:8]:
            out.append(f"{_cx_title(c)}:")
            out.append(_indent_block("INPUT:", c.get("input")))
            out.append(_indent_block("EXPECTED:", c.get("expected")))
            out.append(_indent_block("GOT:", c.get("got")))
            out.append(_indent_block("WHY:", wrap(c.get("why"), 64, "")))
        if len(cx) > 8:
            out.append(f"({len(cx) - 8} more in the JSON report)")

    if r["suggestions"]:
        out.append("")
        out.append("--- Suggestions to raise the grade ---")
        for s in r["suggestions"]:
            out.append(f"- {s}")

    feedback = r.get("code_feedback")
    if feedback:
        out.extend(["", "--- Code feedback (advisory; does not affect marks) ---",
                    "Status: " + str(feedback.get("status", "unknown"))])
        if feedback.get("error"):
            out.append(str(feedback["error"]))
        for issue in feedback.get("issues") or []:
            out.append(f"Line {issue['line']}-{issue['end_line']} [{issue['basis']}]: {issue['explanation']}")
            out.append(issue["code"])
            out.append("Suggested change: " + issue["suggestion"])
        for review in feedback.get("reviews") or []:
            out.append(f"{review['metric']}: {review['comment']}")

    note = r.get("tutor_note")
    if note:
        out.append("")
        out.append(f"--- Tutor note (written by {note['model']}; checked against the evidence, does not affect the grade) ---")
        out.append(wrap(note["text"], 76))

    ul = r["usefulness_limitations"]
    out.append("")
    out.append("--- Usefulness & limitations of this evaluation ---")
    out.append("Useful for:")
    for i, u in enumerate(ul["usefulness"], 1):
        out.append(f"  {i}. {u['use_case']}")
        out.append(f"     Example: {wrap(u['example'], 66, ' ' * 14)}")
        out.append(f"     Benefit: {wrap(u['benefit'], 66, ' ' * 14)}")
    out.append("Limits of this report:")
    for i, lim in enumerate(ul["limitations"], 1):
        out.append(f"  {i}. {lim['limitation']}")
        out.append(f"     Example: {wrap(lim['example'], 66, ' ' * 14)}")
        out.append(f"     Fix: {wrap(lim['mitigation'], 66, ' ' * 10)}")

    out.append("")
    out.append("--- Reproducibility ---")
    out.append(f"submission {r['submission_hash']}")
    out.append(f"rules      {r['rules_hash']}")
    out.append(f"tests/ref  {r['inputs_hash']}")
    out.append("tools      " + "; ".join(f"{k}={_tool(v)}" for k, v in r["tool_versions"].items()))
    return "\n".join(out) + "\n"
