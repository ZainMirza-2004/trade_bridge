import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.models import Base
from app.database.session import Database

PG_ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL", "postgresql+asyncpg://crypto:crypto@localhost:5433/crypto_terminal")


async def _postgres_url() -> str | None:
    """Use a throwaway PostgreSQL database when available (exact NUMERIC semantics), else SQLite."""
    if os.environ.get("TEST_FORCE_SQLITE"):
        return None
    name = f"ct_test_{uuid.uuid4().hex[:10]}"
    try:
        admin = create_async_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        await admin.dispose()
    except Exception:
        return None
    return PG_ADMIN_URL.rsplit("/", 1)[0] + f"/{name}"


async def _drop(url: str) -> None:
    name = url.rsplit("/", 1)[1]
    admin = create_async_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest_asyncio.fixture
async def db(tmp_path):
    url = await _postgres_url()
    database = Database(url or f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()
    if url:
        await _drop(url)


def D(x) -> Decimal:
    return Decimal(str(x))


@pytest.fixture
def anyio_backend():
    return "asyncio"
