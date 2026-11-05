"""Reproduce report experiments without modifying the classroom database."""
import asyncio
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from autograder.config import Settings
from autograder.core.coordinator import grade_submission

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "evidence"

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None, DATA_DIR=OUT / "runtime",
                        LLM_CACHE_DIR=OUT / "runtime" / "llm", LLM_DISABLED=True,
                        SANDBOX="docker", SANDBOX_IMAGE="autograder-sandbox:1")
    rows = []
    for problem in ("add_two_integers", "sort_integers"):
        folder = ROOT / "seeds" / "problems" / problem
        rules = json.loads((folder / "rules.json").read_text())
        for source in sorted((folder / "submissions").glob("*.c")):
            result = await grade_submission(folder, rules, source.read_text(), source.name,
                                            settings=settings, tutor_note=False)
            (OUT / f"{source.stem}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            row = {"problem": problem, "file": source.name, "grade": result["grade"],
                   "metrics": [{k: m.get(k) for k in ("name", "score", "contribution", "status")}
                               for m in result["metrics"]],
                   "flags": [f["type"] for f in result.get("flags", [])]}
            rows.append(row)
            print(json.dumps(row), flush=True)
            (OUT / "sample_results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    folder = ROOT / "seeds" / "problems" / "add_two_integers"
    rules = json.loads((folder / "rules.json").read_text())
    repeat = await grade_submission(folder, rules, (folder / "submissions" / "add.c").read_text(),
                                    "add.c", settings=settings, tutor_note=False)
    first = json.loads((OUT / "add.json").read_text())
    same = repeat["grade"] == first["grade"] and [m["score"] for m in repeat["metrics"]] == [m["score"] for m in first["metrics"]]
    manifest = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "llm_disabled": True,
                "repeat_add_grade": repeat["grade"], "repeat_scores_equal": same,
                "source_hashes": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((ROOT / "backend" / "autograder").rglob("*.py"))}}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("REPEAT_EQUAL", same, flush=True)

if __name__ == "__main__":
    asyncio.run(main())
