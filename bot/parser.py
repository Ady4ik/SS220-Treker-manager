from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Tracker:
    kind: str
    title: str
    description: str
    labels: tuple[str, ...] = ()

    def issue_body(self) -> str:
        details = self.description or "_Описание не указано._"
        return f"## Summary\n\n**Type:** `{self.kind}`\n\n## Tracker\n\n{details}"


_KIND = re.compile(r"(?:тип|type)\s*:\s*(bug|баг|feature|фича)\b", re.I)
_TITLE = re.compile(r"(?:заголовок|title|тема)\s*:\s*(.+)", re.I)
_LABELS = re.compile(r"(?:теги|labels?)\s*:\s*(.+)", re.I)


def parse_tracker(text: str) -> Tracker:
    """Parse a tracker while retaining unknown content in the description."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Трекер пустой")
    kind_match = _KIND.search(text)
    raw_kind = kind_match.group(1).lower() if kind_match else "feature"
    kind = "bug" if raw_kind in {"bug", "баг"} else "feature"
    title_match = next((m for line in lines if (m := _TITLE.match(line))), None)
    title = title_match.group(1).strip() if title_match else lines[0][:120]
    labels = {"bug" if kind == "bug" else "enhancement"}
    labels_match = _LABELS.search(text)
    if labels_match:
        labels.update(x.strip().lower() for x in labels_match.group(1).split(",") if x.strip())
    description = "\n".join(line for line in lines if not _TITLE.match(line) and not _KIND.match(line) and not _LABELS.match(line))
    return Tracker(kind, title, description, tuple(sorted(labels)))

