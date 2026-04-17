from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.storage.models import Base


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, echo=False, future=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def init_database(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_payments_columns(conn)


async def _ensure_payments_columns(conn: AsyncConnection) -> None:
    columns = await _fetch_payments_columns(conn)
    if not columns:
        return

    alter_statements = []
    if "full_name" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN full_name VARCHAR(255)")
    if "address" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN address VARCHAR(512)")
    if "age" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN age INTEGER")
    if "phone" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN phone VARCHAR(64)")
    if "ticket_number" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN ticket_number VARCHAR(3)")
    if "ticket_valid" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN ticket_valid BOOLEAN DEFAULT 0")
    if "ticket_used_at" not in columns:
        alter_statements.append("ALTER TABLE payments ADD COLUMN ticket_used_at DATETIME")

    for statement in alter_statements:
        await conn.execute(text(statement))

    indexes = await _fetch_indexes(conn)
    if "ix_payments_ticket_number" not in indexes:
        await conn.execute(text("CREATE INDEX ix_payments_ticket_number ON payments (ticket_number)"))

    if "uq_payments_ticket_number" not in indexes:
        if conn.engine.dialect.name == "sqlite":
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_ticket_number "
                    "ON payments (ticket_number) WHERE ticket_number IS NOT NULL"
                )
            )


async def _fetch_payments_columns(conn: AsyncConnection) -> set[str]:
    if conn.engine.dialect.name == "sqlite":
        rows = await conn.execute(text("PRAGMA table_info(payments)"))
        return {str(row[1]) for row in rows}

    rows = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'payments'"
        )
    )
    return {str(row[0]) for row in rows}


async def _fetch_indexes(conn: AsyncConnection) -> set[str]:
    if conn.engine.dialect.name == "sqlite":
        rows = await conn.execute(text("PRAGMA index_list(payments)"))
        return {str(row[1]) for row in rows}

    rows = await conn.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'payments'"
        )
    )
    return {str(row[0]) for row in rows}

