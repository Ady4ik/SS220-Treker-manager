from __future__ import annotations

import json
import os
import re
import discord
from discord import app_commands
from dotenv import load_dotenv

from .github import GitHubClient
from .parser import parse_tracker

load_dotenv()

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.github = GitHubClient(os.environ["GITHUB_TOKEN"])

    async def setup_hook(self):
        await self.tree.sync()

    async def close(self):
        await self.github.close()
        await super().close()

bot = Bot()

def resolve_route(kind, repository, project_number):
    route = json.loads(os.getenv("MIGRATION_ROUTES", "{}")).get(kind, {})
    repo = repository or route.get("repository") or os.environ["GITHUB_DEFAULT_REPOSITORY"]
    labels = route.get("labels", [])
    owner = route.get("project_owner") or os.getenv("GITHUB_PROJECT_OWNER")
    project = project_number or route.get("project_number")
    if project is None and os.getenv("GITHUB_PROJECT_NUMBER"):
        project = int(os.environ["GITHUB_PROJECT_NUMBER"])
    return repo, labels, owner, project

async def read_tracker_source(interaction, source):
    match = re.fullmatch(r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)", source.strip())
    if not match:
        return source
    _, channel_id, message_id = map(int, match.groups())
    channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
    return (await channel.fetch_message(message_id)).content

@bot.tree.command(name="migrate", description="Мигрировать трекер в GitHub Issue")
@app_commands.describe(source="Текст трекера или ссылка на сообщение Discord", repository="owner/repository (необязательно)", project_number="Номер GitHub Project (необязательно)")
async def migrate(interaction, source: str, repository: str | None = None, project_number: int | None = None):
    await interaction.response.defer(ephemeral=True)
    try:
        parsed = parse_tracker(await read_tracker_source(interaction, source))
        repo, route_labels, owner, project = resolve_route(parsed.kind, repository, project_number)
        labels = sorted(set(parsed.labels).union(route_labels))
        if os.getenv("DRY_RUN", "true").lower() == "true":
            await interaction.followup.send(f"Dry-run: `{parsed.kind}` — **{parsed.title}**; repo: `{repo}`; labels: {', '.join(labels)}", ephemeral=True)
            return
        issue = await bot.github.create_issue(repo, parsed.title, parsed.issue_body(), labels)
        if project and owner:
            await bot.github.add_to_project(owner, project, issue["node_id"])
        await interaction.followup.send(f"Готово: [Issue #{issue['number']}]({issue['html_url']})", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"Ошибка миграции: `{exc}`", ephemeral=True)

def main():
    bot.run(os.environ["DISCORD_TOKEN"])
