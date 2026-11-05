"""Reset only this copy's local data and queue two small sample submissions.

Stop this copy's API and worker first. Run with --apply to perform the reset.
The current Admin password is preserved; old sessions are invalidated.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="reset this copy's local database")
    if not parser.parse_args().apply:
        parser.error("Pass --apply only after stopping this copy's API and worker")

    os.environ.update(DATA_DIR=str(DATA), DATABASE_URL="", LLM_CACHE_DIR=str(DATA / "cache" / "llm"))
    from autograder.config import get_settings
    from autograder.core.analyzers import REGISTRY
    from autograder.core.rules import validate_rules
    from autograder.db import repo
    from autograder.db.models import User
    from autograder.db.session import init_db, new_session
    from sqlmodel import SQLModel, select

    get_settings.cache_clear()
    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y%m%d-%H%M%S")
    backup = DATA / "backups" / f"before-small-demo-reset-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    database = DATA / "app.db"
    if database.is_file():
        with sqlite3.connect(database) as original, sqlite3.connect(backup / "app.db") as saved:
            original.backup(saved)
            assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    engine = init_db()
    with new_session(engine) as session:
        admin = session.exec(select(User).where(User.role == "admin", User.is_active)
                             .order_by(User.id)).first()
        admin_data = admin.model_dump() if admin else None

    # Move runtime files into this reset's backup, with both paths confined to data/.
    for name in ("problems", "cache", "browser-verification", "feedback-verification"):
        source = (DATA / name).resolve()
        destination = (backup / name).resolve()
        if not source.is_relative_to(DATA.resolve()) or not destination.is_relative_to(backup.resolve()):
            raise RuntimeError("Runtime backup path escaped this copy's data directory")
        if source.exists():
            shutil.move(str(source), str(destination))
    secret = DATA / "jwt_secret"
    if secret.exists():
        shutil.copyfile(secret, backup / "jwt_secret")

    with engine.begin() as connection:
        for table in reversed(SQLModel.metadata.sorted_tables):
            connection.execute(table.delete())
    secret.write_text(secrets.token_hex(32), encoding="ascii")

    credentials = []
    with new_session(engine) as session:
        if admin_data:
            admin_data["token_version"] = (admin_data.get("token_version") or 0) + 1
            admin_data["last_login_at"] = None
            session.add(User(**admin_data))
            session.commit()
            print(f"Preserved Admin login: {admin_data['roll_no']} (password unchanged)")
        else:
            password = secrets.token_urlsafe(16)
            repo.create_user(session, "ADMIN", "Administrator", "admin", password, must_change_pw=True)
            credentials.append(("ADMIN", "Administrator", password))

        for index, slug in enumerate(("add_two_integers", "sort_integers"), 1):
            source = ROOT / "seeds" / "problems" / slug
            staging = backup / "sample-definitions" / slug
            staging.mkdir(parents=True)
            for filename in ("reference.c", "sample.in", "problem.md"):
                shutil.copyfile(source / filename, staging / filename)
            shutil.copytree(source / "tests", staging / "tests")
            rules = json.loads((source / "rules.class.json").read_text(encoding="utf-8"))
            rules["pre_analysis"] = {"llm": False}
            rules["feedback"] = {"llm_tutor_note": False}
            for metric in rules["metrics"]:
                if metric["analyzer"] == "similarity_flag":
                    previous_weight = metric["weight"]
                    metric["weight"] = 0
                    next(m for m in rules["metrics"] if m["analyzer"] == "io_tests")["weight"] += previous_weight
                if metric["analyzer"] == "edge_case_runner":
                    metric["params"]["random_trials"] = 12
                if metric["analyzer"] == "complexity_estimator" and slug == "sort_integers":
                    metric["params"]["sizes"] = [100, 400, 1000]
            if slug == "sort_integers":
                rules["input_spec"]["max_valid_n"] = 1000
            errors = validate_rules(rules, list(REGISTRY))
            if errors:
                raise ValueError(errors)
            (staging / "rules.json").write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
            title = "Sample 1: Add Two Integers" if index == 1 else "Sample 2: Sort Integers"
            problem, version = repo.create_problem_from_dir(session, slug, title, staging, status="published")
            problem.reveal_hidden_tests = True  # Explicitly public demo tests, useful for inspecting feedback.
            # Derive the published sample answer from the known reference test input below.
            sample = (staging / "sample.in").read_text(encoding="utf-8").split()
            version.sample_output = (str(sum(map(int, sample))) + "\n" if index == 1 else
                                     " ".join(map(str, sorted(map(int, sample[1:])))) + "\n")
            session.add(problem)
            session.add(version)
            session.commit()
            roll = f"S{index:02d}"
            password = secrets.token_urlsafe(10)
            user = repo.create_user(session, roll, f"Sample Student {index}", "student", password,
                                    must_change_pw=True)
            credentials.append((roll, user.name, password))
            filename = "add_good.c" if index == 1 else "sort_descending.c"
            sample_source = (source / "submissions" / ("add_good.c" if index == 1 else "sort_good.c")).read_text("utf-8")
            if index == 2:
                sample_source = sample_source.replace("values[left] <= values[right]", "values[left] >= values[right]")
                sample_source = sample_source.replace("Sorts n integers in O(n log n) with a hand-written merge sort.",
                                                       "Sample with an intentional bug: sorts in descending order.")
            sub = repo.create_submission(session, user, problem, filename, sample_source)
            job = repo.enqueue_job(session, "grade", submission_id=sub.id, problem_id=problem.id)
            print(f"Created {slug}, student {roll}, submission {sub.id}, grading job {job.id}")

    path = DATA / "sample-accounts.txt"
    path.write_text("Admin: existing login and password unchanged.\n"
                    + "\n".join(f"{roll}\t{name}\t{password}" for roll, name, password in credentials)
                    + "\nStudents must change these temporary passwords at first login.\n", encoding="utf-8")
    engine.dispose()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    print(f"Backup: {backup}")
    print(f"Sample login details: {path}")


if __name__ == "__main__":
    main()
