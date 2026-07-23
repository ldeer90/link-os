"""SQLAlchemy engine and transaction helpers for canonical LINK OS storage."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def resolve_database_url(explicit_url: str | None = None) -> str:
    url = explicit_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is required; PostgreSQL is the authoritative LINK OS database"
        )
    return url


def create_database_engine(
    url: str | None = None,
    *,
    echo: bool = False,
    pool_pre_ping: bool = True,
) -> Engine:
    resolved = resolve_database_url(url)
    kwargs: dict[str, object] = {
        "echo": echo,
        "pool_pre_ping": pool_pre_ping,
    }
    if resolved.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(resolved, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
