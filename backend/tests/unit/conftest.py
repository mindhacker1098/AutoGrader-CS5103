"""Fixtures for the API tests: a temporary database with two students, a TA, an admin and one problem."""
import pytest
from apiutil import SEEDS, client_for
from autograder.api import auth as auth_api
from autograder.api.main import create_app
from autograder.config import get_settings
from autograder.db import repo
from autograder.db.session import init_db, make_engine, new_session, set_engine


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-secret-0123456789abcdef0123456789")
    monkeypatch.setenv("LLM_DISABLED", "true")
    monkeypatch.setenv("SAMPLE_OUTPUT_AUTO", "false")
    get_settings.cache_clear()
    eng = make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(eng)
    set_engine(eng)
    auth_api.reset_rate_limit()
    with new_session(eng) as s:
        repo.create_user(s, "S01", "Ann", "student", "pass01", must_change_pw=False)
        repo.create_user(s, "S02", "Bob", "student", "pass02", must_change_pw=False)
        repo.create_user(s, "T01", "Tara", "ta", "passta", must_change_pw=False)
        repo.create_user(s, "I01", "Ira", "admin", "passin", must_change_pw=False)
        repo.create_problem_from_dir(s, "add_two_integers", None, SEEDS / "problems" / "add_two_integers",
                                     problems_dir=tmp_path / "problems")
    yield eng
    set_engine(None)
    eng.dispose()
    get_settings.cache_clear()


@pytest.fixture
def app(env):
    return create_app()


@pytest.fixture
def ann(app):
    return client_for(app, "S01", "pass01")


@pytest.fixture
def bob(app):
    return client_for(app, "S02", "pass02")


@pytest.fixture
def ta(app):
    return client_for(app, "T01", "passta")


@pytest.fixture
def inst(app):
    return client_for(app, "I01", "passin")
