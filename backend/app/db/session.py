from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def build_engine(settings: Settings, database_url: str | None = None) -> Engine:
    return create_engine(
        database_url or settings.database_url,
        pool_pre_ping=True,
        # Keep bound values (emails, phone numbers, request context) out of exception
        # messages, which are logged on unexpected errors.
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # Return every timestamptz in UTC regardless of the server's local timezone.
            "options": "-c timezone=UTC",
        },
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False lets services return ORM objects that are serialised
    # into response schemas after the transaction commits.
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def transaction(session: Session) -> Iterator[Session]:
    """Explicit service-level transaction boundary: commit on success, roll back on error."""
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
