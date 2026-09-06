from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


def _add_missing_columns() -> None:
    """Lightweight schema migration: add any model column missing from an
    existing table. There's no Alembic here -- this is enough for a
    single-table personal app where new columns are always nullable/JSON
    (the model's property getters already treat NULL as "empty")."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def init_db() -> None:
    from app import models  # noqa: F401  (ensure tables are registered)

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
