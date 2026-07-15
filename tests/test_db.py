"""Database layer regression tests: per-connection PRAGMAs, pool policy."""
from __future__ import annotations

from sqlalchemy import text

from common.db import get_engine, session_scope


async def _pragmas() -> tuple[int, str, int]:
    """Read the per-connection PRAGMAs over a fresh session."""
    async with session_scope() as s:
        foreign_keys = (await s.execute(text("PRAGMA foreign_keys"))).scalar()
        journal_mode = (await s.execute(text("PRAGMA journal_mode"))).scalar()
        synchronous = (await s.execute(text("PRAGMA synchronous"))).scalar()
    return foreign_keys, str(journal_mode).lower(), synchronous


async def test_pragmas_applied_to_every_connection(db):
    # NullPool opens a fresh connection per session, so every iteration here
    # exercises a brand-new one. Before the connect listener existed the
    # PRAGMAs were applied once at startup, so only the first connection
    # carried them: from the second onwards synchronous silently fell back
    # to FULL (2). That is the exact signature of the bug.
    for _ in range(3):
        foreign_keys, journal_mode, synchronous = await _pragmas()
        assert foreign_keys == 1
        assert journal_mode == "wal"
        assert synchronous == 1  # NORMAL


async def test_engine_does_not_pool_connections(db):
    # A persistent pool is what let a connection wedged by a request cancelled
    # mid-query poison later requests, hanging every DB-backed route until the
    # container was restarted. Pin the policy so that switching back to a
    # pooling class fails here rather than in production 12h later.
    assert type(get_engine().pool).__name__ == "NullPool"
