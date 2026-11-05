"""Engine and session helpers shared by the API, the worker and the CLI."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Boolean, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, SQLModel, create_engine

from ..config import PROJECT_ROOT, Settings, get_settings
from . import models  # noqa: F401  (registers the tables)

_engine: Engine | None = None


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """Create an engine; SQLite gets WAL, busy_timeout and foreign keys on every connection."""
    u = make_url(url)
    if u.get_backend_name() != "sqlite":
        return create_engine(url, echo=echo, pool_pre_ping=True)
    pool = {}
    if u.database and u.database != ":memory:" and not u.database.startswith("file:"):
        db = Path(u.database)
        if not db.is_absolute():
            db = PROJECT_ROOT / db
            url = u.set(database=db.as_posix()).render_as_string(hide_password=False)
        db.parent.mkdir(parents=True, exist_ok=True)
        # SQLite connections are cheap; a class submitting together needs more than the default 5+10
        pool = {"pool_size": 20, "max_overflow": 40, "pool_timeout": 10}
    engine = create_engine(url, echo=echo, connect_args={"check_same_thread": False, "timeout": 5}, **pool)
    event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine((settings or get_settings()).database_url)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Swap the process-wide engine (tests point it at a temporary database)."""
    global _engine
    _engine = engine


def _column_ddl(engine: Engine, col) -> str:
    """`name TYPE [DEFAULT x]` for ALTER TABLE ADD COLUMN; NOT NULL columns always get a default."""
    ddl = f'"{col.name}" {col.type.compile(dialect=engine.dialect)}'
    default = col.default.arg if col.default is not None and col.default.is_scalar else None
    if default is None and not col.nullable:
        try:
            numeric = col.type.python_type in (int, float)
        except NotImplementedError:
            numeric = False
        default = False if isinstance(col.type, Boolean) else 0 if numeric else ""
    if default is None:
        return ddl
    if isinstance(default, bool):
        lit = ("TRUE" if default else "FALSE") if engine.dialect.name != "sqlite" else str(int(default))
    elif isinstance(default, (int, float)):
        lit = str(default)
    else:
        lit = "'" + str(default).replace("'", "''") + "'"
    return f"{ddl} DEFAULT {lit}" + ("" if col.nullable else " NOT NULL")


def upgrade_schema(engine: Engine) -> list[str]:
    """Add columns that newer models have but an existing database lacks. Returns what was added."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {_column_ddl(engine, col)}'))
                    added.append(f"{table.name}.{col.name}")
    return added


def init_db(engine: Engine | None = None) -> Engine:
    """Create missing tables and upgrade old ones in place (existing problems stay published)."""
    engine = engine or get_engine()
    upgrade_schema(engine)
    SQLModel.metadata.create_all(engine)
    # One-time, idempotent role migration. End old sessions so clients refresh their role.
    with engine.begin() as connection:
        connection.execute(text("UPDATE \"user\" SET role = 'admin', token_version = token_version + 1 "
                                "WHERE role = 'instructor'"))
    return engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session


def new_session(engine: Engine | None = None) -> Session:
    """Plain session for the worker and the CLI; the caller closes it."""
    return Session(engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Session that commits on success and rolls back on error."""
    session = new_session(engine)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
