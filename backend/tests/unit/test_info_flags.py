"""Facts about a run (compile_failed, ...) are shown in reports but never counted as flags to review."""
from sqlmodel import Session, SQLModel, create_engine, select

from autograder.db import repo
from autograder.db.models import Flag


def test_info_flags_do_not_need_review():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for t in ("compile_failed", "suspected_infinite_loop", "suspected_copy_peer", "possible_ai_generated"):
            s.add(Flag(result_id=1, submission_id=1, type=t, detail="", review_status="open"))
        s.commit()
        need = s.exec(select(Flag.type).where(Flag.review_status == "open", repo.needs_review(Flag.type))).all()
    assert sorted(need) == ["possible_ai_generated", "suspected_copy_peer"]
