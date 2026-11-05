import json
from types import SimpleNamespace

import pytest
from autograder.config import PROJECT_ROOT
from autograder.core.analyzers import similarity_flag
from autograder.core.corpus import CorpusItem
from autograder.core.report import render_text


def context(items):
    return SimpleNamespace(corpus=SimpleNamespace(peers=lambda *args: items), rules={}, source="source",
                           reference="", starter="")


@pytest.mark.asyncio
async def test_no_sources_does_not_claim_a_successful_comparison(monkeypatch):
    def compare(*args, **kwargs):
        raise AssertionError("No comparison can run with an empty corpus")

    monkeypatch.setattr(similarity_flag, "compare_to_corpus", compare)
    result = await similarity_flag.ANALYZER.run(context([]), {}, lambda event: None)
    assert result["informational"] is True
    assert result["evidence"]["comparison_count"] == 0
    assert result["evidence"]["assessment"] == "not_assessed"
    assert result["evidence"]["top_matches"] == []
    assert "does not establish originality" in result["feedback"]
    assert not result.get("flags")


@pytest.mark.parametrize("fingerprints, similarity, assessment, flag", [
    (2, 1.0, "insufficient_code", None),
    (12, 1.0, "limited_evidence", "possible_copy_low_confidence"),
    (50, 1.0, "review_required", "suspected_copy_peer"),
    (50, 0.0, "compared", None),
])
@pytest.mark.asyncio
async def test_comparison_outcomes(monkeypatch, fingerprints, similarity, assessment, flag):
    monkeypatch.setattr(similarity_flag, "compare_to_corpus", lambda *args, **kwargs: {
        "fingerprints": fingerprints, "ignored": 0,
        "matches": [{"id": "peer:other", "kind": "peer", "similarity": similarity}]})
    items = [CorpusItem("peer:other", "peer", "other source")]
    result = await similarity_flag.ANALYZER.run(context(items), {}, lambda event: None)
    # too little code stays review-only; a confident copy loses originality; the rest is scored
    assert bool(result.get("informational")) == (assessment == "insufficient_code")
    assert result["score"] == (0 if assessment == "review_required" else 1)
    assert result["evidence"]["assessment"] == assessment
    assert result["evidence"]["comparison_count"] == 1
    assert result["evidence"]["source_counts"] == {"peer": 1, "web": 0, "llm": 0}
    assert [item["type"] for item in result.get("flags", [])] == ([flag] if flag else [])
    if assessment == "compared":
        assert "does not prove originality" in result["feedback"]


def test_plain_text_report_does_not_score_review_signals():
    report = json.loads((PROJECT_ROOT / "seeds/expected_reports/add_two_integers/add_good.json").read_text("utf-8"))
    report["metrics"] = [
        {"name": "originality", "analyzer": "similarity_flag", "weight": 0, "score": 1,
         "contribution": 0, "status": "ok", "informational": True, "evidence": {"assessment": "not_assessed"},
         "feedback": "No comparison sources."},
        {"name": "ai_origin", "analyzer": "ai_detector", "weight": 0, "score": 1,
         "contribution": 0, "status": "ok", "informational": True, "feedback": "Heuristic signal only."},
    ]
    text = render_text(report)
    lines = [line for line in text.splitlines() if line.startswith(("originality", "ai_origin"))]
    assert len(lines) == 2
    assert "Not assessed" in lines[0] and "Review only" in lines[1]
    assert all("0/0" not in line and "100%" not in line for line in lines)
