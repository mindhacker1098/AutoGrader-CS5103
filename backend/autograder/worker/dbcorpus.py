"""Class-mode similarity corpus backed by the database (DESIGN section 9)."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sqlmodel import Session

from ..core.corpus import CorpusItem, corpus_hash, read_c_sources
from ..db import repo
from ..db.models import Submission


class DbCorpus:
    """Peers are the latest submission of every other student, then corpus/web and corpus/llm.

    The corpus is read once when the object is built, so every analyzer of a
    grading run (and the corpus_hash) sees the same snapshot. Passing
    peer_ids freezes the peer set, which is what finalize uses.
    """

    def __init__(self, session: Session, problem_id: int, user_id: int | None, version_dir: str | Path, *,
                 peer_ids: Iterable[int] | None = None):
        self.problem_id = problem_id
        self.user_id = user_id
        self.version_dir = Path(version_dir)
        if peer_ids is not None:
            ids = sorted(set(peer_ids))
            subs = [s for s in (session.get(Submission, i) for i in ids) if s is not None]
        else:
            subs = repo.latest_submissions_per_user(session, problem_id)
        self.items: list[CorpusItem] = [
            CorpusItem(f"sub:{s.id}", "peer", s.source, s.filename)
            for s in subs if s.user_id != user_id and s.problem_id == problem_id
        ]
        self.items += read_c_sources(self.version_dir / "corpus" / "web", "web")
        self.items += read_c_sources(self.version_dir / "corpus" / "llm", "llm")

    def peers(self, problem_id: str | None = None, exclude: str = "") -> list[CorpusItem]:
        # Identical copies from other students are kept on purpose: in class
        # mode they are exactly what the similarity metric has to catch.
        return list(self.items)

    @property
    def peer_ids(self) -> list[int]:
        return [int(i.ref[4:]) for i in self.items if i.kind == "peer"]

    @property
    def hash(self) -> str:
        return corpus_hash(self.items)
