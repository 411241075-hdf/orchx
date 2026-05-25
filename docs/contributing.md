# Contributing to orchX

> Спасибо за интерес к orchX! Этот документ описывает, как добавлять код,
> плагины, и проходить CI-checks.

## Quick setup

```bash
git clone https://github.com/411241075-hdf/orchx
cd orchx
make install          # → .venv + ".[dev,test]"
make test             # 254+ tests должны пройти зелёные
make check            # lint + типы + тесты — это то же, что CI
```

> **Python 3.14 нюанс.** Setuptools editable-install в strict-mode
> ломается с Python 3.14 — `make install` использует
> `--config-settings editable_mode=compat`. Если устанавливаете руками,
> используйте этот же флаг.

## Layout

См. [`architecture.md`](./architecture.md) — высокоуровневый обзор.
Coupled module hierarchy:

```
src/orchx/
├── orchestrator/        Main loop (phases, retry, merge, review, supervisor)
├── plugins/             Plugin slots + default impls
│   ├── runtimes/
│   ├── trackers/
│   ├── scm/
│   ├── notifiers/
│   └── memory/
├── agent/               LLM client, worker loop, tools, prompts, permissions
│   └── tools/           Read/Write/Edit/Bash/Search/Browser/MCP/Symbols/Task
├── web/                 Optional FastAPI dashboard + federation REST
├── cost.py              P1.3 cost tracking
├── pr_watcher.py        P0.4 CI/review reactions
├── cli.py               argparse, dispatch
└── tests/
    ├── unit/
    ├── integration/     End-to-end с FakeLLMClient
    └── fixtures/        mock_llm.py
```

## Adding a plugin

orchX поддерживает 5 plugin slots: `runtime`, `tracker`, `scm`,
`notifier`, `memory`. Любой стороний пакет может добавить плагин:

1. **Реализовать контракт.** См. `src/orchx/plugins/contracts.py`.
   Например, notifier:

   ```python
   # my_pkg/teams_notifier.py
   class TeamsNotifier:
       name = "teams"

       def __init__(self, *, webhook_url: str = "", **_) -> None:
           self.url = webhook_url

       async def notify(self, event: str, payload: dict) -> None:
           # POST в Teams Incoming Webhook.
           ...
   ```

2. **Зарегистрировать через entry-points** в своём `pyproject.toml`:

   ```toml
   [project.entry-points."orchx.notifier"]
   teams = "my_pkg.teams_notifier:TeamsNotifier"
   ```

3. **Пользователь подключает** в своём `.orchx/config.yaml`:

   ```yaml
   notifiers: [teams]
   plugin_config:
     teams:
       webhook_url: ${TEAMS_WEBHOOK_URL}
   ```

`orchx plugins list` покажет ваш plugin рядом с встроенными.

## Adding a tool

Tool — это подкласс `orchx.agent.tools.Tool`:

```python
# src/orchx/agent/tools/my_tool.py
from . import Tool, ToolContext, ToolResult, permission_denied


class MyTool(Tool):
    name = "mytool"
    description = "Что делает (в OpenAI tool-schema)."
    parameters = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
        "required": ["foo"],
    }
    permission_attr = "read"  # gating через ctx.permissions.read

    async def run(self, ctx: ToolContext, *, foo: str) -> ToolResult:
        ctx.activity(f"mytool {foo}")
        # ...
        return ToolResult(content="result", is_error=False)
```

Регистрация в `src/orchx/agent/tools/__init__.py:build_tool_registry`.

## Coding style

- `ruff` + `mypy` (см. `pyproject.toml [tool.ruff]`).
- line-length 100 (E501 ignore).
- `from __future__ import annotations` в каждом модуле.
- Async везде, где есть IO.
- Docstrings RU/EN — на ваш выбор, но **обязательно**.

## Tests

- **Unit-тесты** для pure-функций и небольших классов → `src/orchx/tests/unit/`.
- **Integration-тесты** с FakeLLMClient → `src/orchx/tests/integration/`.
- Pytest fixtures для общих case'ов — добавляйте в `tests/fixtures/`.

Для async-тестов используйте `@pytest.mark.asyncio` per-test (не глобальный
pytestmark — он засоряет warnings sync-тестов в том же файле).

## Pull request workflow

1. Создайте feature-branch.
2. Сделайте `make check` локально перед PR.
3. PR должен пройти GitHub Actions CI (`.github/workflows/ci.yml`):
   - ruff lint
   - mypy (не-блокирующий пока)
   - pytest на py 3.13, ubuntu + macos
   - build (sdist + wheel + twine check)

## Release

```bash
# 1. Bump version в pyproject.toml.
# 2. Update docs/changelog.md.
# 3. Build:
make build
# 4. Test install:
pip install dist/orchx-X.Y.Z-py3-none-any.whl
# 5. Publish:
twine upload dist/*
```
