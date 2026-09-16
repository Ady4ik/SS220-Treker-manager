"""Render the supplied GitHub forms as Markdown; never execute template content."""
import re
from dataclasses import replace
from pathlib import Path

import yaml

FILES = {"feature": "transfer_features.yml", "bug": "bug_report.yml", "mapping": "mapping_issue.yml"}
REPOSITORIES = {
    "feature": "SerbiaStrong-220/DevTeam220",
    "bug": "SerbiaStrong-220/space-station-14",
    "mapping": "SerbiaStrong-220/space-station-14",
}


def load_form(kind):
    path = Path(__file__).resolve().parent.parent / "templates" / FILES[kind]
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def format_source(source, *, volume="Средний", map_name="Другое", needs_discussion=False):
    tracker = source.tracker
    form = load_form(tracker.kind)
    fields = [field for field in form["body"] if field["type"] != "markdown"]
    description = tracker.description
    # Keep full history in the main field; extract only explicit steps, never invent them.
    steps = re.search(r"### Шаги воспроизведения\s*\n(.*?)(?=\n#{2,3} |\Z)", description, re.DOTALL)
    links = re.findall(r"https?://[^\s<>]+", description)
    media = "\n".join(dict.fromkeys(link for link in links if "discord.com/channels/" not in link))
    labels = list(form.get("labels", []))
    title = tracker.title
    if tracker.kind == "feature":
        if not source.url:
            raise ValueError("Шаблон идеи требует ссылку на Discord: передайте пост или сообщение")
        if volume not in fields[1]["attributes"]["options"]:
            raise ValueError("Недопустимый объём задачи")
        labels.append({"Малый": "V: Small", "Средний": "V: Medium", "Большой": "V: Large"}[volume])
        discuss = needs_discussion or volume in {"Средний", "Большой"}
        if discuss:
            labels.append("Need head’s discuss")
        checks = fields[2]["attributes"]["options"]
        # This does not assert human approval or completeness on the user's behalf.
        controls = (f"- [{'x' if discuss else ' '}] {checks[0]['label']}\n"
                    f"- [ ] {checks[1]['label']}\n\n"
                    "Автоматический перенос. Полноту описания должен подтвердить человек.")
        values = [source.url, volume, controls,
                  f"## Summary\n\n{tracker.summary}\n\n{description}"]
        if not title.startswith("[F]:"):
            title = "[F]: " + title
    elif tracker.kind == "mapping":
        if map_name not in fields[0]["attributes"]["options"]:
            raise ValueError("Карта отсутствует в mapping_issue.yml")
        values = [map_name, f"{tracker.summary}\n\n{description}", media or "_Не предоставлено._"]
    else:
        values = [f"{tracker.summary}\n\n{description}",
                  steps[1].strip() if steps else "_Не указаны._", media or "_Не предоставлено._"]
    body = "\n\n".join(f"### {field['attributes']['label']}\n\n{value}"
                       for field, value in zip(fields, values, strict=True))
    source.tracker = replace(tracker, title=title[:256], labels=tuple(labels), formatted_body=body)
    return source


def default_routes():
    return {
        kind: {"repository": repo, "labels": [], "project_owner": "SerbiaStrong-220",
               "project_owner_type": "organization",
               "project_number": 30 if kind == "feature" else None}
        for kind, repo in REPOSITORIES.items()
    }
