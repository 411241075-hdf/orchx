"""Лёгкий TUI для Kilo orchX: цвета, спиннер, live-доска фаз/задач.

Без внешних зависимостей: только ANSI escape codes + asyncio. Если stdout не
TTY (пайп, CI), всё деградирует до plain-text построчного журнала.

Идея:

* ``noise_filter`` — отбрасывает мусорные строки из stderr дочернего kilo
  (wasm-предупреждения bun-runtime, повторяющиеся `Aborted(Error: ...)`),
  чтобы они не засоряли терминал пользователя.
* ``Spinner`` — асинхронный одноstring-спиннер для долгих операций
  (planner, push, gh pr create).
* ``LiveBoard`` — асинхронный лайв-рендер прогресса роя: фазы и задачи в них,
  статусы, длительности.
* ``print_*`` — форматированные блоки (баннер, итоговая сводка, PR-инфо).

Все рендер-функции ничего не делают в no-TTY режиме, кроме построчного
вывода ключевых событий.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"


def _is_color_supported() -> bool:
    """Включены ли ANSI-цвета и live-рендер.

    Уважает ``NO_COLOR``, ``ORCHX_NO_TUI``, и проверяет TTY.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("ORCHX_NO_TUI"):
        return False
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if term in ("", "dumb"):
        return False
    return True


_COLOR = _is_color_supported()


def _c(code: str, text: str) -> str:
    """Обернуть текст ANSI-кодом, если терминал поддерживает цвета."""
    if not _COLOR:
        return text
    return f"{code}{text}{_RESET}"


def bold(text: str) -> str:
    """Сделать текст жирным (если терминал поддерживает цвета)."""
    return _c(_BOLD, text)


def dim(text: str) -> str:
    """Сделать текст приглушённым."""
    return _c(_DIM, text)


def red(text: str) -> str:
    """Покрасить текст в красный."""
    return _c("\x1b[31m", text)


def green(text: str) -> str:
    """Покрасить текст в зелёный."""
    return _c("\x1b[32m", text)


def yellow(text: str) -> str:
    """Покрасить текст в жёлтый."""
    return _c("\x1b[33m", text)


def blue(text: str) -> str:
    """Покрасить текст в синий."""
    return _c("\x1b[34m", text)


def magenta(text: str) -> str:
    """Покрасить текст в фуксию."""
    return _c("\x1b[35m", text)


def cyan(text: str) -> str:
    """Покрасить текст в циан."""
    return _c("\x1b[36m", text)


def gray(text: str) -> str:
    """Покрасить текст в серый."""
    return _c("\x1b[90m", text)


# ---------------------------------------------------------------------------
# Noise filter (legacy)
# ---------------------------------------------------------------------------

# Раньше эти функции отфильтровывали bun-runtime / tree-sitter wasm-варнинги
# из stderr дочернего kilo CLI. После миграции на in-process воркер
# дочернего процесса нет — на on_activity приходят чистые text-дельты от
# LLM. Функции оставлены как no-op для совместимости со старым кодом
# (drain_to_log и пр.) и могут быть удалены в следующей зачистке.


def is_noise_line(line: str) -> bool:  # noqa: ARG001 — параметр для совместимости
    """No-op (kept for backwards compatibility, see module docstring)."""
    return False


def filter_noise(text: str) -> str:
    """No-op (kept for backwards compatibility, see module docstring)."""
    return text


# ---------------------------------------------------------------------------
# Basic print helpers
# ---------------------------------------------------------------------------


def _term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # noqa: BLE001
        return default


def out(line: str = "") -> None:
    """Печать строки в stdout с flush."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def err(line: str) -> None:
    """Печать строки в stderr с flush."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def banner(title: str, subtitle: str = "") -> None:
    """Печатает выделенный заголовок секции."""
    width = max(40, min(_term_width(), 100))
    bar = "─" * width
    out("")
    out(cyan(bar))
    out(f" {bold(title)}" + (f"  {dim(subtitle)}" if subtitle else ""))
    out(cyan(bar))


def kv(label: str, value: str) -> None:
    """Печать пары «ключ: значение» в выровненном виде."""
    out(f"  {dim(label + ':'):<22} {value}")


def hr() -> None:
    """Тонкая горизонтальная линия."""
    width = max(40, min(_term_width(), 100))
    out(dim("─" * width))


# ---------------------------------------------------------------------------
# Status glyphs
# ---------------------------------------------------------------------------

_GLYPHS = {
    "pending": gray("·"),
    "running": cyan("▶"),
    "success": green("✓"),
    "failed": red("✗"),
    "skipped": yellow("⊘"),
}


def status_glyph(status: str) -> str:
    """Вернуть глиф (с цветом) для статуса задачи (running/success/failed/...)."""
    return _GLYPHS.get(status, dim("?"))


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------


_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class Spinner:
    """Одно-строчный спиннер с обновляемым подзаголовком.

    Если TUI отключён, печатает один раз сообщение и работает как no-op.

    Использование как ``async with``::

        async with Spinner("doing X"):
            await long_op()

    Или вручную (для случаев, когда жизненный цикл не совпадает с одним
    блоком кода — например, спиннер закрывается из callback'а)::

        sp = Spinner("doing X")
        task = asyncio.create_task(sp._run())
        sp._task = task
        ...
        sp._stop.set()
        await task
    """

    def __init__(self, message: str) -> None:
        """Создать спиннер с начальной строкой ``message``."""
        self.message = message
        self.detail = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._started = time.monotonic()

    def update(self, detail: str) -> None:
        """Обновить вспомогательную подпись (печатается рядом со спиннером)."""
        self.detail = detail

    def stop(self) -> None:
        """Сигнализировать спиннеру завершиться. Сам task надо await-ить отдельно."""
        self._stop.set()

    async def _run(self) -> None:
        if not _COLOR:
            out(f"… {self.message}")
            await self._stop.wait()
            return
        sys.stdout.write(_HIDE_CURSOR)
        i = 0
        try:
            while not self._stop.is_set():
                elapsed = time.monotonic() - self._started
                frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
                detail = f"  {dim(self.detail)}" if self.detail else ""
                line = (
                    f"\r{cyan(frame)} {self.message}  {dim(f'{elapsed:5.1f}s')}{detail}"
                )
                # Обрезаем под ширину терминала, чтобы не наплодить переносов.
                width = _term_width()
                if len(_strip_ansi(line)) > width:
                    line = line[: width + 20]  # +20 — запас под ANSI-коды
                sys.stdout.write("\x1b[2K" + line)
                sys.stdout.flush()
                i += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.1)
                except TimeoutError:
                    pass
        finally:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.write(_SHOW_CURSOR)
            sys.stdout.flush()

    async def __aenter__(self) -> Spinner:
        """Старт спиннера через ``async with``."""
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Остановить спиннер и дождаться завершения фоновой корутины."""
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)


# ---------------------------------------------------------------------------
# Live board
# ---------------------------------------------------------------------------


@dataclass
class _BoardSnapshot:
    """Чисто-данные срез контекста, чтобы не держать lock на ctx."""

    title: str
    phases: list[dict]
    review_status: str
    review_label: str
    elapsed_s: float
    aborted_reason: str


class LiveBoard:
    """Лайв-рендер прогресса роя на основе OrchXContext.

    Использование::

        board = LiveBoard(ctx)
        async with board.run():
            await orchestrator_main_loop(ctx)
        board.print_final()

    В no-TTY режиме переключается на событийный лог: при изменении статуса
    задачи/фазы печатается одна строка.
    """

    def __init__(self, ctx, refresh_s: float = 0.4) -> None:
        """Создать live-доску для ``ctx`` с интервалом обновления ``refresh_s``."""
        self.ctx = ctx
        self.refresh_s = refresh_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_rendered_lines = 0
        self._last_seen_status: dict[str, str] = {}

    # ---- snapshotting ----

    def _snapshot(self) -> _BoardSnapshot:
        phases: list[dict] = []
        for ph_id in [p.id for p in self.ctx.plan.phases]:
            ps = self.ctx.phase_states.get(ph_id)
            if ps is None:
                continue
            tasks = []
            for tid in ps.task_ids:
                ts = self.ctx.states.get(tid)
                if ts is None:
                    continue
                tasks.append(
                    {
                        "id": tid,
                        "agent": ts.spec.agent,
                        "status": ts.status,
                        "attempts": ts.attempt_count,
                        "activity": getattr(ts, "current_activity", "") or "",
                    }
                )
            # PhaseSpec не имеет отдельного name — используем goal как читаемое
            # описание (короткое предложение из планнера), id — fallback.
            display_name = (
                getattr(ps.spec, "goal", None)
                or getattr(ps.spec, "name", None)
                or ph_id
            )
            phases.append(
                {
                    "id": ph_id,
                    "name": display_name,
                    "status": ps.status,
                    "tasks": tasks,
                }
            )
        review_status = "pending"
        review_label = ""
        if self.ctx.review_state is not None:
            review_status = self.ctx.review_state.status
            review_label = self.ctx.review_state.spec.id
        elapsed = max(0.0, time.monotonic() - (self.ctx.started_at or time.monotonic()))
        return _BoardSnapshot(
            title=self.ctx.plan.task_id,
            phases=phases,
            review_status=review_status,
            review_label=review_label,
            elapsed_s=elapsed,
            aborted_reason=self.ctx.abort_reason if self.ctx.aborted else "",
        )

    # ---- rendering ----

    def _render(self, snap: _BoardSnapshot) -> list[str]:
        lines: list[str] = []
        width = max(40, min(_term_width(), 100))
        # Считаем активные/выполненные задачи для header'а.
        # Все задачи плана, без фильтрации по фазам — total должен
        # отражать реальный масштаб работы (phased-план).
        all_tasks = [t for ph in snap.phases for t in ph["tasks"]]
        running = sum(1 for t in all_tasks if t["status"] == "running")
        success = sum(1 for t in all_tasks if t["status"] == "success")
        failed = sum(1 for t in all_tasks if t["status"] == "failed")
        skipped = sum(1 for t in all_tasks if t["status"] == "skipped")
        pending = sum(1 for t in all_tasks if t["status"] == "pending")
        total = len(all_tasks)
        # Текущая фаза для подсказки прогресса по фазам.
        current_phase = next(
            (ph for ph in snap.phases if ph["status"] == "running"), None
        )
        ph_done = sum(1 for ph in snap.phases if ph["status"] == "success")
        ph_total = len(snap.phases)
        phase_marker = (
            f"{cyan('phase ' + str(ph_done + 1) + '/' + str(ph_total))}"
            if current_phase
            else f"{cyan('phases ' + str(ph_done) + '/' + str(ph_total))}"
        )
        header = (
            f"{bold('🛰  orchX')} {cyan(snap.title)}  "
            f"{dim(f'· {snap.elapsed_s:6.1f}s')}  "
            f"{phase_marker}  "
            f"{cyan('▶' + str(running))} "
            f"{green('✓' + str(success))} "
            f"{red('✗' + str(failed))} "
            f"{yellow('⊘' + str(skipped))} "
            f"{dim('·' + str(pending))} "
            f"{dim('/ ' + str(total))}"
        )
        if snap.aborted_reason:
            header += f"  {red('ABORTED: ' + snap.aborted_reason)}"
        lines.append(header)
        lines.append(dim("─" * width))
        for ph in snap.phases:
            ph_glyph = status_glyph(ph["status"])
            # goal может быть длинным предложением — обрезаем по ширине минус
            # запас под id и глиф.
            name_max = max(20, width - len(ph["id"]) - 8)
            ph_name = ph["name"]
            if len(ph_name) > name_max:
                ph_name = ph_name[: name_max - 1] + "…"
            lines.append(f"{ph_glyph} {bold(ph_name)}  {dim('(' + ph['id'] + ')')}")
            for t in ph["tasks"]:
                glyph = status_glyph(t["status"])
                colored_status = _colorize_status(t["status"])
                attempts = (
                    f" {dim('try ' + str(t['attempts']))}" if t["attempts"] > 1 else ""
                )
                # Для running-задач показываем последнюю «полезную» строку из
                # stdout воркера — это даёт пользователю чёткий сигнал, что
                # процесс жив (Read/Glob/Grep/Write…).
                activity = ""
                if t["status"] == "running" and t.get("activity"):
                    act_max = max(20, width - 50)
                    act = t["activity"]
                    if len(act) > act_max:
                        act = act[: act_max - 1] + "…"
                    activity = f"  {dim('· ' + act)}"
                lines.append(
                    f"    {glyph} {t['id']:<28} {dim(t['agent'])}  "
                    f"{colored_status}{attempts}{activity}"
                )
        if snap.review_label:
            r_glyph = status_glyph(snap.review_status)
            lines.append("")
            lines.append(
                f"{r_glyph} {bold('reviewer')}  "
                f"{dim(snap.review_label)}  {_colorize_status(snap.review_status)}"
            )
        return lines

    async def _loop(self) -> None:
        if not _COLOR:
            # Event-mode: только маркеры изменений статусов.
            while not self._stop.is_set():
                self._diff_print()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_s)
                except TimeoutError:
                    pass
            self._diff_print()
            return
        sys.stdout.write(_HIDE_CURSOR)
        try:
            while not self._stop.is_set():
                self._redraw()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_s)
                except TimeoutError:
                    pass
            self._redraw()
        finally:
            sys.stdout.write(_SHOW_CURSOR)
            sys.stdout.flush()

    def _redraw(self) -> None:
        snap = self._snapshot()
        lines = self._render(snap)
        # Стереть прошлый блок.
        if self._last_rendered_lines:
            sys.stdout.write(f"\x1b[{self._last_rendered_lines}A")
            for _ in range(self._last_rendered_lines):
                sys.stdout.write("\x1b[2K\n")
            sys.stdout.write(f"\x1b[{self._last_rendered_lines}A")
        for ln in lines:
            sys.stdout.write(ln + "\n")
        sys.stdout.flush()
        self._last_rendered_lines = len(lines)

    def _diff_print(self) -> None:
        """Печать только изменений статусов (для не-TTY режима)."""
        snap = self._snapshot()
        for ph in snap.phases:
            key_p = f"phase:{ph['id']}"
            if self._last_seen_status.get(key_p) != ph["status"]:
                out(f"[phase] {ph['name']} " f"({ph['id']}) -> {ph['status']}")
                self._last_seen_status[key_p] = ph["status"]
            for t in ph["tasks"]:
                key_t = f"task:{t['id']}"
                if self._last_seen_status.get(key_t) != t["status"]:
                    out(f"[task]  {t['id']:<28} {t['agent']:<24} -> {t['status']}")
                    self._last_seen_status[key_t] = t["status"]
        if snap.review_label:
            key_r = "review"
            if self._last_seen_status.get(key_r) != snap.review_status:
                out(f"[review] {snap.review_label} -> {snap.review_status}")
                self._last_seen_status[key_r] = snap.review_status

    # ---- lifecycle ----

    def start(self) -> None:
        """Запустить фоновый рендер-loop. Идемпотентно."""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Остановить рендер-loop и дождаться завершения."""
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @asynccontextmanager
    async def run(self):
        """Контекст-менеджер для использования в линейном async-коде."""
        self.start()
        try:
            yield self
        finally:
            await self.stop()


def _colorize_status(status: str) -> str:
    if status == "success":
        return green(status)
    if status == "failed":
        return red(status)
    if status == "running":
        return cyan(status)
    if status == "skipped":
        return yellow(status)
    return dim(status)


# ---------------------------------------------------------------------------
# Stream helpers (capture child output silently into a file)
# ---------------------------------------------------------------------------


async def drain_to_log(
    stream: asyncio.StreamReader | None,
    log_file,
    *,
    on_line=None,
) -> None:
    """Читать stdout/stderr дочернего процесса построчно в файл-лог.

    * Шумные строки (wasm/bun) выкидываются.
    * Каждая «полезная» строка записывается в ``log_file`` (open file-handle).
    * Если передан callback ``on_line(line)`` — он вызывается на каждую
      полезную строку (например, чтобы спиннер обновил подзаголовок).
    """
    if stream is None:
        return
    while True:
        raw = await stream.readline()
        if not raw:
            return
        try:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
        except Exception:  # noqa: BLE001
            continue
        if is_noise_line(line):
            continue
        try:
            log_file.write(line + "\n")
            log_file.flush()
        except Exception:  # noqa: BLE001
            pass
        if on_line is not None:
            try:
                on_line(line)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Final summary rendering
# ---------------------------------------------------------------------------


def print_run_summary(summary: dict) -> None:
    """Печать красивого финального резюме прогона роя.

    Не подменяет JSON-лог (он по-прежнему пишется в run-dir), но в терминале
    показывает компактную выжимку.
    """
    banner("orchX summary", summary.get("task_id", ""))
    counts = summary.get("counts", {})
    succ = counts.get("success", 0)
    fail = counts.get("failed", 0)
    skip = counts.get("skipped", 0)
    total = counts.get("total", succ + fail + skip)
    kv("base branch", summary.get("base_branch", ""))
    kv("integration", summary.get("integration_branch", ""))
    kv(
        "tasks",
        f"{green(str(succ))} ok · "
        f"{red(str(fail))} failed · "
        f"{yellow(str(skip))} skipped · "
        f"{dim(str(total))} total",
    )
    kv("retries", str(summary.get("total_retries", 0)))
    review = summary.get("review") or {}
    if review:
        status = review.get("status", "?")
        kv("review", _colorize_status(status))
        report = review.get("report") or {}
        if report:
            blk = report.get("blocking_count", 0)
            non = report.get("non_blocking_count", 0)
            nit = report.get("nit_count", 0)
            findings_summary = (
                f"{red(str(blk) + ' blocking')} · "
                f"{yellow(str(non) + ' non-blocking')} · "
                f"{dim(str(nit) + ' nits')}"
            )
            kv("findings", findings_summary)
    metrics = summary.get("metrics") or {}
    if metrics:
        tokens = metrics.get("total_tokens", 0)
        calls = metrics.get("total_llm_calls", 0)
        compactions = metrics.get("total_compactions", 0)
        if tokens or calls:
            metrics_str = f"{tokens:,} tokens · {calls} llm calls"
            if compactions:
                metrics_str += f" · {compactions} compactions"
            kv("metrics", metrics_str)
        cats = metrics.get("failure_categories") or {}
        if cats:
            cat_str = ", ".join(f"{c}:{n}" for c, n in list(cats.items())[:5])
            kv("failure types", cat_str)
    phases = summary.get("phases") or []
    if phases:
        out("")
        out(f"  {dim('Phases:')}")
        for ph in phases:
            glyph = status_glyph(ph.get("status", "pending"))
            display = ph.get("name") or ph.get("goal") or ph.get("id") or "?"
            out(
                f"    {glyph} {display}  "
                f"{dim('(' + ph.get('id', '') + ')')}  "
                f"{_colorize_status(ph.get('status', 'pending'))}"
            )
    failed_tasks = [
        t for t in (summary.get("tasks") or []) if t.get("status") == "failed"
    ]
    if failed_tasks:
        out("")
        out(f"  {red('Failed tasks:')}")
        for t in failed_tasks:
            note = t.get("notes") or t.get("failure_reason") or ""
            out(f"    {red('✗')} {t.get('id', '?')}  {dim(note[:120])}")


def print_pr_result(pr_result: dict) -> None:
    """Печать результата push+PR (URL или ошибка)."""
    banner("Pull request")
    url = pr_result.get("pr_url") or pr_result.get("url")
    compare_url = pr_result.get("compare_url")
    err_msg = pr_result.get("error")
    meaningful = pr_result.get("diff_meaningful") or []
    artefacts = pr_result.get("diff_artefacts") or []
    if url:
        kv("PR", cyan(url))
    elif compare_url:
        # Без gh — даём compare URL для одноклика.
        kv("create PR", cyan(compare_url))
    if pr_result.get("branch"):
        kv("branch", pr_result["branch"])
    if pr_result.get("commit"):
        kv("commit", pr_result["commit"][:12])
    if meaningful or artefacts:
        kv(
            "diff",
            f"{green(str(len(meaningful)) + ' code')} · "
            f"{dim(str(len(artefacts)) + ' orchX artefacts')}",
        )
    if err_msg:
        # Длинные сообщения переносим на несколько строк, чтобы не ломать layout.
        first, *rest = err_msg.split("\n")
        kv("error", yellow(first) if "gh CLI" in first else red(first))
        for line in rest:
            out(f"  {dim(' ' * 22)} {line}")


def print_intro(task_id: str, base_branch: str, n_phases: int, n_tasks: int) -> None:
    """Стартовый баннер перед прогоном."""
    banner("Kilo orchX", task_id)
    kv("base branch", base_branch)
    kv("phases", str(n_phases))
    kv("tasks", str(n_tasks))


def print_step(title: str) -> None:
    """Печать шага высокого уровня (например, «Planning…», «Pushing PR…»)."""
    out("")
    out(f"{cyan('▶')} {bold(title)}")


def print_done(title: str, detail: str = "") -> None:
    """Печать завершённого шага."""
    suffix = f"  {dim(detail)}" if detail else ""
    out(f"{green('✓')} {title}{suffix}")


def print_warn(title: str) -> None:
    """Напечатать предупреждение жёлтым ``!``-префиксом в stdout."""
    out(f"{yellow('!')} {title}")


def print_error(title: str) -> None:
    """Напечатать ошибку красным ``✗``-префиксом в stderr."""
    err(f"{red('✗')} {title}")


def print_dim(text: str) -> None:
    """Напечатать приглушённый текст в stdout."""
    out(dim(text))


# ---------------------------------------------------------------------------
# Convenience: iterate over Iterable safely (kept for tests / future use)
# ---------------------------------------------------------------------------


def _join_lines(lines: Iterable[str]) -> str:
    return "\n".join(lines)
