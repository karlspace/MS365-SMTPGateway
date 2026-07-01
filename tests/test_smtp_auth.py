"""SMTP handler regression tests: bans are enforced on MAIL FROM."""
from __future__ import annotations

import datetime as dt
import types

from common.db import session_scope
from common.models import Ban, BanKind, BanScope
from relay.smtp_handler import RelayHandler


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _session(ip: str):
    return types.SimpleNamespace(peer=(ip, 5555), auth_data=None, host_name=None)


def _envelope():
    return types.SimpleNamespace(
        mail_from=None, mail_options=[], rcpt_tos=[], rcpt_options=[]
    )


async def test_ban_enforced_in_mail_from(db):
    async with session_scope() as s:
        s.add(Ban(scope=BanScope.IP, kind=BanKind.SMTP, value="1.2.3.4",
                  reason="test", until=_utcnow() + dt.timedelta(hours=1)))
    handler = RelayHandler(max_message_size=1_000_000, max_recipients=100)
    env = _envelope()
    res = await handler.handle_MAIL(None, _session("1.2.3.4"), env, "a@b.c", [])
    assert res.startswith("550")
    assert env.mail_from is None


async def test_unbanned_unauthenticated_requires_auth(db):
    # No ban, no AUTH, not whitelisted -> the ban gate passes through to 530.
    handler = RelayHandler(max_message_size=1_000_000, max_recipients=100)
    res = await handler.handle_MAIL(None, _session("9.9.9.9"), _envelope(), "a@b.c", [])
    assert res.startswith("530")
