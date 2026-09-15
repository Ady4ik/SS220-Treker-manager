"""Versioned provenance and bot-owned Issue sections, independent of Discord objects."""
from __future__ import annotations

import hashlib
import json
import re

SOURCE_PATTERN = re.compile(r"<!-- ss220-source:(\{[^\n]*\}) -->")


def source_hash(repository, source_key):
    return hashlib.sha256(f"{repository}:{source_key}".encode()).hexdigest()


def marker(key):
    return f"<!-- ss220-tracker:{key} -->"


def render_metadata(metadata):
    lines = ["## Источник Discord", "", f"- Область: `{metadata['scope']}`"]
    for name, label in (("guild", "Сервер"), ("forum", "Форум"), ("thread", "Пост"), ("message", "Сообщение")):
        value = metadata.get(f"{name}_id")
        if value is not None:
            lines.append(f"- {label} ID: `{value}`")
        if metadata.get(f"{name}_url"):
            lines.append(f"- {label}: {metadata[f'{name}_url']}")
    return "\n".join(lines)


def render_section(tracker, key, source_url=None, metadata=None):
    # Untrusted source text must not inject our control markers.
    text = tracker.issue_body().replace("<!-- ss220-", "&lt;!-- ss220-")
    if metadata:
        text += "\n\n" + render_metadata(metadata)
        text += "\n\n<!-- ss220-source:" + json.dumps(metadata, sort_keys=True, ensure_ascii=True) + " -->"
    elif source_url:
        text += f"\n\n## Источник\n\n{source_url}"
    return f"<!-- ss220-begin:{key} -->\n{text}\n{marker(key)}\n<!-- ss220-end:{key} -->"


def merge_section(current, section, key):
    """Replace only this source's owned block; never wipe other blocks or manual notes."""
    begin, end = f"<!-- ss220-begin:{key} -->", f"<!-- ss220-end:{key} -->"
    if begin in current or end in current:
        if current.count(begin) != 1 or current.count(end) != 1 or current.index(begin) >= current.index(end):
            raise ValueError("Повреждён служебный блок Issue; исправьте границы перед синхронизацией")
        start, stop = current.index(begin), current.index(end) + len(end)
        return current[:start] + section + current[stop:]
    # Old Issues have no owned boundaries. Keep their body on first upgrade.
    return (current.rstrip() + "\n\n" + section).lstrip() if current else section


def same_forum(body, metadata):
    if not metadata or not metadata.get("forum_id"):
        return False
    for match in SOURCE_PATTERN.finditer(body):
        try:
            stored = json.loads(match[1])
        except ValueError:
            continue
        if isinstance(stored, dict) and all(
            stored.get(key) == metadata.get(key) for key in ("guild_id", "forum_id")
        ):
            return True
    return False


def check_size(body):
    if len(body) > 60000:
        raise ValueError("История слишком большая для одного Issue (60 000 символов); данные не обрезаны")
