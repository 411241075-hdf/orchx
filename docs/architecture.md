# orchX 0.2 — architecture

> Эта страница описывает архитектуру orchX **версии 0.2.0** (после
> P0/P1/P2 рефакторинга — см. [`recommendations.md`](./recommendations.md)).
> Для сравнительного анализа с OpenHands/Ruflo/AO — [`comparison.md`](./comparison.md).

---

## Big picture

```
┌────────────────────────────────────────────────────────────────┐
│  CLI: orchx plan / run / all / watch / dashboard / plugins     │
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  orchx.orchestrator (package)                                  │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ context.py  · state classes (OrchXConfig, Context...)  │    │
│  │ logging_utils.py · append-only journal                 │    │
│  │ git_utils.py · safe git wrappers                       │    │
│  │ supervisor.py · heartbeat + budget + hung-task detect  │    │
│  │ core.py · phases, retry, merge, review, replan         │    │
│  │   ├─ _invoke_runtime(ctx, ...) ─► RuntimePlugin/local  │    │
│  │   ├─ _accumulate_cost(...)     ─► P1.3 cost tracker    │    │
│  │   ├─ _record_run_to_memory(...) ─► P0.3/P2.4 memory    │    │
│  │   ├─ _maybe_spawn_followup_fixups(...) ─► P1.8 auto-fix│    │
│  │   └─ _CompoundNotifier(...) ─► P1.5 fan-out events     │    │
│  └────────────────────────────────────────────────────────┘    │
└────────────────┬──────────────────────────────────┬────────────┘
                 │                                  │
                 ▼                                  ▼
┌─────────────────────────────────┐   ┌─────────────────────────────┐
│ orchx.plugins (P0.2)            │   │ orchx.agent.worker          │
│   runtime: local / docker       │   │   ├ tools/fs/search/shell   │
│   tracker: github               │   │   ├ tools/symbols (P1.6)    │
│   scm:     github               │   │   ├ tools/browser (P1.7)    │
│   notifier: noop/slack/discord/ │   │   └ tools/mcp (P1.1)        │
│             webhook/dashboard   │   └─────────────────────────────┘
│   memory:  noop / sqlite-FTS    │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│ orchx.web (P1.4 + P2.3)         │
│   ├ server.py  FastAPI + SSE    │
│   ├ federation.py  REST API     │
│   └ static/  vanilla HTMX UI    │
└─────────────────────────────────┘
```

---

## Core packages

| Path                      | Что                                                              | Pluggable?             |
| ------------------------- | ---------------------------------------------------------------- | ---------------------- |
| `src/orchx/orchestrator/` | Phased main loop, retry, merge, review, replan, supervisor, cost | —                      |
| `src/orchx/plugins/`      | Plugin-slot system: runtime/tracker/scm/notifier/memory          | Да — extras            |
| `src/orchx/agent/`        | LLM client, system prompts, tool implementations, permissions    | tools — да             |
| `src/orchx/agent/tools/`  | Read/Edit/Write/Bash/Search/Browser/MCP/Symbols/Task/Web         | Регистр статичный      |
| `src/orchx/web/`          | Optional dashboard + federation REST                             | Опц. extras `[server]` |
| `src/orchx/pr_watcher.py` | CI/review polling + reactions                                    | —                      |
| `src/orchx/cost.py`       | Per-model price table + estimator                                | —                      |

---

## Plugin slots (P0.2)

| Slot       | Контракт                                               | Default                      | Альтернативы                          |
| ---------- | ------------------------------------------------------ | ---------------------------- | ------------------------------------- |
| `runtime`  | `RuntimePlugin.spawn_worker(...)`                      | `local` (asyncio + worktree) | `docker` (P1.2)                       |
| `tracker`  | `TrackerPlugin.fetch_task_description / update_status` | `github` (gh CLI)            | linear/jira/gitlab → user             |
| `scm`      | `SCMPlugin.push_branch / open_pr / get_pr_status`      | `github`                     | gitlab/bitbucket → user               |
| `notifier` | `NotifierPlugin.notify(event, payload)`                | `noop`                       | slack, discord, webhook, dashboard    |
| `memory`   | `MemoryPlugin.remember / recall / forget_old`          | `noop`                       | `sqlite` (FTS5 + optional embeddings) |

Сторонние пакеты добавляют плагины через
[`importlib.metadata` entry-points](https://packaging.python.org/en/latest/specifications/entry-points/):

```toml
# stranger-pkg/pyproject.toml
[project.entry-points."orchx.runtime"]
podman = "stranger_pkg.runtime:PodmanRuntime"
```

orchX **автоматически** подхватит plugin при следующем `orchx plugins list`.

### Конфигурация плагинов

`.orchx/config.yaml`:

```yaml
runtime: local
tracker: github
scm: github
notifiers: [slack, dashboard]
memory: sqlite

plugin_config:
  slack:
    webhook_url: ${SLACK_WEBHOOK_URL}
  sqlite:
    path: .orchx/memory.db
    embed_endpoint: ${OPENAI_BASE_URL}/embeddings
    embed_model: text-embedding-3-small

reactions:
  ci_failed: { auto: true, action: send-to-debugger, max_retries: 3 }
  changes_requested: { auto: true, action: send-to-implementer }
  approved_and_green: { auto: false, action: notify }
```

---

## Event flow (P1.5)

Каждый significant event в orchestrator'е публикуется через `ctx.notifier.notify(...)`.
Подписаны: внешние notifier'ы (Slack/Discord/Webhook) + web dashboard SSE-канал.

| Event                  | Когда                              | Payload (примерно)                  |
| ---------------------- | ---------------------------------- | ----------------------------------- |
| `run_started`          | в начале `run_orchX`               | task_id, phases, tasks              |
| `phase_completed`      | фаза успешна                       | phase_id, duration                  |
| `phase_failed`         | фаза упала                         | phase_id, reasons                   |
| `replan_triggered`     | вызван orchX-planner для recovery  | replan_count                        |
| `pr_opened`            | gh pr create                       | pr_url, marker                      |
| `cost_alert`           | пересечён порог 50/75/90% бюджета  | threshold_pct, total_usd            |
| `budget_exceeded`      | supervisor abort'нул по cost       | total_usd, budget_usd               |
| `wall_budget_exceeded` | supervisor abort'нул по wall-time  | elapsed_s                           |
| `ci_failed`            | pr_watcher увидел CI failure       | retry, pr_url                       |
| `changes_requested`    | reviewer запросил изменения        | comments_count, pr_url              |
| `approved_and_green`   | PR approved + CI green             | auto_merge                          |
| `auto_fixup_planned`   | reviewer findings → debugger tasks | count, plan_path                    |
| `run_finished`         | orchx done                         | counts, total_cost_usd, halt_reason |

---

## Memory namespaces (P0.3 / P2.4)

| Namespace  | Что хранится                        | Кто пишет                        | Кто читает                  |
| ---------- | ----------------------------------- | -------------------------------- | --------------------------- |
| `plans`    | task_id + summary успешного прогона | orchestrator (run end)           | planner (recall похожих)    |
| `failures` | failed-tasks + reason               | orchestrator (run end)           | planner / debugger          |
| `fixes`    | (резерв) успешный debugger fix      | orchestrator (резерв, v2)        | debugger                    |
| `reviews`  | review findings + verdicts          | orchestrator (если ran reviewer) | reviewer (self-improvement) |

`recall(namespace, query, k)` — semantic vector search через embeddings (если
есть `embed_endpoint` в config) с FTS5 fallback.

---

## Tools (worker)

| Tool                              | Permission                                   | Notes                                         |
| --------------------------------- | -------------------------------------------- | --------------------------------------------- |
| `read`                            | `read`                                       | Read files                                    |
| `write` / `edit`                  | `edit` (bool или glob-list)                  | Path-gated                                    |
| `glob` / `grep` / `codesearch`    | `glob` / `grep` / `codesearch`               | Read-only                                     |
| `bash`                            | `bash` (prefix allow-list + injection guard) | Prefix-extract + injection-block              |
| `webfetch`                        | `webfetch`                                   | Anti-SSRF                                     |
| `task`                            | `task`                                       | Sub-agent spawn                               |
| `todowrite`                       | always                                       | In-memory todos                               |
| **`find_symbol`** (P1.6)          | `lsp`                                        | AST для Python, regex для JS/TS               |
| **`find_references`** (P1.6)      | `lsp`                                        | Word-boundary regex                           |
| **`rename_symbol`** (P1.6)        | `lsp` + per-file edit                        | Python AST only                               |
| **`browser`** (P1.7)              | `browser`                                    | Playwright, sandbox localhost only by default |
| **`<server>__<tool>`** (P1.1 MCP) | — (sandbox в MCP-сервере)                    | Префикс по name MCP-сервера                   |

---

## Безопасность

- **Bash**: prefix-extract (composite `&&`/`;`/`|` блокируется как injection) + per-role allow-list. См. `agent/permissions.py:BashRule`.
- **Edit**: path-gated, sandbox строго внутри cwd worktree. Глобовые правила.
- **WebFetch**: anti-SSRF (private IPs, loopback блокируются).
- **Browser**: by-default `localhost:*` / `127.0.0.1:*` only.
- **Docker runtime** (P1.2): `--network none --cap-drop=ALL --read-only` для repo mount.
- **Federation** (P2.3): Bearer-token auth (`ORCHX_FEDERATION_TOKEN`) с
  constant-time compare.

---

## Deployment modes

### Mode 1 — standalone CLI (default)

```bash
pip install orchx
orchx init
orchx all "Add user auth to backend"
```

In-process asyncio, всё локально, никаких daemon'ов.

### Mode 2 — с dashboard

```bash
pip install 'orchx[server]'
orchx dashboard --port 8421 &
orchx all "..." # события стримятся в dashboard
```

### Mode 3 — с Docker-runtime + memory + MCP + dashboard

```bash
pip install 'orchx[all]'
# Сборка worker-image:
make worker-image
# Config:
cat > .orchx/config.yaml << EOF
runtime: docker
memory: sqlite
notifiers: [slack, dashboard]
plugin_config:
  sqlite:
    path: .orchx/memory.db
  slack:
    webhook_url: \${SLACK_WEBHOOK_URL}
EOF
orchx dashboard &
orchx all "..."
```

### Mode 4 — federation (multi-machine)

На master:

```bash
ORCHX_FEDERATION_TOKEN=secret orchx dashboard --host 0.0.0.0
```

На client:

```bash
curl -X POST http://master.local:8421/api/runs/spawn \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d "$(cat plan.json | jq -c '{plan: .}')"
```

---

## Что **НЕ** входит в 0.2

Полностью выполненный декомпозированный orchestrator (`phases.py`,
`retry.py`, `merge.py`, `review.py` как отдельные модули) — был
сознательно не сделан, потому что **требует e2e-теста на reviewer
pipeline** (которого пока нет — нет mock LLM для multi-pass reviewer).

Что уже разделено: `context.py`, `logging_utils.py`, `git_utils.py`,
`supervisor.py`. core.py остался ~2400 строк — это компромисс между
безопасностью и качеством.

Будущая нарезка — отдельный PR после написания e2e теста.

---

## Тестирование

```bash
make test            # все 254+ тестов
make test-unit       # только unit
make test-integration # FakeLLMClient end-to-end
make test-cov        # coverage report
make check           # lint + typecheck + test
```

Fake LLM: `src/orchx/tests/fixtures/mock_llm.py:FakeLLMClient`.
Integration: `src/orchx/tests/integration/test_agent_with_fake_llm.py`.
