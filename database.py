"""
database.py – SQLAlchemy engine, session factory, and declarative base.

All other modules import `Base`, `engine`, and `get_db` from here.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# SQLite database stored in the backend directory.
# `check_same_thread=False` is required for SQLite when used with FastAPI's
# multi-threaded request handling.
DATABASE_URL = "sqlite:///./xray_manager.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,          # Set to True to log all SQL statements during development
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def get_db():
    """
    FastAPI dependency that yields a database session per request and
    ensures the session is closed when the request completes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
