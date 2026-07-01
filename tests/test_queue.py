"""Queue worker regression tests: reaper, retry policy, archive audit."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from common import archive
from common.db import session_scope
from common.graph_client import GraphError, TokenInfo
from common.models import (
    AuditEventType,
    AuditLog,
    MailQueue,
    MailStatus,
    Settings,
)
from relay import queue_manager


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


async def _add(**kwargs) -> int:
    async with session_scope() as s:
        row = MailQueue(
            sender="a@b.c", recipients_json='["x@y.z"]', raw_mime_b64="", **kwargs
        )
        s.add(row)
        await s.flush()
        return row.id


class _Fail:
    def __init__(self, exc: GraphError) -> None:
        self._exc = exc

    def send_mime(self, sender, raw):
        raise self._exc


class _OK:
    def send_mime(self, sender, raw):
        return None

    def acquire_token(self):
        return TokenInfo(access_token="x", expires_at=_utcnow())


async def test_reaper_recovers_sending(db):
    stuck = await _add(status=MailStatus.SENDING, attempts=1)
    sent = await _add(status=MailStatus.SENT, attempts=1)
    n = await queue_manager.recover_orphaned_sending()
    assert n == 1
    async with session_scope() as s:
        assert (await s.get(MailQueue, stuck)).status == MailStatus.PENDING
        assert (await s.get(MailQueue, sent)).status == MailStatus.SENT


async def test_transient_stays_pending_and_honours_retry_after(db):
    async with session_scope() as s:
        s.add(Settings(id=1, queue_max_attempts=3))
    rid = await _add(status=MailStatus.PENDING, attempts=3, next_attempt_at=_utcnow())

    worker = queue_manager.QueueWorker()

    async def gc():
        return _Fail(GraphError("throttled", status_code=429,
                                retry_after=120, transient=True))
    worker._graph_client = gc

    before = _utcnow()
    await worker._process(rid)
    async with session_scope() as s:
        row = await s.get(MailQueue, rid)
        assert row.status == MailStatus.PENDING  # not DEAD despite attempts >= max
        assert 110 <= (row.next_attempt_at - before).total_seconds() <= 130


async def test_permanent_failure_dead_at_max(db):
    async with session_scope() as s:
        s.add(Settings(id=1, queue_max_attempts=3))
    rid = await _add(status=MailStatus.PENDING, attempts=3, next_attempt_at=_utcnow())

    worker = queue_manager.QueueWorker()

    async def gc():
        return _Fail(GraphError("bad recipient", status_code=550, transient=False))
    worker._graph_client = gc

    await worker._process(rid)
    async with session_scope() as s:
        assert (await s.get(MailQueue, rid)).status == MailStatus.DEAD


async def test_archive_failure_audited_but_still_sent(db, monkeypatch):
    async with session_scope() as s:
        s.add(Settings(id=1, queue_max_attempts=3))
    rid = await _add(status=MailStatus.PENDING, attempts=0, next_attempt_at=_utcnow())

    worker = queue_manager.QueueWorker()

    async def gc():
        return _OK()
    worker._graph_client = gc

    def boom(**kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(archive, "write_eml", boom)

    await worker._process(rid)
    async with session_scope() as s:
        row = await s.get(MailQueue, rid)
        assert row.status == MailStatus.SENT  # delivered; must not retry
        assert row.archive_path is None
        events = (await s.execute(
            select(AuditLog).where(
                AuditLog.event_type == AuditEventType.ARCHIVE_WRITE_FAIL)
        )).scalars().all()
        assert len(events) == 1
