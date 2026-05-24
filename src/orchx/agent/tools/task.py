"""TaskTool — спавнит sub-агента в том же worktree для open-ended research.

Use-case: implementer/architect/debugger хочет глубокое исследование
(«найди все callers FooClass и опиши их роли»), которое съест десятки
grep'ов в основном loop'е и забьёт context window. TaskTool вызывает
``run_agent`` рекурсивно с урезанными permissions, своим log-файлом и
жёстким timeout'ом, а возвращает один summary-message.

**Ограничения:**

- Глубина рекурсии — максимум 1 уровень. Sub-агент не может звать `task`
  снова (см. ``ORCHX_SUBAGENT_DEPTH``).
- `subagent_role="explore"` — read-only (`edit=False`, `bash` deny).
- `subagent_role="general"` — копия permissions родителя минус task.
- Timeout — short (по умолчанию 300s, не наследуется от родителя).
"""

from __future__ import annotations

import os
import uuid
from copy import deepcopy
from typing import Any

from ..frontmatter import AgentSpec
from ..permissions import Permissions
from . import Tool, ToolContext, ToolResult, permission_denied

# Env-флаг для tracking'а recursion depth. Установлен в "1" в child-процессе
# sub-агента → child видит, что он уже sub-agent, и его собственный task tool
# откажет (защита от бесконечной рекурсии).
_DEPTH_ENV = "ORCHX_SUBAGENT_DEPTH"
_MAX_DEPTH = 1

# Минимально допустимый sub-agent timeout — чтобы случайно не вырубить
# research до первого LLM-ответа.
_MIN_SUBAGENT_TIMEOUT_S = 120
_DEFAULT_SUBAGENT_TIMEOUT_S = 300

_EXPLORE_BODY = """\
You are an exploration sub-agent. The parent worker delegated a focused
research task to you. Your job:

1. Read the task carefully (it is the user message you received).
2. Use `read`, `glob`, `grep`, `codesearch` to investigate the codebase.
3. Return a single, well-structured summary as your final assistant
   message (no tool calls in the final turn).

You are READ-ONLY: you cannot write/edit files or run shell commands.
You cannot spawn further sub-agents (no `task` tool).

Keep the summary concise and factual:
- cite `path:line` for every claim;
- group findings by topic;
- finish with a 1-3 sentence conclusion that directly answers the task.
"""

_GENERAL_BODY = """\
You are a general-purpose sub-agent. The parent worker delegated a
self-contained sub-task to you. You inherit the parent's tool permissions
(except you cannot spawn further sub-agents). Investigate, act, and
return a single summary assistant message in the end.

Keep your final message focused: cite files/lines for changes, list any
side effects, and finish with a 1-3 sentence conclusion.
"""


def _build_subagent_spec(role: str, parent_perms: Permissions) -> AgentSpec:
    """Сконструировать ``AgentSpec`` для sub-агента в памяти.

    Args:
        role: ``"explore"`` или ``"general"``.
        parent_perms: Permissions родителя — основа для урезания.
    """
    p = deepcopy(parent_perms)
    # Sub-агенты НИКОГДА не могут спавнить ещё sub-агентов.
    p.task = False
    if role == "explore":
        p.edit = False
        p.bash = {"*": "deny"}
        p.webfetch = False
        body = _EXPLORE_BODY
    else:  # "general"
        body = _GENERAL_BODY
    return AgentSpec(
        name=f"orchX-subagent-{role}",
        role=f"subagent-{role}",
        description=f"In-process sub-agent ({role})",
        body=body,
        max_steps=20,  # sub-агенты — короткие, не должны блуждать.
        permissions=p,
    )


class TaskTool(Tool):
    """Запустить sub-агента для focused research."""

    name = "task"
    description = (
        "Spawn a short-lived sub-agent in the same worktree to handle a "
        "self-contained research or sub-task (e.g. 'find all callers of "
        "FooClass and summarize their roles'). The sub-agent runs in its "
        "own LLM context window (does not pollute yours) and returns one "
        "summary message. Use for open-ended exploration; for surgical "
        "edits you should still call tools directly."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "One-line task summary (used in logs).",
            },
            "prompt": {
                "type": "string",
                "description": "Detailed task for the sub-agent (becomes its user message).",
            },
            "subagent_role": {
                "type": "string",
                "enum": ["explore", "general"],
                "description": (
                    "`explore` is read-only (read/glob/grep/codesearch only). "
                    "`general` inherits parent's permissions (still no nested "
                    "sub-agents)."
                ),
            },
            "timeout_s": {
                "type": "integer",
                "minimum": _MIN_SUBAGENT_TIMEOUT_S,
                "description": (
                    f"Wall-clock budget for the sub-agent (default "
                    f"{_DEFAULT_SUBAGENT_TIMEOUT_S}s, min "
                    f"{_MIN_SUBAGENT_TIMEOUT_S}s)."
                ),
            },
        },
        "required": ["description", "prompt", "subagent_role"],
    }
    permission_attr = "task"

    async def run(
        self,
        ctx: ToolContext,
        *,
        description: str,
        prompt: str,
        subagent_role: str,
        timeout_s: int = _DEFAULT_SUBAGENT_TIMEOUT_S,
    ) -> ToolResult:
        """Запустить sub-агента (см. описание класса)."""
        # Guard: nested sub-agents запрещены.
        try:
            depth = int(os.environ.get(_DEPTH_ENV, "0"))
        except ValueError:
            depth = 0
        if depth >= _MAX_DEPTH:
            return permission_denied(
                tool="task",
                target=description,
                reason=(
                    f"nested sub-agents are not allowed (current depth={depth}, "
                    f"max={_MAX_DEPTH})"
                ),
                hint=(
                    "You are already a sub-agent. Finish your current "
                    "investigation and return a summary to the parent."
                ),
            )

        if subagent_role not in ("explore", "general"):
            return ToolResult(
                content=(
                    f"Invalid subagent_role={subagent_role!r}. "
                    "Use 'explore' (read-only) or 'general'."
                ),
                is_error=True,
            )
        if timeout_s < _MIN_SUBAGENT_TIMEOUT_S:
            timeout_s = _MIN_SUBAGENT_TIMEOUT_S

        ctx.activity(f"task[{subagent_role}] {description[:60]}")

        # Sub-agent spec — синтетический, не читаем с диска.
        sub_spec = _build_subagent_spec(subagent_role, ctx.permissions)

        # Лог sub-агента — в subtasks/<uuid>.log внутри worktree, чтобы
        # потом можно было отдебажить.
        sub_id = uuid.uuid4().hex[:8]
        sub_log = ctx.cwd / "orchx" / "subtasks" / f"{sub_id}.log"

        # Спавним child-процесс с пометкой depth → его собственный TaskTool
        # тоже откажется работать.
        os.environ[_DEPTH_ENV] = str(depth + 1)
        try:
            # ВАЖНО: мы не можем легко переиспользовать run_agent напрямую,
            # потому что он сам зовёт `load_agent_spec(role, repo_root)`.
            # Делаем минимальную обёртку — спавним agent loop вручную через
            # внутреннюю функцию ``_run_agent_with_spec``.
            outcome = await _run_subagent_with_spec(
                spec=sub_spec,
                cwd=ctx.cwd,
                repo_root=ctx.repo_root,
                user_prompt=prompt,
                llm=_resolve_llm_from_env(),
                timeout_s=timeout_s,
                log_file=sub_log,
            )
        except RuntimeError as e:
            return ToolResult(
                content=f"Sub-agent failed to start: {e}",
                is_error=True,
            )
        finally:
            # Восстанавливаем env-флаг — другие parallel-вызовы не должны
            # видеть наш bump'нутый depth.
            if depth == 0:
                os.environ.pop(_DEPTH_ENV, None)
            else:
                os.environ[_DEPTH_ENV] = str(depth)

        # outcome.stdout — последний assistant text (см. worker.py).
        summary = (outcome.stdout or "").strip()
        if not summary:
            summary = "(sub-agent returned no summary)"
        meta = (
            f"\n\n---\nsub-agent stats: rc={outcome.returncode} "
            f"steps_llm_calls={outcome.llm_calls} "
            f"duration={outcome.duration_s:.1f}s "
            f"log={sub_log.relative_to(ctx.cwd) if sub_log.is_relative_to(ctx.cwd) else sub_log}"
        )
        is_error = outcome.returncode != 0 and outcome.returncode != 125
        return ToolResult(content=summary + meta, is_error=is_error)


def _resolve_llm_from_env() -> Any:
    """Создать LLMClient из env-переменных (как делает CLI).

    Sub-агент использует тот же провайдер/модель/effort, что и родитель —
    самый простой способ это получить — пересоздать клиент из env. У нас
    нет доступа к родительскому LLMClient через ToolContext (он не
    хранится там, чтобы не плодить циклические зависимости).
    """
    from ..llm import LLMClient, LLMConfig

    cfg = LLMConfig.from_env()
    return LLMClient(cfg)


async def _run_subagent_with_spec(
    *,
    spec: AgentSpec,
    cwd,  # noqa: ANN001
    repo_root,  # noqa: ANN001
    user_prompt: str,
    llm,  # noqa: ANN001
    timeout_s: int,
    log_file,  # noqa: ANN001
):  # noqa: ANN202
    """Минимальная обёртка над worker'ом для синтетического AgentSpec.

    Дублирует основную часть ``worker.run_agent``, но не загружает spec с
    диска. Это позволяет передать sub-agent'у синтетические permissions
    и body.
    """
    # Импортируем здесь, чтобы избежать циклов на верхнем уровне.
    from ..worker import run_agent_with_spec

    return await run_agent_with_spec(
        spec=spec,
        cwd=cwd,
        repo_root=repo_root,
        user_prompt=user_prompt,
        llm=llm,
        timeout_s=timeout_s,
        log_file=log_file,
    )
