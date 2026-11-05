"""Runtime settings, read from the environment and an optional .env file."""
from __future__ import annotations

import hashlib
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    DATA_DIR: Path = Path("data")
    DATABASE_URL: str = ""
    SANDBOX: Literal["auto", "docker", "wsl", "bwrap", "native"] = "auto"
    SANDBOX_IMAGE: str = "autograder-sandbox:1"
    MAX_SANDBOXES: int = 3
    WORKER_JOBS: int = 2
    OLLAMA_HOST: str = "http://localhost:11434"
    OLLAMA_NUM_CTX: int = 4096
    LLM_PROFILE: Literal["laptop", "lab"] = "laptop"
    MODEL_REVIEW: str = "qwen2.5-coder:7b"
    MODEL_PRE: str = "qwen2.5-coder:7b"
    MODEL_EXPLAIN: str = "qwen2.5-coder:7b"
    GROQ_API_KEY: str = ""
    # optional official search APIs for the web-solutions corpus (only the problem title is searched)
    BRAVE_API_KEY: str = ""
    GOOGLE_CSE_KEY: str = ""
    GOOGLE_CSE_CX: str = ""
    JWT_SECRET: str = ""
    COOKIE_SECURE: bool = False
    LOGIN_FAILS_PER_IP: int = 30
    LOGIN_ATTEMPTS_PER_IP: int = 1000
    TRUSTED_PROXIES: str = ""
    ALLOW_WEAK_SANDBOX: bool = False
    LLM_CACHE_DIR: Path = Path("data/cache/llm")
    LLM_DISABLED: bool = False
    WSL_DISTRO: str = "Ubuntu"
    PRE_ANALYSIS_LLM: bool = False
    SAMPLE_OUTPUT_AUTO: bool = True  # run the reference on the sample input when a student opens a problem
    BINOCULARS_OBSERVER: str = ""
    BINOCULARS_PERFORMER: str = ""
    # Codacy: CLI path inside WSL (Windows) or on this host (Linux); cloud mode needs all four CODACY_* ids
    CODACY_CLI: str = "~/.local/bin/codacy-cli"
    CODACY_TEMPLATE_DIR: Path = Path("sandbox/codacy")
    CODACY_TIMEOUT_S: float = 60
    CODACY_API_TOKEN: str = ""
    CODACY_PROVIDER: str = ""
    CODACY_ORG: str = ""
    CODACY_REPO: str = ""

    @field_validator("SANDBOX", "LLM_PROFILE", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.lower() if isinstance(v, str) else v

    @field_validator("DATA_DIR", "LLM_CACHE_DIR", "CODACY_TEMPLATE_DIR", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return _resolve(v)

    @property
    def database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return "sqlite:///" + (self.DATA_DIR / "app.db").as_posix()

    @property
    def instance_id(self) -> str:
        """Names this installation on a shared Docker host, so workers only sweep their own sandboxes."""
        return hashlib.sha256(self.database_url.encode("utf-8")).hexdigest()[:12]

    @property
    def problems_dir(self) -> Path:
        return self.DATA_DIR / "problems"

    @property
    def seeds_dir(self) -> Path:
        return PROJECT_ROOT / "seeds"

    def jwt_key(self) -> str:
        """JWT_SECRET, or a random key kept in DATA_DIR/jwt_secret when it is unset or weak."""
        if not weak_secret(self.JWT_SECRET):
            return self.JWT_SECRET
        path = self.DATA_DIR / "jwt_secret"
        if str(path) in _KEYS:
            return _KEYS[str(path)]
        try:
            key = path.read_text("ascii").strip()
        except OSError:
            key = ""
        if weak_secret(key):
            key = secrets.token_hex(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key)
        _KEYS[str(path)] = key
        return key


_KEYS: dict[str, str] = {}
WEAK_SECRETS = {"", "change-me", "changeme", "secret", "jwt-secret"}


def weak_secret(value: str | None) -> bool:
    v = (value or "").strip()
    return v.lower() in WEAK_SECRETS or len(v.encode("utf-8")) < 32


@lru_cache
def get_settings() -> Settings:
    return Settings()
