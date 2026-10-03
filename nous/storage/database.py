"""Async database engine and session management."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nous.config import Settings


class Database:
    def __init__(self, settings: Settings, *, lock_timeout_seconds: int = 0) -> None:
        self.lock_timeout_seconds = lock_timeout_seconds
        options: dict = {}
        if lock_timeout_seconds:
            # In the startup packet, so every pooled connection carries it.
            # At 0 nothing is sent and the server's default applies.
            options["connect_args"] = {"server_settings": {"lock_timeout": str(lock_timeout_seconds * 1000)}}
        self.engine = create_async_engine(
            settings.db_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            echo=settings.log_level == "debug",
            **options,
        )
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def connect(self) -> None:
        """Verify connection and schema existence."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name IN ('brain', 'heart', 'nous_system')"
                )
            )
            schemas = {row[0] for row in result}
            expected = {"brain", "heart", "nous_system"}
            if schemas != expected:
                missing = expected - schemas
                raise RuntimeError(f"Missing database schemas: {missing}")

    async def disconnect(self) -> None:
        """Dispose of connection pool."""
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield an async session with automatic cleanup."""
        async with self.session_factory() as session:
            yield session

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()
