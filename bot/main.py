from __future__ import annotations

import hashlib
import json
import os
import re
from logging import getLogger
from pathlib import Path

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv

from .github import GitHubClient, GitHubError
from .parser import parse_tracker
from .service import MigrationService, PartialMigration, resolve_route

MESSAGE_LINK = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
LOGGER = getLogger(__name__)


async def read_tracker_source(interaction, source=None):
    """Read on demand, only within the guild and caller-visible channels."""
    if source and not MESSAGE_LINK.fullmatch(source.strip()):
        if source.strip().startswith(("https://", "http://")):
            raise ValueError("Нужна ссылка на сообщение Discord, а не внешний трекер")
        key = f"text:{interaction.guild_id}:" + hashlib.sha256(source.encode()).hexdigest()
        return source, (), None, None, key
    if source:
        guild_id, channel_id, message_id = map(int, MESSAGE_LINK.fullmatch(source.strip()).groups())
        if guild_id != interaction.guild_id:
            raise ValueError("Трекер должен находиться на этом сервере")
        channel = interaction.guild.get_channel_or_thread(channel_id)
        if channel is None:
            channel = await interaction.client.fetch_channel(channel_id)
    else:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            raise ValueError("Вызовите /migrate внутри поста форума или передайте source")
        message_id = channel.id
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise TypeError("Не поддерживаемый канал")
    if channel.guild.id != interaction.guild_id:
        raise ValueError("Другой сервер")
    permissions = channel.permissions_for(interaction.user)
    if not permissions.view_channel or not permissions.read_message_history:
        raise ValueError("Нет прав читать исходный канал")
    if isinstance(channel, discord.Thread) and channel.is_private() and not permissions.manage_threads:
        try:
            await channel.fetch_member(interaction.user.id)
        except discord.NotFound as exc:
            raise ValueError("Вы не участник приватного треда") from exc
    message = await channel.fetch_message(message_id)
    tags = tuple(tag.name for tag in channel.applied_tags) if isinstance(channel, discord.Thread) else ()
    title = channel.name if isinstance(channel, discord.Thread) else None
    text = message.content
    if not text.strip():
        raise ValueError("Текст сообщения недоступен. Проверьте Message Content Intent")
    if message.attachments:
        text += "\n\nВложения:\n" + "\n".join(a.url for a in message.attachments)
    return text, tags, title, message.jump_url, f"discord:{message.id}"


@app_commands.command(name="migrate", description="Мигрировать трекер в GitHub Issue и Project")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(source="Ссылка или текст; без аргумента — текущий пост форума",
                       repository="owner/repository из настроенного маршрута")
async def migrate(interaction: discord.Interaction, source: str | None = None,
                  repository: str | None = None):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    try:
        text, tags, title, url, source_key = await read_tracker_source(interaction, source)
        tracker = parse_tracker(text, tags, title)
        route = resolve_route(bot.routes, tracker.kind, repository)
        if bot.dry_run:
            labels = sorted(set(tracker.labels + tuple(route.get("labels", []))))
            preview = (f"Dry-run → `{route['repository']}` / Project {route['project_number']}\n"
                       f"**{tracker.title}**\n{tracker.summary}\nLabels: {', '.join(labels)}")
            await interaction.followup.send(preview[:1900], ephemeral=True)
            return
        issue_url, reused = await bot.service.migrate(tracker, route, source_key, url)
        await interaction.followup.send(
            f"{'Существующий Issue, Project обновлён' if reused else 'Готово'}: {issue_url}", ephemeral=True)
    except PartialMigration as exc:
        await interaction.followup.send(f"{exc}\n{exc.url}", ephemeral=True)
    except (ValueError, GitHubError) as exc:
        await interaction.followup.send(str(exc)[:1900], ephemeral=True)
    except (discord.HTTPException, httpx.HTTPError):
        await interaction.followup.send("Ошибка API/сети. Проверьте доступ и повторите команду.", ephemeral=True)
    except RuntimeError:
        LOGGER.exception("Unexpected migration failure; source and credentials omitted")
        await interaction.followup.send("Внутренняя ошибка миграции.", ephemeral=True)


@migrate.error
async def migrate_error(interaction, error):
    await interaction.response.send_message(
        "Команда доступна только на сервере участникам с правом Manage Messages.", ephemeral=True)


class Bot(discord.Client):
    def __init__(self, routes, token, dry_run=True, database="data/migrations.sqlite3"):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.routes = routes
        self.dry_run = dry_run
        self.github = GitHubClient(token)
        self.service = MigrationService(self.github, database)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(migrate)

    async def setup_hook(self):
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        self.service.close()
        await self.github.close()
        await super().close()


def main():
    load_dotenv(encoding="utf-8-sig")
    token = os.getenv("DISCORD_TOKEN")
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    github_token = os.getenv("GITHUB_TOKEN", "")
    if not token or (not dry_run and not github_token):
        raise SystemExit("Заполните DISCORD_TOKEN и GITHUB_TOKEN в .env")
    routes = json.loads(Path(os.getenv("ROUTES_FILE", "routes.json")).read_text(encoding="utf-8-sig"))
    for kind in ("bug", "feature"):
        resolve_route(routes, kind)
    Bot(routes, github_token, dry_run, os.getenv("DATABASE_PATH", "data/migrations.sqlite3")).run(token)
