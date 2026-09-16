from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv

from .github import GitHubClient
from .issue_forms import default_routes
from .jobs import Jobs, safe_error, status_text
from .responses import acknowledge, reply
from .service import MigrationService, resolve_route
from .sources import resolve_source


@app_commands.command(name="migrate", description="Мигрировать форум или пост с полной историей в GitHub")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.choices(report_type=[
    app_commands.Choice(name="По тегам трекера", value="auto"),
    app_commands.Choice(name="Перенести идею из SS14-Трекер", value="feature"),
    app_commands.Choice(name="Сообщить об ошибке", value="bug"),
    app_commands.Choice(name="Сообщить о проблеме с игровой картой", value="mapping"),
])
@app_commands.describe(
    source="Ссылка на форум, пост или сообщение; без аргумента — весь форум",
    repository="owner/repository из настроенного маршрута",
    operation="sync_forum обновляет существующие Issue; create_issue создаёт Issue из источника",
    issue_number="При sync_forum дополнительно обновить указанный Issue",
    scope="auto: ссылка на сообщение читает комментарий; post: весь пост; message: один комментарий",
    report_type="Шаблон и маршрут; явный выбор переопределяет теги всех выбранных постов",
    volume="Объём идеи",
    map_name="Карта для проблемы маппинга",
    needs_discussion="Идея требует обсуждения руководителем",
)
async def migrate(interaction: discord.Interaction, source: str | None = None,
                  repository: str | None = None,
                  operation: Literal["sync_forum", "create_issue"] = "sync_forum",
                  issue_number: int | None = None,
                  scope: Literal["auto", "post", "message"] = "auto",
                  report_type: str = "auto",
                  volume: Literal["Малый", "Средний", "Большой"] = "Средний",
                  map_name: Literal["Frankenstein", "Axioma", "Donuts", "Eclipse", "Astro",
                                    "Nightshift", "Tox", "Другое"] = "Другое",
                  needs_discussion: bool = False):
    if not await acknowledge(interaction):
        return
    bot = interaction.client
    try:
        default_forum = os.getenv("DISCORD_FORUM_CHANNEL_ID")
        target, message_id = await resolve_source(
            interaction, source, int(default_forum) if default_forum else None,
        )
        if operation not in {"sync_forum", "create_issue"}:
            raise ValueError("operation должен быть sync_forum или create_issue")
        job_id = bot.jobs.start(interaction, target, message_id, repository, operation, issue_number, scope,
                               report_type, volume, map_name, needs_discussion)
        logging.getLogger(__name__).info("Migration job started: %s", job_id)
        await reply(interaction,
            f"Задание `{job_id}` запущено: `{operation}`, scope:`{scope}`.\n"
            + ("Создание новых Issue, даже для ранее перенесённых источников.\n"
               if operation == "create_issue" else "Только обновление связанных Issue; новые не создаются.\n")
            +
            f"Режим: {'dry-run (без записи в GitHub)' if bot.dry_run else 'запись в GitHub'}.\n"
            f"Проверить: `/migration-status job_id:{job_id}`",
        )
    except Exception as exc:  # noqa: BLE001 - command boundary must report every failure safely.
        await reply(interaction, safe_error(exc))


@app_commands.command(name="migration-status", description="Прогресс и отчёт миграции форума")
@app_commands.guild_only()
@app_commands.describe(job_id="ID задания из ответа /migrate", report_file="Скачать полный JSON-отчёт")
async def migration_status(interaction: discord.Interaction, job_id: str, report_file: bool = False):
    if not await acknowledge(interaction):
        return
    try:
        report = interaction.client.jobs.load(job_id, interaction.guild_id, interaction.user.id)
        kwargs = {}
        if report_file:
            payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
            if len(payload) <= interaction.guild.filesize_limit:
                kwargs["file"] = discord.File(io.BytesIO(payload), filename=f"{job_id}.json")
            else:
                await reply(interaction,
                    "Отчёт превышает лимит вложения Discord; он сохранён в каталоге заданий на хосте бота.",
                )
        await reply(interaction, status_text(report)[:1900], **kwargs)
    except ValueError as exc:
        await reply(interaction, str(exc))


@app_commands.command(name="forum-sync", description="Синхронизировать только уже связанные Issue форума")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(source="Ссылка на форум; без аргумента — DISCORD_FORUM_CHANNEL_ID")
async def forum_sync(interaction: discord.Interaction, source: str | None = None):
    await migrate.callback(interaction, source, None, "sync_forum", None)


@app_commands.command(name="issue-from-source", description="Создать новый Issue из поста или сообщения")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(source="Ссылка на пост или сообщение Discord")
async def issue_from_source(interaction: discord.Interaction, source: str):
    await migrate.callback(interaction, source, None, "create_issue", None)


@migrate.error
@forum_sync.error
@issue_from_source.error
@migration_status.error
async def migrate_error(interaction, error):
    original = getattr(error, "original", error)
    if isinstance(original, (discord.HTTPException, discord.InteractionResponded, OSError)):
        logging.getLogger(__name__).warning("Command transport failure: %s; no retry", type(original).__name__)
        return
    if isinstance(error, app_commands.CheckFailure):
        await reply(interaction, "Команда доступна только на сервере участникам с правом Manage Messages.")
    else:
        await reply(interaction, safe_error(original))


class Bot(discord.Client):
    def __init__(self, routes, token, dry_run=True, database="data/migrations.sqlite3"):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.routes, self.dry_run = routes, dry_run
        self.github = GitHubClient(token)
        self.service = MigrationService(self.github, database)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(migrate)
        self.tree.add_command(forum_sync)
        self.tree.add_command(issue_from_source)
        self.tree.add_command(migration_status)
        self.jobs = Jobs(self, os.getenv("JOBS_DIRECTORY", "data/jobs"))

    async def setup_hook(self):
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        await self.jobs.close()
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
    routes = default_routes()
    route_path = Path(os.getenv("ROUTES_FILE", "routes.json"))
    if route_path.exists():
        configured = json.loads(route_path.read_text(encoding="utf-8-sig"))
        for kind, route in configured.items():
            routes[kind] = {**routes.get(kind, {}), **route}
    for kind in ("bug", "feature", "mapping"):
        resolve_route(routes, kind)
    Bot(routes, github_token, dry_run, os.getenv("DATABASE_PATH", "data/migrations.sqlite3")).run(token)
