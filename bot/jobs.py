"""Persistent progress for jobs that can outlive a slash interaction token."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import discord
import httpx

from .github import GitHubError
from .service import PartialMigration, resolve_route
from .sources import forum_threads, read_single


def safe_error(error):
    if isinstance(error, (ValueError, GitHubError, PartialMigration)):
        return str(error)[:500]
    return f"Ошибка {type(error).__name__}; проверьте доступ/API и повторите команду"


class Jobs:
    def __init__(self, bot, directory):
        self.bot = bot
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.tasks = {}
        self.guild_jobs = {}
        # No silent restart using stale permissions; a user must explicitly run again.
        for path in self.directory.glob("*.json"):
            report = json.loads(path.read_text(encoding="utf-8"))
            if report["state"] == "running":
                report["state"] = "interrupted"
                self.save(report)

    def save(self, report):
        path = self.directory / f"{report['id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def load(self, job_id, guild_id, user_id):
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Неверный ID задания")
        path = self.directory / f"{job_id}.json"
        if not path.exists():
            raise ValueError("Задание не найдено")
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["guild_id"] != guild_id or report["user_id"] != user_id:
            raise ValueError("Отчёт доступен только инициатору на том же сервере")
        return report

    def start(self, interaction, target, message_id, repository):
        guild_id = interaction.guild_id
        if guild_id in self.guild_jobs:
            raise ValueError(f"На сервере уже выполняется задание {self.guild_jobs[guild_id]}")
        job_id = uuid4().hex
        report = {
            "id": job_id, "guild_id": guild_id, "user_id": interaction.user.id,
            "state": "running", "dry_run": self.bot.dry_run,
            "started_at": datetime.now(UTC).isoformat(), "results": [],
        }
        self.save(report)
        self.guild_jobs[guild_id] = job_id
        self.tasks[job_id] = asyncio.create_task(
            self.run(report, interaction.guild, target, message_id, repository)
        )
        return job_id

    async def run(self, report, guild, target, message_id, repository):
        try:
            before = datetime.fromisoformat(report["started_at"])
            if isinstance(target, discord.ForumChannel):
                async for thread in forum_threads(target):
                    if thread.created_at >= before:
                        continue
                    await self.process(report, guild, thread, None, repository, before)
            else:
                await self.process(report, guild, target, message_id, repository, before)
            report["state"] = (
                "completed_with_errors" if any(r["state"] in {"failed", "partial"} for r in report["results"])
                else "completed"
            )
        except asyncio.CancelledError:
            report["state"] = "interrupted"
            raise
        except Exception as error:  # noqa: BLE001 - a failed job must be persisted, not crash the worker.
            report["state"] = "failed"
            report["error"] = safe_error(error)
        finally:
            self.save(report)
            self.guild_jobs.pop(report["guild_id"], None)
            self.tasks.pop(report["id"], None)

    async def process(self, report, guild, target, message_id, repository, before):
        result = {"source_id": getattr(target, "id", None)}
        try:
            # Refresh member roles for each post during a long-running scan.
            member = await guild.fetch_member(report["user_id"])
            if not isinstance(target, str) and not target.permissions_for(member).manage_messages:
                raise ValueError("У инициатора больше нет Manage Messages в исходном канале")
            source = await read_single(target, message_id, member, before)
            route = resolve_route(self.bot.routes, source.tracker.kind, repository)
            result.update(
                title=source.tracker.title, messages=source.message_count, repository=route["repository"],
                source=source.url, summary=source.tracker.summary,
                labels=sorted(set(source.tracker.labels + tuple(route.get("labels", [])))),
                project_number=route["project_number"],
            )
            # Same full-body size check as service, including source URL and marker.
            overhead = len(f"\n\n<!-- ss220-tracker:{'0' * 64} -->")
            if source.url:
                overhead += len(f"\n\n## Источник\n\n{source.url}")
            if len(source.tracker.issue_body()) + overhead > 60000:
                raise ValueError("История слишком большая для одного Issue; разделите пост перед миграцией")
            if self.bot.dry_run:
                result["state"] = "dry_run"
            else:
                url, reused = await self.bot.service.migrate(
                    source.tracker, route, source.key, source.url,
                    update_existing=isinstance(target, discord.Thread),
                )
                result.update(state="reused" if reused else "created", issue=url)
        except PartialMigration as error:
            result.update(state="partial", error=safe_error(error), issue=error.url)
        except (ValueError, GitHubError, discord.HTTPException, httpx.HTTPError) as error:
            result.update(state="failed", error=safe_error(error))
        except Exception as error:  # noqa: BLE001 - isolate one malformed post from the rest of the forum.
            result.update(state="failed", error=safe_error(error))
        report["results"].append(result)
        self.save(report)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def status_text(report):
    counts = Counter(result["state"] for result in report["results"])
    messages = sum(result.get("messages", 0) for result in report["results"])
    return (
        f"Задание `{report['id']}`: **{report['state']}**\n"
        f"Постов обработано: {len(report['results'])}; сообщений прочитано: {messages}\n"
        + ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
        + (f"\n{report['error']}" if report.get("error") else "")
    )
