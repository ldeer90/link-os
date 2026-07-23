from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from deal_tracker.platform.db import create_database_engine, make_session_factory


@lru_cache(maxsize=1)
def engine() -> Engine:
    return create_database_engine()


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    return make_session_factory(engine())


@contextmanager
def platform_session() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def platform_configured() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def reset_runtime() -> None:
    """Clear cached connections after a test or explicit config change."""
    if engine.cache_info().currsize:
        engine().dispose()
    session_factory.cache_clear()
    engine.cache_clear()

