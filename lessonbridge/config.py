"""Runtime configuration.

Everything is overridable through environment variables so the CLI, the web
app and the tests can point at different databases and storage directories.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "off", "n", "f", ""}


def env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable; unknown spellings are an error rather than silently true (LB-59)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in TRUE_VALUES:
        return True
    if v in FALSE_VALUES:
        return False
    raise ValueError(f"{name}={raw!r} is not a boolean; use one of: {', '.join(sorted(TRUE_VALUES | FALSE_VALUES - {''}))}")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: _env_path("LESSONBRIDGE_DATA_DIR", Path("data")))
    database_url: str | None = field(default_factory=lambda: os.environ.get("LESSONBRIDGE_DATABASE_URL"))
    model: str = field(default_factory=lambda: os.environ.get("LESSONBRIDGE_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.environ.get("LESSONBRIDGE_EFFORT", "medium"))
    # "auto" uses Claude when credentials exist, else the deterministic template generator.
    generator: str = field(default_factory=lambda: os.environ.get("LESSONBRIDGE_GENERATOR", "auto").strip().lower())
    allow_network: bool = field(default_factory=lambda: env_bool("LESSONBRIDGE_ALLOW_NETWORK", True))
    max_generation_attempts: int = 3

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'lessonbridge.db').as_posix()}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
