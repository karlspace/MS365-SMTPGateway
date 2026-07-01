"""Shared fixtures: an isolated SQLite database per test."""
from __future__ import annotations

import os
import tempfile

import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    """Create a fresh schema on a temp SQLite file for one test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + path.replace(os.sep, "/")

    from common.db import dispose_engine, get_engine
    from common.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        await dispose_engine()
        try:
            os.remove(path)
        except OSError:
            pass
