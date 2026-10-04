"""Runtime configuration.

Everything is overridable through environment variables so the CLI, the web
app and the tests can point at different databases and storage directories.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    generator: str = field(default_factory=lambda: os.environ.get("LESSONBRIDGE_GENERATOR", "auto"))
    allow_network: bool = field(
        default_factory=lambda: os.environ.get("LESSONBRIDGE_ALLOW_NETWORK", "1") not in ("0", "false", "no")
    )
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
