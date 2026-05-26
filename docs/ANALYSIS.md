# Анализ работы orchX по задаче `hidden-review-auto-success` (GH #114)

> Источник: `.orchx/runs/hidden-review-auto-success/{orchx.log, planner.log, plan.json, summary.json, logs/*, dispatcher.log}` + `memory.db`.
>
> Итог: 10/10 tasks SUCCESS, 2 retries, 1 merge-conflict, 1 replan = 0. Wall-time **42 минуты** (2541 s), стоимость **$134**, 26.3 M токенов, 418 LLM-вызовов. Auto-review нашёл 9 non-blocking + 2 nit, blocking = 0.

## 1. Качество плана (планировщик)

### 1.1 Что плохо

| Проблема                                                                                | Где                                                                                                          | Цена                                                                                                                                                                                                                     |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Избыточная декомпозиция** на 10 атомарных задач                                       | `plan.json`                                                                                                  | +25 LLM-агентов, ~$80 явно лишних                                                                                                                                                                                        |
| **Жёсткая последовательность** там, где её нет                                          | p1.runbook-migration → p1.migration, p2.cron-hidden-tests → p2.cron-hidden-success, p4.docs → p4.api → p4.db | Из 10 задач реально могло идти параллельно **минимум 6** одновременно (миграция + биллинг + sync_service + db_service независимы по `file_scope`)                                                                        |
| **Фаза = искусственный барьер**                                                         | 4 фазы по 2-3 задачи                                                                                         | После фазы 1 фаза 2 ждёт, хотя `cron-hidden-success` (endpoints.py) **не зависит** от файла миграции — он зависит только от _знания о новом столбце_. Можно было сделать 1 фазу = 1 уровень параллелизма                 |
| **Runbook отделён от миграции**                                                         | p1.runbook-migration — отдельная задача с собственным агентом                                                | Это **один и тот же** артефакт смысла «миграция 238»: SQL + runbook. Один implementer на один read-write проход сделал бы оба за 40-60 s вместо 90 s + 90 s + retry                                                      |
| **Тесты отделены от кода**                                                              | `cron-hidden-tests` (отдельный tester), `billing-resume-tests` (отдельный tester)                            | Тестировщик начинает с пустого контекста, заново читает `endpoints.py` (6303 строки), пытается понять что туда положил implementer — это самые дорогие задачи в run ($40 + $11). Implementer мог бы написать тесты сразу |
| **Docs отделены от кода**                                                               | p4.docs-hidden-review — отдельный последовательный таск                                                      | Тоже заново читает всё, что уже читали 5 предыдущих агентов                                                                                                                                                              |
| **fan-out плана = 4 (config)**, но **реально достигнут максимум 2** в одном уровне (p3) | p3 единственная имеет 2 задачи в level 0                                                                     | Бюджет `max_parallel=4` простаивает 90% времени                                                                                                                                                                          |

### 1.2 Что хорошо

- Все 10 acceptance-блоков **executable** (file_exists, file_contains regex, `python -m py_compile`) → планировщик заранее знает, как проверять.
- Корректно определена зависимость `analytics-api-schema → db-service-analytics` (контракт ответа не построить без агрегатов).
- Корректно определена зависимость `docs-hidden-review → {db-service-analytics, analytics-api-schema}` — документация описывает уже существующий контракт.
- `file_scope` дисциплинирует воркеров и сокращает конфликты до 1 (см. §3).

### 1.3 Оптимальный план

Идеальная форма для этой задачи (на основе постановки в issue #114):

```text
phase-1 (всё параллельно, 4 воркера):
  ├── migration+runbook (один implementer: 238_*.sql + runbook .md) [file_scope: миграция + runbook]
  ├── cron-hidden-success+tests (один implementer: endpoints.py + test_cron_hidden_review.py) [file_scope: endpoints.py + test файл]
  ├── billing-include-hidden+resume-on-new-message+billing-tests (один implementer: billing.py + sync_service.py + test_billing_hidden_review.py)
  └── analytics+dashboard+db_service+docs (один implementer: db_service.py + analytics.py + dashboard.py + docs/backend/*.md)
```

= **4 implementer-таска вместо 10**, фактический параллелизм **4** вместо **1-2**. Ожидаемое время ~10-15 минут вместо 42, стоимость ~$40 вместо $134.

## 2. Поведение агентов — повторяющиеся паттерны

### 2.1 Каждый воркер заново читает одни и те же файлы

| Файл                                                                       | Сколько раз прочитан воркерами                                                                                                                           |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/api/endpoints.py` (6303 строки)                                   | **5** (planner + cron-hidden-success + cron-hidden-tests×2 + db-service-analytics + analytics-api-schema + docs-hidden-review + reviewer) — фактически 8 |
| `backend/cron/processor.py`                                                | 4+                                                                                                                                                       |
| `backend/services/sync_service.py`                                         | 3                                                                                                                                                        |
| `backend/cron/billing.py`                                                  | 3                                                                                                                                                        |
| `tests/unit_tests/test_billing.py` (как шаблон)                            | 4                                                                                                                                                        |
| Соседний runbook `2026-05-25-notification-reads-migration.md` (как шаблон) | 2                                                                                                                                                        |
| `docs/backend/README.md`                                                   | 3                                                                                                                                                        |

**Причина:** orchX-worktree изолирован, у воркера нет персистентного «знания о проекте». Memory.db (`namespace='plans'`) кладёт только сухой план задачи (1.2 KB JSON), без контекстных фактов уровня «функция X живёт в файле Y, строка Z».

### 2.2 Дублирующиеся поисковые операции

- Почти **каждый** воркер начинает с `read .orchx/task.md`, затем 3-5 `grep`/`codesearch` чтобы найти **уже известное** место (например, `_review_id_for_visibility` находится в endpoints.py:4907 — это упомянуто в issue #114, в task.md task `cron-hidden-success`, и реквизировано как `inputs`, но воркер всё равно делает 2-3 grep'а).
- **Угол:** в `inputs` плана уже перечислены файлы — но в task.md эти inputs воспринимаются как «справочные», а не как «именно эти строки». Не указаны **конкретные line ranges**, поэтому каждый воркер заново их ищет.

### 2.3 Grep tool регулярно возвращает «(no matches)» — фантомные провалы

В каждом логе с большим числом tool-call виден повторяющийся паттерн:

```text
[tool-call] grep            → (no matches)
[tool-call] grep            → (no matches)
[tool-call] bash (cat ...)  → файл существует, паттерн есть
```

| Лог                        | «(no matches)» / Permission denied |
| -------------------------- | ---------------------------------- |
| cron-hidden-tests.attempt1 | **27**                             |
| analytics-api-schema       | 19                                 |
| billing-resume-tests       | 18                                 |
| review                     | 17                                 |
| cron-hidden-success        | 15                                 |

**Это >150 неудачных tool-call'ов из ~440 общих** — ~34% LLM-итераций потрачено на «разведку», которая не возвращает результат. Каждая такая итерация = 1 LLM-вызов с растущим контекстом.

**Причины (по логам):**

1. **Grep tool ловит относительные/абсолютные пути worktree некорректно** (видно по тому, что после неудачного grep'а bash `cat ... | grep` подтверждает наличие паттерна).
2. **Bash-permissions агрессивно блокируют** простые команды: `grep -n "pattern" file` → `prefix='grep' matched wildcard '*' → deny`. Это **системная ошибка allowlist**: документация говорит «допустимы 'grep*', 'find*'», но факт — отказывает.
3. **Command-injection guard ложно срабатывает** на `|`, `&&`, `;` (видно: «`ls foo | head -30` → command injection detected»). Воркер тратит итерации на обходы.

### 2.4 Длинные функции — главный враг агента

`backend/api/endpoints.py` = 6303 строки, `process_cron_batch` ≈ 600+ строк. Воркеры `cron-hidden-success` и `cron-hidden-tests` тратят 30-50% времени на навигацию **внутри одной функции**. Это **проблема кодовой базы**, которая бьёт по агентам сильнее, чем по людям.

### 2.5 Tester дублирует логику вместо вызова реального кода

Это самая дорогая системная проблема. Reviewer её зафиксировал (`tests/unit_tests/test_cron_hidden_review.py:30`):

> Тесты не вызывают `process_cron_batch` из endpoints.py. Вместо этого они переписали логику в локальный helper `_decide_hidden_review_action` и тестируют его.

**Почему так получилось** (видно в `cron-hidden-tests.attempt2.log`, step 24):

> «File scope: JUST tests/unit_tests/test_cron_hidden_review.py. Тесты могут … но идеально, чтобы вызывали реальный код. Простейший подход: написать тесты, которые **воспроизводят логику условий напрямую** (поскольку реальный код живёт inline внутри гигантской функции)»

Tester упёрся в:

- невозможность импортировать `backend.api.endpoints` (heavy import: langchain_core нет в sandbox);
- невозможность изолированно вызвать «эту самую if/elif ветку» (она внутри 600-строчной функции);
- ограничение `file_scope = test файл`, нельзя рефакторить endpoints.py (выделить helper в отдельную функцию).

Это **архитектурное ограничение, заложенное планировщиком**: если бы один и тот же агент писал и код, и тесты, он мог бы выделить helper `_should_auto_close_hidden_review(case_data, vis_row, now) -> bool` в endpoints.py и сразу написать на него тесты.

### 2.6 Поведение на чтении плана

- Planner потратил **163 s** на 19 шагов = адекватно (нужно было реально проверить line-numbers в issue).
- Planner делает `read .orchx/task.md` — **этот файл не существует в момент планирования** (он создаётся для воркеров). Это безвредно, но шумит.
- Planner один раз попадает в `command injection detected` (`ls backups/migrations/ | tail -30`) — обходит через `glob` без проблем.

### 2.7 Кладёт ли воркер хорошие notes? — Да

`summary.json.tasks[].notes` для всех 10 задач — детальные (200-1500 символов), точно описывают что и где сделано, какие acceptance прошли, какие edge-cases учтены. Это **готовый материал для memory.db**, но он туда не попадает.

## 3. Перезапуски агентов: `runbook-migration` и `cron-hidden-tests`

### 3.1 Что произошло

| Task                  | Attempt 1 result                                                                                    | Attempt 2 result                                  | Причина перезапуска                                                                                                                                                                         |
| --------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **runbook-migration** | SUCCESS by implementer (87 s, файл записан, acceptance passed)                                      | SUCCESS by debugger (95 s, файл переписан с нуля) | **Merge conflict / stash failed → дополнительный retry**. orchx.log:17-19: «task runbook-migration MERGE CONFLICT into orchX/hidden-review-auto-success: fatal: stash failed → retry (1/1)» |
| **cron-hidden-tests** | FAILED with `agent exit=125` after **408 s** (15 шагов писал, 25 шагов крутился, 27 пустых grep'ов) | SUCCESS by debugger (243 s, файл написан с нуля)  | **Lost-edits / worktree rebuild**. `cron-hidden-tests.attempt2.log` step 2: `tests/unit_tests/test_cron_hidden_review.py → File not found`. Worktree пересоздан, файл из attempt 1 утрачен  |

### 3.2 Root cause analysis

#### runbook-migration: **проблема merge-фазы, а не воркера**

Воркер всё сделал правильно за 87 s. Но после `orchX-tasks/...` → `orchX/hidden-review-auto-success` merge упал с `fatal: stash failed`. Это типично для случая, когда **integration worktree был dirty** (видимо, .orchx/task.md или .orchx/results/\*.json от предыдущей merge-операции). Debugger при attempt 2:

- видит чистый worktree (новый);
- `ls docs/runbooks/` → файла нет (потерян при reset);
- _признаёт_ это как «classic lost-edits» (step 3) — это даже отражено в системном промпте debugger'а («§workflow.2»);
- переписывает с нуля за 95 s.

**Двойная стоимость:** $0.84 + $1.66 ≈ $2.50 (не критично), но **+3 минуты wall-time** и удвоенный риск регрессий, потому что второй раз пишется не тот же файл (он стал на 1620 байт больше; см. `summary.json` notes).

#### cron-hidden-tests: **fundamental architectural mismatch**

Здесь корень глубже. Attempt 1 (tester):

- 408 секунд, 60+ tool-calls, 27 пустых grep'ов;
- exit code **125** (orchX-специфичный — превышен max_steps=60? таймаут? кеш токенов?);
- ничего не записал в worktree.

Прочитав attempt1 log полностью (см. ниже), видна следующая последовательность:

1. **step 1-15**: tester читает task.md, endpoints.py, processor.py, test_billing.py. Понимает, что логика inline внутри `process_cron_batch`.
2. **step 15-30**: пытается понять как мокать. Sandbox не позволяет импортировать `backend.api.endpoints` (langchain_core not installed). Tester мечется: написать тесты, которые «дублируют логику», или попробовать импортировать через monkeypatch.
3. **step 30-39**: пишет 15 KB файла теста с ExitStack-патчами всех зависимостей. Делает 6 рефакторингов через `edit`.
4. **step 40-58**: пытается проверить через `grep`/`codesearch` — всё возвращает `(no matches)`. Пытается через `bash uv run pytest` — `Permission denied: 'uv run' matched wildcard '*' → deny`.
5. **step 58-60+**: **превышает max_steps (60) или timeout** → exit 125 → **никакой write на .orchx/results/, никаких commit'ов**.

Attempt 2 (debugger, effort=xhigh) делает за 243 s **другую стратегию**: пишет тесты как unit-тесты на helper, который **дублирует** бизнес-логику в самом тестовом файле. Это **тоже неправильно** (reviewer это поймал), но проходит acceptance (file_contains + py_compile).

### 3.3 Как избежать в будущем

| Проблема                                                                | Фикс                                                                                                                                                                                                                                                 |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Merge conflict из-за dirty integration worktree** (runbook-migration) | Перед merge всегда `git -C _integration reset --hard HEAD && git clean -fdx .orchx/` ; либо merge через `git merge --no-ff` без локального stash                                                                                                     |
| **Lost-edits при retry**                                                | Перед retry **сохранить .orchx/results/\*.json и worktree-файлы предыдущей попытки** в `.orchx/runs/.../snapshots/<task>.attempt<N>/`. Debugger должен начать с `cp -r snapshot/* worktree/`, а не с пустого worktree                                |
| **Tester упирается в недоступный реальный код**                         | Объединить implementer + tester в одной задаче (см. §1.3). Или дать tester'у право расширять `file_scope` на исходный файл — чтобы выделить тестируемый helper                                                                                       |
| **Heavy imports в sandbox**                                             | Заранее на bootstrap'е orchX-run выполнять `uv sync --frozen` в одном общем venv; либо разрешить tester'у запускать `mypy backend/api/endpoints.py` (это работает без langchain runtime) и тестировать через type-проверки + smoke import-only тесты |
| **max_steps=60 для tester мало** для гигантских функций                 | Поднять до 100 для tester или сделать adaptive: если task content > 1500 символов, дать +20 шагов                                                                                                                                                    |
| **27 пустых grep-ов подряд**                                            | Tooling-фикс: если 3 grep'а подряд вернули «(no matches)», агент должен **сменить стратегию** (read + glob). Подсказка от системы или fail-fast политика                                                                                             |

## 4. Использование `memory.db`

### 4.1 Что фактически положено

```sql
SELECT namespace, key, length(value) FROM memories;
-- plans   | hidden-review-auto-success | 1179 байт
-- reviews | hidden-review-auto-success | 3697 байт
```

**Всего 2 записи** на завершённую задачу. И обе кладутся **постфактум** (после `all phases completed`), а не во время работы.

### 4.2 Что не используется

- Нет namespace `task_notes`, `file_context`, `code_locations`, `decisions`.
- Memory.db **не читается** агентами в процессе работы (нет `serena_read_memory`-вызовов в логах воркеров).
- Богатые `notes` (1500+ символов на task) **остаются только в summary.json** конкретного run'а — следующий run на похожую тему их не увидит.
- Embedding столбец есть в схеме, но **NULL** для обеих записей (никакого семантического поиска).
- FTS5-индекс есть (`memories_fts`), но **никто не делает search**.

### 4.3 Что должно быть

| Что класть в memory.db                                                                                                       | Когда                                           | Кто читает                                         |
| ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | -------------------------------------------------- |
| `file_locations` namespace: «`process_cron_batch` → endpoints.py:4665, `_review_id_for_visibility` → endpoints.py:4907»      | После каждой успешной задачи извлекать из notes | Все будущие воркеры (планировщик в первую очередь) |
| `architecture_facts`: «миграции в backups/migrations/NNN\_\*.sql, нумерация 3-digit, последняя 237 на момент задачи 114»     | Bootstrap при первом запуске на repo            | Планировщик                                        |
| `test_patterns`: «backend.\* тесты требуют langchain_core в venv; смотри tests/unit_tests/conftest.py для asyncio_mode=auto» | Извлекать из неудачных tester-попыток           | Tester                                             |
| `known_pitfalls`: «.orchx/task.md часто даёт merge conflict при merge → используй -X ours»                                   | После каждого merge-conflict                    | Merger                                             |
| `code_diff_summary` от прошлых runs: «issue 114 → 238_review_hidden_resolved.sql + 6 backend файлов + 2 теста»               | После success run'а                             | Планировщик при похожей задаче                     |

### 4.4 Поведение orchX как целого

Хорошо:

- **0 replan'ов** — план был валидный.
- **0 blocking findings** в auto-review.
- **0 потерянных результатов** (все 10 successfully merged).
- Supervisor каждые 30 s корректно репортит progress.
- Merger корректно разрешил конфликт в `.orchx/task.md` (39 s).

Плохо:

- `dispatcher.log` показывает: «Removed 9 macOS-duplicate file(s) from worktree cron-hidden-tests before commit (would have been wrongly added to integration branch)» — `'admin 2', 'deploy 2', ...`. Это **macOS `.DS_Store`-подобный артефакт от git worktree+iCloud/Finder**. Хорошо, что dispatcher это ловит, но **причина** в том, что worktree создаётся внутри `.orchx/runs/` и Finder/Spotlight его «дублирует». Решение: worktree вне индексируемой Finder'ом директории, например `/tmp/orchx-worktrees/<task_id>/`.
- Supervisor каждые 30 s печатает **identical** строку («elapsed=X counts={...}»). За 42 минуты это 84 строки шума, в которых нет дельты прогресса. Лучше печатать только при изменении counts/cost.

## 5. Как одновременно повысить качество и скорость

Сейчас задача из issue #114 (~250 LOC изменений в коде + миграция + 2 теста + 1 doc + 1 runbook) решена за **42 минуты и $134**.

Для сравнения: один человек-senior с контекстом проекта сделал бы это за **2-3 часа** и был бы дороже. Одна модель той же мощности (`claude-opus-4-7 xhigh`) **без orchX**, прочитав issue #114 → ~15 минут и ~$8-15 (1 длинная сессия с 1 контекстом, без пересоздания worktree).

**Почему orchX в 5-10× медленнее и дороже:**

1. **Холодный старт каждого воркера** — нет кэша контекста, повторное чтение endpoints.py × 5 = ~30 K токенов × 5 = 150 K токенов **только на чтение**.
2. **Избыточная декомпозиция** = 10 LLM-сессий вместо 1-4.
3. **34% tool-вызовов мимо цели** (см. §2.3).
4. **xhigh effort** для reviewer/debugger × 2 retry = ×4 цена.

### 5.1 Конкретные фиксы (по убыванию ROI)

#### A. План: «склеить» родственные задачи (×3 ускорение)

Делать **1 задача = 1 семантический модуль изменения**, а не 1 задача = 1 файл:

- `migration+runbook` → один implementer.
- `cron-code+cron-tests` → один implementer (он же выделит testable helper в endpoints.py).
- `billing-code+resume-code+billing-tests` → один implementer.
- `analytics-stack+docs` → один implementer.

Это даёт **4 параллельных воркера** (соответствует `max_parallel=4` из конфига, который сейчас не используется) и убирает 60% избыточных read'ов.

**Цена:** $40-50 вместо $134. **Время:** 12-15 минут вместо 42.

#### B. Шаблонные tool-аллоулисты (×1.3 ускорение)

В `kilo.json`/`config.yaml` orchX поднять allowlist для воркеров:

```yaml
bash_allowlist:
  - "grep *" # снять текущий ложный deny
  - "grep -rn *"
  - "find *"
  - "wc *"
  - "ls *"
  - "uv run pytest *" # сейчас режется prefix-матчером
  - "uv run mypy *"
  - "uv run ruff *"
disable_command_injection_guard_for: ["grep -n * | head *", "ls * | head *"]
```

Только это уберёт ~25% потерянных итераций.

#### C. Pre-warmed context в task.md (×1.5 ускорение)

В файл задачи, который пишет dispatcher, **встраивать выдержки из inputs**, а не только пути:

```markdown
## Pre-loaded context

### backend/api/endpoints.py:4880-5010 (the \_review_id_for_visibility block)

\`\`\`python
... 130 строк уже вырезаны ...
\`\`\`

### backend/cron/processor.py:CronProcessor.determine_action

... 40 строк сигнатуры + docstring ...
```

Tasker уже знает, какие именно строки релевантны (это есть в issue #114 — «endpoints.py:5311», «endpoints.py:3440», и т.д.). Воркер не делает 5 grep'ов, чтобы их найти заново.

**Стоимость:** один build-time call с `read` по `inputs[]` и `head -200 + grep -A 30` в task.md. Платится один раз, экономит десятки шагов на воркера.

#### D. Memory.db живёт между задачами (×1.4 на повторных задачах)

После каждой задачи писать в memory.db:

- `code_locations.{symbol_name}` = `{file: ..., line_range: ..., last_seen_sha: ...}`
- `task_archive.{task_id}` = `{files_changed: [...], notes: ..., learned_facts: [...]}`
- Embedding всего этого, semantic_search на старте планировщика.

Когда придёт задача #115 «расширить sync_service на новый триггер», планировщик сразу знает: «sync_service.py главный entry — process_chat_batch строка ~1900, паттерн тестов test_billing.py». Воркеры стартуют с готовым контекстом.

#### E. Tester и implementer = одна роль для маленьких задач (×1.5)

Текущая дихотомия «implementer пишет код, tester пишет тесты» вынуждает tester заново разбирать код. Для задач ≤300 LOC объединить.

Если хочется сохранить разделение для review-цели — **давать tester'у read-доступ к worktree implementer'а** (а не пересоздавать чистый worktree).

#### F. Auto-cleanup macOS-артефактов на источнике (×1.05)

Создавать worktree не в `.orchx/runs/` (внутри iCloud/Finder-индексируемой dir), а в `~/Library/Caches/orchx/worktrees/<task_id>/`. Это исключит «`admin 2`/`deploy 2`/…» дубли, которые сейчас отлавливаются на каждом коммите и логируются.

#### G. Поднять effort выборочно (×1.2 по цене)

Сейчас:

- `effort=high` для implementer (= нормально),
- `reviewer_effort=xhigh` (= 2-3× дороже),
- `debugger_effort=xhigh` (= 2-3× дороже).

Reviewer-xhigh оправдан (он нашёл 9 содержательных findings — это качество). Debugger-xhigh **не оправдан** для большинства retry: половина случаев — это «файл потерялся, переписать с нуля» (см. runbook-migration, cron-hidden-tests). High там хватило бы.

### 5.2 Ожидаемый суммарный эффект

Применив A+B+C+D+E:

| Метрика                      | Сейчас            | После                                                                             | Δ    |
| ---------------------------- | ----------------- | --------------------------------------------------------------------------------- | ---- |
| Wall time                    | 42 min            | **12-15 min**                                                                     | −65% |
| Cost                         | $134              | **$35-45**                                                                        | −70% |
| LLM calls                    | 418               | **~120**                                                                          | −70% |
| Tokens                       | 26.3 M            | **~8 M**                                                                          | −70% |
| Tool-call failure rate       | 34%               | **~10%**                                                                          | -70% |
| Reviewer findings (качество) | 11 (9 nb + 2 nit) | **≤5** (за счёт что implementer видит реальный код + тесты вызывают реальный код) | -55% |

## 6. TL;DR — топ-5 действий

1. **Снять prefix-deny с `grep`, `find`, `uv run pytest *`** в allowlist — мгновенный выигрыш −25% потерянных итераций.
2. **Планировщику запретить декомпозицию по `1 файл = 1 task`** — группировать по семантическому изменению (миграция+runbook, код+тесты, и т.д.). 4 задачи вместо 10.
3. **Tester объединить с implementer'ом** для задач, где тестируемый код inline в большой функции. Иначе tester либо упирается в heavy imports, либо дублирует логику (как сейчас).
4. **Memory.db должен жить между задачами**: code_locations, learned_pitfalls, embeddings. Текущие 2 записи на run — статистика, а не знание.
5. **Перед retry сохранять snapshot worktree предыдущей попытки** — `runbook-migration` и `cron-hidden-tests` оба потеряли результат attempt 1 и переписывали с нуля.

## Приложение: статистика по логам

```text
log                                          tool_calls  errors  ratio
cron-hidden-tests.attempt1.log               68          27      40%   (failed, exit 125)
analytics-api-schema.attempt1.log            49          19      39%
billing-resume-tests.attempt1.log            40          18      45%
review__hidden-review-auto-success.log       58          17      29%
cron-hidden-success.attempt1.log             40          15      37%
docs-hidden-review.attempt1.log              36          13      36%
db-service-analytics.attempt1.log            36          10      28%
cron-hidden-tests.attempt2.log               30          12      40%   (debugger)
resume-on-new-message.attempt1.log           21          6       29%
runbook-migration.attempt2.log               17          7       41%   (debugger)
runbook-migration.attempt1.log               11          4       36%
billing-include-hidden.attempt1.log          10          2       20%
resume-on-new-message.merger.attempt1.log    9           1       11%
migration-review-hidden-resolved.attempt1.log 8          1       12%
```

**Средняя ошибка-rate tool calls: 33%**. Доминируют: `grep returns (no matches)` (фантомные провалы) и `Permission denied: prefix matched wildcard '*' → deny`.

**Стоимость по ролям:**

- implementer: $64.34 (48%)
- tester: $50.92 (38%) ← неоправданно высокая доля, из-за 2 attempt'ов на cron-hidden-tests
- reviewer: $18.74 (14%)

**Стоимость по задачам (топ-3):**

- `cron-hidden-tests`: **$40.10** (30% всего бюджета на 1 задачу из 10, из-за retry + xhigh debugger)
- `analytics-api-schema`: $18.33
- `cron-hidden-success`: $13.97
