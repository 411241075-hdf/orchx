# orchX — параллельный мультиагентный рой

Реализация концепции «рой агентов»: standalone Python-диспетчер декомпозирует задачу через `orchX-planner`, спавнит N независимых in-process воркеров в изолированных git worktree-ах, проверяет acceptance, мерджит результаты в интеграционную ветку и **всегда открывает GitHub PR**. Решение мержить PR — за человеком; рой никогда не пушит в `main` напрямую.

Подходит и для маленьких задач (`Реализуй компонент X`), и для больших ТЗ из `docs/tasks/*.md` — для последних planner автоматически разбивает работу на фазы с checkpoint'ами, а оркестратор перепланирует остаток при провале.

## Архитектура

```text
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
   │    │    │    │    │    │
   └────┴────┘    └────┴────┘    └────┴────┘
        │              │              │
        ▼              ▼              ▼
    merge p1 ───►  merge p2  ───►  merge pN
                       │
                ┌──────┴──────┐
                │ replan?     │
                │ если фаза   │
                │ упала       │
                └─────────────┘
                       ▼
                 integration  ──► PR
```

- **planner** — декомпозирует задачу в plan.json. Для больших задач выдаёт PHASED-план (массив `phases`), для маленьких — FLAT (плоский `tasks`).
- **orchestrator** — обходит фазы строго последовательно, внутри фазы спавнит воркеров параллельно по топологическим уровням, после успешной фазы мерджит её в интеграционную ветку (checkpoint), при провале фазы — вызывает `orchX-planner` повторно с контекстом провала (auto-replan), эскалирует на `orchX-debugger` / `orchX-merger` / `orchX-reviewer` по необходимости.
- **worker** — асинхронная корутина (`orchx.agent.worker.run_agent`) в своём git worktree. Парсит роль из `orchx/prompts/orchX-<role>.md`, поднимает реестр tool'ов (read/write/edit/glob/grep/codesearch/bash/todowrite), гоняет цикл «LLM → tool → LLM» через OpenAI-совместимый Proxy.

## Два формата плана

| Формат     | Когда                                             | Структура                                                                                                                                |
| ---------- | ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **FLAT**   | Маленькие задачи (≤8 задач, 1-2 слоя архитектуры) | `{ "tasks": [...] }` — плоский DAG, depends_on задаёт порядок                                                                            |
| **PHASED** | Большие задачи (ТЗ, рефакторинги, миграции БД)    | `{ "phases": [{ "id", "goal", "tasks": [...] }, ...] }` — фазы выполняются строго последовательно, между ними merge commit (=checkpoint) |

Planner сам выбирает формат на основе размера задачи. PHASED предпочтителен для:

- любого ТЗ из `docs/tasks/*.md`,
- задач с миграциями БД (нужен checkpoint после миграции),
- массовых rename/переноса файлов (нужен checkpoint после рефакторинга импортов),
- задач, затрагивающих ≥ 3 слоя архитектуры (БД + бэкенд + фронт).

## Установка

orchX поставляется как Python-пакет `orchx` в этом репо.

```bash
# 1. Поставить рой и его deps (openai-SDK, PyYAML) в venv проекта.
uv sync --extra orchx
# Альтернатива без uv:
#   pip install -e ".[orchx]"

# 2. Сконфигурировать LLM Proxy (обязательно).
export ORCHX_LLM_BASE_URL=https://your-proxy.example.com/v1
export ORCHX_LLM_API_KEY=sk-...
export ORCHX_MODEL=anthropic/claude-opus-4-7

# 3. Для GitHub PR-интеграции:
brew install gh && gh auth login
```

После `uv sync --extra orchx` появится console-script `orchx` — его можно
звать из любой точки внутри репозитория. Альтернативно работает
`python -m orchx ...`.

### Опциональные env

| Переменная             | Что задаёт                                                  |
| ---------------------- | ----------------------------------------------------------- |
| `ORCHX_PLANNER_MODEL`  | Override модели для роли planner (по умолч. `ORCHX_MODEL`). |
| `ORCHX_REVIEWER_MODEL` | То же для reviewer.                                         |
| `ORCHX_DEBUGGER_MODEL` | То же для debugger.                                         |
| `ORCHX_MERGER_MODEL`   | То же для merger.                                           |
| `ORCHX_TIMEOUT_S`      | HTTP-таймаут на один запрос к Proxy (default 600s).         |

Effort и поведенческие настройки — через CLI-флаги (`--effort`, `--reviewer-effort`, ...).

## Использование

Точки входа (любая работает):

- console-script `orchx` (после `uv sync --extra orchx` / `pip install -e .[orchx]`);
- `python -m orchx`.

Запускается из корня репозитория.

### Однострочник для маленькой задачи

```bash
orchx all "Реализуй компонент UserSettings: API + UI + тесты"
```

Planner создаст FLAT-план на 3-4 задачи, оркестратор прогонит их параллельно, на финале — reviewer и PR.

### Однострочник для большой задачи (ТЗ)

```bash
orchx all "Реализуй ТЗ docs/tasks/03-backend-modularity.md"
```

Planner прочитает указанный файл целиком, выявит этапы (миграции → перенос → API → UI), создаст PHASED-план с 4-6 фазами и адекватным `max_wall_seconds` (до 24h). Оркестратор пройдёт фазы последовательно, между фазами — checkpoint в integration ветке. Если фаза упадёт — planner будет вызван повторно с контекстом провала и сгенерирует план остатка.

Сгенерирует план, прогонит, на финале запустит `orchX-reviewer`, выведет summary, **запушит интеграционную ветку и откроет PR**. PR создаётся всегда — даже если часть задач упала, replan не помог или reviewer нашёл блокирующие проблемы; в этом случае в заголовке PR появится маркер `orchX[failed]:` или `orchX[review-blocked]:`, а решение мержить или нет ты принимаешь на стороне GitHub.

Требование: установлен и авторизован `gh` CLI. Если `gh` не найден — диспетчер ветку запушит, но PR не создаст и завершится с ненулевым кодом.

### Раздельно (по шагам)

```bash
# 1. Только спланировать
orchx plan "Реализуй UserSettings"
# отредактируй orchx/plan.json при желании

# 2. Прогнать (всегда откроет PR на финале)
orchx run

# 3. Прогнать чужой план (всегда откроет PR)
orchx run path/to/custom-plan.json
```

### Из интерактивного Kilo

```text
/orchX Реализуй компонент UserSettings: API + UI + тесты
```

### Behavior-флаги (для `run` и `all`)

Всё включённое по умолчанию можно отключить, всё выключенное — включить:

| Флаг                        | Default  | Что делает                                                                                                                                                   |
| --------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--no-review`               | off      | Не запускать финальный `orchX-reviewer` на интеграционном диффе.                                                                                             |
| `--per-task-review`         | off      | Запускать lightweight reviewer на дифф КАЖДОЙ задачи перед merge'ем. Blocking findings → задача уходит на retry с findings'ами в качестве failure-context'а. |
| `--per-task-review-effort`  | `medium` | Effort pre-merge reviewer'а.                                                                                                                                 |
| `--auto-followup`           | off      | Динамически добавлять задачи из `needs_followup` worker'ов в DAG.                                                                                            |
| `--max-followup-depth N`    | `1`      | Максимальная глубина каскада followup'ов (anti-loop).                                                                                                        |
| `--no-debugger`             | off      | На retry использовать оригинального агента, не `orchX-debugger`.                                                                                             |
| `--no-merger`               | off      | При merge-конфликте делать `git merge --abort` + fail, без `orchX-merger`.                                                                                   |
| `--no-replan`               | off      | При провале фазы остановиться, без вызова `orchX-planner` повторно.                                                                                          |
| `--no-supervisor`           | off      | Отключить фоновый supervisor (heartbeat + enforcement бюджета).                                                                                              |
| `--supervisor-interval-s F` | `30`     | Период heartbeat'а supervisor'а в секундах.                                                                                                                  |
| `--resume`                  | off      | Продолжить незавершённый прогон того же `task_id` (вместо стирания run-dir). Уже завершённые задачи пропускаются.                                            |
| `--auto-stash`              | off      | `git stash push` перед стартом и `git stash pop` после.                                                                                                      |
| `--allow-dirty`             | off      | UNSAFE: разрешить запуск с грязным workdir.                                                                                                                  |
| `--effort {minimal..max}`   | `high`   | Reasoning effort для воркеров (мапится в provider-specific параметр LLM).                                                                                    |
| `--reviewer-effort`         | `xhigh`  | Effort для финального reviewer'а — recall важнее скорости.                                                                                                   |
| `--debugger-effort`         | `xhigh`  | Effort для debugger'а — диагностика требует глубины.                                                                                                         |
| `--merger-effort`           | `high`   | Effort для merger'а.                                                                                                                                         |
| `--replanner-effort`        | `xhigh`  | Effort для `orchX-planner` при replan'е (переразбивка требует глубины).                                                                                      |

Planner всегда запускается с effort `xhigh` — декомпозиция требует максимальной глубины и переопределению из CLI не подлежит.

PR создаётся всегда после `run`/`all` и не управляется флагами — это часть контракта роя. Если нужно прогнать рой и не открывать PR (например, локальный эксперимент), просто прерви процесс до завершения или удали интеграционную ветку и открытый PR вручную через `gh pr close`.

### Дополнительные subcommand'ы

```bash
# Список всех run'ов (свежие сверху)
orchx list

# Лог последнего run'а (главный orchx.log + dispatcher.log, tail 80 строк)
orchx logs

# Лог конкретной задачи внутри run'а
orchx logs <task_id> --task <subtask_id> --tail 200
```

### Provider-aware effort (P1.7)

orchX автоматически переводит `--effort` в provider-specific параметры LLM:

| Семейство модели          | Что выставляется                                               |
| ------------------------- | -------------------------------------------------------------- |
| Anthropic Claude 4.6+     | `thinking: {type: adaptive}` + `output_config.effort`          |
| Anthropic Opus 4.7+       | то же + `display: "summarized"` (иначе thinking пустой)        |
| OpenAI o-series / GPT-5   | `reasoning_effort`                                             |
| Google Gemini 2.5+        | `thinking_config.thinking_budget` (0/1024/4096/12288/24576/-1) |
| DeepSeek R-серия          | `reasoning_effort`                                             |
| Прочее (GPT-4o, Llama, …) | пусто (модель работает в обычном режиме)                       |

Per-task override доступен через поле `effort` в plan.json (`"effort": "xhigh"` на одной задаче) — перебивает глобальный `--effort` для этой задачи.

### Безопасность bash sandbox (P0.3)

Bash-tool матчит **извлечённый prefix** команды, не полную строку. Композитные команды (`&&`, `||`, `;`, `|`, `$(...)`, backtick'и, process-substitution) автоматически блокируются как **command injection** независимо от allow-list'а. Это закрывает классическую дыру:

```
allow-list: "git status*": allow
input:      git status && rm -rf /     ← раньше пропускалось, теперь блокируется
```

Для команд из `_TWO_TOKEN_COMMANDS` (`git`, `gh`, `npm`, `uv`, …) prefix состоит из двух токенов: `git status`, `gh pr view`, `npm run lint`. Это позволяет точечно разрешить `git status` и явно запретить `git push`. Старые spec'ы со стилем `"git status*": allow` продолжают работать без изменений (паттерн нормализуется).

### Pre-merge review (P0.2)

С `--per-task-review` после прохождения acceptance каждая задача отправляется к lightweight reviewer'у на дифф vs. integration ветки. Если он возвращает blocking findings — задача отправляется на retry через debugger, и debugger получает findings в качестве failure_context'а. Это ловит correctness-bugs до того, как они попадут в integration и накопятся.

### 3-state verifier для финального review (P1.10)

Финальный `orchX-reviewer` после первого прохода (3 finder-angle'а) запускает второй verifier-проход на каждое finding. Verifier ставит вердикт `confirmed` / `plausible` / `refuted`. REFUTED-finding'и автоматически отбрасываются — это сильно снижает шум в PR без потери recall'а.

### Compaction для длинных воркеров (P1.6)

Когда `messages` воркера достигает ~75% от context window провайдера, orchX делает один проход «summarize» и заменяет середину диалога на summary. Это позволяет debugger'ам и reviewer'ам с большим scope не упираться в context limit. Контролируется через ENV `ORCHX_CONTEXT_WINDOW` (количество токенов).

### Resume падшего прогона (P2.13)

```bash
# Упал на середине? Запусти то же самое с --resume:
orchx all --resume "Реализуй ТЗ docs/tasks/03-backend-modularity.md"
# или
orchx run --resume orchx/runs/<task_id>/plan.json
```

Уже завершённые задачи (с success-result.json в integration worktree) пропускаются. Особенно полезно при wall-budget'ах в часах — теряется только текущая задача, не весь прогон.

## Большие задачи и checkpoints

Для большой задачи (например, ТЗ из `docs/tasks/`) planner создаст PHASED-план. Что важно знать:

1. **Фазы строго последовательны.** Phase 2 не стартует, пока все задачи Phase 1 не пройдут acceptance и не смержатся в integration ветку. Это гарантирует, что код Phase 2 видит зафиксированный результат Phase 1 (например, применённую миграцию БД).

2. **Каждая фаза = checkpoint.** Между фазами появляется отдельный merge commit. Если Phase 3 окажется сломанной концептуально, можно откатить integration ветку до конца Phase 2 без потери первых фаз.

3. **Auto-replan при провале.** Если все retry'и фазы исчерпаны и debugger не справился, оркестратор сохраняет контекст провала в `orchx/replan-context.md`, бэкапит текущий план как `plan.before-replan-N.json` и вызывает `orchX-planner` повторно. Planner получает:
   - оригинальный `task_id` (сохраняется!) и `spec_files`,
   - список упавших задач с причинами,
   - список уже успешных фаз (НЕ повторять),
   - оставшиеся фазы (можно переразбить или оставить).

   Planner пишет новый `plan.json`. Оркестратор продолжает с него, пропуская уже завершённые фазы.

4. **Лимит replan'ов** задаётся через `global_budget.max_replans` в plan.json (по умолчанию 3). Защищает от зацикливания.

5. **`allow_replan: false`** — на критичных фазах (миграции БД, deletion). Если такая фаза падает, оркестратор останавливается без replan'а и открывает PR с маркером `orchX[failed]:`. Человек разбирается вручную.

6. **Жёсткий hardcap 24h** на `max_wall_seconds` — защита от runaway-прогонов с большим bill'ом по токенам.

### Workflow большой задачи

```bash
# 1. Запустить
orchx all "Реализуй ТЗ docs/tasks/03-backend-modularity.md"

# 2. Смотреть прогресс в реальном времени
tail -f orchx/runs/<task_id>/orchX.log

# 3. После завершения посмотреть summary
cat orchx/runs/<task_id>/summary.json | jq '.phases'

# 4. Если был replan — посмотреть historic plans
ls -la orchx/runs/<task_id>/plan.before-replan-*.json
```

## Файловая раскладка

Код роя живёт в top-level Python-пакете `orchx/` (рядом с `backend/`, `frontend/`). Runtime-артефакты — в скрытом каталоге `orchx/` в корне репо (gitignored).

```text
orchx/                                              # Python-пакет диспетчера (импортируется как `import orchx`)
├── README.md                                       # этот файл (документация пакета)
├── cli.py                                          # argparse + subcommands plan/run/all
├── orchestrator.py                                 # phased обход, retry, debugger/merger/reviewer/supervisor/replan/followup
├── paths.py                                        # ЕДИНАЯ раскладка путей (runs/<task_id>/...) — источник правды
├── replanner.py                                    # вызов orchX-planner с контекстом провала фазы
├── runner.py                                       # тонкий адаптер: спавн in-process воркера + render_task_md
├── worktree.py                                     # git worktree операции
├── dag.py                                          # топологическая сортировка (по фазам)
├── models.py                                       # Plan/PhaseSpec/TaskSpec + загрузка/валидация plan.json
├── acceptance.py                                   # проверки acceptance (command/file_exists/file_contains)
├── pr.py                                           # push + gh pr create + render_pr_body
├── tui.py                                          # live-доска прогресса
├── schemas/                                        # JSON-схемы и шаблоны (шипятся с пакетом)
│   ├── plan.schema.json                            # контракт между планнером и диспетчером
│   ├── result.schema.json                          # контракт между воркером и диспетчером
│   └── task.template.md                            # шаблон task.md для воркера
└── agent/                                          # ВСЁ, что заменяет kilo runtime воркера
    ├── llm.py                                      # OpenAI-совместимый клиент к Proxy (стрим, tool_calls)
    ├── frontmatter.py                              # YAML-парсер orchx/prompts/orchX-*.md
    ├── permissions.py                              # bash allowlist + edit path-gating
    ├── prompts.py                                  # сборка system prompt'а воркера
    ├── worker.py                                   # agent loop (in-process замена kilo run)
    └── tools/                                      # реализация tool'ов (JSON-schema → run)
        ├── fs.py                                   # read, write, edit, glob
        ├── search.py                               # grep, codesearch (rg + python-fallback)
        ├── shell.py                                # bash с allowlist-sandbox'ом
        └── todo.py                                 # TodoWrite

orchx/                                             # runtime data (полностью gitignored)
├── _pending/                                       # staging до того, как planner запишет task_id
│   ├── plan.json                                   # промежуточный план; перемещается в runs/<task_id>/plan.json
│   ├── planner.log                                 # лог planner'а на initial planning
│   └── dispatcher.log                              # лог диспетчера до момента, когда task_id известен
└── runs/<task_id>/                                 # ВСЁ runtime одного прогона — здесь
    ├── plan.json                                   # активный план (после replan'а — последняя версия)
    ├── plan.before-replan-N.json                   # бэкап плана перед N-м replan'ом
    ├── replan-context.md                           # бриф для planner'а при replan'е
    ├── dispatcher.log                              # лог Python-диспетчера (root logger)
    ├── planner.log                                 # лог initial planner'а
    ├── orchx.log                                   # человекочитаемый журнал прогона (с фазами и replan'ами)
    ├── summary.json                                # итоговая сводка (phases + replan_history + tasks)
    ├── logs/
    │   ├── <subtask>.attempt<N>.log                # transcript воркера
    │   ├── <subtask>.merger.attempt<N>.log         # лог orchX-merger при конфликте
    │   ├── replan-<N>.log                          # лог orchX-planner'а на N-м replan'е
    │   └── review__<task_id>.log                   # лог финального reviewer'а
    └── worktrees/
        ├── _integration/                           # ветка orchX/<task_id>, сюда мерджатся результаты
        ├── _review/                                # ветка orchX-review/<task_id>, рабочая зона reviewer'а
        └── <subtask_id>/                           # worktree-ы воркеров (один на subtask)
```

Контракт раскладки:

- **Всё, что относится к одному прогону, лежит в `orchx/runs/<task_id>/`.** Это единая папка, по которой можно понять, что произошло за прогон, и которую можно безопасно снести `rm -rf`.
- **`task_id` — на английском, в kebab-case** (например, `ts-03-backend-modularity`). Его генерирует planner.
- **`orchx/_pending/`** — временный staging для `orchX plan`, пока task_id ещё не известен. После записи плана диспетчер читает task_id и перемещает содержимое в `runs/<task_id>/`.
- **Повторный запуск того же task_id** полностью затирает старую `runs/<task_id>/` (включая worktree-ы и связанные ветки).
- **Воркеры внутри своих worktree-ов** пишут в локальные `orchx/task.md` и `orchx/results/<id>.json` — это их checkout, не корень репо.

Все runtime-артефакты в `.gitignore`. Если хочешь сохранить план в репо для воспроизводимости — скопируй `orchx/runs/<task_id>/plan.json` в локальное место (например, `orchX-plans/<task_id>.json`) и закоммить отдельно.

## Ветки

- `orchX/<task_id>` — интеграционная, в неё последовательно мерджатся результаты воркеров.
- `orchX-tasks/<task_id>/<sub_task_id>` — ветка одного воркера, после успеха мерджится в интеграционную.
- `orchX-review/<task_id>` — ветка для финального reviewer'а, не сливается никуда (он только пишет отчёт).

## Контракты

- **Планнер** пишет `orchx/_pending/plan.json` (initial planning, до того как task_id известен) или `orchx/runs/<task_id>/plan.json` (replan, путь сообщает диспетчер) по схеме `orchx/schemas/plan.schema.json`.
- **Воркер** читает `orchx/task.md` внутри своего worktree (генерируется диспетчером по `orchx/schemas/task.template.md`).
- **Воркер** пишет `orchx/results/<task_id>.json` внутри своего worktree по схеме `orchx/schemas/result.schema.json`. Reviewer (финальный и pre-merge) дополнительно заполняет `review_report.findings[]` со структурированными severity/category/file/line/description/failure_scenario/suggestion + опциональным `verifier_verdict`. Diспетчер автоматически парсит этот блок и:
  - использует `blocking_count > 0` как override для статуса задачи (failed, даже если reviewer написал success);
  - рендерит таблицу findings в PR body, сгруппированную по severity;
  - в режиме pre-merge review отправляет задачу на retry через debugger с findings в качестве failure_context'а.
- **Acceptance** возвращает `CheckOutcome` с категорией провала (`env`, `cmd_failed`, `pattern_no_match`, `timeout`, `syntax_error`, `file_missing`, `unknown`). Replanner использует это: при чисто `env`-провалах фазы он не вызывает planner повторно, а останавливает рой с advisory message.
- **Plan-task** опционально содержит `effort: minimal|low|medium|high|xhigh|max` для per-task override reasoning'а.

## Агенты

Все orchX-агенты лежат в `orchx/prompts/orchX-*.md`:

| Агент               | Роль                             | Когда вызывает диспетчер             |
| ------------------- | -------------------------------- | ------------------------------------ |
| `orchX-planner`     | Декомпозирует задачу в plan.json | Один раз на старте                   |
| `orchX-architect`   | ADR, контракты, структура        | По заданиям с `agent: architect`     |
| `orchX-implementer` | Реализация кода                  | По заданиям с `agent: implementer`   |
| `orchX-tester`      | pytest/vitest                    | По заданиям с `agent: tester`        |
| `orchX-reviewer`    | Финальный ревью диффа            | По заданиям с `agent: reviewer`      |
| `orchX-debugger`    | Чинит провалившуюся задачу       | На retry после failed acceptance     |
| `orchX-merger`      | Разрешает merge-конфликты        | При наличии конфлектов между ветками |

Файлы — plain markdown с YAML-frontmatter. orchX парсит их сам (`orchx.agent.frontmatter`), kilo-runtime для этого не нужен.

`.kilo/INSTRUCTIONS.md` orchX-воркеры **не загружают автоматически** (нет kilo-loader'а). Если роли он нужен — упомяните соответствующие конвенции прямо в body agent-файла.

## Worker tools

Реестр tool'ов для воркеров строится в `orchx/agent/tools/__init__.py:build_tool_registry` по permissions из frontmatter'а роли. Полный список:

| name         | gate (Permissions)         | назначение                                                                               |
| ------------ | -------------------------- | ---------------------------------------------------------------------------------------- |
| `read`       | `read`                     | Read a file (с номерами строк) или показать содержимое директории.                       |
| `glob`       | `glob`                     | Find files matching a glob (`**/*.py`); sorted by mtime.                                 |
| `grep`       | `grep`                     | Regex-поиск по содержимому файлов (rg при наличии, python-fallback иначе).               |
| `codesearch` | `codesearch`               | То же, с фильтром по rg `--type` (py/ts/rust/…).                                         |
| `write`      | `edit` (path-gated)        | Полная перезапись файла. Sandboxed внутри cwd.                                           |
| `edit`       | `edit` (path-gated)        | Точечная замена `old_string → new_string`. Поддерживает `replace_all=true`.              |
| `bash`       | `bash` (prefix allow-list) | Один shell-command. Injection-guard, `workdir` строго внутри cwd.                        |
| `todowrite`  | always                     | Перезаписать in-memory TODO-список воркера.                                              |
| `task`       | `task` (default: deny)     | Спавн short-lived sub-агента (`explore` read-only или `general`). Глубина рекурсии — 1.  |
| `webfetch`   | `webfetch` (default: deny) | HTTPS-fetch публичного URL, HTML→Markdown. Private/loopback IPs блокируются (anti-SSRF). |

**Sandbox.** read/glob/grep/codesearch — read-only внутри `cwd ∪ repo_root` (можно читать общие `AGENTS.md`/`.kilo/INSTRUCTIONS.md`). write/edit/bash — строго внутри `cwd` (worktree воркера). Любой выход за границу — `Permission denied:` БЕЗ обращения к ФС или exec'у.

**Bash allow-list.** Все правила матчатся по **извлечённому prefix'у** команды (см. секцию «Безопасность bash sandbox» ниже). Для python (`python -m pytest`, `python -c "..."`), ruff (`ruff check`/`ruff format`), а также прямого `mypy file.py` — добавлены явные правила в frontmatter'ах implementer/debugger/tester/merger. Это позволяет воркеру самостоятельно прогонять линт/типизацию/тесты без `uv run` (см. также `.kilo/INSTRUCTIONS.md`).

**Permission-denied формат.** Все tool'ы используют общий хелпер `orchx.agent.tools.permission_denied(tool, target, reason, hint)`, который выдаёт стабильный prefix `Permission denied: <tool> on <target> — <reason>.`. Это упрощает парсинг отказов в логах и тестах.

## Что в TODO

- Чистка устаревших `_review` веток через `git branch -D 'orchX-review/*'` — сейчас они удаляются только при повторном запуске того же `task_id`.
- `supervisor` пока только логирует heartbeat и обрывает по wall-budget; не реагирует на «зависший» процесс воркера и не пишет машиночитаемый progress-стрим.
- Auto-конвертация blocking-findings финального reviewer'а в новые debugger-задачи (сейчас они попадают только в `summary.json` / PR body, но в DAG автоматически не добавляются).
- Mid-phase replan: сейчас replan вызывается только после полного провала всех retry'ев фазы. Прерывание прямо в середине фазы (например, через сигнал от supervisor'а) — не поддерживается.
- Удалённые worktree-ы для уже завершённых задач занимают диск до конца прогона. Можно было бы убирать их после успешного merge в integration.
- Daemon-режим (`orchx run --serve :8421`) с HTTP API для дашборда / webhooks — нужно для продолжительных runs, где TUI не подходит.

### Planned tools

- `semantic_search` / `codebase_search` — natural-language code search через делегацию во внутренний mini-agent loop (см. TOOLING_GAPS TASK-5). На данный момент эквивалент достигается через паттерн «grep → group by file → edit replace_all» (см. секцию «Refactor patterns» в системном промпте воркера).
- LSP-симвoл-tools (`find_symbol` / `find_references` / `rename_symbol`) — требуют LSP-клиента; пока — workaround через `grep + edit replace_all` (TASK-6 в TOOLING_GAPS).

## Безопасность

- Воркеры запускаются с **узкими** `permission.bash` (read-only git, никаких push/reset/worktree-команд).
- **Bash injection guard:** команды с `&&`, `||`, `;`, `|`, `$(...)`, backtick'ами и process-substitution автоматически блокируются ДО матча с allow-list'ом. Так нельзя «обойти» правило `"git status*": allow` командой `git status && rm -rf /` (см. P0.3 в README выше).
- **Prefix matching:** allow-list матчит извлечённый prefix команды (например, `git status`, `gh pr view`), а не сырую строку. Старые `"git status*"`-стиль паттерны продолжают работать.
- `permission.edit` у воркеров широкий, но реальный scope контролируется через task.md и проверяется диспетчером (нарушение scope = задача не пройдёт acceptance).
- Диспетчер пушит **только** интеграционную ветку `orchX/<task_id>` и открывает на неё PR. В `main` (и любую защищённую ветку) ничего не уходит без человеческого мержа PR.
- Перед стартом проверяется чистый рабочий каталог — рой не стартует на грязном репозитории.
- Все ветки роя префиксованы `orchX/`, `orchX-tasks/` и `orchX-review/` для лёгкой массовой чистки (`git branch -D` по шаблону).

## Сравнение с Agent Manager (VSCode)

| Аспект               | Agent Manager      | orchX                                               |
| -------------------- | ------------------ | --------------------------------------------------- |
| Параллельные воркеры | по одному вручную  | DAG, до `max_parallel`                              |
| Декомпозиция задачи  | пользователь       | orchX-planner (FLAT или PHASED)                     |
| Иерархия / фазы      | нет                | PHASED-план + checkpoints + auto-replan             |
| Большие ТЗ           | не подходит        | первоклассно (до 24h, до N replan'ов)               |
| Акcеptance           | вручную            | автоматическая по plan.json                         |
| Merge стратегия      | apply / merge / PR | автоматический мердж в интеграционную + PR (всегда) |
| Worktrees            | `.kilo/worktrees/` | `orchx/runs/<task>/worktrees/`                      |
| UI                   | VSCode             | CLI                                                 |

Они не конфликтуют: разные подкаталоги, разные имена веток.

## Очистка

```bash
# Удалить все worktrees роя для конкретной задачи
for wt in orchx/runs/<task_id>/worktrees/*; do
  git worktree remove --force "$wt" 2>/dev/null || true
done

# Удалить все ветки роя для этой задачи
git branch -D $(git branch --list "orchX/<task_id>") 2>/dev/null || true
git branch -D $(git branch --list "orchX-tasks/<task_id>/*") 2>/dev/null || true
git branch -D $(git branch --list "orchX-review/<task_id>") 2>/dev/null || true

# Снести всю папку прогона разом
rm -rf orchx/runs/<task_id>
```

> Идемпотентный старт диспетчера сам чистит остатки предыдущего прогона того
> же `task_id` (полностью пересоздаёт `orchx/runs/<task_id>/`), так что
> ручная чистка обычно нужна, только если хочешь освободить диск или удалить
> task_id, к которому больше не вернёшься.
