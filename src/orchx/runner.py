"""Тонкий адаптер для оркестратора: render task.md + спавн in-process воркера.

Раньше ``runner.py`` спавнил kilo CLI subprocess'ом под PTY. Сейчас всё
делает :func:`orchx.agent.worker.run_agent`. Этот модуль остаётся
исключительно для обратной совместимости с импортами в ``orchestrator.py``
(``runner.run_worker``, ``runner.WorkerOutcome``, ``runner.render_task_md``).

УДАЛЕНО (теперь не нужно):
- ``find_kilo_binary`` / ``KILO_BIN`` — kilo не используется.
- ``clean_kilo_env`` — нет дочернего kilo, env-mungling не требуется.
- ``kilo_agent_name`` — оркестратор передаёт короткие role-имена,
  :mod:`orchx.agent.frontmatter` сам знает префикс.
- PTY / pipe режимы — воркер in-process, никаких subprocess'ов.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .agent.llm import LLMClient
from .agent.worker import WorkerOutcome, run_agent
from .models import TaskSpec

logger = logging.getLogger(__name__)


__all__ = [
    "WorkerOutcome",
    "render_task_md",
    "run_worker",
]


# ---------------------------------------------------------------------------
# task.md rendering — без изменений относительно старой версии.
# ---------------------------------------------------------------------------


def _render_list_block(items: tuple[str, ...] | list[str], empty_msg: str) -> str:
    if not items:
        return f"_{empty_msg}_"
    return "\n".join(f"- `{item}`" for item in items)


def _render_acceptance(checks: tuple) -> str:
    if not checks:
        return "_(нет проверок)_"
    lines: list[str] = []
    for c in checks:
        if c.type == "command":
            lines.append(f"- {c.description}\n    - command: `{c.command}`")
        elif c.type == "file_exists":
            lines.append(f"- {c.description}\n    - file exists: `{c.path}`")
        else:
            lines.append(
                f"- {c.description}\n    - file `{c.path}` matches `{c.pattern}`"
            )
    return "\n".join(lines)


def render_task_md(template: str, task: TaskSpec, branch: str, result_path: str) -> str:
    """Подставить значения в шаблон ``orchx/schemas/task.template.md``."""
    return (
        template.replace("{{task_id}}", task.id)
        .replace("{{goal}}", task.goal)
        .replace("{{agent}}", task.agent)
        .replace(
            "{{inputs_block}}",
            _render_list_block(task.inputs, "нет — задача не требует входов"),
        )
        .replace(
            "{{file_scope_block}}",
            _render_list_block(task.file_scope, "не задан — недопустимо"),
        )
        .replace(
            "{{outputs_block}}",
            _render_list_block(task.outputs, "не задано — определи сам"),
        )
        .replace("{{acceptance_block}}", _render_acceptance(task.acceptance))
        .replace("{{branch}}", branch)
        .replace("{{result_path}}", result_path)
    )


# ---------------------------------------------------------------------------
# Worker spawn (in-process)
# ---------------------------------------------------------------------------


async def run_worker(
    *,
    llm: LLMClient,
    cwd: Path,
    role: str,
    prompt: str,
    timeout_s: int,
    log_file: Path,
    repo_root: Path | None = None,
    effort: str | None = None,
    on_activity=None,
) -> WorkerOutcome:
    """Спавнить in-process воркера и дождаться его завершения.

    Args:
        llm: Базовый :class:`LLMClient`. Воркер получит per-role клон через
            ``llm.for_role(role, effort=effort)``.
        cwd: Рабочий каталог воркера (worktree). Все tool-пути резолвятся
            относительно него.
        role: Короткое имя роли (``implementer``, ``planner``, ...).
        prompt: User-сообщение для воркера.
        timeout_s: Wall-clock timeout.
        log_file: Куда писать transcript.
        repo_root: Корень репо (для system-prompt'а). По умолчанию = ``cwd``,
            что подходит для воркеров в их worktree, где ``orchx/prompts/*.md``
            уже доступны через worktree (он-же чекаут той же ветки).
        effort: ``low|medium|high|xhigh``. ``None`` — без override.
        on_activity: Callback, получающий полезные строки из стрима LLM.
            Используется live-доской TUI.
    """
    return await run_agent(
        role=role,
        cwd=cwd,
        repo_root=repo_root or cwd,
        user_prompt=prompt,
        llm=llm,
        effort=effort,
        timeout_s=timeout_s,
        log_file=log_file,
        on_activity=on_activity,
    )
