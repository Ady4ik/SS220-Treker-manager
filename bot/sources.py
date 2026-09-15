"""On-demand forum snapshots. No background channel monitoring."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime

import discord

from .parser import Tracker, parse_tracker

CHANNEL_LINK = re.compile(
    r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?/?"
)


@dataclass
class Source:
    tracker: Tracker
    key: str
    url: str | None
    message_count: int = 1
    forum_id: int | None = None
    forum_url: str | None = None
    thread_id: int | None = None
    thread_url: str | None = None
    message_id: int | None = None
    message_url: str | None = None
    guild_id: int | None = None
    scope: str = "post"

    def metadata(self):
        return {
            "version": 1, "source_key": self.key, "scope": self.scope,
            "guild_id": self.guild_id, "forum_id": self.forum_id, "forum_url": self.forum_url,
            "thread_id": self.thread_id, "thread_url": self.thread_url,
            "message_id": self.message_id, "message_url": self.message_url,
        }


async def forum_origin(thread):
    parent = thread.parent
    if parent is None:
        parent = await thread.guild.fetch_channel(thread.parent_id)
    if isinstance(parent, discord.ForumChannel):
        return parent.id, f"https://discord.com/channels/{thread.guild.id}/{parent.id}"
    return None, None


async def check_access(channel, member):
    if channel.guild.id != member.guild.id:
        raise ValueError("Источник должен находиться на этом сервере")
    for actor in (member, channel.guild.me):
        if actor is None:
            raise ValueError("Не удалось проверить права бота")
        permissions = channel.permissions_for(actor)
        if not permissions.view_channel or not permissions.read_message_history:
            raise ValueError("Нет прав чтения канала у пользователя или бота")
        if isinstance(channel, discord.Thread) and channel.is_private() and not permissions.manage_threads:
            try:
                await channel.fetch_member(actor.id)
            except discord.NotFound as exc:
                raise ValueError("Пользователь или бот не состоит в приватном треде") from exc


async def resolve_source(interaction, source=None, default_forum_id=None):
    """Return a channel + optional message ID, or literal tracker text."""
    if source:
        match = CHANNEL_LINK.fullmatch(source.strip())
        if not match:
            if source.strip().startswith(("https://", "http://")):
                raise ValueError("Нужна ссылка на форум, пост или сообщение Discord")
            return source, None
        guild_id, channel_id = int(match[1]), int(match[2])
        if guild_id != interaction.guild_id:
            raise ValueError("Источник должен находиться на этом сервере")
        channel = await interaction.guild.fetch_channel(channel_id)
        message_id = int(match[3]) if match[3] else None
    elif default_forum_id:
        channel = await interaction.guild.fetch_channel(default_forum_id)
        message_id = None
        if not isinstance(channel, discord.ForumChannel):
            raise ValueError("FORUM_CHANNEL_ID должен указывать на Discord Forum")
    else:
        channel, message_id = interaction.channel, None
        # /migrate inside a forum post defaults to the entire parent forum.
        if isinstance(channel, discord.Thread):
            parent = await interaction.guild.fetch_channel(channel.parent_id)
            if isinstance(parent, discord.ForumChannel):
                channel = parent
    if not isinstance(channel, (discord.ForumChannel, discord.Thread, discord.TextChannel)):
        raise TypeError("Укажите ссылку на форум или пост")
    if isinstance(channel, discord.TextChannel) and message_id is None:
        raise ValueError("Укажите форум или ссылку на конкретное сообщение текстового канала")
    await check_access(channel, interaction.user)
    return channel, message_id


async def forum_threads(forum):
    """Fetch active threads from API (not cache) and paginate the complete archive."""
    seen = set()
    for thread in await forum.guild.active_threads():
        if thread.parent_id == forum.id and thread.id not in seen:
            seen.add(thread.id)
            yield thread
    async for thread in forum.archived_threads(limit=None):
        if thread.parent_id == forum.id and thread.id not in seen:
            seen.add(thread.id)
            yield thread


def message_text(message):
    parts = [message.content] if message.content else []
    for embed in message.embeds:
        parts.extend(value for value in (embed.title, embed.description, embed.url) if value)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        for media in (embed.image, embed.thumbnail):
            if media.url:
                parts.append(media.url)
        if embed.footer.text:
            parts.append(embed.footer.text)
    parts.extend(f"Вложение: {a.filename}\n{a.url}" for a in message.attachments)
    parts.extend(f"Стикер: {sticker.name}\n{sticker.url}" for sticker in message.stickers)
    return "\n\n".join(parts)


async def read_thread(thread, member, before: datetime):
    await check_access(thread, member)
    transcript, starter, excerpts = [], None, []
    count, has_content = 0, False
    # Discord.py paginates history; no 100-message cap, no cache-only reads.
    async for message in thread.history(limit=None, oldest_first=True, before=before):
        count += 1
        text = message_text(message)
        has_content |= bool(text.strip())
        if message.id == thread.id:
            starter = text
        elif text and len(excerpts) < 3:
            excerpts.append(re.sub(r"\s+", " ", text)[:120])
        author = discord.utils.escape_markdown(message.author.display_name)
        transcript.append(
            f"### {message.created_at.isoformat()} — {author} ({message.author.id})\n"
            f"{message.jump_url}\n\n{text or '[Сообщение без текстового содержимого]'}"
        )
    if not count or not has_content:
        raise ValueError("История пуста или недоступна: проверьте Message Content Intent")
    # Replies must never change classification/title by containing 'Тип:' or 'Заголовок:'.
    parsed = parse_tracker(
        starter or "Исходное сообщение отсутствует; см. полную историю ниже.",
        tags=[tag.name for tag in thread.applied_tags], title=thread.name,
    )
    summary = parsed.summary
    if excerpts:
        summary = f"{summary[:180]}\n\nИз обсуждения: " + " / ".join(excerpts)
    parsed = replace(
        parsed, title=thread.name[:256], summary=summary[:600],
        description=parsed.description + f"\n\n## Полная история ({count} сообщений)\n\n"
        + "\n\n---\n\n".join(transcript),
    )
    # Forum starter IDs equal thread IDs: retain compatibility with old migration keys.
    forum_id, forum_url = await forum_origin(thread)
    return Source(
        parsed, f"discord:{thread.id}", thread.jump_url, count,
        forum_id, forum_url, thread.id, thread.jump_url, thread.id,
        f"https://discord.com/channels/{thread.guild.id}/{thread.id}/{thread.id}",
        thread.guild.id, "post",
    )


async def read_single(channel_or_text, message_id, member, before, *, scope="post"):
    if scope not in {"post", "message"}:
        raise ValueError("scope должен быть post или message")
    if isinstance(channel_or_text, str):
        if scope == "message":
            raise ValueError("Для scope:message нужна ссылка на конкретное сообщение")
        key = f"text:{member.guild.id}:" + hashlib.sha256(channel_or_text.encode()).hexdigest()
        return Source(parse_tracker(channel_or_text), key, None, guild_id=member.guild.id, scope="text")
    if isinstance(channel_or_text, discord.Thread) and scope == "post":
        return await read_thread(channel_or_text, member, before)
    if not message_id:
        raise ValueError("Для scope:message нужна ссылка на конкретное сообщение")
    await check_access(channel_or_text, member)
    message = await channel_or_text.fetch_message(message_id)
    if message.created_at >= before:
        raise ValueError("Сообщение создано после начала задания; запустите новое задание")
    is_thread = isinstance(channel_or_text, discord.Thread)
    forum_id, forum_url = await forum_origin(channel_or_text) if is_thread else (None, None)
    tags = [tag.name for tag in channel_or_text.applied_tags] if is_thread else ()
    parsed = parse_tracker(message_text(message), tags=tags)
    # A selected comment, including the starter, is a different source from the whole post.
    key = f"discord-message:{message.id}" if is_thread else f"discord:{message.id}"
    return Source(
        parsed, key, message.jump_url,
        1, forum_id, forum_url, channel_or_text.id if is_thread else None,
        channel_or_text.jump_url if is_thread else None, message.id, message.jump_url,
        channel_or_text.guild.id, "message",
    )
