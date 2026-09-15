import pytest

from bot.parser import parse_tracker


def test_bug_tracker():
    tracker = parse_tracker("Тип: баг\nЗаголовок: Кнопка не отвечает\nТеги: payments, urgent\nШаги: открыть checkout")
    assert tracker.kind == "bug"
    assert tracker.title == "Кнопка не отвечает"
    assert tracker.labels == ("bug",)
    assert "### Шаги воспроизведения" in tracker.issue_body()


def test_forum_tags():
    tracker = parse_tracker("Нужен CSV\nОжидаемый результат: экспорт", tags=["Фича"], title="Экспорт")
    assert tracker.kind == "feature"
    assert tracker.title == "Экспорт"
    assert tracker.labels == ("enhancement",)


@pytest.mark.parametrize("text", ["", "Без типа", "Тип: баг\nТеги: фича\nОписание"])
def test_missing_or_conflicting_kind(text):
    with pytest.raises(ValueError):
        parse_tracker(text)


def test_bounded_summary_preserves_source():
    tracker = parse_tracker("Тип: баг\n" + "Details " * 500)
    assert len(tracker.summary) <= 600
    assert "Details " * 100 in tracker.description
