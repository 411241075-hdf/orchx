"""Agent loop — сердце переписки воркера с LLM.

In-process замена ``kilo run --auto --agent orchX-<role> "<prompt>"``.

Контракт ``WorkerOutcome`` совместим со старым ``runner.WorkerOutcome``,
поэтому ``orchestrator.py`` не меняется.

**Compaction.** Когда диалог разрастается до ~75% от ``context_window``
модели, мы делаем один проход «summarize» — LLM получает специальный
prompt и сжимает старые сообщения в одно `role=user` summary. Это
позволяет воркерам с большими scope (debugger на сложном баге,
reviewer на 30-файловом PR'е) не упираться в context limit провайдера.
Поддерживается как Anthropic (`thinking: adaptive`), так и OpenAI
o-series, и любой другой OpenAI-совместимый Proxy.

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
    input_tokens: int = 0
    """Сумма input-токенов за все LLM-вызовы воркера (если Proxy сообщал)."""
    output_tokens: int = 0
    """Сумма output-токенов за все LLM-вызовы воркера."""
    llm_calls: int = 0
    """Количество LLM-вызовов в этом воркере (для cost-analysis)."""
    compactions: int = 0
    """Сколько раз история была сжата компактором."""


# Сколько символов tool-результата кладём в лог (не в LLM-сообщение).
_LOG_TOOL_RESULT_SNIPPET = 400


# Эвристика context window'а провайдера. Берётся из переменной окружения
# ORCHX_CONTEXT_WINDOW (если задана), иначе используем дефолт под Claude
# Sonnet 4.6 / Opus 4.7 (1M) — большинство современных моделей выше.
# Для не-Claude через Proxy 200k — безопасный нижний порог.
def _context_window_chars(model: str) -> int:
    """Возвращает context window провайдера в символах (примерно 3.5 char/token)."""
    import os

    raw = os.environ.get("ORCHX_CONTEXT_WINDOW")
    if raw:
        try:
            tokens = int(raw)
            return tokens * 4  # консервативный char→token коэффициент.
        except ValueError:
            pass
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        # Sonnet/Opus 4.6+ держат 1M; берём 950k чтобы был запас.
        return 950_000 * 4
    if "gemini" in m:
        return 950_000 * 4  # 1M
    if "gpt-5" in m or m.startswith(("o3", "o4")) or "openai/o" in m:
        return 200_000 * 4
    # Conservative default.
    return 128_000 * 4


def _approx_messages_chars(messages: list[dict[str, Any]]) -> int:
    """Грубая оценка размера messages в символах."""
    n = 0
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for piece in c:
                if isinstance(piece, dict):
                    txt = piece.get("text") or piece.get("content")
                    if isinstance(txt, str):
                        n += len(txt)
        # tool_calls в assistant-ходе.
        for tc in msg.get("tool_calls", []) or []:
            fn = (tc or {}).get("function") or {}
            n += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return n


_COMPACTION_SUMMARY_PROMPT = """Ты — компактор контекста. Текущий диалог \
agent loop разрастается; нужно сжать историю до момента «прямо сейчас».

Сохрани:
- какая исходная задача стоит перед воркером (из task.md);
- какие файлы воркер уже прочитал и что в них нашёл (с путями + краткие \
факты);
- какие правки уже сделаны (списком файлов с однострочным описанием);
- какие tools и команды уже запускались и что они вернули (PASS/FAIL + \
суть);
- какие гипотезы / промежуточные выводы зафиксированы;
- любые security-relevant constraints (file_scope, allow-list bash-команд, \
явные деnies);
- последний намеченный план действий.

Не отбрасывай ни одного результата acceptance-проверки или error-сообщения, \
если оно не повторяется. Сжимай в plain Markdown без воды. Финальная длина \
— до 8 коротких параграфов или 40 bullet-points.
"""


async def _maybe_compact_messages(
    *,
    role_llm: LLMClient,
    messages: list[dict[str, Any]],
    log_fh,
    threshold_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Если ``messages`` превышает порог — сжать всё кроме system + tail.

    Стратегия: оставляем первое system-сообщение, последние 4 ходов
    (tool-results + последний assistant), всё посередине заменяем на одно
    user-сообщение с summary.

    Returns:
        (new_messages, compacted). ``compacted=True`` означает, что мы
        реально сделали LLM-call на summary.
    """
    size = _approx_messages_chars(messages)
    if size < threshold_chars:
        return messages, False
    if len(messages) <= 6:
        # Слишком мало для compaction — ничего не выиграем.
        return messages, False

    # Найдём первый system, оставим последние 4 ходов.
    system_idx = next(
        (i for i, m in enumerate(messages) if m.get("role") == "system"),
        None,
    )
    head: list[dict[str, Any]] = []
    if system_idx is not None:
        head = messages[: system_idx + 1]
    tail_start = max(len(messages) - 4, system_idx + 1 if system_idx is not None else 0)
    tail = messages[tail_start:]
    middle = messages[len(head) : tail_start]
    if not middle:
        return messages, False

    log_fh.write(
        f"\n=== compaction trigger: size~{size} chars >= {threshold_chars} "
        f"chars; compacting {len(middle)} middle messages ===\n"
    )
    log_fh.flush()

    # Готовим input для compactor'а.
    middle_text_parts: list[str] = []
    for m in middle:
        role = m.get("role", "")
        content = m.get("content")
        if isinstance(content, str):
            middle_text_parts.append(f"## {role}\n{content}")
        elif isinstance(content, list):
            joined = "\n".join(
                str(p.get("text") or p.get("content") or "")
                for p in content
                if isinstance(p, dict)
            )
            middle_text_parts.append(f"## {role}\n{joined}")
        # tool_calls
        for tc in m.get("tool_calls", []) or []:
            fn = (tc or {}).get("function") or {}
            middle_text_parts.append(
                f"### tool_call {fn.get('name', '')}\nargs: {fn.get('arguments', '')}"
            )
    middle_text = "\n\n".join(middle_text_parts)

    summarizer_messages = [
        {"role": "system", "content": _COMPACTION_SUMMARY_PROMPT},
        {
            "role": "user",
            "content": (
                "Сожми следующую часть истории agent loop. Верни plain "
                "Markdown без преамбулы и без финальных вопросов.\n\n"
                "---\n\n" + middle_text
            ),
        },
    ]
    try:
        resp = await role_llm.chat(messages=summarizer_messages, tools=None)
        summary_text = (resp.text or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("compaction failed: %s — leaving messages as-is", e)
        log_fh.write(f"\n[compaction-error] {e!r}\n")
        log_fh.flush()
        return messages, False

    if not summary_text:
        log_fh.write("\n[compaction-error] empty summary returned\n")
        log_fh.flush()
        return messages, False

    log_fh.write(f"\n[compaction-summary]\n{summary_text}\n")
    log_fh.flush()

    new_messages = (
        list(head)
        + [
            {
                "role": "user",
                "content": (
                    "## Compacted history\n\nThe earlier portion of this "
                    "agent-loop conversation was summarized to save context. "
                    "Below is the summary; treat it as authoritative for "
                    "what's been read/edited/run so far. Continue from "
                    "where the recent messages left off.\n\n" + summary_text
                ),
            }
        ]
        + list(tail)
    )
    return new_messages, True


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
            «прочитай ``.orchx/task.md`` и сделай задачу».
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
    # Загрузим спеку роли (system prompt + permissions + max_steps).
    # Каскад поиска промпта — через OrchXRuntime: сначала
    # <project>/.orchx/prompts/, затем дефолт пакета (templates/prompts).
    from ..runtime import OrchXRuntime

    runtime = OrchXRuntime.from_project_root(repo_root)
    spec = _fm.load_agent_spec(role, runtime)
    return await run_agent_with_spec(
        spec=spec,
        cwd=cwd,
        repo_root=repo_root,
        user_prompt=user_prompt,
        llm=llm,
        effort=effort,
        timeout_s=timeout_s,
        log_file=log_file,
        on_activity=on_activity,
        role_for_llm=role,
    )


async def run_agent_with_spec(
    *,
    spec: _fm.AgentSpec,
    cwd: Path,
    repo_root: Path,
    user_prompt: str,
    llm: LLMClient,
    effort: str | None = None,
    timeout_s: int = 1800,
    log_file: Path,
    on_activity: Callable[[str], Any] | None = None,
    role_for_llm: str | None = None,
) -> WorkerOutcome:
    """Прогнать воркера с уже загруженным :class:`AgentSpec`.

    Это используется и :func:`run_agent` (после загрузки spec с диска), и
    sub-агентами (которые синтезируют spec в памяти, см.
    :mod:`orchx.agent.tools.task`).

    Args:
        spec: Уже распарсенная роль (frontmatter + permissions + body).
        role_for_llm: Имя роли для ``llm.for_role(...)``-резолва per-role
            модели. Если ``None`` — используется ``spec.role``.
    """
    started = time.monotonic()
    deadline = started + timeout_s
    role = role_for_llm or spec.role

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
        tool_descriptions={name: tool.description for name, tool in tools.items()},
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

    # Compaction-порог: 75% от приблизительного context window.
    context_window = _context_window_chars(role_llm.model)
    compaction_threshold = int(context_window * 0.75)

    full_text: list[str] = []
    timed_out = False
    rc = -1
    compactions_done = 0
    total_input_tokens = 0
    total_output_tokens = 0
    llm_calls = 0

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

            # Compaction: если messages разросся, пробуем сжать. Делаем
            # не чаще одного раза за шаг.
            messages, compacted = await _maybe_compact_messages(
                role_llm=role_llm,
                messages=messages,
                log_fh=log_fh,
                threshold_chars=compaction_threshold,
            )
            if compacted:
                compactions_done += 1
                _activity(f"compacted history (#{compactions_done})")

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
            llm_calls += 1
            total_input_tokens += resp.input_tokens or 0
            total_output_tokens += resp.output_tokens or 0

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
                f"\n# returncode={rc} timed_out={timed_out} duration={duration:.1f}s "
                f"compactions={compactions_done}\n"
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
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        llm_calls=llm_calls,
        compactions=compactions_done,
    )
