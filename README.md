# SS220 Treker Manager

Discord-бот для миграции трекеров в GitHub Issues и GitHub Projects.

## Возможности

- `/migrate` принимает текст трекера или ссылку на сообщение Discord;
- выделяет тип `bug`/`feature`, заголовок, описание и пользовательские теги;
- создаёт GitHub Issue с labels `bug`/`enhancement`;
- маршрутизирует типы в разные репозитории, labels и GitHub Projects через `MIGRATION_ROUTES`;
- поддерживает `DRY_RUN=true` для проверки без создания Issue.

## Запуск

Скопируйте `.env.example` в `.env`, заполните Discord/GitHub токены и выполните:

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python -m bot
```

Пример: `/migrate source:<текст или ссылка> repository:ss220/server project_number:3`.

Токены хранятся только в `.env` и не коммитятся.
