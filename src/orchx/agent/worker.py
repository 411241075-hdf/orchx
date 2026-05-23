"""Agent loop — сердце переписки воркера с LLM.

In-process замена ``kilo run --auto --agent orchX-<role> "<prompt>"``.

Контракт ``WorkerOutcome`` совместим со старым ``runner.WorkerOutcome``,
поэтому ``orchestrator.py`` не меняется.

Коды возврата:
- ``0``  — модель сама закончила (отдала assistant-ход без tool_calls).
- ``124`` — wall-clock timeout (``timeout_s``).
- ``125`` — исчерпан ``max_steps`` (бесконечный цикл tool-вызовов).
- ``-1``  — внутренняя ошибка (исключение в LLM-клиенте, потеряли стрим).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import frontmatter as _fm
from . import prompts as _prompts
from .llm import LLMClient
from .tools import (
    ToolContext,
    ToolResult,
    build_tool_registry,
    to_openai_schema,
)

logger = logging.getLogger(__name__)


@dataclass
class WorkerOutcome:
    """Контракт совместим с прежним ``runner.WorkerOutcome``."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


# Сколько символов tool-результата кладём в лог (не в LLM-сообщение).
_LOG_TOOL_RESULT_SNIPPET = 400


async def run_agent(
    *,
    role: str,
    cwd: Path,
    repo_root: Path,
    user_prompt: str,
    llm: LLMClient,
    effort: str | None = None,
    timeout_s: int = 1800,
    log_file: Path,
    on_activity: Callable[[str], Any] | None = None,
) -> WorkerOutcome:
    """Прогнать одного воркера.

    Args:
        role: Короткое имя роли (``implementer``, ``planner``, ...).
        cwd: Рабочий каталог воркера (обычно его worktree). Все tool-пути
            резолвятся отсюда.
        repo_root: Корень репозитория (для info-строки в system-prompt'е).
        user_prompt: ``user``-сообщение от диспетчера. Обычно короткое
            «прочитай ``orchx/task.md`` и сделай задачу».
        llm: Базовый LLM-клиент. Внутри вызовем ``llm.for_role(role, effort=...)``
            чтобы поднять per-role override модели/effort'а.
        effort: Reasoning-effort (``low|medium|high|xhigh``). ``None`` → не
            добавляем effort-параметры в запрос.
        timeout_s: Wall-clock budget на всё взаимодействие с этим воркером.
        log_file: Файл, куда пишем human-readable transcript (открывается
            на запись, перетирая прежнее содержимое).
        on_activity: Callback на каждое заметное событие (текстовая дельта,
            начало tool-вызова). Используется TUI live-доской.
    """
    started = time.monotonic()
    deadline = started + timeout_s

    # Загрузим спеку роли (system prompt + permissions + max_steps).
    spec = _fm.load_agent_spec(role, repo_root)

    # Контекст и реестр инструментов.
    activity_cb = on_activity or (lambda _: None)

    def _activity(msg: str) -> None:
        try:
            activity_cb(msg)
        except Exception:  # noqa: BLE001
            pass

    ctx = ToolContext(
        cwd=cwd,
        repo_root=repo_root,
        permissions=spec.permissions,
        activity=_activity,
        todos=[],
    )
    tools = build_tool_registry(ctx)
    tool_schemas = [to_openai_schema(t) for t in tools.values()]

    # LLM-клиент для конкретной роли (с per-role моделью).
    role_llm = llm.for_role(role, effort=effort)

    # System + initial user.
    system_prompt = _prompts.build_system_prompt(
        spec,
        cwd=cwd,
        repo_root=repo_root,
        tool_names=list(tools.keys()),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_file.open("w", encoding="utf-8")
    log_fh.write(
        f"# orchX agent={role} cwd={cwd}\n"
        f"# model={role_llm.model} effort={role_llm.effort}\n"
        f"# repo_root={repo_root} timeout_s={timeout_s} max_steps={spec.max_steps}\n\n"
    )
    log_fh.flush()

    full_text: list[str] = []
    timed_out = False
    rc = -1

    # Стрим-колбэки определены один раз — оба замыкания захватывают
    # loop-invariant ``log_fh`` и ``_activity``, перецеплять их по шагу
    # нет смысла.
    def _on_text(delta: str) -> None:
        try:
            log_fh.write(delta)
            log_fh.flush()
        except Exception:  # noqa: BLE001
            pass
        _activity(delta)

    def _on_tc(name: str) -> None:
        try:
            log_fh.write(f"\n[tool-call] {name}\n")
            log_fh.flush()
        except Exception:  # noqa: BLE001
            pass
        _activity(f"→ tool {name}")

    try:
        for step in range(1, spec.max_steps + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                rc = 124
                break

            log_fh.write(f"\n=== step {step} (assistant) ===\n")
            log_fh.flush()

            try:
                resp = await role_llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                    on_text_delta=_on_text,
                    on_tool_call_delta=_on_tc,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("orchX worker LLM call failed")
                log_fh.write(f"\n[llm-error] {e!r}\n")
                rc = -1
                break

            if resp.text:
                full_text.append(resp.text)

            if not resp.tool_calls:
                # Модель закончила без tool-call'ов → success.
                rc = 0
                break

            # Кладём ход ассистента (с tool_calls) в историю.
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": resp.text or "",
                "tool_calls": resp.tool_calls_raw,
            }
            messages.append(assistant_msg)

            # Выполняем все tool-вызовы последовательно.
            for tc in resp.tool_calls:
                tool = tools.get(tc.name)
                if tool is None:
                    result = ToolResult(
                        content=(
                            f"Unknown tool: {tc.name}. Available tools: "
                            + ", ".join(tools.keys())
                        ),
                        is_error=True,
                    )
                else:
                    try:
                        result = await tool.run(ctx, **tc.arguments)
                    except TypeError as e:
                        # Неправильные имена аргументов от LLM — частый кейс.
                        result = ToolResult(
                            content=f"Bad arguments for tool {tc.name}: {e}",
                            is_error=True,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception("orchX tool %s failed", tc.name)
                        result = ToolResult(
                            content=f"Tool error: {e!r}",
                            is_error=True,
                        )
                # tool-message обратно в LLM.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id or tc.name,
                        "content": result.content,
                    }
                )
                # Лог.
                snippet = result.content[:_LOG_TOOL_RESULT_SNIPPET]
                if len(result.content) > _LOG_TOOL_RESULT_SNIPPET:
                    snippet += "…"
                log_fh.write(
                    f"\n[tool-result {tc.name}] is_error={result.is_error}\n{snippet}\n"
                )
                log_fh.flush()
        else:
            # Цикл for завершился без break — исчерпан max_steps.
            rc = 125
    finally:
        duration = time.monotonic() - started
        try:
            log_fh.write(
                f"\n# returncode={rc} timed_out={timed_out} duration={duration:.1f}s\n"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            log_fh.close()
        except Exception:  # noqa: BLE001
            pass

    return WorkerOutcome(
        returncode=rc,
        stdout="\n".join(full_text),
        stderr="",
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
    )
