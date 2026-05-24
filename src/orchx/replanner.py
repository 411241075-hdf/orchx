"""Авто-перепланирование: вызов orchX-planner на остаток работы после провала.

Используется оркестратором, когда фаза провалилась (debugger не справился,
все retry'и исчерпаны), но фаза помечена ``allow_replan: true`` и глобальный
``max_replans`` ещё не исчерпан.

Контракт:

1. Оркестратор собирает контекст провала (упавшие задачи + их причины +
   успешные фазы + оригинальный план + spec_files).
2. Replanner записывает контекст в ``orchx/runs/<task_id>/replan-context.md``
   — человеко-читаемый бриф для planner'а.
3. Запускает in-process воркера ``planner`` со специальной инструкцией
   «прочитай runs/<task_id>/replan-context.md и перепиши runs/<task_id>/plan.json».
4. После завершения проверяет, что planner записал валидный новый план,
   и возвращает его оркестратору.

Replanner не модифицирует state роя — только подменяет ``ctx.plan`` на
свежий, помещает новые фазы в DAG и сбрасывает счётчики retry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import runner
from .agent.llm import LLMClient
from .models import Plan, load_plan

logger = logging.getLogger(__name__)


@dataclass
class ReplanContext:
    """Бриф для planner'а при перепланировании."""

    repo_root: Path
    plan: Plan
    failed_phase_id: str
    failed_task_ids: list[str]
    failure_reasons: dict[str, str]  # task_id → reason
    completed_phase_ids: list[str]
    replan_attempt: int
    max_replans: int
    extra_notes: str = ""


def render_replan_context(ctx: ReplanContext) -> str:
    """Сформировать markdown-бриф для planner'а.

    Файл попадает в ``run_dir``, planner читает его как первый источник
    правды при формировании нового плана.
    """
    parts: list[str] = []
    parts.append(f"# Replan context (attempt {ctx.replan_attempt}/{ctx.max_replans})")
    parts.append("")
    parts.append(
        "Диспетчер просит перепланировать остаток работы. Предыдущий план "
        "застрял на провале фазы; debugger исчерпал retry'и."
    )
    parts.append("")
    parts.append(
        f"## Original task_id\n\n`{ctx.plan.task_id}` — **сохрани его в новом плане**."
    )
    parts.append("")
    if ctx.plan.summary:
        parts.append(f"## Goal\n\n{ctx.plan.summary}")
        parts.append("")
    if ctx.plan.spec_files:
        parts.append("## Specification files (источник правды)")
        parts.append("")
        for sf in ctx.plan.spec_files:
            parts.append(f"- `{sf}`")
        parts.append("")
        parts.append(
            "Перечитай их перед формированием нового плана — реальность "
            "могла отличаться от первой интерпретации."
        )
        parts.append("")

    parts.append("## Что уже сделано (НЕ повторяй)")
    parts.append("")
    if ctx.completed_phase_ids:
        for pid in ctx.completed_phase_ids:
            phase = next((p for p in ctx.plan.phases if p.id == pid), None)
            if phase:
                parts.append(f"- `{pid}` — {phase.goal}")
    else:
        parts.append("_(ничего; провал на первой фазе)_")
    parts.append("")
    parts.append(
        "Эти фазы уже смержены в интеграционную ветку `orchX/{task_id}`. "
        "Их код доступен новым задачам."
    )
    parts.append("")

    parts.append(f"## Что провалилось — фаза `{ctx.failed_phase_id}`")
    parts.append("")
    failed_phase = next(
        (p for p in ctx.plan.phases if p.id == ctx.failed_phase_id), None
    )
    if failed_phase:
        parts.append(f"**Goal фазы:** {failed_phase.goal}")
        parts.append("")
        parts.append("**Упавшие задачи:**")
        parts.append("")
        for tid in ctx.failed_task_ids:
            task = next((t for t in failed_phase.tasks if t.id == tid), None)
            reason = ctx.failure_reasons.get(tid, "(нет деталей)")
            if task:
                parts.append(f"### `{tid}` ({task.agent})")
                parts.append(f"- **Goal:** {task.goal}")
                parts.append(f"- **Reason:** {reason}")
                parts.append(f"- **file_scope:** `{list(task.file_scope)}`")
                parts.append("")

    parts.append("## Оставшиеся фазы (если есть)")
    parts.append("")
    remaining = _remaining_phases(ctx)
    if remaining:
        for pid in remaining:
            phase = next(p for p in ctx.plan.phases if p.id == pid)
            parts.append(f"- `{pid}` — {phase.goal}")
        parts.append("")
        parts.append(
            "Их можно перенести в новый план без изменений ИЛИ переразбить, "
            "если провалившаяся фаза изменила landscape."
        )
    else:
        parts.append("_(нет — провалилась последняя фаза)_")
    parts.append("")

    if ctx.extra_notes:
        parts.append("## Дополнительные заметки от диспетчера")
        parts.append("")
        parts.append(ctx.extra_notes)
        parts.append("")

    parts.append("## Твоя задача")
    parts.append("")
    parts.append(
        "1. Перечитай `spec_files` (если есть) и пойми, что именно провалилось.\n"
        f"2. Перепиши `orchx/runs/{ctx.plan.task_id}/plan.json`:\n"
        "   - Сохрани оригинальный `task_id`.\n"
        "   - **Не включай уже завершённые фазы** — их код уже в integration ветке.\n"
        "   - Переразбей упавшую фазу на более мелкие задачи, либо найди "
        "обходной путь.\n"
        "   - Если упала миграция/необратимая операция — поставь BLOCKED-задачу "
        "и опиши, что должен сделать человек.\n"
        f"   - Сохрани `max_replans` ≥ {max(1, ctx.max_replans - ctx.replan_attempt)} "
        f"(anti-loop: не больше {ctx.max_replans - ctx.replan_attempt} "
        f"оставшихся попыток).\n"
        "3. Не угадывай фикс кода — твоё дело только переразбить задачу. "
        "Реализуют worker'ы.\n"
        "4. Сохрани `spec_files` в новом плане без изменений.\n"
    )
    parts.append("")
    parts.append("Финальная реплика — `plan written`.")
    return "\n".join(parts)


def _remaining_phases(ctx: ReplanContext) -> list[str]:
    """Фазы, идущие после провалившейся (в порядке плана)."""
    found = False
    out: list[str] = []
    for p in ctx.plan.phases:
        if found:
            out.append(p.id)
        if p.id == ctx.failed_phase_id:
            found = True
    return out


async def run_replan(
    *,
    repo_root: Path,
    llm: LLMClient,
    context: ReplanContext,
    plan_path: Path,
    run_dir: Path,
    log_dir: Path,
    effort: str | None = "xhigh",
) -> Plan:
    """Запустить orchX-planner с контекстом провала и получить новый план.

    Если planner написал план, который не проходит валидацию (например,
    cross-phase ``depends_on``, дубликаты id, циклы), — диспетчер делает
    **один self-heal retry**: вызывает planner повторно, прикладывая
    текст validation-ошибки и backup невалидного плана. Это спасает прогон
    от halt'а в случаях, когда planner упустил тонкое ограничение схемы
    (в прошлом прогоне `admin-subdomain` orchx остановился именно из-за
    cross-phase deps — фазы p4-p6 целиком были пропущены).

    Args:
        repo_root: Корень репозитория (worktree planner'а — он же).
        llm: Базовый LLM-клиент.
        context: Контекст провала для бриф'а planner'у.
        plan_path: Путь к существующему plan.json — будет перезаписан.
        run_dir: ``orchx/runs/<task_id>/`` — сюда кладём ``replan-context.md``
            и ``plan.before-replan-N.json``.
        log_dir: Куда писать лог planner'а (обычно ``run_dir / "logs"``).
        effort: Reasoning effort planner'а (по умолчанию ``xhigh``).

    Returns:
        Свежезагруженный ``Plan`` с новой версией.

    Raises:
        RuntimeError: Если planner не записал валидный план даже после
            одного self-heal retry'я.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    context_file = run_dir / "replan-context.md"
    context_file.write_text(render_replan_context(context), encoding="utf-8")

    # Сохраним предыдущий план рядом — planner будет его читать.
    backup_path = run_dir / f"plan.before-replan-{context.replan_attempt}.json"
    if plan_path.exists():
        backup_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
    # Удалим текущий plan.json, чтобы planner точно его пересоздал.
    if plan_path.exists():
        plan_path.unlink()

    log_dir.mkdir(parents=True, exist_ok=True)

    rel_plan = plan_path.relative_to(repo_root)
    rel_context = context_file.relative_to(repo_root)
    rel_backup = backup_path.relative_to(repo_root)
    base_prompt = (
        "REPLAN MODE: предыдущий план провалился. Прочитай "
        f"`{rel_context}` — это бриф от диспетчера с причинами провала. "
        f"Прочитай `{rel_backup}` — это предыдущий план для справки. "
        f"Затем перепиши `{rel_plan}` под новые обстоятельства, сохранив "
        "оригинальный task_id."
    )

    # До 2 попыток (1 основная + 1 self-heal). На self-heal'е добавляем
    # к prompt'у текст ошибки валидации, чтобы planner понял конкретную
    # причину отбраковки.
    last_error: str | None = None
    invalid_backup_path: Path | None = None
    for sub_attempt in range(2):
        log_file = log_dir / (
            f"replan-{context.replan_attempt}.log"
            if sub_attempt == 0
            else f"replan-{context.replan_attempt}-heal{sub_attempt}.log"
        )
        if sub_attempt == 0:
            prompt = base_prompt
        else:
            heal_hint = _build_heal_hint(last_error or "(unknown)", invalid_backup_path)
            prompt = base_prompt + "\n\n" + heal_hint

        outcome = await runner.run_worker(
            llm=llm,
            cwd=repo_root,
            repo_root=repo_root,
            role="planner",
            prompt=prompt,
            timeout_s=900,
            log_file=log_file,
            effort=effort,
        )
        if outcome.timed_out:
            raise RuntimeError(
                f"replan: orchX-planner timed out after {outcome.duration_s:.0f}s; "
                f"see log at {log_file}"
            )
        if outcome.returncode != 0:
            raise RuntimeError(
                f"replan: orchX-planner exited with code {outcome.returncode}; "
                f"see log at {log_file}"
            )
        if not plan_path.exists():
            raise RuntimeError(
                f"replan: planner did not write {plan_path}; see log at {log_file}"
            )
        try:
            new_plan = load_plan(plan_path)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = f"{type(e).__name__}: {e}"
            # Сохраним невалидный план под именем .invalid-N.json для
            # forensics и для следующего вызова planner'а как backup.
            invalid_backup_path = run_dir / (
                f"plan.before-replan-{context.replan_attempt}"
                f".invalid-{sub_attempt + 1}.json"
            )
            try:
                invalid_backup_path.write_text(
                    plan_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError:
                pass
            try:
                plan_path.unlink()
            except OSError:
                pass
            logger.warning(
                "replan: planner wrote invalid plan (attempt %d/2): %s",
                sub_attempt + 1,
                last_error,
            )
            if sub_attempt == 0:
                # Идём на self-heal retry.
                continue
            raise RuntimeError(
                f"replan: planner wrote invalid plan after 1 self-heal retry: "
                f"{last_error}"
            ) from e
        # Валидный план получен.
        if new_plan.task_id != context.plan.task_id:
            # Защита: иначе оркестратор начнёт новую интеграционную ветку.
            raise RuntimeError(
                f"replan: planner changed task_id "
                f"({context.plan.task_id!r} → {new_plan.task_id!r}); refusing"
            )
        return new_plan

    # Достижимо только если цикл не вернул раньше — defensive.
    raise RuntimeError(
        f"replan: unreachable — exhausted retries without verdict: {last_error}"
    )


def _build_heal_hint(error_text: str, invalid_backup: Path | None) -> str:
    """Сформировать секцию для повторного prompt'а planner'а с описанием ошибки.

    Включает конкретное сообщение валидатора + ссылки на общие правила
    схемы, нарушенные в прошлом прогоне.
    """
    backup_ref = (
        f"Твой предыдущий невалидный план сохранён в `{invalid_backup}` — "
        f"открой его и прочитай, чтобы понять, что именно нарушено."
        if invalid_backup
        else ""
    )
    return (
        "## SELF-HEAL: предыдущий план не прошёл валидацию\n\n"
        f"**Ошибка от validator'а:** `{error_text}`\n\n"
        f"{backup_ref}\n\n"
        "### Типовые причины и фиксы\n\n"
        "1. **`depends_on '<id>' not in this phase`** — task в одной фазе "
        "ссылается на id из другой фазы. Схема orchX **запрещает** cross-phase "
        "depends_on на уровне tasks: зависимость между фазами выражается через "
        "поле `depends_on` САМОЙ фазы (по умолчанию каждая фаза неявно зависит "
        "от предыдущей в порядке плана). Если задаче из p4 нужен результат "
        "p3 — это автоматически гарантировано порядком фаз; убирай ссылку из "
        "`depends_on` task'а.\n\n"
        "2. **`Duplicate task ids across phases`** — глобальная уникальность "
        "id ОБЯЗАТЕЛЬНА. Если переиспользуешь имя из исходного плана "
        "(например, `api-admin-db`) — это конфликт; добавь суффикс "
        "(`api-admin-db-v2`).\n\n"
        "3. **`Phase <id>: depends_on '<dep>' unknown`** — `phase.depends_on` "
        "должен указывать на УЖЕ ОБЪЯВЛЕННУЮ выше фазу.\n\n"
        "4. **Циклы в depends_on** — task A зависит от B, B от A. Перестрой "
        "DAG: вынеси общую часть в третью task'у.\n\n"
        "5. **`max_wall_seconds exceeds hardcap`** — не больше 86400s (24h).\n\n"
        "Перепиши `plan.json` ещё раз, устранив ошибку. ВАЖНО: не меняй "
        "`task_id`, не дублируй уже завершённые фазы из `## Что уже сделано` "
        "секции бриф'а. Финальная реплика — `plan written`."
    )
