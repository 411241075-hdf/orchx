# Changelog

All notable changes to **orchX** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/) (pre-1.0 — minor может ломать API).

## [0.2.0] — 2026-05-25

Большой релиз по roadmap'у из [`docs/recommendations.md`](./recommendations.md).
**Breaking changes**: см. секцию ниже.

### Added (P0 — критические)

- **P0.1**: Декомпозиция `orchestrator.py` → пакет `orchx.orchestrator/`:
  - `context.py` — все state dataclass'ы (`OrchXConfig`, `OrchXContext`, `TaskState`, `PhaseState`, `AttemptInfo`).
  - `logging_utils.py` — append-only журнал прогона.
  - `git_utils.py` — git-обёртки (unmerged files, conflict markers, diff).
  - `supervisor.py` — supervisor loop + budget enforcement + P2.2 hung-task detection.
  - `core.py` — оставшаяся бизнес-логика. Дальнейшая нарезка (phases/retry/merge/review) — после написания e2e-тестов на reviewer-pipeline.
- **P0.2**: Plugin-slot system (5 slots — runtime, tracker, scm, notifier, memory). 10 дефолтных реализаций. Discovery через `importlib.metadata` entry-points. Каждый сторонний пакет может зарегистрировать свой плагин.
- **P0.3**: SQLite + FTS5 memory backend с опциональными embeddings (OpenAI-compatible endpoint).
- **P0.4**: PR feedback loop — `orchx watch <task_id>` + конфиг `reactions:` в `.orchx/config.yaml` (ci_failed → debugger, changes_requested → implementer, approved_and_green → notify/auto-merge).
- **P0.5**: GitHub Actions CI (lint + typecheck + tests py3.11/3.12 на ubuntu+macos). Coverage report. FakeLLMClient для integration-тестов. `Makefile`. 254 tests passing (было 118).

### Added (P1 — важные)

- **P1.1**: MCP-bridge — orchX-воркеры могут подключаться к Model Context Protocol серверам. Frontmatter `mcp_servers:` объявляет server'ы, tools префиксуются `<server>__<name>` чтобы не конфликтовать с native.
- **P1.2**: Docker-runtime plugin + Dockerfile.worker. Опциональный sandboxed runtime через `runtime: docker` в config. `--network none --cap-drop=ALL --read-only` для repo.
- **P1.3**: Cost tracker + per-model price table + budget enforcement. Поле `cost` в summary.json. CLI `--max-cost-usd`. Notifications на 50/75/90% бюджета.
- **P1.4**: Optional web dashboard (`pip install 'orchx[server]'`). FastAPI + Server-Sent Events для live state. Минимальный vanilla HTML+CSS+JS frontend (no React/Vue/build). `orchx dashboard --port 8421`.
- **P1.5**: Notification plugins (Slack, Discord, Webhook). Auto-fan-out через `_CompoundNotifier` при множественных notifiers. События: run*started, phase*_, replan\__, pr_opened, cost_alert, ci_failed, changes_requested, approved_and_green, run_finished, auto_fixup_planned.
- **P1.6**: Symbol-intelligence tools: `find_symbol` (AST для Python, regex для JS/TS), `find_references` (word-boundary regex), `rename_symbol` (Python AST). Opt-in через permission `lsp: allow`.
- **P1.7**: Browser tool (Playwright). Sandbox: by-default только `localhost/127.0.0.1` allowed. Singleton page per worker. Actions: goto/click/fill/screenshot/evaluate/text/close.
- **P1.8**: PR auto-fixup chain — blocking findings reviewer'а конвертируются в follow-up debugger TaskSpec'и (сохраняются в `runs/<task_id>/auto_fixup_plan.json` + notification). v1: file-based; v2: будет автоматическое исполнение через DAG extension.

### Added (P2 — стратегические)

- **P2.1**: `--cleanup-worktrees` — после успешного merge в integration удалять worktree (экономия диска).
- **P2.2**: Supervisor детектирует hung-задачи (висят > 2× своего timeout) и поднимает flag `mid_phase_replan_requested` — потенциальный hook для будущей mid-phase replan интеграции.
- **P2.3**: Federation REST API: `POST /api/runs/spawn`, `GET /api/runs/<id>/status`, `DELETE /api/runs/<id>`. Bearer-token auth через `ORCHX_FEDERATION_TOKEN`.
- **P2.4**: Cross-session learning hook — `_record_run_to_memory` после прогона записывает plans/failures/reviews в memory plugin. Дальше — embedding-search в planner/debugger spawn (P3).

### Added (P3)

- `docs/architecture.md` — полный архитектурный обзор 0.2.
- `docs/contributing.md` — как добавить плагин/tool/тест.
- `docs/changelog.md` (этот файл).
- `docs/comparison.md` — сравнение с OpenHands/Ruflo/AO (4 проекта).
- `docs/recommendations.md` — roadmap, по которому шёл этот релиз.
- `Makefile` для всех dev-задач.
- GitHub Actions CI.
- `pyproject.toml` extras: `test`, `server`, `mcp`, `docker`, `browser`, `memory-embed`, `pydantic`, `all`.

### Changed

- **Public API**: только `OrchXConfig` и `run_orchX` — стабильны.
  `run_orchX` теперь принимает дополнительный keyword-only `plugins: dict[str, Any] | None`. По умолчанию `None` — поведение совместимо с 0.1.0.
- `WorkerOutcome` получил поле `cost_usd: float = 0.0`.
- `Permissions` получил `lsp: bool = False` и `browser: bool = False`.
- `OrchXContext` получил поля: `total_cost_usd`, `cost_by_role`, `cost_by_task`, `memory`, `notifier`, `runtime`, `mid_phase_replan_requested`, `mid_phase_replan_reason`.
- `OrchXConfig` получил: `cleanup_worktrees_after_merge`, `pr_watcher_enabled`, `auto_fixup_chain`, `max_cost_usd`.
- Pyproject версия → 0.2.0.

### Fixed

- Ruff lint: устранён legacy `import pytest as _pytest` в test_tools.py.
- Корректное определение `asyncio_mode = "auto"` для pytest без false-positive PytestWarnings для sync-тестов.

### Breaking

- Минимальная Python версия осталась 3.11. **Python 3.14**: editable-install требует `--config-settings editable_mode=compat` (см. Makefile, `make install`).
- `orchx watch` и `orchx plugins list` и `orchx dashboard` — новые CLI commands.
- Новые CLI флаги: `--cleanup-worktrees`, `--max-cost-usd`, `--no-auto-fixup` (на `run` / `all`).
- `WorkerOutcome.cost_usd` (новое поле) — внешние интеграции должны учитывать.

## [0.1.0] — 2026-05 (initial release)

- Базовая функциональность: planner → DAG → workers → merge → PR.
- Phased plans + replan + 3-state reviewer + per-task pre-merge review.
- Bash injection-guard + path-gated edit + provider-aware reasoning effort.
- TUI live-board (`tui.py`).
- CLI: `init`, `plan`, `run`, `all`, `list`, `logs`.
