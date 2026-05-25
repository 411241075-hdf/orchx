# orchX — параллельный мультиагентный рой для git-проектов

orchX превращает свободное описание задачи в выполненный pull-request:
LLM-планнер декомпозирует работу в DAG, диспетчер запускает несколько
in-process воркеров (`implementer`, `architect`, `tester`, `reviewer`,
`debugger`, `merger`) в **изолированных git worktree-ах**, мерджит результат
в интеграционную ветку и **всегда открывает GitHub PR**. Решение мержить —
за человеком; рой никогда не пушит в `main` напрямую.

```
                  task / spec
                       │
                       ▼
                   planner ──► plan (phases × DAG)
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

orchX подходит и для маленьких задач (`Реализуй компонент X`), и для
больших ТЗ — для последних planner автоматически разбивает работу на фазы
с checkpoint'ами, а оркестратор перепланирует остаток при провале.

## Что нового в 0.2 (P0-P2)

* **Plugin-slot system**: 5 slots (`runtime`, `tracker`, `scm`, `notifier`,
  `memory`). Сторонние пакеты регистрируют плагины через `entry-points`
  и подхватываются `orchx plugins list` автоматически.
* **PR feedback loop** (`orchx watch`): авто-debugger на CI failures,
  авто-implementer на review comments, опциональный auto-merge.
* **Долгоживущая память** (`memory: sqlite`): SQLite + FTS5 + опциональные
  OpenAI-compatible embeddings. Orchestrator пишет успешные планы +
  провалы + reviews; planner может recall'ить похожие.
* **MCP-bridge**: воркеры подключаются к Model Context Protocol серверам
  через `mcp_servers:` в frontmatter роли. Tools префиксуются `<server>__<name>`.
* **Docker-runtime plugin** (`runtime: docker`): sandboxed worker'ы в
  контейнерах (`--network=none --cap-drop=ALL --read-only` repo mount).
* **Cost tracking**: per-model price table, summary.json с per-role/per-task
  cost, `--max-cost-usd` budget enforcement, notifications на 50/75/90%.
* **Web dashboard** (`pip install 'orchx[server]'`): FastAPI + SSE + minimal
  HTMX UI. `orchx dashboard --port 8421`. Federation REST API
  (`POST /api/runs/spawn` для cross-machine).
* **Symbol-intelligence tools** (P1.6): `find_symbol` / `find_references` /
  `rename_symbol` — AST для Python, regex для JS/TS.
* **Browser tool** (`pip install 'orchx[browser]'`): Playwright, sandbox
  localhost only by default.
* **PR auto-fixup chain**: blocking reviewer findings конвертируются в
  follow-up debugger TaskSpec'и (сохраняются в `auto_fixup_plan.json`).
* **Notifications**: Slack/Discord/Webhook plugins. Multi-fan-out при
  нескольких notifiers.
* **CI / coverage**: GitHub Actions, 254+ tests, FakeLLMClient для
  integration-тестов.

См. [`docs/changelog.md`](docs/changelog.md) для подробностей, [`docs/architecture.md`](docs/architecture.md)
для архитектуры и [`docs/comparison.md`](docs/comparison.md) — для сравнения с
OpenHands, Ruflo, ComposioHQ/agent-orchestrator.

## Установка

```bash
pip install orchx
# или: uv pip install orchx
# или: pip install git+https://github.com/411241075-hdf/orchx.git
```

Требования:

- Python ≥ 3.13.
- `git` ≥ 2.30 (для worktree).
- `gh` CLI (для авто-создания PR; `brew install gh && gh auth login`).
- OpenAI-совместимый LLM Proxy с поддержкой tool calling (любой
  endpoint `/v1/chat/completions`: OpenRouter, vLLM, Anthropic Proxy, …).

## Быстрый старт

```bash
# 1. Зайди в свой git-репозиторий.
cd path/to/your/project

# 2. Развернуть .orchx/ — папку с конфигом, дефолтными промптами и шаблонами.
orchx init

# 3. Заполнить .env (один раз, gitignored).
cp .orchx/.env.example .orchx/.env
$EDITOR .orchx/.env
#   ORCHX_LLM_BASE_URL=https://your-proxy/v1
#   ORCHX_LLM_API_KEY=sk-...
#   ORCHX_MODEL=anthropic/claude-sonnet-4-6

# 4. (Сильно рекомендуется) Описать стек проекта для агентов.
$EDITOR .orchx/PROJECT.md

# 5. Запустить рой.
orchx all "Реализуй фичу X — нужно API + UI + тесты"
```

После завершения orchx запушит интеграционную ветку и откроет PR на GitHub.
Решение «мержить» остаётся за тобой — рой только готовит изменения.

## Что создаёт `orchx init`

```
your-project/
├── .gitignore                 # ← добавлены строки .orchx/runs/, .orchx/_pending/, .orchx/.env
└── .orchx/
    ├── .env.example           # шаблон конфига
    ├── PROJECT.md             # описание стека (читают все роли)
    ├── README.md              # справка по runtime-каталогу
    ├── prompts/               # копии дефолтных промптов (можно править)
    │   ├── orchX-planner.md
    │   ├── orchX-implementer.md
    │   ├── orchX-debugger.md
    │   ├── orchX-merger.md
    │   ├── orchX-reviewer.md
    │   ├── orchX-tester.md
    │   └── orchX-architect.md
    ├── runs/                  # ← создаётся при первом прогоне (gitignored)
    └── _pending/              # ← staging для plan'а (gitignored)
```

Промпты копируются в `.orchx/prompts/` чтобы их можно было править под
свой стек. Если не хочешь — запусти `orchx init --minimal`: рой будет
использовать промпты прямо из пакета, и репо останется чистым.

## Кастомизация под свой проект

orchX устроен так, чтобы **специфика проекта жила в `.orchx/`**, а не в
коде пакета. Два рычага:

**1. `.orchx/PROJECT.md` — контекст проекта.**

Все роли подгружают его в system prompt. Сюда пиши: язык/фреймворк, layout
репо, конвенции коммитов, registry-файлы (типа `webapp.py`, `App.jsx`,
`pyproject.toml`), команды тестов/линтера, опасные операции (миграции БД).
Шаблон с разделами кладётся при `orchx init` — заполни placeholder'ы.

**2. `.orchx/prompts/orchX-<role>.md` — переопределение роли.**

Каскад загрузки: сначала `<project>/.orchx/prompts/orchX-<role>.md`, потом
дефолт пакета. Можно править копии под свой стек, добавлять кастомные роли
или убирать те, что не нужны. После `pip install --upgrade orchx` новые
дефолты не затрут твои правки — они в пакете, не в репо.

## Использование

### Планирование + прогон одной командой

```bash
orchx all "<описание задачи>"
```

Planner сам решает, делать FLAT-план (3-8 атомарных задач) или PHASED (фазы
для больших ТЗ с миграциями/рефакторингами). После прогона — PR.

### По шагам

```bash
orchx plan "<описание>"     # сгенерировать .orchx/runs/<task_id>/plan.json
$EDITOR .orchx/runs/.../plan.json
orchx run                    # прогнать самый свежий план
```

### Resume падшего прогона

```bash
orchx all --resume "<та же задача>"
# Уже завершённые задачи (с success-result.json в integration ветке) пропускаются.
```

### Прочее

```bash
orchx list                   # все прогоны (свежие сверху)
orchx logs                   # лог последнего прогона
orchx logs <task_id> --task <subtask>  # лог конкретной задачи
```

Подробный справочник behavior-флагов (`--no-review`, `--per-task-review`,
`--auto-followup`, `--effort`, `--reviewer-effort`, ...) — в
[`docs/internals.md`](docs/internals.md).

## Архитектура (в двух словах)

- **planner** — декомпозирует задачу в `plan.json` (FLAT или PHASED).
- **orchestrator** — обходит фазы последовательно, внутри фазы спавнит
  воркеров параллельно по топологическим уровням, после успешной фазы
  мерджит её в интеграционную ветку (checkpoint).
- **worker** — асинхронная корутина в своём git worktree. Парсит роль из
  markdown-промпта, поднимает реестр tool'ов
  (`read`/`write`/`edit`/`glob`/`grep`/`codesearch`/`bash`/`todowrite`),
  гоняет цикл «LLM → tool → LLM».
- **debugger** — на retry задачи, если оригинальный воркер не прошёл
  acceptance: воспроизводит, находит root cause, делает минимальную правку.
- **merger** — при merge-конфликте между двумя задачами выбирает осмысленную
  композицию (для shared registry-файлов).
- **reviewer** — финальный код-ревью на интеграционный дифф (3 finder-angle
  + verifier-проход для отсева ложных срабатываний).

Внутренние подробности: [`docs/internals.md`](docs/internals.md).

## Безопасность

- **Bash sandbox с prefix-detection.** Allow-list матчит prefix команды
  (`git status`), композитные конструкции (`&&`, `;`, `|`, `$(...)`,
  backtick'и) автоматически блокируются как command injection — независимо
  от allow-list'а.
- **edit path-gating.** Воркер может редактировать только пути из
  `file_scope` своей задачи и из своего `permission.edit:` allow-list'а.
- **Изолированные worktree.** Каждый воркер работает в отдельном `git
  worktree`. Конфликты не попадают в working tree пользователя.
- **Никогда не пушит в `main`.** Только интеграционная ветка
  `orchX/<task_id>` + PR через `gh`. Финальный merge — ручной.

## Лицензия

[MIT](LICENSE).

## Contributing

Проект ранний (0.1.0). Issues / PRs приветствуются на
<https://github.com/411241075-hdf/orchx>.
