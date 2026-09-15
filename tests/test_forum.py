import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.jobs import Jobs
from bot.parser import parse_tracker
from bot.service import MigrationService, PartialMigration
from bot.sources import forum_threads, read_single, read_thread, resolve_source

BEFORE = datetime(2026, 9, 15, tzinfo=UTC)
EARLIER = datetime(2026, 9, 1, tzinfo=UTC)


async def iterate(values):
    for value in values:
        yield value


def context():
    guild = SimpleNamespace(id=1097181193939730453)
    member = SimpleNamespace(id=7, guild=guild)
    guild.me = SimpleNamespace(id=8, guild=guild)
    guild.fetch_member = AsyncMock(return_value=member)
    guild.fetch_channel = AsyncMock()
    return guild, member


def channel(kind, guild, channel_id):
    result = MagicMock(spec=kind)
    result.id = channel_id
    result.guild = guild
    result.permissions_for.return_value = SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=True, manage_messages=True,
    )
    return result


def message(message_id, text):
    return SimpleNamespace(
        id=message_id, content=text, embeds=[], attachments=[], stickers=[],
        created_at=EARLIER, author=SimpleNamespace(id=9, display_name="Author"),
        jump_url=f"https://discord.com/channels/1/2/{message_id}",
    )


def thread(guild, thread_id=100, messages=None, tags=("bug",)):
    result = channel(discord.Thread, guild, thread_id)
    result.parent_id = 1385519706781253632
    result.name = f"Post {thread_id}"
    result.jump_url = f"https://discord.com/channels/{guild.id}/{thread_id}"
    result.created_at = EARLIER
    result.is_private.return_value = False
    result.applied_tags = [SimpleNamespace(name=tag) for tag in tags]
    result.history.side_effect = lambda **kwargs: iterate(
        messages if messages is not None else [message(thread_id, "Description: Broken")]
    )
    return result


def routes():
    return {
        kind: {"repository": "owner/repo", "project_owner": "owner", "project_number": 1}
        for kind in ("bug", "feature")
    }


def test_active_and_archived_threads_paginated_and_deduplicated():
    async def scenario():
        guild, _ = context()
        forum = channel(discord.ForumChannel, guild, 1385519706781253632)
        active = thread(guild, 100)
        foreign = thread(guild, 99)
        foreign.parent_id = 444
        archived = [thread(guild, i) for i in range(101, 250)]
        guild.active_threads = AsyncMock(return_value=[active, foreign])
        forum.archived_threads.side_effect = lambda **kwargs: iterate([active, *archived])
        found = [item.id async for item in forum_threads(forum)]
        assert found == list(range(100, 250))
        guild.active_threads.assert_awaited_once()
        forum.archived_threads.assert_called_once_with(limit=None)
    asyncio.run(scenario())


def test_complete_history_more_than_100_messages_and_reply_metadata():
    async def scenario():
        guild, member = context()
        messages = [message(100, "Тип: баг\nЗаголовок: Starter\nОписание: Broken")]
        messages += [message(i, f"Reply {i}") for i in range(101, 250)]
        messages.append(message(250, "Тип: фича\nЗаголовок: Do not override"))
        post = thread(guild, messages=messages)
        source = await read_thread(post, member, BEFORE)
        assert source.message_count == 151
        assert source.key == "discord:100"
        assert source.tracker.kind == "bug"
        assert source.tracker.title == "Post 100"
        assert "Reply 249" in source.tracker.description
        assert "Do not override" in source.tracker.description
        assert source.tracker.description.index("Reply 101") < source.tracker.description.index("Reply 249")
        post.history.assert_called_once_with(limit=None, oldest_first=True, before=BEFORE)
        post.fetch_message.assert_not_called()
    asyncio.run(scenario())


def test_attachment_only_and_embed_messages_are_included():
    async def scenario():
        guild, member = context()
        first = message(100, "")
        first.attachments = [SimpleNamespace(filename="picture.png", url="https://cdn.example/picture.png")]
        reply = message(101, "")
        reply.embeds = [discord.Embed(title="Observation", description="Embedded reply")]
        source = await read_thread(thread(guild, messages=[first, reply]), member, BEFORE)
        assert "picture.png" in source.tracker.description
        assert "Embedded reply" in source.tracker.description
        assert source.message_count == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("messages", [[], [message(100, "")]])
def test_empty_or_unreadable_history_fails(messages):
    async def scenario():
        guild, member = context()
        with pytest.raises(ValueError):
            await read_thread(thread(guild, messages=messages), member, BEFORE)
    asyncio.run(scenario())


def test_user_and_bot_access_checked_before_history():
    async def scenario():
        guild, member = context()
        post = thread(guild)
        post.permissions_for.side_effect = lambda actor: SimpleNamespace(
            view_channel=actor.id != guild.me.id, read_message_history=True,
        )
        with pytest.raises(ValueError):
            await read_thread(post, member, BEFORE)
        post.history.assert_not_called()
    asyncio.run(scenario())


def test_provided_forum_link_and_cross_guild_rejection():
    async def scenario():
        guild, member = context()
        forum = channel(discord.ForumChannel, guild, 1385519706781253632)
        guild.fetch_channel.return_value = forum
        interaction = SimpleNamespace(guild=guild, guild_id=guild.id, user=member, channel=forum)
        result = await resolve_source(
            interaction, "https://discord.com/channels/1097181193939730453/1385519706781253632",
        )
        assert result == (forum, None)
        guild.fetch_channel.reset_mock()
        with pytest.raises(ValueError):
            await resolve_source(interaction, "https://discord.com/channels/123/1385519706781253632")
        guild.fetch_channel.assert_not_awaited()
    asyncio.run(scenario())


def test_no_argument_uses_parent_forum_and_explicit_reply_uses_entire_thread():
    async def scenario():
        guild, member = context()
        post = thread(guild)
        forum = channel(discord.ForumChannel, guild, post.parent_id)
        guild.fetch_channel.return_value = forum
        interaction = SimpleNamespace(guild=guild, guild_id=guild.id, user=member, channel=post)
        assert await resolve_source(interaction) == (forum, None)
        source = await read_single(post, 999, member, BEFORE)
        assert source.key == "discord:100"
        post.fetch_message.assert_not_called()
    asyncio.run(scenario())


def test_bulk_job_continues_after_bad_post_and_persists_report(tmp_path):
    async def scenario():
        guild, member = context()
        forum = channel(discord.ForumChannel, guild, 1385519706781253632)
        bad = thread(guild, 100, tags=())
        good = thread(guild, 101)
        guild.active_threads = AsyncMock(return_value=[bad, good])
        forum.archived_threads.side_effect = lambda **kwargs: iterate([])
        bot = SimpleNamespace(dry_run=True, routes=routes(), service=AsyncMock())
        jobs = Jobs(bot, tmp_path)
        interaction = SimpleNamespace(guild=guild, guild_id=guild.id, user=member)
        job_id = jobs.start(interaction, forum, None, None)
        task = jobs.tasks[job_id]
        with pytest.raises(ValueError, match="уже выполняется"):
            jobs.start(interaction, forum, None, None)
        await task
        report = jobs.load(job_id, guild.id, member.id)
        assert report["state"] == "completed_with_errors"
        assert [r["state"] for r in report["results"]] == ["failed", "dry_run"]
        bot.service.migrate.assert_not_awaited()
        with pytest.raises(ValueError):
            jobs.load(job_id, guild.id, 999)
        assert not jobs.guild_jobs
        assert (tmp_path / f"{job_id}.json").exists()
    asyncio.run(scenario())


def test_interrupted_job_marked_on_restart(tmp_path):
    jobs = Jobs(SimpleNamespace(), tmp_path)
    job_id = "a" * 32
    jobs.save({"id": job_id, "state": "running", "guild_id": 1, "user_id": 2})
    restarted = Jobs(SimpleNamespace(), tmp_path)
    assert restarted.load(job_id, 1, 2)["state"] == "interrupted"


def test_long_history_fails_explicitly_without_creating_issue(tmp_path):
    async def scenario():
        guild, member = context()
        post = thread(guild, messages=[message(100, "x" * 61000)])
        bot = SimpleNamespace(dry_run=False, routes=routes(), service=AsyncMock())
        jobs = Jobs(bot, tmp_path)
        report = {"id": "b" * 32, "user_id": member.id, "results": []}
        await jobs.process(report, guild, post, None, None, BEFORE)
        assert report["results"][0]["state"] == "failed"
        assert "слишком большая" in report["results"][0]["error"]
        bot.service.migrate.assert_not_awaited()
    asyncio.run(scenario())


def test_existing_issue_updated_and_project_failure_retried_without_duplicate(tmp_path):
    async def scenario():
        github = AsyncMock()
        github.find_issue.return_value = None
        issue = {"number": 1, "node_id": "I_1", "html_url": "https://github.com/owner/repo/issues/1"}
        github.create_issue.return_value = issue
        github.update_issue_body.return_value = issue
        github.add_to_project.side_effect = [RuntimeError("unavailable"), None]
        service = MigrationService(github, str(tmp_path / "db.sqlite3"))
        tracker = parse_tracker("Тип: баг\nОписание: old")
        try:
            with pytest.raises(PartialMigration):
                await service.migrate(tracker, routes()["bug"], "discord:100", update_existing=True)
            updated = parse_tracker("Тип: баг\nОписание: new reply")
            url, reused = await service.migrate(
                updated, routes()["bug"], "discord:100", update_existing=True,
            )
            assert reused and url == issue["html_url"]
            github.create_issue.assert_awaited_once()
            assert "new reply" in github.update_issue_body.call_args.args[2]
            assert "ss220-tracker:" in github.update_issue_body.call_args.args[2]
        finally:
            service.close()
    asyncio.run(scenario())
