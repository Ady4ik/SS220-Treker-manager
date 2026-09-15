from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

from .provenance import check_size, marker, merge_section, render_section, same_forum, source_hash


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
        super().__init__("Issue существует, но миграция не завершена. Повторите с operation:sync_forum.")


class MigrationService:
    """Single process worker with durable checkpoints and Issue recovery markers."""
    def __init__(self, github, path="data/migrations.sqlite3"):
        self.github = github
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS migrations (key TEXT PRIMARY KEY, issue TEXT NOT NULL)")
        # Keep old checkpoints, plus readable provenance. Repository is part of the key.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS source_bindings "
            "(key TEXT PRIMARY KEY, repository TEXT NOT NULL, issue_number INTEGER NOT NULL, "
            "metadata TEXT NOT NULL)"
        )
        self.db.commit()
        self.lock = asyncio.Lock()

    def close(self):
        self.db.close()

    async def migrate(self, tracker, route, source_key, source_url=None, *, operation="sync_forum",
                      source_metadata=None, issue_number=None):
        if operation not in {"sync_forum", "create_issue"}:
            raise ValueError("operation должен быть sync_forum или create_issue")
        if issue_number is not None and (
            operation != "sync_forum" or type(issue_number) is not int or issue_number <= 0
        ):
            raise ValueError("Положительный issue_number доступен только для sync_forum")
        repo = route["repository"]
        key = source_hash(repo, source_key)
        hidden_marker = marker(key)
        async with self.lock:
            labels = sorted(set(tracker.labels + tuple(route.get("labels", []))))
            issue = None
            if operation == "sync_forum":
                row = self.db.execute("SELECT issue FROM migrations WHERE key=?", (key,)).fetchone()
                checkpoint = json.loads(row[0]) if row else None
                if issue_number is not None:
                    if checkpoint and checkpoint["number"] != issue_number:
                        raise ValueError("Источник уже связан с другим Issue; автоматическая перепривязка запрещена")
                    issue = await self.github.get_issue(repo, issue_number)
                    if hidden_marker not in (issue.get("body") or "") and not same_forum(
                        issue.get("body") or "", source_metadata,
                    ):
                        raise ValueError("Указанный Issue не связан с этим Discord-форумом")
                elif checkpoint:
                    issue = await self.github.get_issue(repo, checkpoint["number"])
                    if hidden_marker not in (issue.get("body") or ""):
                        raise ValueError("В связанном Issue удалён маркер источника; синхронизация остановлена")
                else:
                    issue = await self.github.find_issue(repo, hidden_marker)
                    if issue:
                        issue = await self.github.get_issue(repo, issue["number"])
                        if hidden_marker not in (issue.get("body") or ""):
                            raise ValueError("Маркер источника изменился; синхронизация остановлена")
                if issue is None:
                    return None, "skipped"
            # `create_issue` intentionally skips lookup, but keeps the latest canonical
            # binding so a later `sync_forum` can update the chosen existing Issue.

            section = render_section(tracker, key, source_url, source_metadata)
            current_body = (issue.get("body") or "") if issue else ""
            body = merge_section(current_body, section, key) if operation == "sync_forum" else section
            if not issue or operation == "sync_forum":
                check_size(body)
            project = await self.github.project(route)
            if issue is None:
                await self.github.ensure_labels(repo, labels)
                issue = await self.github.create_issue(repo, tracker.title, body, labels)
                action = "created"
            elif operation == "sync_forum":
                try:
                    if body != current_body:
                        issue = await self.github.update_issue_body(repo, issue["number"], body)
                except Exception as exc:
                    raise PartialMigration(issue["html_url"]) from exc
                action = "updated" if body != current_body else "unchanged"
            serialized = json.dumps(issue)
            # Store the canonical source binding for both modes. `create_issue` still
            # skips lookup on its own invocation, while a later sync can find this Issue.
            self.db.execute("INSERT OR REPLACE INTO migrations VALUES (?,?)", (key, serialized))
            if source_metadata:
                self.db.execute(
                    "INSERT OR REPLACE INTO source_bindings VALUES (?,?,?,?)",
                    (key, repo, issue["number"], json.dumps(source_metadata)),
                )
            self.db.commit()
            try:
                await self.github.add_to_project(project, issue["node_id"])
            except Exception as exc:
                raise PartialMigration(issue["html_url"]) from exc
            return issue["html_url"], action
