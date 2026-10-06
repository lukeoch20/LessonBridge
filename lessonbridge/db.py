"""Database engine and session management."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, settings as default_settings
from .models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(cfg: Settings | None = None, *, echo: bool = False) -> Engine:
    """Create (or recreate) the global engine and the schema."""
    global _engine, _SessionLocal
    cfg = cfg or default_settings
    cfg.ensure_dirs()
    url = cfg.resolved_database_url
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    _engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            # WAL lets readers proceed while a writer commits, and a long busy timeout
            # makes concurrent web requests wait instead of failing (LB-41).
            if ":memory:" not in url:
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=30000")

    Base.metadata.create_all(_engine)
    if url.startswith("sqlite"):
        _add_missing_columns(_engine)
    from .ingestion.index import ensure_fts_schema

    ensure_fts_schema(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Additive schema migration for SQLite: add columns introduced after a database was created.

    Only nullable columns or columns with a server default are added, which is
    every column added since the first release. Nothing is dropped or altered.
    """
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if col.server_default is not None:
                    ddl += f" DEFAULT {col.server_default.arg.text}"
                conn.execute(text(ddl))


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
