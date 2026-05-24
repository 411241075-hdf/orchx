# orchX worker tooling — gaps & implementation tasks

Документ для LLM-исполнителя. Все правки — в каталоге `orchx/`.
Стиль кода и тестирования — как в существующих файлах (см. `orchx/agent/tools/*.py`, `orchx/tests/test_tools.py`). Питон 3.14, async-first, типизация обязательна, docstrings Google-style.

## Контекст

Worker'ы orchX — это in-process замена `kilo run --auto`. Реестр инструментов строится в `orchx/agent/tools/__init__.py:80` (`build_tool_registry`). Сейчас доступно ровно 8 tools: `read`, `write`, `edit`, `glob`, `grep`, `codesearch`, `bash`, `todowrite`. Permissions определяются frontmatter'ом в `orchx/prompts/orchX-<role>.md` и парсятся в `orchx/agent/permissions.py`.

Полная инвентаризация и анализ — ниже. Каждая задача оформлена как отдельный block с приоритетом, контекстом, точными правками и acceptance-проверками.

## P0 — критично, ломает текущий workflow

### TASK-1. Sandbox path traversal в read/write/edit/glob

**Проблема.** `orchx/agent/tools/fs.py:15` — `_resolve` принимает абсолютный путь и возвращает его как есть. Воркер может прочитать `/etc/passwd` или записать в `/tmp/foo` вне своего worktree. То же касается `bash` — параметр `workdir` (`orchx/agent/tools/shell.py:97`) резолвится без проверки границ.

**Цель.** Все file-операции и bash должны быть ограничены `ctx.cwd` (worktree воркера). Исключение — `read` для путей внутри `ctx.repo_root`, потому что воркеру может понадобиться прочитать общий `.kilo/INSTRUCTIONS.md` или `AGENTS.md`. Запись/edit — строго внутри `ctx.cwd`.

**Правки.**

1. В `orchx/agent/tools/fs.py` добавить хелпер:

   ```python
   def _ensure_within(path: Path, *, allowed_roots: list[Path]) -> Path | None:
       """Резолвит symlink'и обеих сторон и проверяет, что path внутри одного
       из allowed_roots. Возвращает резолвленный путь или None при escape."""
   ```

   Использовать `Path.resolve()` на обеих сторонах (без `strict=True` — путь может ещё не существовать для write).

2. В `ReadTool.run` пускать через `_ensure_within(p, allowed_roots=[ctx.cwd, ctx.repo_root])`. При escape — `ToolResult(content="Permission denied: path is outside the worker sandbox", is_error=True)`.

3. В `WriteTool.run`, `EditTool.run`, `GlobTool.run` — `_ensure_within(p, allowed_roots=[ctx.cwd])`. Edit/write строго в worktree.

4. В `BashTool.run` (`orchx/agent/tools/shell.py:97`) — если `workdir` задан, проверить, что он внутри `ctx.cwd`. При escape — отказать с тем же 403-style сообщением.

5. Также проверить grep/codesearch (`orchx/agent/tools/search.py:154`) — параметр `path` сейчас тоже резолвится без проверки. Применить то же ограничение `[ctx.cwd, ctx.repo_root]`.

**Acceptance.**

- `orchx/tests/test_tools.py` — добавить тесты:
  - `test_read_blocks_path_outside_repo` — попытка `read /etc/hosts` → `is_error`.
  - `test_write_blocks_path_outside_worktree` — попытка `write ../../foo.txt` (с реальной `tmp_path/sub` как cwd) → `is_error`, файла нет.
  - `test_bash_workdir_must_be_inside_cwd` — `bash` с `workdir="/tmp"` → `is_error`.
  - `test_read_can_access_repo_root_files` — read `<repo_root>/AGENTS.md` через cwd-worktree → success.
- `uv run pytest orchx/tests/test_tools.py -q` — зелёный.
- Symlink'и проверить отдельным тестом: внутри `tmp_path` создать symlink на `/etc`, попытка `read symlink/passwd` → блокировка.

### TASK-2. Расширить bash allow-list под планы planner'а

**Проблема.** `orchx/prompts/orchX-planner.md` стабильно генерирует acceptance-проверки и self-verify-команды вида:

```
python -m pytest tests/foo -q
python -m py_compile backend/X.py
python -c "import ast; ast.parse(open('backend/X.py').read())"
ruff check backend/X.py
mypy backend/X.py --ignore-missing-imports --no-incremental
```

Эти команды **не входят** в bash-allowlist `implementer` / `debugger` / `tester` / `merger`. Sandbox отвергнет их до запуска (`orchx/agent/permissions.py:295` — `bash_check`). В итоге воркер получает «Permission denied» и не может проверить свою же работу.

**Правки.**

В каждом из файлов добавить нужные правила (порядок — самые специфичные первыми, `"*": "deny"` остаётся последним fallback'ом):

`orchx/prompts/orchX-implementer.md` (frontmatter `permission.bash`):

```yaml
"python -m": allow
"python -c": allow
"python3 -m": allow
"python3 -c": allow
"ruff check*": allow
"ruff format*": allow
"mypy *": allow
"node -e*": allow
```

`orchx/prompts/orchX-debugger.md` — то же.
`orchx/prompts/orchX-tester.md` — то же.
`orchx/prompts/orchX-merger.md` — добавить `python -m`, `python -c`, `ruff check*`, `mypy *` (без `pytest`/`vitest` — merger не должен запускать тесты).

`orchx/prompts/orchX-architect.md` — добавить только `python -c` (для smoke-проверок импортов).
`orchx/prompts/orchX-reviewer.md` — без изменений (его дело — читать, не запускать).
`orchx/prompts/orchX-planner.md` — добавить `python -c` для exploratory-проверок.

**Важно про prefix-detection.** `orchx/agent/permissions.py:94` — `extract_command_prefix`. Команды `python -m` и `python -c` имеют двухтокенный prefix (`python` НЕ в `_TWO_TOKEN_COMMANDS`, но первый токен после флага `-m`/`-c` будет позиционным аргументом, а не subcommand'ой). Проверить вручную:

```python
from orchx.agent.permissions import extract_command_prefix
print(extract_command_prefix("python -m pytest tests/foo -q"))
# Сейчас вернёт 'python' (одиночный токен), потому что -m начинается с '-'.
```

Это значит правило `"python -m": allow` — **не сматчится**, потому что prefix будет `python`, а не `python -m`. Нужно одно из двух:

- (А) Добавить `python` и `python3` в `_TWO_TOKEN_COMMANDS` — тогда prefix станет `python -m` / `python -c` / `python tests/...` (последнее — тоже разумно, если хотим разрешить только конкретные подкоманды).
- (Б) Использовать правила `"python*": allow` и `"python3*": allow` — это аллоуит **любую** команду `python`, включая `python /tmp/evil.py`. Слишком широко.

**Решение — (А).** В `orchx/agent/permissions.py:76` добавить `"python"`, `"python3"` в `_TWO_TOKEN_COMMANDS`. Потом allow-list будет работать ожидаемо: `"python -m": allow` сматчится по prefix-у `python -m`.

**Acceptance.**

- `orchx/tests/test_permissions.py` — добавить:
  - `test_python_two_token_prefix` — `extract_command_prefix("python -m pytest")` == `"python -m"`.
  - `test_python3_two_token_prefix` — то же для `python3`.
  - `test_implementer_allows_python_m_pytest` — загрузить frontmatter implementer'а через `frontmatter.load_agent_spec("implementer", repo_root)`, проверить `bash_check("python -m pytest tests/foo -q").allowed` == True.
  - `test_implementer_blocks_arbitrary_python` — `bash_check("python /tmp/evil.py").allowed` — должно зависеть от того, какие правила добавили. Если `"python -m": allow` и `"python -c": allow` — тогда `python /tmp/evil.py` имеет prefix `python /tmp/evil.py` (двухтокенный), не сматчит ни одно правило → deny. Желаемое поведение.
- `uv run pytest orchx/tests/test_permissions.py -q` — зелёный.

### TASK-3. Добавить bash blacklist MCP-инструментов в system prompt

**Проблема.** В `orchx/prompts/orchX-implementer.md:142`, `orchx/prompts/orchX-debugger.md:132` и др. описано, что LLM регулярно пытается вызывать MCP-tools (`5stars_*`, `finland_*`, `turbocards_*`, `langfuse_*`, `images_*`, `*_execute`, `*_upload`, `*_download`). У воркера их нет, и каждая попытка — потерянный step.

В `orchx/agent/prompts.py:41` уже есть строка «You DO NOT have access to MCP servers, sub-agents, web fetch/search». Этого мало — нужен явный список префиксов.

**Правки.**

В `orchx/agent/prompts.py:build_system_prompt` расширить блок «Available tools» так:

```python
f"You DO NOT have access to MCP servers, sub-agents (`task` tool), "
f"web fetch/search, or any kilo-specific skills. Tool names with these "
f"prefixes DO NOT EXIST in this runtime: `5stars_`, `finland_`, "
f"`turbocards_`, `langfuse_`, `images_`, `serena_`, and any name "
f"ending with `_execute`, `_upload`, `_download`, `_analyze_image`, "
f"`_generate_image`. Calling them produces a tool-not-found error and "
f"wastes a step. If the role's prompt references such a tool, ignore "
f"it and use only the tools listed above.\n"
```

**Acceptance.**

- В `orchx/tests/test_worker.py` (или новый `test_prompts.py`) — `test_system_prompt_lists_forbidden_prefixes`: построить system prompt через `build_system_prompt`, убедиться что в строке есть `5stars_`, `finland_`, `turbocards_`, `_execute`, `_upload`, `_download`.

## P1 — продуктивность, не блокирует, но сильно помогает

### TASK-4. Реализовать `task` tool (sub-агенты)

**Проблема.** `Permissions.task` парсится, но никакого `TaskTool` не существует. Implementer/debugger при глубоком исследовании («где все референсы FooClass?») вынужден делать десятки grep-вызовов в основном loop'е, расходуя context window.

**Правки.**

1. Создать `orchx/agent/tools/task.py`:

   ```python
   class TaskTool(Tool):
       name = "task"
       description = (
           "Spawn a sub-agent worker in the same worktree to handle a "
           "self-contained research task (e.g. `find all callers of FooClass`, "
           "`summarize how authentication works in backend/`). The sub-agent "
           "shares the parent's permissions but runs in its own LLM context "
           "and returns a single-message summary. Use for open-ended "
           "exploration that would otherwise pollute your main context."
       )
       parameters = {
           "type": "object",
           "properties": {
               "description": {"type": "string", "description": "1-line task summary."},
               "prompt": {"type": "string", "description": "Detailed task for the sub-agent."},
               "subagent_role": {
                   "type": "string",
                   "enum": ["explore", "general"],
                   "description": "Role profile. `explore` is read-only, `general` allows tool use under parent's permissions.",
               },
           },
           "required": ["description", "prompt", "subagent_role"],
       }
       permission_attr = "task"
   ```

2. В `run`: вызвать `worker.run_agent` рекурсивно с урезанным timeout (например, parent_timeout / 4, минимум 120s) и собственным log-file внутри `ctx.cwd / "orchx" / "subtasks" / f"{name}.log"`. Permissions — копия родителя, но `task=False` (запрет вложенных sub-агентов), `edit=False` для `subagent_role="explore"`.

3. Зарегистрировать в `orchx/agent/tools/__init__.py:build_tool_registry`:

   ```python
   if p.task:
       registry["task"] = TaskTool()
   ```

4. В `prompts.py:build_system_prompt` — упомянуть, что task tool доступен (если в реестре).

5. В frontmatter'ах — по умолчанию НЕ включать (`task: deny`). Включить только в `architect` и `debugger`, где open-ended research реально нужен.

**Ограничения.**

- Глубина рекурсии: максимум 1 уровень (sub-агенты не могут спавнить sub-под-агентов). Реализовать через env-флаг `ORCHX_SUBAGENT_DEPTH` или поле в `ToolContext`.
- Timeout жёсткий (не наследуется свободно от родителя).
- Sub-agent results возвращаются как plain text (последний assistant-message без tool_calls).

**Acceptance.**

- `orchx/tests/test_tools.py` — `test_task_tool_runs_subagent` с моком `LLMClient` (см. `tests/test_worker.py` — там есть pattern мока).
- `test_task_tool_blocks_nested_subagents` — sub-agent не может вызвать `task` снова.
- `test_task_tool_respects_explore_readonly` — для `subagent_role="explore"` в реестре sub-агента нет `write`/`edit`/`bash`.

### TASK-5. Добавить `semantic_search` / `codebase_search` tool

**Проблема.** Architect и planner стартуют со «понять структуру кодовой базы». Regex-grep плохо находит концептуальные паттерны («где обрабатываются ошибки клиента»).

**Решение.** Добавить tool, который делегирует поиск во внешний LLM с функцией «выдай ranked snippets». Реализация — отдельный mini-agent loop без tool-доступа (только read), который получает natural-language query и возвращает форматированный список `path:line_start-line_end` со сниппетами.

**Правки.**

1. Создать `orchx/agent/tools/semantic.py`:

   ```python
   class SemanticSearchTool(Tool):
       name = "semantic_search"
       description = (
           "Find code by natural-language meaning, not regex. Use for "
           "open-ended questions like 'where is JWT validated' or 'find "
           "session-resume logic'. Returns ranked snippets with file:line "
           "ranges. Slower than `grep`; prefer `grep` when you know the "
           "exact symbol/string."
       )
       parameters = {
           "type": "object",
           "properties": {
               "query": {"type": "string", "description": "Natural-language query in English."},
               "path": {"type": "string", "description": "Optional subdirectory to limit search."},
           },
           "required": ["query"],
       }
       permission_attr = "semantic_search"
   ```

2. Реализация — внутренний agent loop с инструментами `read`/`glob`/`grep`/`codesearch`, prompt вида «You are a code search agent. Find snippets relevant to: <query>. Return JSON list of {path, line_start, line_end, reason}». Лимит — max 8 LLM-ходов и 60s wall-clock.

3. Regгистрация в `build_tool_registry` — по флагу `p.semantic_search`.

4. По умолчанию включить в frontmatter `architect`, `planner`, `reviewer` (`semantic_search: allow`).

**Acceptance.**

- Юнит-тест с mock'ом LLM: `test_semantic_search_returns_snippets` — мок возвращает фиксированный JSON, tool парсит и форматирует.
- Интеграционный тест помечен `pytest.mark.live` (не запускается в CI без API-ключа).

### TASK-6. Symbol-aware tools (find_symbol / find_references / rename_symbol)

**Проблема.** `edit` с уникальным `old_string` хорошо для точечных правок, но при рефакторинге («переименовать `getCwd` → `getCurrentWorkingDirectory` в 15 файлах») воркер делает 15 раздельных `edit`-вызовов. У Kilo-CLI есть `serena_*` API, который делает это за один шаг через LSP.

**Решение.** Это большой объём работы (нужен LSP-клиент). На данном этапе предлагаю **не имплементить**, а добавить в `prompts.py:build_system_prompt` явное упоминание паттерна:

```
For multi-file rename refactors, use `grep` to find all occurrences first,
then plan a sequence of `edit replace_all=true` calls grouped by file.
```

Полноценный LSP-tool — отдельный future ticket. Зафиксировать как TODO в `orchx/README.md` секции «Planned tools».

**Acceptance.**

- Соответствующий абзац в system prompt.
- TODO в README.

### TASK-7. Опциональный `webfetch`

**Проблема.** `Permissions.webfetch` парсится, но tool'а нет. Иногда (debugger на экзотической ошибке, planner на новой технологии) реально нужны внешние доки.

**Правки.**

1. Создать `orchx/agent/tools/web.py`:

   ```python
   class WebFetchTool(Tool):
       name = "webfetch"
       description = (
           "Fetch a URL and return its content as Markdown (default) or text. "
           "HTTPS only. Used for reading public documentation. NEVER guess "
           "URLs — use only URLs explicitly provided in the task or found "
           "via cited references."
       )
       parameters = {
           "type": "object",
           "properties": {
               "url": {"type": "string", "description": "Full https:// URL."},
               "format": {"type": "string", "enum": ["markdown", "text"]},
           },
           "required": ["url"],
       }
       permission_attr = "webfetch"
   ```

2. Реализация: `httpx.AsyncClient`, timeout 30s, max content size 256KB. HTML → Markdown через `markdownify` или `html2text` (добавить в `pyproject.toml` опциональную зависимость).

3. Безопасность: блок-лист RFC1918 / link-local IP диапазонов, чтобы воркер не мог сходить в `http://192.168.0.1/` или `http://169.254.169.254/` (cloud metadata endpoint). Список:
   - `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (private)
   - `127.0.0.0/8` (loopback)
   - `169.254.0.0/16` (link-local)
   - `::1`, `fc00::/7`, `fe80::/10` (IPv6)

   Резолвить hostname → IP, проверить против блок-листа, отказать при попадании.

4. По умолчанию `webfetch: deny` во всех frontmatter'ах. Включать вручную.

**Acceptance.**

- `test_webfetch_blocks_private_ip` — попытка `webfetch http://10.0.0.1/` → `is_error`, реального запроса не сделано (мок `httpx`).
- `test_webfetch_blocks_metadata_endpoint` — `http://169.254.169.254/`.
- `test_webfetch_strips_html` — мок ответ возвращает HTML, проверить, что в результате только plain text/markdown.

## P2 — косметика и чистка

### TASK-8. Документировать в README реальные доступные tools

**Проблема.** `orchx/README.md` (если в нём про tools есть) и каждый `orchx/prompts/orchX-*.md` описывают tools неконсистентно. Например, `orchX-implementer.md:130` говорит «Конвенции — в .kilo/INSTRUCTIONS.md», ссылаясь на mypy без `uv run`, но allow-list не пускает.

**Правки.**

1. В `orchx/README.md` добавить секцию «## Worker tools» с таблицей `name | gate | description` (взять из tool.description-полей).
2. В каждом `orchx/prompts/orchX-*.md` в секции «Tools / commands» сослаться на эту таблицу одним абзацем, не дублировать.
3. В `orchx/agent/prompts.py:build_system_prompt` добавить генерацию краткого блока «Tool capabilities» с `description` всех tools реестра — чтобы LLM не гадала по name.

**Acceptance.**

- Visual review.
- `test_system_prompt_includes_tool_descriptions` — проверить, что в результате `build_system_prompt` для каждого имени из реестра присутствует первая строка его `description`.

### TASK-9. Унифицировать формат ошибок permission-denied

**Проблема.** Сейчас одни tools говорят `"Permission denied: write to {rel} is not allowed by this agent's edit-policy"`, другие — `"Permission denied: edit to {rel} is not allowed"`, третьи (bash) — длинное многострочное объяснение с allow-list'ом.

**Правки.**

Один хелпер в `orchx/agent/tools/__init__.py`:

```python
def permission_denied(*, tool: str, target: str, reason: str, hint: str | None = None) -> ToolResult:
    body = f"Permission denied: {tool} on {target} — {reason}."
    if hint:
        body += f"\nHint: {hint}"
    return ToolResult(content=body, is_error=True)
```

Использовать во всех tool'ах. У `bash` — отдельная ветка (с allow-list-listing'ом), но через тот же хелпер.

**Acceptance.**

- Существующие тесты должны продолжать проходить (они проверяют подстроку `"Permission denied"`).
- Новый `test_permission_denied_format` — формат стабильный.

### TASK-10. Truncation накладок в `bash`

**Проблема.** `orchx/agent/tools/shell.py:174` (timeout-ветка) и `:213-220` (success-ветка) обрезают output **дважды** — один раз внутри `_read_stream` через `_TRUNCATION_LIMIT`, второй раз при склейке `out[:_TRUNCATION_LIMIT]`. Безвредно, но запутанно.

**Правки.**

Убрать второй срез — данных и так не больше лимита благодаря `_read_stream`. Заменить на:

```python
display_out = out
if out_truncated:
    display_out += "\n... (stdout truncated at 50KB)"
```

Где `out_truncated = len(out) >= _TRUNCATION_LIMIT - 1` (с погрешностью на размер чанка).

**Acceptance.**

- `test_bash_output_truncation_marker` — проверить наличие маркера на выводе > 50KB.

## Краткая сводка приоритетов

| ID      | Заголовок                                        | Приоритет | Объём (примерно)                                         |
| ------- | ------------------------------------------------ | --------- | -------------------------------------------------------- |
| TASK-1  | Sandbox path traversal                           | P0        | 1 file change + 4 tests                                  |
| TASK-2  | Bash allow-list для python/ruff/mypy             | P0        | 5 frontmatter правок + 1 правка permissions.py + 4 теста |
| TASK-3  | Blacklist MCP-префиксов в system prompt          | P0        | 1 правка + 1 тест                                        |
| TASK-4  | task tool (sub-агенты)                           | P1        | new file + регистрация + 3 теста                         |
| TASK-5  | semantic_search tool                             | P1        | new file + 1 тест                                        |
| TASK-6  | Документировать паттерн refactor через grep+edit | P1        | prompts.py + README                                      |
| TASK-7  | webfetch tool                                    | P1        | new file + опциональная dep + 3 теста                    |
| TASK-8  | Унифицировать описания tools                     | P2        | README + prompts.py                                      |
| TASK-9  | Хелпер permission_denied                         | P2        | минимальный рефакторинг                                  |
| TASK-10 | Чистка double-truncate в bash                    | P2        | shell.py                                                 |

Все P0 — обязательные, без них workflow рассыпается. P1 закрывает реальные пробелы продуктивности. P2 — гигиена.

## Общие правила реализации

1. **Сначала тесты.** Для каждой задачи писать тест(ы) до или вместе с кодом. Тестовый pattern — см. `orchx/tests/test_tools.py`.
2. **Никаких MCP в самих воркерах.** Все task'и не должны добавлять зависимостей на `5stars_*` / `finland_*` / `turbocards_*` MCP-серверы. Они работают на удалённых машинах.
3. **Pin асинхронность.** Все tool'ы async. Не использовать `subprocess.run` напрямую — только `asyncio.create_subprocess_exec`/`_shell` с явным timeout'ом.
4. **Логирование.** Активность tool'а — через `ctx.activity(...)`, чтобы TUI отрисовал live-доску.
5. **Permission-deny — без побочных эффектов.** Tool обязан **сначала** проверить permission, **потом** трогать файловую систему/процесс.
6. **Линтер.** `uv run ruff check orchx/` и `uv run mypy orchx/` должны быть зелёными после каждой задачи.
7. **Изоляция изменений.** Один task = один коммит/ветка/worktree. Не складывать P0+P1 в один коммит.
