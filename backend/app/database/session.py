from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str):
        kwargs = {"pool_pre_ping": True} if url.startswith("postgresql") else {}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            async with session.begin():
                yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
