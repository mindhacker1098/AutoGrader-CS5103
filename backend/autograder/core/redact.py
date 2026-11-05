"""Student-facing copy of a result: no peer names, no AI-detector internals, no staff-only data."""
from __future__ import annotations

import copy
import re
from typing import Any

PEER_TEXT = "another student's submission"
PEER_REF_RE = re.compile(r"\b(?:peer|sub):[^\s,;()\"'<>]*[^\s,;()\"'<>.]", re.ASCII)

STAFF_ONLY_KEYS = {"pre_analysis", "model_digests", "corpus_hash", "staff", "staff_notes", "ai_detection"}
STAFF_ONLY_FLAGS = {"possible_ai_generated"}
COPY_FLAGS_PREFIX = "suspected_copy_"
LOW_COPY_FLAG = "possible_copy_low_confidence"
STAFF_FLAG_KEYS = {"reviewed_by", "review_note", "review_status", "peer", "ref", "refs", "regions", "submission_id"}
STAFF_EVIDENCE_KEYS = {"regions", "peer_source", "shared_hashes", "peer_submission_id"}


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return PEER_REF_RE.sub(PEER_TEXT, value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in STAFF_EVIDENCE_KEYS}
    return value


HIDDEN = "(hidden test)"
_HIDDEN_WHY = {
    "ok": "The output differs from the expected output on this hidden test.",
    "timeout": "The program did not finish within the time limit on this hidden test.",
    "crash": "The program crashed on this hidden test; check array bounds, NULL pointers and how many items are read.",
    "nonzero_exit": "The program exited with a non-zero status on this hidden test.",
}
_HIDDEN_NOTE = {"ok": "wrong answer", "timeout": "time limit exceeded", "crash": "crashed",
                "nonzero_exit": "non-zero exit"}


def hide_case(c: dict) -> dict:
    """Copy of a hidden-test failure without its input, expected output, program output or derived text."""
    status = c.get("status") or "ok"
    out = {k: v for k, v in c.items() if k not in ("input", "expected", "got", "why", "note")}
    out.update(input=HIDDEN, expected=HIDDEN, got=HIDDEN,
               why=_HIDDEN_WHY.get(status, "The program failed this hidden test."),
               note=_HIDDEN_NOTE.get(status, status))
    return out


def hidden_inputs(report: dict) -> set:
    """Raw inputs of the hidden tests that appear in a report."""
    found = {c.get("input") for c in report.get("counterexamples") or [] if c.get("source") == "hidden test"}
    for m in report.get("metrics") or []:
        if m.get("analyzer") == "io_tests":
            found.update(f.get("input") for f in (m.get("evidence") or {}).get("failing") or [] if isinstance(f, dict))
    found.discard(None)
    return found


def _ai_metric(m: dict) -> dict:
    out = {k: m[k] for k in ("name", "analyzer", "weight", "score", "contribution", "status") if k in m}
    out["evidence"] = {}
    out["feedback"] = "Checked by the staff tools; this metric does not affect your grade." if not m.get("weight") \
        else "Checked by the staff tools."
    out["suggestions"] = []
    return out


def flag_visible(f: dict, status: str) -> bool:
    """Students never see AI flags or dismissed flags; copy flags only once staff confirm them."""
    ftype = f.get("type") or ""
    if ftype in STAFF_ONLY_FLAGS or status == "dismissed":
        return False
    if ftype.startswith(COPY_FLAGS_PREFIX) or ftype == LOW_COPY_FLAG:
        return status == "confirmed"
    return True


def student_view(report: dict, *, reveal_hidden_tests: bool = True,
                 reviews: dict[tuple[str, str], str] | None = None) -> dict:
    """reviews maps (flag type, detail) to its review_status; flags missing from it count as open."""
    reviews = reviews or {}
    r = copy.deepcopy(report)
    for k in STAFF_ONLY_KEYS:
        r.pop(k, None)

    metrics = []
    for m in r.get("metrics") or []:
        if m.get("analyzer") == "ai_detector":
            if m.get("weight"):
                metrics.append(_ai_metric(m))
            continue
        metrics.append(m)
    if "metrics" in r:
        r["metrics"] = metrics

    if "flags" in r:
        r["flags"] = [
            {k: v for k, v in f.items() if k not in STAFF_FLAG_KEYS}
            for f in r["flags"] or []
            if flag_visible(f, reviews.get((f.get("type") or "", f.get("detail") or ""), "open"))
        ]

    enrichment = r.get("enrichment")
    if isinstance(enrichment, dict):
        for k in STAFF_ONLY_KEYS:
            enrichment.pop(k, None)

    if not reveal_hidden_tests:
        secret = hidden_inputs(report)
        if "counterexamples" in r:
            r["counterexamples"] = [hide_case(c) if c.get("source") == "hidden test" or c.get("input") in secret
                                    else c for c in r["counterexamples"] or []]
        for m in metrics:
            if m.get("analyzer") != "io_tests":
                continue
            ev = m.get("evidence") or {}
            if ev.get("failing"):
                ev["failing"] = [hide_case(f) if isinstance(f, dict) else f for f in ev["failing"]]
                m["feedback"] = (f"{ev.get('tests_passed')}/{ev.get('tests_total')} tests pass. "
                                 f"First failure: test {ev['failing'][0].get('test')}.")
        if isinstance(enrichment, dict):
            enrichment["explanations"] = [
                {**e, "input": HIDDEN, "text": hide_case(e)["why"], "source": "deterministic"}
                if isinstance(e, dict) and e.get("input") in secret else e
                for e in enrichment.get("explanations") or []]

    return _scrub(r)
