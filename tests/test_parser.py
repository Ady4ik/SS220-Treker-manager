from bot.parser import parse_tracker

def test_bug_tracker():
    tracker = parse_tracker("Тип: баг\nЗаголовок: Кнопка не отвечает\nТеги: payments, urgent\nШаги: открыть checkout")
    assert tracker.kind == "bug"
    assert tracker.title == "Кнопка не отвечает"
    assert tracker.labels == ("bug", "payments", "urgent")
    assert "Тип:" not in tracker.description

def test_default_feature():
    tracker = parse_tracker("Добавить экспорт отчета\nНужен CSV")
    assert tracker.kind == "feature"
    assert tracker.labels == ("enhancement",)
