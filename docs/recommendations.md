# orchX — рекомендации по доработке

> **Цель документа.** Прицельный, исполнимый roadmap улучшений orchX на
> основе анализа OpenHands, Ruflo и ComposioHQ/agent-orchestrator
> (см. [`comparison.md`](./comparison.md)).
>
> Принцип отбора: **сохраняем уникальность orchX** (declarative
> plan.json + PHASED-checkpoints + auto-replan + 3-state reviewer),
> **закрываем операционные пробелы** (memory, feedback loop, runtime
> isolation, plugin API).

Каждая рекомендация содержит:

- **что делаем** (конкретное изменение),
- **где в репо** (точные пути с line-references),
- **как реализовать** (структура кода / migration plan),
- **acceptance** (как проверить),
- **источник идеи** (какой проект делает это хорошо).

## 0. Приоритизация

| Приоритет | Когда делать               | Темы                                                                |
| --------- | -------------------------- | ------------------------------------------------------------------- |
| **P0**    | До следующего релиза 0.2.0 | Рефакторинг orchestrator + REST API + memory MVP + PR feedback loop |
| **P1**    | До 0.3.0                   | Plugin system + MCP-bridge + Docker runtime + cost dashboard        |
| **P2**    | До 1.0.0                   | Browser tool + GraphQL/CRDT для multi-instance + Federation         |
| **P3**    | Continuous                 | Документация, тесты, CI/CD, community                               |

## P0 — критические улучшения

### P0.1 Декомпозиция `orchestrator.py` (2671 строк)

**Что делаем.** Разбить `src/orchx/orchestrator.py` на 4–5 модулей с
чёткими boundaries.

**Где в репо.**

- Сейчас: `src/orchx/orchestrator.py` (2671 строк, 1 модуль).
- Целевая структура:

  ```text
  src/orchx/orchestrator/
    __init__.py            # фасад: run_orchx(plan_path, config, ...)
    context.py             # OrchXContext, AttemptInfo, TaskState, PhaseState
    setup.py               # _initialize_context, _cleanup_previous_run, resume
    phases.py              # phase-loop, level execution, checkpoint merge
    retry.py               # retry-логика, debugger spawn, pre-merge review
    merge.py               # _merge_into_integration, merger spawn, conflict handling
    review.py              # финальный reviewer + 3-state verifier
    followup.py            # auto-followup chain, dynamic DAG expansion
    supervisor.py          # фоновый watchdog (budget enforcement)
    summary.py             # _build_summary, render_pr_body integration
  ```

**Как реализовать.**

1. Извлечь dataclass'ы (`OrchXContext`, `AttemptInfo`, `TaskState`,
   `PhaseState`) в `context.py` — это безопасный первый шаг (no logic).
2. Извлечь `_initialize_context`, `_cleanup_previous_run`,
   `_restore_states_from_results` в `setup.py`.
3. Phase-loop (`_run_phase`, `_execute_level`) → `phases.py`.
4. Retry/debugger spawn → `retry.py`.
5. Merge-логика (`_merge_into_integration`, merger spawn) → `merge.py`.
6. Reviewer + verifier → `review.py`.
7. `orchestrator/__init__.py` — фасад с публичным API: `run_orchx`,
   `OrchXConfig` (re-export для обратной совместимости).
8. Проверить, что все импорты `from orchx.orchestrator import ...`
   продолжают работать.

**Acceptance.**

- `python -m pytest src/orchx/tests/ -q` — все тесты зелёные.
- `wc -l src/orchx/orchestrator/*.py` — каждый модуль ≤ 600 строк.
- Можно прогнать `orchx all "..."` end-to-end без regression.

**Источник.** OpenHands SDK (чёткое разделение `sdk/agent`, `sdk/conversation`,
`sdk/llm`, `sdk/security`) — даёт типизированные границы.

### P0.2 Plugin-slot system (по образцу AO)

**Что делаем.** Ввести 5 plugin-slot'ов с TypeScript-style контрактами
через `typing.Protocol`:

| Slot       | Назначение                                 | Default                      |
| ---------- | ------------------------------------------ | ---------------------------- |
| `runtime`  | Где исполняется worker (subprocess/docker) | `local` (asyncio + worktree) |
| `tracker`  | Откуда брать задачи / куда писать статусы  | `github`                     |
| `scm`      | Где жить веткам / PR'ам                    | `github`                     |
| `notifier` | Куда отправлять события                    | `noop`                       |
| `memory`   | Бэкенд для долговременной памяти           | `noop` (пока нет)            |

**Где в репо.**

- Новый каталог: `src/orchx/plugins/`
- Базовые типы: `src/orchx/plugins/contracts.py`
- Дефолтные реализации:
  - `src/orchx/plugins/runtimes/local.py` (текущий код из `runner.py`)
  - `src/orchx/plugins/runtimes/docker.py` (новый, см. P1.2)
  - `src/orchx/plugins/trackers/github.py` (то, что сейчас в `pr.py`)
  - `src/orchx/plugins/notifiers/noop.py`, `slack.py`, `discord.py`, `webhook.py`
  - `src/orchx/plugins/memory/noop.py`, `sqlite.py` (P0.4)

**Как реализовать.**

```python
# src/orchx/plugins/contracts.py
from typing import Protocol, runtime_checkable
from pathlib import Path
from ..models import TaskSpec, TaskResult


@runtime_checkable
class RuntimePlugin(Protocol):
    """Где и как исполняется worker."""

    async def spawn_worker(
        self,
        *,
        cwd: Path,
        repo_root: Path,
        role: str,
        prompt: str,
        timeout_s: float,
        log_file: Path,
        effort: str | None,
    ) -> "WorkerOutcome":
        ...


@runtime_checkable
class TrackerPlugin(Protocol):
    """Откуда задачи приходят и куда статусы отдаём."""

    async def fetch_task(self, task_id: str) -> str | None: ...
    async def update_status(self, task_id: str, status: str, details: str) -> None: ...


@runtime_checkable
class NotifierPlugin(Protocol):
    """Куда отправлять события (run started / phase done / PR opened / failed)."""

    async def notify(self, event: str, payload: dict) -> None: ...


@runtime_checkable
class MemoryPlugin(Protocol):
    """Долговременная память (RAG для planner'а и debugger'а)."""

    async def remember(self, namespace: str, key: str, value: dict) -> None: ...
    async def recall(self, namespace: str, query: str, k: int = 5) -> list[dict]: ...


@runtime_checkable
class SCMPlugin(Protocol):
    """Где живут ветки / PR'ы. По умолчанию — GitHub через `gh`."""

    async def open_pr(self, branch: str, base: str, title: str, body: str) -> str: ...
    async def push_branch(self, branch: str) -> None: ...
```

```python
# src/orchx/plugins/__init__.py
from importlib.metadata import entry_points
from typing import Any


def load_plugin(slot: str, name: str) -> Any:
    """Загрузить плагин из entry_points('orchx.<slot>')."""
    eps = entry_points(group=f"orchx.{slot}")
    for ep in eps:
        if ep.name == name:
            return ep.load()()
    raise ValueError(f"Plugin {name!r} not found in slot {slot!r}")
```

В `pyproject.toml`:

```toml
[project.entry-points."orchx.runtime"]
local = "orchx.plugins.runtimes.local:LocalRuntime"
docker = "orchx.plugins.runtimes.docker:DockerRuntime"

[project.entry-points."orchx.tracker"]
github = "orchx.plugins.trackers.github:GithubTracker"

[project.entry-points."orchx.notifier"]
noop = "orchx.plugins.notifiers.noop:NoopNotifier"
slack = "orchx.plugins.notifiers.slack:SlackNotifier"
discord = "orchx.plugins.notifiers.discord:DiscordNotifier"
webhook = "orchx.plugins.notifiers.webhook:WebhookNotifier"

[project.entry-points."orchx.memory"]
noop = "orchx.plugins.memory.noop:NoopMemory"
sqlite = "orchx.plugins.memory.sqlite:SqliteMemory"
```

В конфиге проекта (`.orchx/config.yaml` — новый, опциональный):

```yaml
runtime: local
tracker: github
scm: github
notifiers:
  - slack
  - webhook
memory: sqlite

# Конфиг каждого плагина:
plugin_config:
  slack:
    webhook_url: ${SLACK_WEBHOOK_URL}
  webhook:
    url: https://my-internal/orchx-events
  sqlite:
    path: .orchx/memory.db
```

**Acceptance.**

- `orchx all "..."` работает с `runtime: local` (без regressions).
- `orchx all "..." --notifier=slack` посылает событие в Slack при старте/PR-open.
- Сторонний пакет `orchx-runtime-podman` через `entry_points` подключается без модификации orchx.

**Источник.** ComposioHQ/agent-orchestrator (7 plugin slots), OpenHands
(Workspace abstract base class).

### P0.3 Memory backend MVP (SQLite + векторный поиск)

**Что делаем.** Долговременная память: planner и debugger получают
доступ к истории прошлых прогонов того же репо для:

- **Pattern retrieval** (planner): «уже видел задачу про admin-subdomain
  — вот что сработало / что упало».
- **Failure context** (debugger): «эта ошибка уже встречалась в task
  X — фикс был Y».
- **Acceptance hints** (planner): «для тестов в `backend/` используют
  `python -m pytest`, не `uv run pytest`».

**Где в репо.**

- Новый модуль: `src/orchx/plugins/memory/sqlite.py`
- Интеграция в planner: модификация `src/orchx/orchestrator/setup.py`
  (передать `memory` в context).
- Интеграция в debugger spawn: `src/orchx/orchestrator/retry.py`.

**Как реализовать.**

1. **Storage.** SQLite + FTS5 для текстового поиска + опционально
   `sqlite-vec` или `chromadb` для embedding-search.

   ```sql
   CREATE TABLE memories (
     id INTEGER PRIMARY KEY,
     namespace TEXT NOT NULL,     -- 'plans', 'failures', 'fixes', 'acceptance'
     repo_root TEXT NOT NULL,     -- абсолютный путь к репо
     key TEXT NOT NULL,           -- task_id или сabbreviated topic
     value TEXT NOT NULL,         -- JSON
     embedding BLOB,              -- опционально, для vec-search
     created_at REAL NOT NULL,
     last_used_at REAL,
     usage_count INTEGER DEFAULT 0
   );
   CREATE VIRTUAL TABLE memories_fts USING fts5(value, content=memories);
   ```

2. **Embeddings (опционально).** Использовать **OpenAI-совместимый
   embedding endpoint** через тот же Proxy (`ORCHX_EMBED_MODEL=text-embedding-3-small`).
   Если эмбеддинги не настроены — fallback на FTS-поиск.

3. **API.**

   ```python
   class SqliteMemory:
       async def remember(self, namespace: str, key: str, value: dict) -> None:
           """Сохранить факт. Если есть OPENAI-embed — посчитать вектор."""
       async def recall(self, namespace: str, query: str, k: int = 5) -> list[dict]:
           """Найти top-k релевантных фактов. Vec-search + FTS fallback."""
       async def forget_old(self, days: int = 90) -> int:
           """Garbage-collect старые / unused записи."""
   ```

4. **Точки записи (memory.remember).**
   - **После успешного прогона** (`_build_summary`): записать
     задачу+план+файлы в `namespace='plans'`.
   - **После провала фазы** (перед replan): записать reason+failed-task
     в `namespace='failures'`.
   - **После успешного debugger-attempt**: записать diff+original_failure
     в `namespace='fixes'`.
   - **После reviewer'а**: записать findings + verdict в
     `namespace='reviews'`.

5. **Точки чтения (memory.recall).**
   - **Planner**: в system prompt планнера добавить блок
     `<historical_context>` с recall'ом по `namespace='plans'` и
     `namespace='failures'`.
   - **Debugger**: в failure_context передать recall по
     `namespace='fixes'` с похожим error-сигналом.

**Acceptance.**

- В `.orchx/memory.db` сохраняются записи после каждого прогона.
- При повторе похожей задачи planner получает контекст:
  `Found 2 historical plans for similar tasks: <task_ids>` (видно в
  planner.log).
- Debugger при тех же reasons получает hint в свой prompt.

**Источник.** Ruflo (AgentDB + HNSW + ReasoningBank); OpenHands
(Conversation persistence).

### P0.4 PR feedback loop (CI failures + review comments)

**Что делаем.** После открытия PR orchX не останавливается, а
запускает фоновый watcher, который:

- Слушает GitHub events (через polling `gh pr view --json statusCheckRollup,comments`).
- При **CI failure** — спавнит debugger со всеми логами CI.
- При **review comment с change-request'ом** — спавнит implementer с
  цитатой комментария.
- При **approved + green CI** — нотифицирует (или опционально
  auto-merge'ит).

**Где в репо.**

- Новый модуль: `src/orchx/pr_watcher.py`
- Расширение `pr.py`: новые helpers `parse_ci_failure`, `parse_review_comments`.
- CLI: `orchx watch <task_id>` — запустить watcher отдельным процессом.
- Конфигурация:

  ```yaml
  reactions:
    ci_failed:
      auto: true
      action: send-to-debugger
      max_retries: 3
    changes_requested:
      auto: true
      action: send-to-implementer
      escalate_after: 30m
    approved_and_green:
      auto: false # true для авто-merge
      action: notify
  ```

**Как реализовать.**

```python
# src/orchx/pr_watcher.py
import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ReactionEvent = Literal["ci_failed", "changes_requested", "approved_and_green"]


@dataclass
class ReactionConfig:
    auto: bool = True
    action: str = "send-to-debugger"  # или "notify", "send-to-implementer"
    max_retries: int = 3
    escalate_after_min: int = 30


async def watch_pr(
    *,
    repo_root: Path,
    pr_url: str,
    task_id: str,
    reactions: dict[ReactionEvent, ReactionConfig],
    poll_interval_s: float = 60.0,
) -> None:
    """Фоновый watcher PR'а до merge / close / timeout."""
    last_seen_comment_ids: set[str] = set()
    last_ci_status: str | None = None

    while True:
        pr_data = _gh_pr_view(repo_root, pr_url)
        if pr_data.get("state") in ("MERGED", "CLOSED"):
            break

        # CI failures
        ci_status = _extract_ci_rollup(pr_data)
        if ci_status == "FAILURE" and last_ci_status != "FAILURE":
            rc = reactions.get("ci_failed")
            if rc and rc.auto:
                logs = _gh_run_logs(repo_root, pr_data)
                await _spawn_debugger_on_ci_fail(task_id, logs, rc)
        last_ci_status = ci_status

        # Change-requests
        for comment in pr_data.get("reviewComments", []):
            if comment["id"] in last_seen_comment_ids:
                continue
            last_seen_comment_ids.add(comment["id"])
            if comment.get("state") == "CHANGES_REQUESTED":
                rc = reactions.get("changes_requested")
                if rc and rc.auto:
                    await _spawn_implementer_on_comment(task_id, comment, rc)

        # Approved + green
        if pr_data.get("reviewDecision") == "APPROVED" and ci_status == "SUCCESS":
            rc = reactions.get("approved_and_green")
            if rc:
                if rc.action == "notify":
                    await _notify(f"PR {pr_url} ready for merge")
                elif rc.action == "auto-merge":
                    subprocess.run(["gh", "pr", "merge", pr_url, "--squash"], check=True)
                break

        await asyncio.sleep(poll_interval_s)
```

В CLI:

```bash
# В режиме `--watch` orchx запускает watcher после opening PR:
orchx all "..." --watch

# Отдельно:
orchx watch <task_id> --auto-fix-ci --escalate-after 30m
```

**Acceptance.**

- После `orchx all "..." --watch`: процесс не завершается после opening
  PR, а полл'ит каждые 60s.
- Если CI падает — в integration ветке появляется новый коммит от
  debugger'а; в PR появляется коммент «orchX-debugger: applied fix for
  CI failure».
- Если рецензент написал «please rename X to Y» — implementer
  спавнится, делает правку, push'ит.
- Если PR approved + green — notification отправляется через
  notifier-plugin.

**Источник.** ComposioHQ/agent-orchestrator (reactions: ci-failed,
changes-requested, approved-and-green — это его killer-feature).

### P0.5 Расширенная test-coverage и CI

**Что делаем.**

1. Добавить **end-to-end интеграционные тесты** с mock-LLM.
2. Добавить **GitHub Actions CI**: lint (ruff), type-check (mypy strict),
   unit tests, coverage upload.
3. Добавить **тесты для критичных компонентов**, которые сейчас не
   покрыты:
   - `compaction` (`src/orchx/agent/worker.py:_maybe_compact_messages`)
   - `replan` (`src/orchx/replanner.py`)
   - `supervisor` (в orchestrator.py)
   - `merger spawn at conflict`
   - `pre-merge review pipeline`
   - `effort mapping для всех 5 семейств моделей`

**Где в репо.**

- Сейчас: `src/orchx/tests/` (7 модулей, ~600 LOC).
- Целевая структура:

  ```text
  src/orchx/tests/
    unit/          # текущие тесты, разнесены по подмодулям
    integration/   # новые e2e с mock-LLM
      test_full_run_flat_plan.py
      test_full_run_phased_plan.py
      test_resume.py
      test_replan_on_phase_failure.py
      test_merger_on_conflict.py
      test_per_task_review.py
    fixtures/
      mock_llm.py  # in-memory FakeLLMClient с программируемыми ответами
      fixture_repos.py  # git init + commit, чтобы worker мог работать
    conftest.py
  ```

- Новый `mock_llm.py`:

  ```python
  class FakeLLMClient:
      """Программируемый LLM-клиент для тестов.

      В каждом тесте задаём конкретные ответы по сценарию:
      llm = FakeLLMClient([
          {"text": "PLAN", "tool_calls": [{"name": "write", "args": {...}}]},
          {"text": "DONE", "tool_calls": []},
      ])
      """
  ```

- `.github/workflows/ci.yml`:

  ```yaml
  name: CI
  on: [push, pull_request]
  jobs:
    lint:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: '3.13' }
        - run: pip install -e ".[dev]"
        - run: ruff check src/
        - run: ruff format --check src/
    typecheck:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: '3.13' }
        - run: pip install -e ".[dev]"
        - run: mypy --strict src/orchx
    test:
      runs-on: ubuntu-latest
      strategy:
        matrix:
          python: ['3.13']
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: ${{ matrix.python }} }
        - run: pip install -e ".[dev,test]"
        - run: pytest --cov=orchx --cov-report=xml
        - uses: codecov/codecov-action@v4
  ```

- В `pyproject.toml` — `[project.optional-dependencies]`:

  ```toml
  [project.optional-dependencies]
  dev = ["ruff>=0.5", "mypy>=1.10"]
  test = ["pytest>=8", "pytest-asyncio>=0.23", "pytest-cov>=5", "pytest-mock>=3"]
  ```

**Acceptance.**

- `pytest --cov=orchx` показывает coverage ≥ 70% (с целью 85%+).
- GitHub Actions проходит на каждом PR.
- mypy --strict без errors.

**Источник.** OpenHands (полное unit-покрытие в `tests/unit/`), AO
(3,288 test cases).

## P1 — важные улучшения

### P1.1 MCP-bridge (Model Context Protocol)

**Что делаем.** Добавить orchX как **MCP-client**, чтобы воркеры могли
пользоваться сторонними MCP-серверами (filesystem, git, GitHub, Sentry,
Linear, custom tools).

**Где в репо.**

- Новый модуль: `src/orchx/agent/tools/mcp.py`
- Расширение реестра: `src/orchx/agent/tools/__init__.py:build_tool_registry`
  — добавить tools из MCP-серверов, перечисленных в frontmatter роли:

  ```yaml
  ---
  mcp_servers:
    - name: github
      url: https://api.githubcopilot.com/mcp/
    - name: filesystem
      command: npx
      args: [-y, "@modelcontextprotocol/server-filesystem", "/Users/..."]
  ---
  ```

**Как реализовать.**

1. Использовать готовый Python-клиент: [`mcp`](https://github.com/modelcontextprotocol/python-sdk).
2. При старте воркера — поднять connections ко всем MCP-серверам
   из frontmatter роли.
3. Получить tool-list через `tools/list` RPC, конвертировать в
   OpenAI-tool-schema, **префиксовать имена** (`github__list_issues`,
   `fs__read_file`) чтобы не конфликтовать с native tools.
4. При вызове LLM'ом `github__list_issues` — проксировать в MCP
   через `tools/call`.

```python
# src/orchx/agent/tools/mcp.py
import asyncio
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from . import Tool, ToolContext, ToolResult


class MCPProxyTool(Tool):
    def __init__(self, server_name: str, mcp_tool_def: dict, session):
        self.name = f"{server_name}__{mcp_tool_def['name']}"
        self.description = mcp_tool_def.get("description", "")
        self.parameters = mcp_tool_def.get("inputSchema", {"type": "object"})
        self._session = session
        self._mcp_tool_name = mcp_tool_def["name"]

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        result = await self._session.call_tool(self._mcp_tool_name, kwargs)
        content_pieces = []
        for piece in result.content or []:
            if hasattr(piece, "text"):
                content_pieces.append(piece.text)
        return ToolResult(content="\n".join(content_pieces), is_error=result.isError)


async def build_mcp_tools(mcp_configs: list[dict]) -> list[Tool]:
    """Поднять MCP-сессии и сконвертировать tools в orchX-Tool'ы."""
    tools = []
    stack = AsyncExitStack()
    for cfg in mcp_configs:
        params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []))
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        for t in tools_resp.tools:
            tools.append(MCPProxyTool(cfg["name"], t.model_dump(), session))
    return tools, stack
```

**Acceptance.**

- В тестовом prompt'е роль с `mcp_servers: [github]` видит tool
  `github__list_issues` и может его вызвать.
- Удаление `mcp_servers` из frontmatter убирает эти tools (LLM их не видит).
- При падении MCP-сервера worker получает clear error, не падает в panic.

**Источник.** OpenHands (MCP интегрирован нативно), Ruflo (314 MCP tools).

### P1.2 Docker-runtime plugin (опциональная sandboxed-изоляция)

**Что делаем.** Дополнительный runtime-plugin: воркер запускается **внутри
container'а**. Это даёт:

- Защиту от malicious-кода в untrusted-задачах.
- Reproducible-окружение (тестируем на CI: всегда тот же python).
- Чистый rollback (контейнер удаляется после).

**Где в репо.**

- Новый модуль: `src/orchx/plugins/runtimes/docker.py`
- Docker-compose шаблон: `src/orchx/templates/runtime/Dockerfile.worker`

  ```dockerfile
  FROM python:3.13-slim
  RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep curl && rm -rf /var/lib/apt/lists/*
  RUN pip install --no-cache-dir openai pyyaml
  WORKDIR /workspace
  ENTRYPOINT ["python", "-m", "orchx.agent.worker"]
  ```

**Как реализовать.**

1. `DockerRuntime` имплементирует `RuntimePlugin`.
2. `spawn_worker` → `docker run --rm -v <worktree>:/workspace -v
<repo_root>:/repo:ro --network none orchx-worker:latest --role
<role> --prompt-file /workspace/.orchx/prompt.txt`.
3. Сама роль (`worker.py`) добавляет stdin-приём prompt'а / JSON-config'а.
4. Permissions становятся **жёстче**: `--network=none`,
   `--cap-drop=ALL`, `--read-only` для repo_root, RW только для worktree.

**Acceptance.**

- `--runtime docker` запускает worker'а в контейнере.
- worker не может писать в `/repo` (read-only mount).
- worker не имеет network (если ему явно не дали `network: host`).
- Все остальные orchX-фичи (acceptance, merge, PR) работают без изменений.

**Источник.** OpenHands (DockerWorkspace по умолчанию для production), AO
(runtime-docker plugin).

### P1.3 Cost dashboard + budget enforcement per role/run

**Что делаем.** Сейчас orchX отслеживает только `input_tokens` и
`output_tokens` в `WorkerOutcome`. Добавляем:

- **Сумму $$$** через таблицу цен моделей.
- **Per-run budget** в `global_budget.max_cost_usd` (новое поле).
- **Per-role budget** в `OrchXConfig` (опциональный).
- **Live в TUI** колонка «cost so far».
- **PR body** — summary блок «Token usage: X input / Y output, ~$Z».
- **Notifications** при превышении 50%/75%/90% бюджета.

**Где в репо.**

- Новый файл: `src/orchx/cost.py` (таблица цен + maybe загрузка из
  `https://api.openrouter.ai/api/v1/models`).
- Расширение `models.py:GlobalBudget`:

  ```python
  @dataclass(frozen=True)
  class GlobalBudget:
      max_parallel: int = 6
      max_wall_seconds: int = 7200
      max_total_retries: int = 10
      max_replans: int = 3
      max_cost_usd: float | None = None  # None = no limit
  ```

- Расширение `OrchXContext` — `total_cost_usd: float`.
- Расширение supervisor: enforcement бюджета (как сейчас wall-time).

**Acceptance.**

- В `summary.json` появляется поле `cost`:

  ```json
  {
    "cost": {
      "total_usd": 4.231,
      "by_role": {"implementer": 1.2, "reviewer": 0.8, "planner": 0.4, ...},
      "by_task": {...}
    }
  }
  ```

- При превышении `max_cost_usd` orchX останавливается и открывает PR с
  пометкой `orchX[budget-exceeded]:`.
- Slack/Discord notification на `cost_alert_threshold` (50/75/90%).

**Источник.** Ruflo (`ruflo-cost-tracker` plugin), OpenHands
(встроенная telemetry).

### P1.4 Web dashboard (минимальный, опциональный)

**Что делаем.** Опциональный HTTP-server (FastAPI), отдающий live-state
прогона: phases, tasks, attempts, logs, cost. По-default'у — выключен;
включается через `orchx all --dashboard :8421`.

**Где в репо.**

- Новый каталог: `src/orchx/web/`
  - `server.py` (FastAPI app, REST endpoints + WebSocket для live updates)
  - `static/` (single-file HTML+JS, без сборки — vanilla или
    [HTMX](https://htmx.org)+Alpine.js для минимализма; не Next.js, не
    React).
- Endpoints:
  - `GET /api/runs` — список прогонов
  - `GET /api/runs/<task_id>` — полный state
  - `GET /api/runs/<task_id>/tasks/<task_id>/logs?attempt=N` — лог attempt'а
  - `WS /api/runs/<task_id>/events` — server-sent events про phase
    transitions, task completions, merges, reviewer findings.
- Integration с orchestrator через **publish/subscribe**: при каждом
  state-update orchestrator publish'ит event, web-сервер форвардит
  в WebSocket.

**Acceptance.**

- `orchx all "..." --dashboard :8421` поднимает HTTP на `:8421`.
- Открыть `http://localhost:8421` — увидеть live-доску фаз и задач.
- При завершении worker'а доска обновляется без F5.
- Опционально работает headless: `--dashboard 127.0.0.1:8421 --no-tui`.

**Источник.** AO (Next.js kanban dashboard), OpenHands (REST + WebSocket
agent-server).

### P1.5 Notification plugins (Slack / Discord / Webhook)

**Что делаем.** Через P0.2 plugin-system: имплементировать
`SlackNotifier`, `DiscordNotifier`, `WebhookNotifier`.

**Где в репо.** `src/orchx/plugins/notifiers/{slack,discord,webhook}.py`

**События для notification.**

- `run_started` (task_id, summary, total_phases, total_tasks)
- `phase_completed` (phase_id, duration, tasks_succeeded, tasks_failed)
- `phase_failed` (phase_id, reasons, will_replan)
- `replan_triggered` (replan_count, max_replans)
- `pr_opened` (pr_url, marker если failed)
- `cost_alert` (current_usd, budget_usd, threshold)
- `budget_exceeded` (total_usd, max_cost_usd)
- `wall_budget_exceeded` (elapsed_s, max_wall_seconds)

**Acceptance.**

- В `.orchx/config.yaml` указан `notifiers: [slack]` с
  `SLACK_WEBHOOK_URL` в env.
- При старте → в Slack приходит сообщение «orchX started: <task_id>».
- При opening PR → «orchX opened PR + ссылка».
- При cost-alert (75% budget) → warning в Slack.

**Источник.** AO (notifier plugins built-in).

### P1.6 LSP-based code intelligence tools

**Что делаем.** Заменить (или дополнить) `grep`/`codesearch` на
**настоящие LSP-symbol tools**:

- `find_symbol` — найти класс/функцию/метод по name path
  (`MyClass/my_method`).
- `find_references` — найти все usages символа.
- `find_implementations` — найти все реализации интерфейса.
- `rename_symbol` — переименование с обновлением всех usage'ов.

Это уже планировалось в TODO `docs/internals.md` (TASK-6).

**Где в репо.**

- Новый модуль: `src/orchx/agent/tools/lsp.py`
- Использовать `pylsp` для Python, `typescript-language-server` для TS.
- Интеграция: спавним LSP по требованию (если в `permission` роли
  есть `lsp: allow`).

**Acceptance.**

- В debugger frontmatter `lsp: allow` — debugger видит `find_references`,
  `find_symbol`, `rename_symbol`.
- На задаче «переименуй `getCwd` в `getCurrentWorkingDirectory` во всём
  проекте» rename_symbol работает за один step, без edit + grep по всем
  файлам.

**Источник.** OpenHands (LSP-tools в agent-server), Ruflo (через MCP).

### P1.7 Browser-tool (Playwright)

**Что делаем.** Tool `browser` для UI-тестирования и веб-скрапинга:

- `browser.goto(url)`
- `browser.click(selector)`
- `browser.fill(selector, text)`
- `browser.screenshot()` → base64 PNG
- `browser.evaluate(js)` → возврат JSON

**Где в репо.**

- Новый модуль: `src/orchx/agent/tools/browser.py`
- Подсхема разрешений:

  ```yaml
  browser:
    allowed_domains:
      ["localhost:*", "127.0.0.1:*", "https://staging.example.com"]
    headless: true
    screenshot_dir: ".orchx/screenshots"
  ```

- Использовать `playwright` Python-SDK.

**Acceptance.**

- В роли `tester` с `browser: allow + allowed_domains: [localhost:5173]`
  можно прогонять задачу «открой страницу, заполни форму, проверь
  результат».
- Screenshot складывается в `runs/<task_id>/screenshots/`.

**Источник.** OpenHands (browser-tool built-in).

### P1.8 PR auto-fixup chain (followup convertor)

**Что делаем.** Сейчас reviewer-findings попадают только в PR body /
summary.json. Реализуем то, что в TODO (`docs/internals.md`):

> Auto-конвертация blocking-findings финального reviewer'а в новые
> debugger-задачи (сейчас они попадают только в `summary.json` / PR
> body, но в DAG автоматически не добавляются).

**Где в репо.**

- `src/orchx/orchestrator/review.py` (после P0.1)
- Логика: после reviewer'а в `summary.json` для каждого finding с
  `severity: blocking` создать new TaskSpec:

  ```python
  TaskSpec(
      id=f"fix-{finding_id}",
      agent="debugger",
      depends_on=(),
      goal=f"Fix blocking finding: {finding.description}",
      file_scope=(finding.file,),
      acceptance=(
          AcceptanceCheck(type="command", command=finding.failure_scenario),
      ),
      max_retries=2,
      timeout_seconds=900,
  )
  ```

- Опциональный flag `--no-auto-fixup` отключает поведение.

**Acceptance.**

- Если reviewer вернул 3 blocking findings — после первого прохода
  reviewer'а orchx спавнит 3 follow-up задачи через `debugger`.
- После их merge'а — reviewer запускается повторно (1 раз).
- В PR body появляется секция «Fixed by orchX-debugger follow-up:
  количество findings».

**Источник.** AO (reactions auto-debugger), Ruflo (TeammateIdle hook).

## P2 — стратегические улучшения

### P2.1 Cleanup завершённых worktree-ов

**Что делаем.** Сейчас все worktree остаются до конца прогона. На
больших ТЗ это десятки гигабайт. Реализовать то, что в TODO:

> Удалённые worktree-ы для уже завершённых задач занимают диск до конца
> прогона. Можно было бы убирать их после успешного merge в integration.

**Где в репо.** `src/orchx/orchestrator/merge.py` — после успешного
`_merge_into_integration`:

```python
if config.cleanup_worktrees_after_merge:
    await worktree.remove_worktree(ctx.repo_root, state.worktree_path)
    state.worktree_path = None  # ставим маркер «уже удалён»
```

CLI флаг: `--cleanup-worktrees` (по умолчанию off, для debug удобнее
оставлять).

**Acceptance.** После прогона на 30 задачах с `--cleanup-worktrees`
дискового места не осталось забрано worktree'ями (видно через
`du -sh runs/<task_id>/worktrees`).

### P2.2 Mid-phase replan через supervisor

**Что делаем.** Сейчас replan вызывается только после полного провала
фазы. Реализовать то, что в TODO:

> Mid-phase replan: сейчас replan вызывается только после полного
> провала всех retry'ев фазы. Прерывание прямо в середине фазы
> (например, через сигнал от supervisor'а) — не поддерживается.

**Где в репо.** `src/orchx/orchestrator/supervisor.py` (после P0.1).
Логика:

- Supervisor каждые `supervisor_interval_s` проверяет:
  - Есть ли воркер, висящий > 2 × `timeout_seconds` от плана?
  - Есть ли воркер, тратящий > 2 × медианной стоимости остальных?
  - Есть ли паттерн «много retry'ев в задаче X»?
- Если да → шлёт signal в orchestrator: «abort phase, trigger replan».
- Replan получает дополнительный контекст: «прерван supervisor'ом
  по причине X».

**Acceptance.** Тест: задача с `timeout_seconds: 30`, но воркер
зацикливается. Через 60s supervisor триггерит abort+replan; planner
получает «task X was killed by supervisor: ran 2× timeout».

### P2.3 Federation (cross-machine orchestration)

**Что делаем.** Для команд, где разные сервисы живут в разных репо
(monorepo не подходит): один orchX-process управляет несколькими
«remote» orchX-instances через REST API.

**Где в репо.** Расширение `src/orchx/web/server.py`. Новые endpoints:

- `POST /api/runs` — принять plan.json от remote orchx, запустить
  локально.
- `GET /api/runs/<task_id>/status` — статус для remote
  poll'а.

Federation-config:

```yaml
federations:
  backend:
    url: https://orchx-backend.internal:8421
    auth_token: ${ORCHX_BACKEND_TOKEN}
  frontend:
    url: https://orchx-frontend.internal:8421
    auth_token: ${ORCHX_FRONTEND_TOKEN}
```

Тогда в plan.json можно указать `federation: backend` для задачи, и
orchestrator проксирует её на remote-instance.

**Acceptance.** orchX-instance на машине A с plan'ом, где task X имеет
`federation: backend` — реально исполняется на машине B; результат
возвращается, merge'ится в A.

**Источник.** Ruflo (agent federation с mTLS).

### P2.4 Cross-session learning (расширение P0.3)

**Что делаем.** Поверх SQLite-memory из P0.3:

- **Reinforcement signal**: если задача провалилась и план был похож на
  историческую — снижаем «score» исторического паттерна.
- **Pattern templates**: planner может «достать» из памяти full plan
  для «backend feature with migration» и адаптировать.
- **Embedding-search**: использовать embeddings (OpenAI / Cohere) для
  semantic-retrieval, не только FTS.

**Acceptance.** После 10 прогонов того же типа planner показывает в
своём логе «matched historical pattern: <task_id> (similarity 0.87)»
и генерирует план быстрее (≤ 30s вместо 60s+).

**Источник.** Ruflo (SONA + ReasoningBank + EWC++).

## P3 — continuous improvements

### P3.1 Полноценная документация

- `docs/architecture.md` — диаграммы (mermaid), описание modules.
- `docs/contributing.md` — как добавить plugin, как запустить тесты.
- `docs/changelog.md` — release notes.
- `docs/api/` — API reference (`pdoc` или `sphinx`).
- `docs/recipes/` — готовые примеры:
  - «Прогнать orchx на vibe-coded MVP-проекте»
  - «Использовать orchx в GitHub Actions»
  - «Custom-плагин нотификатора»
  - «Embed orchx в собственный CLI»

### P3.2 Examples

- `examples/hello-world/` — минимальный проект + готовый plan.json.
- `examples/backend-fastapi/` — миддл-проект с PHASED-планом.
- `examples/with-docker-runtime/`
- `examples/with-mcp-server/`

### P3.3 Community

- Slack / Discord канал.
- GitHub Discussions включить.
- Roadmap doc в `docs/roadmap.md` (этот файл — её первый кирпич).
- `CODE_OF_CONDUCT.md`.

### P3.4 Локальные оптимизации

- **`models.py` → пакет.** Сейчас 679 строк в одном модуле — разнести
  на `plan.py`, `acceptance.py`, `result.py`, `budget.py`.
- **`cli.py` → пакет.** 1105 строк, можно разнести по subcommands:
  `cli/plan.py`, `cli/run.py`, `cli/list.py`, `cli/logs.py`,
  `cli/watch.py`.
- **`pr.py` → разнести.** PR builder отдельно от gh-командной обёртки.
- **`agent/llm.py:LLMConfig.from_env`** → перенести env-loading в
  отдельный модуль `config/env.py` (для testability).
- **Заменить `dataclass` на `pydantic`** для `Plan`/`TaskSpec`/`TaskResult`
  и т.п. — даст runtime validation, JSON-serialization out-of-the-box,
  более понятные error messages.

## Сводный roadmap (визуально)

```text
0.1.x — текущий: standalone Python CLI batch-tool

  ├── 0.2.0 (P0):
  │   ├── refactor orchestrator.py (P0.1)
  │   ├── plugin-slot system (P0.2)
  │   ├── memory MVP — SQLite (P0.3)
  │   ├── PR feedback loop (P0.4)
  │   └── CI + coverage 70%+ (P0.5)
  │
  ├── 0.3.0 (P1):
  │   ├── MCP-bridge (P1.1)
  │   ├── Docker-runtime (P1.2)
  │   ├── cost dashboard (P1.3)
  │   ├── web dashboard MVP (P1.4)
  │   ├── notification plugins (P1.5)
  │   ├── LSP tools (P1.6)
  │   ├── browser tool (P1.7)
  │   └── auto-fixup chain (P1.8)
  │
  ├── 0.4.0 (P2):
  │   ├── worktree cleanup (P2.1)
  │   ├── mid-phase replan (P2.2)
  │   ├── federation (P2.3)
  │   └── cross-session learning (P2.4)
  │
  └── 1.0.0 (P3): полная документация, examples, community, packaging
```

## Приложение А: сводный «what to copy from whom»

| Что копируем                                            | Откуда                        | Куда в orchX                                                                          |
| ------------------------------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------- |
| Чёткое разделение SDK / tools / workspace / server      | OpenHands                     | P0.1 refactor + P1.4 web-server                                                       |
| Skill / Condenser / Security как built-in concepts      | OpenHands                     | Skill — будущий P2; Condenser уже есть (compaction); Security — расширить permissions |
| 4-package install profile                               | OpenHands                     | `pip install orchx[server]` / `[docker]` / `[mcp]` через extras                       |
| Microagents (markdown + YAML trigger frontmatter)       | OpenHands                     | Расширить frontmatter prompts (триггеры по keyword'у задачи)                          |
| Plugin-slot architecture (runtime / tracker / notifier) | ComposioHQ/agent-orchestrator | P0.2                                                                                  |
| Reactions (ci-failed / changes-requested / approved)    | ComposioHQ/agent-orchestrator | P0.4                                                                                  |
| Hash-based namespacing (`{hash}-{projectId}`)           | ComposioHQ/agent-orchestrator | Опционально, для multi-project P2.3                                                   |
| Convention over configuration                           | ComposioHQ/agent-orchestrator | Везде где можно автодедуцировать                                                      |
| Auto-derived paths                                      | ComposioHQ/agent-orchestrator | `paths.py` уже хорош, расширить для multi-instance                                    |
| Vector memory (AgentDB + HNSW)                          | Ruflo                         | P0.3 + P2.4                                                                           |
| Cross-session learning (SONA + ReasoningBank)           | Ruflo                         | P2.4                                                                                  |
| 3-tier model routing (Agent Booster → Haiku → Sonnet)   | Ruflo                         | Расширить `effort` mapping (P3)                                                       |
| Cost tracker plugin                                     | Ruflo                         | P1.3                                                                                  |
| Agent federation (mTLS + PII pipeline)                  | Ruflo                         | P2.3 (упрощённая версия)                                                              |
| Background workers (audit / testgaps / optimize)        | Ruflo                         | Расширение supervisor'а (P2.2)                                                        |
| AIDefence (prompt injection detection)                  | Ruflo                         | P3 (security hardening)                                                               |
| MCP server / client integration                         | OpenHands + Ruflo + AO        | P1.1                                                                                  |
| Docker workspace isolation                              | OpenHands + AO                | P1.2                                                                                  |
| Web dashboard + WebSocket events                        | OpenHands + AO                | P1.4                                                                                  |
| Browser tool (Playwright)                               | OpenHands                     | P1.7                                                                                  |
| LSP-symbol tools                                        | OpenHands                     | P1.6                                                                                  |
| Multi-project orchestration                             | AO + Ruflo (federation)       | P2.3                                                                                  |
| Design system + UX                                      | AO                            | P3 (когда появится web dashboard)                                                     |

## Приложение Б: чего **не** копировать

Некоторые вещи у конкурентов сделаны хорошо, но **для orchX они
contraproductive**:

- **Ruflo's complexity.** 314 MCP tools, 100+ agents, 33+ plugins —
  огромная surface area, тяжёлая onboarding-кривая. orchX должен
  остаться **минимально-инвазивным CLI**.
- **OpenHands's heavy GUI / multi-user infrastructure.** orchX —
  batch-tool; не нужно строить полноценный SaaS.
- **AO's TypeScript stack.** orchX — Python; переход на Node не даёт
  выгоды.
- **Ruflo's «1 message = all operations» MCP-stress-pattern.** Это
  стиль Claude-Code prompt'инга — у orchX другая модель.
- **OpenHands's MicroAgents auto-loading without user explicit
  request.** Может создавать неявный context-bloat; в orchX лучше
  оставить явные frontmatter triggers.
- **Ruflo's IPFS plugin registry.** Overkill для compact-tool; обычный
  entry-points + PyPI достаточно.

## Финальный вывод

orchX — **отличный foundation**: declarative-plan + PHASED-checkpoints +
3-state reviewer + auto-replan = уникальные фичи, которых нет ни у
кого. Что нужно — это **операционная зрелость**: расширяемость
(plugin slots), feedback loop (PR reactions), память (vector RAG),
runtime изоляция (docker), наблюдаемость (web dashboard + cost +
notifications).

Roadmap из 0.2.0 → 1.0.0 закрывает большинство пробелов **без потери
характера orchX** — он остаётся headless batch-CLI для git-проектов с
declarative-plan'ом, но получает то, что критично для production-use.
