"""Command line tool.

    python -m autograder.cli grade <problem> <file.c> [--rules r.json] [--no-tutor] [--verify] [--quiet]
    python -m autograder.cli batch [all|<problem>] [--no-tutor] [--quiet]
    python -m autograder.cli validate <rules.json>
    python -m autograder.cli init-db | bootstrap-admin
    python -m autograder.cli create-user <roll> <name> <role> [--password PW]
    python -m autograder.cli reset-password <roll> [--password PW]
    python -m autograder.cli import-roster <roster.csv> [--out passwords.csv]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import secrets
import sys
from pathlib import Path

from .config import get_settings
from .core.analyzers import REGISTRY
from .core.coordinator import grade_submission, load_problem, write_reports
from .core.corpus import read_text
from .core.textfmt import dump_json, to_fixed
from .core.report import render_text
from .core.rules import validate_rules

COLORS = {"plan": 36, "ok": 32, "verdict": 32, "warn": 33, "security": 35, "error": 31, "result": 37}


def make_logger(quiet: bool = False, keep=("verdict", "error")):
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def log(e: dict) -> None:
        if e.get("kind") == "stage" or (quiet and e.get("kind") not in keep):
            return
        tag = str(e.get("metric") or e.get("kind")).ljust(13)
        if color:
            print(f"\x1b[{COLORS.get(e.get('kind'), 90)}m[{tag}]\x1b[0m {e.get('msg')}", flush=True)
        else:
            print(f"[{tag}] {e.get('msg')}", flush=True)

    return log


def problems_root(args) -> Path:
    if args.problems_dir:
        p = Path(args.problems_dir)
        return p if p.is_absolute() else Path.cwd() / p
    return get_settings().seeds_dir / "problems"


def problem_dir_of(root: Path, p: str) -> Path:
    return root / p if (root / p).is_dir() else Path(p).resolve()


def reports_dir(problem_dir: Path) -> Path:
    return get_settings().DATA_DIR / "reports" / problem_dir.name


async def grade_one(problem_dir: Path, file: Path, rules: dict, log, *, tutor_note=None, settings=None,
                    corpus=None, out_dir: Path | None = None, inputs_hash_sep: str = "/"):
    source = read_text(file)
    result = await grade_submission(problem_dir, rules, source, file.name, log, corpus=corpus,
                                    tutor_note=tutor_note, settings=settings, inputs_hash_sep=inputs_hash_sep)
    out = write_reports(result, None, out_dir or reports_dir(problem_dir), log)
    return result, out


def cmd_validate(args) -> int:
    try:
        rules = json.loads(Path(args.rules_file).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"INVALID:\n  - cannot read rules: {e}")
        return 1
    errs = validate_rules(rules, list(REGISTRY))
    print("INVALID:\n  - " + "\n  - ".join(errs) if errs else "rules OK")
    return 1 if errs else 0


async def cmd_grade(args) -> int:
    log = make_logger(args.quiet)
    problem_dir = problem_dir_of(problems_root(args), args.problem)
    rules_path = Path(args.rules) if args.rules else problem_dir / "rules.json"
    rules = json.loads(rules_path.read_text("utf-8"))
    file = Path(args.file) if Path(args.file).is_file() else problem_dir / "submissions" / args.file
    tutor = False if args.no_tutor else None
    result, out = await grade_one(problem_dir, file, rules, log, tutor_note=tutor)
    print("\n" + render_text(result))
    print(f"reports: {out['txt']}\n         {out['json']}")
    if args.verify:
        print("\n--verify: grading the same submission again...")
        again, _ = await grade_one(problem_dir, file, rules, log, tutor_note=tutor)
        same = again["grade"] == result["grade"] and all(
            m["score"] == n["score"] for m, n in zip(result["metrics"], again["metrics"]))
        if same:
            print(f"REPRODUCIBLE: both runs gave {to_fixed(result['grade'], 1)}/100 with identical metric scores.")
        else:
            print(f"NOT REPRODUCIBLE: {result['grade']} vs {again['grade']}")
        return 0 if same else 2
    return 0


async def cmd_batch(args) -> int:
    log = make_logger(args.quiet)
    root = problems_root(args)
    if not args.problem or args.problem == "all":
        names = sorted(p.name for p in root.iterdir() if (p / "rules.json").is_file())
    else:
        names = [args.problem]
    tutor = False if args.no_tutor else None
    summary = []
    for name in names:
        problem_dir = problem_dir_of(root, name)
        prob = load_problem(problem_dir)
        for sub in prob["submissions"]:
            print(f"\n=== {name}/{sub} ===")
            result, _ = await grade_one(problem_dir, problem_dir / "submissions" / sub, prob["rules"], log,
                                        tutor_note=tutor)
            summary.append({"problem": name, "submission": sub, "grade": result["grade"],
                            "flags": ",".join(f["type"] for f in result["flags"])})
    print("\n================ SUMMARY ================")
    for r in summary:
        print(f"{r['problem'].ljust(18)} {r['submission'].ljust(24)} {to_fixed(r['grade'], 1).rjust(6)}  {r['flags']}")
    out = get_settings().DATA_DIR / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(dump_json(summary, indent=2), "utf-8", newline="")
    return 0


def cmd_init_db(args) -> int:
    from .db.session import init_db

    init_db()
    print(f"database ready: {get_settings().database_url}")
    return 0


def cmd_create_user(args) -> int:
    from .db import repo
    from .db.session import init_db, session_scope

    init_db()
    password = args.password or repo.generate_password()
    with session_scope() as s:
        user = repo.create_user(s, args.roll, args.name, args.role.lower(), password, must_change_pw=True)
        print(f"created {user.role} {user.roll_no} (id {user.id})")
    if not args.password:
        print(f"password: {password}")
    return 0


def cmd_reset_password(args) -> int:
    from .db import repo
    from .db.session import init_db, session_scope

    init_db()
    password = args.password or repo.generate_password()
    with session_scope() as s:
        user = repo.get_user_by_roll(s, args.roll)
        if user is None:
            print(f"no user with roll number {args.roll!r}", file=sys.stderr)
            return 1
        user.password_hash = repo.hash_password(password)
        user.must_change_pw = True
        user.is_active = True
        user.token_version = (user.token_version or 0) + 1  # signs out every open session
        s.add(user)
        print(f"password reset for {user.role} {user.roll_no}; it must be changed at the next login")
    if not args.password:
        print(f"password: {password}")
    return 0


def cmd_import_roster(args) -> int:
    from .db import repo
    from .db.session import init_db, session_scope

    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip().lower(): (v or "") for k, v in r.items()} for r in csv.DictReader(f)]
    init_db()
    with session_scope() as s:
        out = repo.import_roster(s, rows)
    fields = ["roll_no", "name", "role", "password", "status", "reason"]
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
        print(f"{sum(r['status'] == 'create' for r in out)} created, passwords written to {args.out}")
    else:
        w = csv.DictWriter(sys.stdout, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    return 0


def cmd_bootstrap_admin(args) -> int:
    from sqlmodel import select
    from .db import repo
    from .db.models import User
    from .db.session import init_db, session_scope
    init_db()
    with session_scope() as session:
        if session.exec(select(User).where(User.role == "admin", User.is_active == True)).first():
            print("An active administrator already exists; account data unchanged.")
            return 0
        if repo.get_user_by_roll(session, args.roll):
            raise ValueError("That account ID already exists; choose a different --roll.")
        password = secrets.token_urlsafe(16)
        repo.create_user(session, args.roll, args.name, "admin", password, must_change_pw=True)
        print(f"Administrator account: {args.roll} / {password} (change password at first login)")
    return 0


async def cmd_web_corpus(args) -> int:
    from .core.webcorpus import build

    problem_dir = Path(args.problem_dir) if args.problem_dir else _problem_folder(args.problem)
    res = await build(problem_dir, urls=args.url, query=args.query,
                      log=lambda e: print(f"[{e.get('kind', '')}] {e.get('msg', '')}"))
    print(f"\n{'searched with API' if res['searched'] else 'given URLs only'}; saved {len(res['kept'])} file(s) "
          f"to {problem_dir / 'corpus' / 'web'}")
    for page in res["pages"]:
        print(f"  {page['kept']}/{page['programs']} kept  {page['status']:<22} {page['url']}")
    return 0


def _problem_folder(name: str) -> Path:
    """The current version folder of a problem in the app's data directory."""
    from .config import get_settings
    base = get_settings().problems_dir / name
    versions = sorted((p for p in base.glob("v*") if p.name[1:].isdigit()), key=lambda p: int(p.name[1:]))
    if not versions:
        raise ValueError(f"No problem '{name}' in {get_settings().problems_dir}; use --problem-dir")
    return versions[-1]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m autograder.cli", description="AutoGrader command line")
    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("grade", help="grade one submission")
    g.add_argument("problem")
    g.add_argument("file")
    g.add_argument("--rules")
    g.add_argument("--verify", action="store_true", help="grade twice and check the results match")
    g.add_argument("--no-tutor", action="store_true")
    g.add_argument("--quiet", action="store_true")
    g.add_argument("--problems-dir")

    b = sub.add_parser("batch", help="grade every submission of a problem (or all problems)")
    b.add_argument("problem", nargs="?", default="all")
    b.add_argument("--no-tutor", action="store_true")
    b.add_argument("--quiet", action="store_true")
    b.add_argument("--problems-dir")

    v = sub.add_parser("validate", help="check a rules.json file")
    v.add_argument("rules_file")

    sub.add_parser("init-db", help="create the database tables")
    u = sub.add_parser("create-user", help="add one user")
    u.add_argument("roll")
    u.add_argument("name")
    u.add_argument("role", choices=["student", "ta", "admin"], type=str.lower)
    u.add_argument("--password", help="default: generate one and print it")
    rp = sub.add_parser("reset-password", help="set a new password for an existing user (e.g. a locked-out admin)")
    rp.add_argument("roll")
    rp.add_argument("--password", help="default: generate one and print it")
    r = sub.add_parser("import-roster", help="add users from a CSV with columns roll_no,name,role[,password]")
    r.add_argument("csv_file")
    r.add_argument("--out", help="write the generated passwords here instead of stdout")
    wc = sub.add_parser("web-corpus", help="save known web solutions (corpus/web) for copy detection")
    wc.add_argument("problem", help="problem slug, e.g. prime_number")
    wc.add_argument("--url", action="append", default=[], help="page with a solution (repeat for several)")
    wc.add_argument("--query", help="search the web for this text (needs BRAVE_API_KEY or GOOGLE_CSE_KEY/CX)")
    wc.add_argument("--problem-dir", help="problem folder (default: the current version in DATA_DIR)")
    setup = sub.add_parser("bootstrap-admin", help="create the first administrator without sample data")
    setup.add_argument("--roll", default="admin")
    setup.add_argument("--name", default="Administrator")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        if args.cmd == "validate":
            return cmd_validate(args)
        if args.cmd == "grade":
            return asyncio.run(cmd_grade(args))
        if args.cmd == "batch":
            return asyncio.run(cmd_batch(args))
        if args.cmd == "init-db":
            return cmd_init_db(args)
        if args.cmd == "create-user":
            return cmd_create_user(args)
        if args.cmd == "reset-password":
            return cmd_reset_password(args)
        if args.cmd == "import-roster":
            return cmd_import_roster(args)
        if args.cmd == "web-corpus":
            return asyncio.run(cmd_web_corpus(args))
        if args.cmd == "bootstrap-admin":
            return cmd_bootstrap_admin(args)
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        return 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
