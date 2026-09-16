from __future__ import annotations

import re
from dataclasses import dataclass

ALIASES = {"bug": "bug", "баг": "bug", "feature": "feature", "фича": "feature", "enhancement": "feature"}
ALIASES.update({"mapping": "mapping", "mapping issues": "mapping", "проблема карты": "mapping"})
TITLE = re.compile(r"^(?:заголовок|title|тема)\s*:\s*(.+)$", re.IGNORECASE)
META = re.compile(r"^(тип|type|теги|tags?|labels?)\s*:\s*(.+)$", re.IGNORECASE)
SECTIONS = {
    "описание": "Описание", "description": "Описание",
    "шаги": "Шаги воспроизведения", "шаги воспроизведения": "Шаги воспроизведения",
    "steps": "Шаги воспроизведения", "ожидаемый результат": "Ожидаемый результат",
    "expected": "Ожидаемый результат", "фактический результат": "Фактический результат",
    "actual": "Фактический результат", "окружение": "Окружение", "environment": "Окружение",
}


@dataclass(frozen=True)
class Tracker:
    kind: str
    title: str
    description: str
    summary: str
    labels: tuple[str, ...]
    formatted_body: str | None = None

    def issue_body(self):
        if self.formatted_body is not None:
            return self.formatted_body
        return (f"## Summary\n\n{self.summary}\n\n**Тип:** `{self.kind}`"
                f"\n\n## Основные элементы\n\n{self.description}")


def parse_tracker(text: str, tags=(), title=None, kind_override=None) -> Tracker:
    """Extract a short summary and sections without inventing tracker information."""
    if not text.strip():
        raise ValueError("Трекер пустой или Message Content Intent не включён")
    all_tags = {tag.strip().casefold() for tag in tags}
    content = []
    for line in text.splitlines():
        cleaned = re.sub(r"^[#\s]+", "", line).replace("**", "")
        if match := TITLE.match(cleaned):
            title = match[1].strip()
        elif match := META.match(cleaned):
            all_tags.update(t.strip().casefold() for t in re.split(r"[,;]", match[2]))
        else:
            content.append(line)
    kinds = {ALIASES[t] for t in all_tags if t in ALIASES}
    if kind_override:
        if kind_override not in {"bug", "feature", "mapping"}:
            raise ValueError("Неизвестный тип шаблона")
        kinds = {kind_override}
    elif kinds == {"mapping", "bug"}:
        kinds = {"mapping"}
    if len(kinds) != 1:
        raise ValueError("Нужен ровно один тег типа: баг/bug или фича/feature")
    kind = kinds.pop()
    raw = "\n".join(content).strip()
    if not title:
        title = next((line.strip() for line in content if line.strip()), "Трекер")[:120]
    if len(title) > 256:
        title = title[:253] + "..."
    highlights = []
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        heading = SECTIONS.get(key.strip().strip("#* ").casefold()) if sep else None
        highlights.append(f"\n### {heading}\n\n{value.strip()}" if heading else line)
    summary = re.sub(r"\s+", " ", raw) or title
    if len(summary) > 600:
        summary = summary[:597].rsplit(" ", 1)[0] + "..."
    label = {"bug": "bug", "feature": "enhancement", "mapping": "Mapping Issues"}[kind]
    return Tracker(kind, title, "\n".join(highlights).strip() or title, summary, (label,))
