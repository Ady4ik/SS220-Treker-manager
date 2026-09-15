from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from pathlib import Path


def resolve_route(routes, kind, repository=None):
    route = dict(routes.get(kind, {}))
    if not route:
        raise ValueError(f"Не настроен маршрут для {kind}")
    repo = repository or route.get("repository", "")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Репозиторий должен иметь формат owner/repository")
    if repo != route.get("repository"):
        raise ValueError("Выбранный репозиторий не соответствует маршруту типа трекера")
    route["repository"] = repo
    labels = route.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(x, str) or not 0 < len(x) <= 50 for x in labels):
        raise ValueError("labels должен быть списком имён длиной 1–50 символов")
    number = route.get("project_number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError("Укажите положительный project_number")
    if not route.get("project_owner"):
        raise ValueError("Укажите project_owner")
    if route.get("project_owner_type", "organization") not in {"user", "organization"}:
        raise ValueError("project_owner_type должен быть user или organization")
    return route


class PartialMigration(RuntimeError):
    def __init__(self, url):
        self.url = url
        super().__init__("Issue создан, но Project не обновлён. Повторите команду для завершения.")


class MigrationService:
    """Single process worker with durable checkpoints and Issue recovery markers."""
    def __init__(self, github, path="data/migrations.sqlite3"):
        self.github = github
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS migrations (key TEXT PRIMARY KEY, issue TEXT NOT NULL)")
        self.db.commit()
        self.lock = asyncio.Lock()

    def close(self):
        self.db.close()

    async def migrate(self, tracker, route, source_key, source_url=None):
        repo = route["repository"]
        key = hashlib.sha256(f"{repo}:{source_key}".encode()).hexdigest()
        marker = f"<!-- ss220-tracker:{key} -->"
        async with self.lock:
            project = await self.github.project(route)
            labels = sorted(set(tracker.labels + tuple(route.get("labels", []))))
            row = self.db.execute("SELECT issue FROM migrations WHERE key=?", (key,)).fetchone()
            issue = json.loads(row[0]) if row else await self.github.find_issue(repo, marker)
            reused = issue is not None
            if not issue:
                body = tracker.issue_body()
                if source_url:
                    body += f"\n\n## Источник\n\n{source_url}"
                body += f"\n\n{marker}"
                if len(body) > 60000:
                    raise ValueError("Трекер слишком большой для одного Issue")
                await self.github.ensure_labels(repo, labels)
                issue = await self.github.create_issue(repo, tracker.title, body, labels)
            self.db.execute("INSERT OR REPLACE INTO migrations VALUES (?,?)", (key, json.dumps(issue)))
            self.db.commit()
            try:
                await self.github.add_to_project(project, issue["node_id"])
            except Exception as exc:
                raise PartialMigration(issue["html_url"]) from exc
            return issue["html_url"], reused
