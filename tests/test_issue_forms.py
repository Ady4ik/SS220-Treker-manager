import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.issue_forms import default_routes, format_source, load_form
from bot.parser import parse_tracker
from bot.service import MigrationService, resolve_route
from bot.sources import Source


def source(kind):
    return Source(
        parse_tracker("Описание: Проверка\nШаги: Открыть окно", kind_override=kind),
        "discord:100", "https://discord.com/channels/1/2/100",
    )


@pytest.mark.parametrize("kind", ["feature", "bug", "mapping"])
def test_form_fields_and_routing(kind):
    result = format_source(source(kind))
    for field in load_form(kind)["body"]:
        if field["type"] != "markdown":
            assert f"### {field['attributes']['label']}" in result.tracker.issue_body()
    route = resolve_route(default_routes(), kind)
    assert route["repository"] == (
        "SerbiaStrong-220/DevTeam220" if kind == "feature" else "SerbiaStrong-220/space-station-14"
    )


def test_feature_labels_and_no_fake_attestation():
    result = format_source(source("feature"))
    assert result.tracker.title.startswith("[F]: ")
    assert set(result.tracker.labels) == {"Features", "V: Medium", "Need head’s discuss"}
    assert "- [ ] Я приложил ссылку" in result.tracker.issue_body()
    small = format_source(source("feature"), volume="Малый")
    assert set(small.tracker.labels) == {"Features", "V: Small"}
    assert default_routes()["feature"]["project_number"] == 30


def test_two_distinct_bugs_and_unknown_map():
    bug = format_source(source("bug"))
    mapping = format_source(source("mapping"))
    assert bug.tracker.labels == ("triage",)
    assert mapping.tracker.labels == ("Mapping Issues",)
    assert "### Выберите игровую карту\n\nДругое" in mapping.tracker.issue_body()
    assert "### Шаги воспроизведения\n\nОткрыть окно" in bug.tracker.issue_body()
    assert "enhancement" not in mapping.tracker.labels
    with pytest.raises(ValueError):
        format_source(source("mapping"), map_name="Invented map")


def test_explicit_selection_overrides_tags_and_auto_remains_strict():
    assert parse_tracker("Тип: баг\nОписание", kind_override="mapping").kind == "mapping"
    assert parse_tracker("Описание", tags=["bug", "Mapping Issues"]).kind == "mapping"
    with pytest.raises(ValueError):
        parse_tracker("Описание")


def test_feature_requires_discord_link():
    item = source("feature")
    item.url = None
    with pytest.raises(ValueError):
        format_source(item)


def test_bug_without_project_still_creates_issue(tmp_path):
    async def scenario():
        github = AsyncMock()
        github.create_issue.return_value = {
            "number": 1, "node_id": "I_1", "html_url": "https://github.com/example/issues/1",
        }
        service = MigrationService(github, str(tmp_path / "state.db"))
        try:
            item = format_source(source("mapping"), map_name="Axioma")
            await service.migrate(item.tracker, default_routes()["mapping"], item.key,
                                  operation="create_issue")
            github.create_issue.assert_awaited_once()
            assert github.create_issue.call_args.args[0] == "SerbiaStrong-220/space-station-14"
            github.project.assert_not_awaited()
            github.add_to_project.assert_not_awaited()
        finally:
            service.close()
    asyncio.run(scenario())
