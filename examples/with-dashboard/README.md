# orchX + web dashboard

Web-дашборд показывает list прогонов + live SSE-события.

## Установка

```bash
pip install 'orchx[server]'
```

## Запуск

В одном терминале:

```bash
cd /your/repo
orchx dashboard --port 8421
# открывается http://localhost:8421
```

В другом терминале:

```bash
cd /your/repo
# Подключим dashboard как notifier (события из orchestrator → SSE → браузер):
cat > .orchx/config.yaml << EOF
notifiers: [dashboard]
EOF

orchx all "Add user registration form"
```

В браузере на http://localhost:8421:

- Таблица **Runs** обновляется каждые 10s (REST `/api/runs`).
- Секция **Live events** показывает события от orchestrator в реальном
  времени (через `/api/events` SSE).

## Federation

`orchx dashboard` опционально регистрирует [federation endpoints](../../docs/recipes/)
(`POST /api/runs/spawn`, `GET /api/runs/<id>/status`). Используйте
`ORCHX_FEDERATION_TOKEN` для auth.
