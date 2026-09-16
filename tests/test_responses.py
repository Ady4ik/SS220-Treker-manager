import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.main import forum_sync, migrate_error
from bot.responses import acknowledge, reply


def interaction():
    item = MagicMock()
    item.response.defer = AsyncMock()
    item.response.send_message = AsyncMock()
    item.response.is_done.return_value = False
    item.followup.send = AsyncMock()
    item.is_expired.return_value = False
    return item


def unknown():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                            {"code": 10062, "message": "Unknown interaction"})


def test_failed_ack_does_not_start_job_or_retry():
    async def scenario():
        item = interaction()
        item.response.defer.side_effect = unknown()
        await forum_sync.callback(item)
        item.client.jobs.start.assert_not_called()
        item.followup.send.assert_not_awaited()
        item.response.send_message.assert_not_awaited()
    asyncio.run(scenario())


def test_error_handler_does_not_reply_to_unknown_interaction():
    async def scenario():
        item = interaction()
        await migrate_error(item, SimpleNamespace(original=unknown()))
        item.response.send_message.assert_not_awaited()
        item.followup.send.assert_not_awaited()
    asyncio.run(scenario())


def test_followup_failure_is_not_retried():
    async def scenario():
        item = interaction()
        item.response.is_done.return_value = True
        item.followup.send.side_effect = unknown()
        assert not await reply(item, "job started")
        item.followup.send.assert_awaited_once()
        item.response.send_message.assert_not_awaited()
    asyncio.run(scenario())


def test_ack_success_and_expired_reply():
    async def scenario():
        item = interaction()
        assert await acknowledge(item)
        item.is_expired.return_value = True
        assert not await reply(item, "expired")
        item.followup.send.assert_not_awaited()
    asyncio.run(scenario())
