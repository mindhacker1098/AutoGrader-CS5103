"""Isolated end-to-end fixtures; never used by application startup."""
import argparse
import secrets
from autograder.config import get_settings
from autograder.core.coordinator import load_problem
from autograder.core.corpus import read_text

DEMO_NAMES = [
    "Aarav Sharma", "Diya Patel", "Vivaan Gupta", "Ananya Iyer", "Arjun Reddy", "Ishita Banerjee",
    "Kabir Singh", "Meera Nair", "Rohan Das", "Saanvi Kulkarni", "Aditya Verma", "Priya Menon",
    "Vihaan Joshi", "Kavya Rao", "Siddharth Mishra", "Neha Chatterjee", "Aryan Kapoor", "Tanvi Desai",
]


def cmd_seed_demo(args) -> int:
    from autograder.db import repo
    from autograder.db.session import init_db, session_scope

    init_db()
    seeds = get_settings().seeds_dir / "problems"
    with session_scope() as s:
        if not repo.get_user_by_roll(s, "admin"):
            password = args.admin_password or secrets.token_urlsafe(9)
            repo.create_user(s, "admin", "Dr. Kavita Rao", "admin", password, must_change_pw=True)
            print(f"created admin admin / {password} (must change password)")
        n_user = n_job = 0
        for slug in ("add_two_integers", "sort_integers"):
            problem = repo.get_problem_by_slug(s, slug)
            if problem is None:
                problem, pv = repo.create_problem_from_dir(s, slug, None, seeds / slug, status="published")
                print(f"created problem {slug} v{pv.version} ({pv.inputs_hash[:20]}...)")
            for name in load_problem(seeds / slug)["submissions"]:
                n_user += 1
                roll = f"S{n_user:02d}"
                user = repo.get_user_by_roll(s, roll)
                if user is None:
                    user = repo.create_user(s, roll, DEMO_NAMES[(n_user - 1) % len(DEMO_NAMES)], "student",
                                            "pass1234", must_change_pw=False)
                if any(x.user_id == user.id for x in repo.latest_submissions_per_user(s, problem.id)):
                    continue
                sub = repo.create_submission(s, user, problem, name, read_text(seeds / slug / "submissions" / name))
                repo.enqueue_job(s, "grade", submission_id=sub.id, problem_id=problem.id)
                n_job += 1
    print(f"{n_user} demo students (password pass1234), {n_job} grade jobs queued")
    return 0


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--admin-password")
    cmd_seed_demo(parser.parse_args())
