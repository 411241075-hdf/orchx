# Recipe — long-term memory (SQLite + FTS5)

> orchX будет помнить предыдущие прогоны (планы, провалы, ревью). На
> похожих задачах planner/debugger смогут опираться на исторический
> контекст.

> **С 0.2.1 memory: sqlite включён по умолчанию.** Если просто запустить
> `orchx init` — `.orchx/memory.db` создастся автоматически после
> первого `orchx run`. Этот recipe нужен только если хочется поменять
> путь, отключить, или добавить embeddings.

## 1. Базовая настройка (только FTS, без embeddings)

Не нужно ничего делать — это **дефолт** с 0.2.1. Если хотите кастомный
путь, в `.orchx/config.yaml`:

```yaml
memory: sqlite

plugin_config:
  sqlite:
    path: .orchx/memory.db
```

Чтобы **выключить**:

```yaml
memory: noop
```

После каждого прогона orchX автоматически пишет:

- `plans/<task_id>` — успешный план + counts + wall_seconds.
- `failures/<task_id>` — если есть failed tasks.
- `reviews/<task_id>` — review findings + verifier verdicts.

Использование:

```python
import asyncio
from orchx.plugins import load_plugin

async def main():
    m = load_plugin("memory", "sqlite", config={"path": ".orchx/memory.db"})
    results = await m.recall("plans", "authentication module")
    for r in results:
        print(r["key"], "->", r["value"].get("summary"))

asyncio.run(main())
```

FTS5 — токенайзер: word-level, case-insensitive. Хорошо ищет по
keyword'ам; не идеально по семантике («OAuth» != «authentication»).

## 2. Включить embeddings (semantic search)

Нужно ещё:

```bash
pip install 'orchx[memory-embed]'
```

`.orchx/config.yaml`:

```yaml
memory: sqlite

plugin_config:
  sqlite:
    path: .orchx/memory.db
    embed_endpoint: https://api.openai.com/v1/embeddings
    embed_model: text-embedding-3-small
    embed_api_key: ${OPENAI_API_KEY}
```

Теперь `recall(query)` сначала пытается semantic search через cosine-
similarity (если у memory есть embeddings для записей), и только если
ничего — fallback на FTS5.

> **Cost note**: embedding одной короткой записи (~500 токенов)
> через `text-embedding-3-small` стоит ~$0.00001. На 1000 прогонов —
> $0.01. Безопасно даже для бюджетных пользователей.

## 3. Garbage collection

```python
deleted = await m.forget_old(days=90)
print(f"forgot {deleted} old records")
```

Удаляет записи, у которых нет `last_used_at` за 90+ дней. Раз в месяц
по cron'у — не разрастётся БД.

## Что **планируется** (P3+)

- Автоподмешивание recall в planner/debugger prompt'ы (сейчас orchX
  только пишет; явный recall — вручную через API).
- Reinforcement signal: если прогон провалился — снизить score
  исторического pattern'а.
- Pattern templates: planner может «достать» полный plan для похожей
  задачи и адаптировать.
