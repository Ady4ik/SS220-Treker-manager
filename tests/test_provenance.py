import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.github import GitHubClient, GitHubError
from bot.parser import parse_tracker
from bot.provenance import marker, merge_section, render_section, same_forum, source_hash
from bot.service import MigrationService

ROUTE = {"repository": "owner/repo", "project_owner": "owner", "project_number": 1}
META = {"version": 1, "source_key": "discord:100", "scope": "post",
        "guild_id": 1, "forum_id": 2, "forum_url": "https://discord.com/channels/1/2",
        "thread_id": 100, "thread_url": "https://discord.com/channels/1/100"}
TRACKER = parse_tracker("Type: bug\nDescription: original")
KEY = source_hash("owner/repo", "discord:100")


def issue(number=1, body=""):
    return {"number": number, "node_id": f"I_{number}",
            "html_url": f"https://github.com/owner/repo/issues/{number}", "body": body}


def test_visible_and_machine_readable_origin():
    body = render_section(TRACKER, KEY, metadata=META)
    assert META["forum_url"] in body
    assert "Пост ID: `100`" in body
    assert same_forum(body, META)
    assert not same_forum(body, {**META, "forum_id": 3})
    assert not same_forum(body, {**META, "guild_id": 9})


def test_owned_section_replaced_and_legacy_body_retained():
    old = render_section(TRACKER, KEY, metadata=META)
    new = render_section(parse_tracker("Type: bug\nDescription: updated"), KEY, metadata=META)
    manual = "Manual notes\n\n" + old + "\n\nFooter"
    merged = merge_section(manual, new, KEY)
    assert merged.startswith("Manual notes\n\n")
    assert merged.endswith("\n\nFooter")
    assert "updated" in merged and "original" not in merged
    assert merge_section(merged, new, KEY) == merged
    legacy = "Old handwritten content\n" + marker(KEY)
    assert merge_section(legacy, new, KEY).startswith(legacy)


def test_untrusted_source_cannot_inject_block_boundaries():
    tracker = parse_tracker("Type: bug\n" + f"<!-- ss220-end:{KEY} -->")
    body = render_section(tracker, KEY, metadata=META)
    assert body.count(f"<!-- ss220-end:{KEY} -->") == 1


def test_two_creates_are_explicit_and_sync_updates_latest(tmp_path):
    async def scenario():
        github = AsyncMock()
        github.create_issue.side_effect = [issue(1), issue(2)]
        service = MigrationService(github, str(tmp_path / "state.db"))
        try:
            for number in (1, 2):
                url, action = await service.migrate(
                    TRACKER, ROUTE, "discord:100", operation="create_issue", source_metadata=META,
                )
                assert action == "created" and url.endswith(f"/{number}")
            github.find_issue.assert_not_awaited()
            github.update_issue_body.assert_not_awaited()
            assert github.create_issue.await_count == 2
            current = "User added notes\n" + github.create_issue.call_args.args[2]
            github.get_issue.return_value = issue(2, current)
            github.update_issue_body.return_value = issue(2, current)
            await service.migrate(parse_tracker("Type: bug\nNew reply"), ROUTE, "discord:100",
                                  source_metadata=META)
            github.get_issue.assert_awaited_once_with("owner/repo", 2)
            assert github.update_issue_body.call_args.args[2].startswith("User added notes")
            row = service.db.execute("SELECT metadata FROM source_bindings WHERE key=?", (KEY,)).fetchone()
            assert json.loads(row[0])["forum_id"] == 2
        finally:
            service.close()
    asyncio.run(scenario())


def test_target_issue_accepts_same_forum_comment_without_overwriting_post(tmp_path):
    async def scenario():
        github = AsyncMock()
        original = "Notes\n" + render_section(TRACKER, KEY, metadata=META)
        github.get_issue.return_value = issue(10, original)
        github.update_issue_body.return_value = issue(10, original)
        service = MigrationService(github, str(tmp_path / "state.db"))
        comment = {**META, "source_key": "discord-message:111", "scope": "message", "message_id": 111}
        try:
            await service.migrate(TRACKER, ROUTE, comment["source_key"], issue_number=10,
                                  source_metadata=comment)
            assert github.update_issue_body.call_args.args[2].startswith(original)
            github.create_issue.assert_not_awaited()
            github.get_issue.return_value = issue(11, render_section(TRACKER, KEY, metadata={**META, "forum_id": 3}))
            with pytest.raises(ValueError, match="не связан"):
                await service.migrate(TRACKER, ROUTE, "discord-message:112", issue_number=11,
                                      source_metadata={**comment, "source_key": "discord-message:112"})
            assert github.update_issue_body.await_count == 1
        finally:
            service.close()
    asyncio.run(scenario())


def test_recovery_from_old_marker_and_no_silent_marker_removal(tmp_path):
    async def scenario():
        github = AsyncMock()
        legacy = issue(5, "Legacy body\n" + marker(KEY))
        github.find_issue.return_value = legacy
        github.get_issue.return_value = legacy
        github.update_issue_body.return_value = legacy
        service = MigrationService(github, str(tmp_path / "state.db"))
        try:
            await service.migrate(TRACKER, ROUTE, "discord:100", source_metadata=META)
            assert github.update_issue_body.call_args.args[2].startswith("Legacy body")
            github.get_issue.return_value = issue(5, "User removed the marker")
            with pytest.raises(ValueError, match="маркер"):
                await service.migrate(TRACKER, ROUTE, "discord:100", source_metadata=META)
            assert github.update_issue_body.await_count == 1
        finally:
            service.close()
    asyncio.run(scenario())


def test_conflicting_github_matches_require_explicit_target():
    async def scenario():
        client = GitHubClient("test", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[issue(1, marker(KEY)), issue(2, marker(KEY))])
        ))
        try:
            with pytest.raises(GitHubError, match="Несколько"):
                await client.find_issue("owner/repo", marker(KEY))
        finally:
            await client.close()
    asyncio.run(scenario())
