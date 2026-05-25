<div align="center">

# OrchX

**ТЗ → DAG → параллельные агенты → PR.**

Headless мультиагентный рой для git-проектов: декомпозирует задачу,
запускает воркеров в изолированных git worktree, мерджит результат и
открывает pull-request. Решение «мержить» — за человеком.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Python: 3.13+](https://img.shields.io/badge/python-3.13%2B-3776ab?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Tests: 254 passed](https://img.shields.io/badge/tests-254%20passed-success?style=flat-square)](src/orchx/tests)
[![Status: 0.2-alpha](https://img.shields.io/badge/status-0.2--alpha-orange?style=flat-square)](docs/changelog.md)

[**Quickstart**](#quickstart) ·
[**Что внутри**](#что-внутри) ·
[**Плагины**](#плагины) ·
[**Документация**](#документация)

</div>

---

## Зачем

Современные LLM умеют писать код, но **не умеют управлять собой на длинной
дистанции**: фокусироваться, прогонять checkpoints, мержить параллельные
ветки, обрабатывать падения CI, отвечать на code-review. orchX берёт это
на себя — оставляя за человеком только то, что человек делать должен:
читать PR и нажимать «merge».

```
                  task / spec
                       │
                       ▼
                   planner ──► plan.json  (FLAT или PHASED + DAG)
                       │
                       ▼
                 orchestrator
                       │
        ┌──────────────┼──────────────┐
        │              │              │
       phase 1        phase 2        phase N
   ┌────┴────┐    ┌────┴────┐    ┌────┴────┐
   ▼    ▼    ▼    ▼    ▼    ▼    ▼    ▼    ▼
  w1   w2   w3   w4   w5   w6   ...
        │              │              │
        ▼              ▼              ▼
    merge p1 ───►  merge p2  ───►  merge pN
                       │
                       ▼
                 integration  ──► PR
```

## Что внутри

| Возможность                   | Описание                                                                                                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Declarative `plan.json`**   | FLAT или PHASED-планы с явными acceptance checks. Никакого «agent loops до победного» — есть план, есть критерии.                                               |
| **PHASED checkpoints**        | Большие ТЗ дробятся на фазы, между ними — merge commit в integration. Откат — это `git revert` одной фазы.                                                      |
| **Auto-replan**               | Фаза провалилась → orchX-planner получает контекст провала и переписывает остаток плана. До `max_replans` раз.                                                  |
| **3-state verifier reviewer** | Финальный reviewer прогоняет findings через verifier: `confirmed / plausible / refuted`. Refuted отбрасываются — снижает noise в финальном PR.                  |
| **Pre-merge per-task review** | Опционально reviewer прогоняется на дифф каждой задачи **до** merge. Ловит correctness-bugs пока дифф маленький.                                                |
| **Merger при конфликтах**     | На merge-конфликте спавнится отдельный `orchX-merger` — выбирает осмысленную композицию для shared registry-файлов.                                             |
| **Git worktree isolation**    | Каждый воркер в своём worktree — параллельные правки не топчут друг друга.                                                                                      |
| **Bash sandbox**              | Prefix-extract + injection-guard: `&&`, `;`, `\|`, `$(...)`, backtick'и блокируются как injection до сверки с allow-list.                                       |
| **Path-gated edit**           | Воркер может писать только в свой worktree + только в файлы из своего `file_scope` + edit-permissions.                                                          |
| **Provider-aware effort**     | `low / medium / high / xhigh / max` маппится в provider-specific параметр (`reasoning_effort` для OpenAI, `thinking` для Claude, `thinking_config` для Gemini). |
| **Compaction**                | На ~75% context window — single-pass summary, чтобы не врезаться в стенку.                                                                                      |
| **Resume падшего прогона**    | `orchx all --resume "..."` — уже завершённые задачи пропускаются.                                                                                               |

## Quickstart

```bash
# 1. Установить
pip install orchx

# 2. В корне любого git-репозитория развернуть .orchx/
cd /path/to/your-project
orchx init

# 3. Заполнить переменные окружения (один раз)
cp .orchx/.env.example .orchx/.env
$EDITOR .orchx/.env
#   ORCHX_LLM_BASE_URL=https://your-openai-compatible-proxy/v1
#   ORCHX_LLM_API_KEY=sk-...
#   ORCHX_MODEL=anthropic/claude-sonnet-4-7

# 4. (Сильно желательно) Описать стек в .orchx/PROJECT.md
$EDITOR .orchx/PROJECT.md

# 5. Запустить
orchx all "Реализуй авторизацию: API + UI + тесты"
```

Через несколько минут — pull-request на GitHub с готовым кодом и summary,
что было сделано, какие были проблемы и как их разрешили.

### Прочие команды

```bash
orchx plan "..."           # только декомпозиция → .orchx/runs/<id>/plan.json
orchx run                  # прогнать самый свежий plan.json
orchx list                 # все прогоны
orchx logs                 # лог последнего прогона
orchx watch                # P0.4 — feedback loop на открытый PR
orchx dashboard --port X   # P1.4 — web-дашборд (нужен 'orchx[server]')
orchx plugins list         # P0.2 — все зарегистрированные плагины

orchx tasks ready          # задачи в Ready-колонке трекера
orchx tasks pick           # атомарно забрать следующую (двинет в In Progress)
orchx tasks pick --run     # pick + сразу `orchx all` с правильным tracker-id
orchx tasks move <id> <col># вручную двинуть карточку
```

### Замкнутый цикл с GitHub Projects

Если в `.orchx/config.yaml` подключён `tracker: github-projects`, рой
сам двигает карточку Backlog → Ready → In Progress → Done и оставляет
коммент в issue со ссылкой на PR. Чтобы это сработало, orchestrator'у
нужен composite id трекера (например, `PVTI_lAHO...:114`) — slug-имя
плана для git-веток (`task_id`) этого делать не умеет.

Передаётся одним из трёх способов:

```bash
# 1) Самый удобный: пропустит pick + сразу запустит рой и закроет цикл.
orchx tasks pick --run

# 2) Явный флаг для всех команд (plan / run / all).
orchx all --tracker-task "PVTI_lAHO...:114" "<задача>"

# 3) Через окружение — удобно в скриптах.
ORCHX_TRACKER_TASK_ID="PVTI_lAHO...:114" orchx all "<задача>"
```

Под капотом CLI добавляет поле `tracker_task_id` в `plan.json`, а
orchestrator при старте/финале вызывает `tracker.update_status` именно
с этим composite id (а не со slug'ом). Для других трекеров поле тоже
работает — формат composite id определяет конкретный плагин.

## Плагины

orchX расширяется через 5 plugin slots — каждый со своим Protocol-контрактом.
Сторонние пакеты регистрируют плагины через `entry-points` и подхватываются
автоматически.

| Slot       | По умолчанию                 | Альтернативы из коробки                    |
| ---------- | ---------------------------- | ------------------------------------------ |
| `runtime`  | `local` (asyncio + worktree) | `docker`                                   |
| `tracker`  | `github` (gh CLI)            | — (легко добавить linear/jira/gitlab)      |
| `scm`      | `github`                     | —                                          |
| `notifier` | `noop`                       | `slack`, `discord`, `webhook`, `dashboard` |
| `memory`   | `noop`                       | `sqlite` (FTS5 + optional embeddings)      |

Подключаются через `.orchx/config.yaml`:

```yaml
runtime: docker
memory:  sqlite
notifiers: [slack, dashboard]

plugin_config:
  slack:
    webhook_url: ${SLACK_WEBHOOK_URL}
  sqlite:
    path: .orchx/memory.db

reactions:
  ci_failed:         {auto: true, action: send-to-debugger, max_retries: 3}
  changes_requested: {auto: true, action: send-to-implementer}
  approved_and_green:{auto: false, action: notify}
```

Как написать свой плагин — [`docs/contributing.md`](docs/contributing.md).

## Безопасность по умолчанию

- Никогда не пушит в `main` — только `orchX/<task_id>` + PR.
- Bash injection-guard срабатывает раньше allow-list'а.
- Edit строго в свой worktree + только разрешённые пути.
- `webfetch` блокирует private/loopback IP (anti-SSRF).
- `browser` (P1.7) ограничен `localhost`/`127.0.0.1` по умолчанию.
- `docker` runtime (P1.2) — `--network=none --cap-drop=ALL --read-only` для repo.
- Federation REST (P2.3) — Bearer-token auth с constant-time compare.

## Документация

| Документ                                             | О чём                                                                  |
| ---------------------------------------------------- | ---------------------------------------------------------------------- |
| [`docs/architecture.md`](docs/architecture.md)       | Архитектура 0.2: модули, plugin slots, event flow, deployment modes    |
| [`docs/internals.md`](docs/internals.md)             | Низкоуровневые подробности (планер, replan, merger, reviewer pipeline) |
| [`docs/contributing.md`](docs/contributing.md)       | Как добавить плагин / tool / тест                                      |
| [`docs/changelog.md`](docs/changelog.md)             | Подробный список изменений по версиям                                  |
| [`docs/recipes/`](docs/recipes)                      | Готовые конфиги: Slack-нотификации, Docker-runtime, memory-RAG         |
| [`examples/`](examples)                              | Hello-world, with-dashboard, with-docker-runtime, with-mcp-server      |

## Требования

- Python ≥ 3.13
- `git` ≥ 2.30 (для worktree)
- `gh` CLI (для авто-создания PR): `brew install gh && gh auth login`
- OpenAI-совместимый LLM endpoint с tool-calling (OpenRouter, vLLM, Anthropic Proxy, Ollama-shim, …)

## Уникальность в landscape

```
                   batch-первичный             interactive-первичный
                          │                              │
                          │                              │
  богатый                 │     ┌─ OpenHands ────────────┼──
  фронтенд /              │     │                        │
  агентский UI            │     ├─ Ruflo (Claude Code)   │
                          │     │                        │
                          │     ├─ Composio Agent        │
                          │     │  Orchestrator          │
                          │     │                        │
  ━━━━━━━━━━━━━━━━━━━━━━━━┼━━━━━┴━━━━━━━━━━━━━━━━━━━━━━━━┼━━━━
                          │                              │
                  ┌── orchX ──┐                          │
  headless CLI    │ declarative │                        │
  batch-first     │ plan +      │                        │
                  │ checkpoints │                        │
                  │ + reviewer  │                        │
                  └─────────────┘                        │
                          │                              │
                          │                              │
```

orchX — единственный, у кого есть формализованный declarative `plan.json`

- PHASED checkpoints + auto-replan + 3-state verifier reviewer **в
  батчевом headless CLI**.

## Лицензия

[MIT](LICENSE).

## Contributing

Issues и PRs приветствуются: <https://github.com/411241075-hdf/orchx>.
См. [`docs/contributing.md`](docs/contributing.md) и [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
