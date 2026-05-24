"""Проверка acceptance-критериев задачи.

Каждый CheckOutcome возвращается с категорией провала (`category`) — это
позволяет диспетчеру и replan-логике различать «провал из-за окружения»
(`env`) от «провал из-за кода» (`cmd_failed`/`pattern_no_match`).
В частности replanner.py при ENV-failure не вызывает planner вторично, а
останавливает рой с advisory message.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import AcceptanceCheck

logger = logging.getLogger(__name__)


# Категории провала. `none` — успех (passed=True).
OutcomeCategory = Literal[
    "none",
    "env",
    "cmd_failed",
    "file_missing",
    "pattern_no_match",
    "timeout",
    "syntax_error",
    "unknown",
]


@dataclass
class CheckOutcome:
    """Результат одной acceptance-проверки.

    Backward compat: старый код, проверяющий `passed`/`description`/`detail`,
    продолжает работать. Новые поля `category`/`hint` доступны без изменения
    сигнатуры — `category` дефолтится по `passed`.
    """

    passed: bool
    description: str
    detail: str
    category: OutcomeCategory = "unknown"
    """Категория провала. `none` означает passed=True."""
    hint: str | None = None
    """Подсказка для debugger'а / реплана. Заполняется только при провале."""


async def run_check(check: AcceptanceCheck, cwd: Path) -> CheckOutcome:
    """Прогнать одну проверку в контексте worktree."""
    if check.type == "command":
        return await _run_command(check, cwd)
    if check.type == "file_exists":
        path = cwd / (check.path or "")
        ok = path.is_file()
        if ok:
            return CheckOutcome(
                passed=True,
                description=check.description,
                detail=f"path={path} exists=True",
                category="none",
            )
        return CheckOutcome(
            passed=False,
            description=check.description,
            detail=f"path={path} not found",
            category="file_missing",
            hint=f"file {check.path!r} was not produced; create it as part of the fix",
        )
    if check.type == "file_contains":
        path = cwd / (check.path or "")
        if not path.is_file():
            return CheckOutcome(
                passed=False,
                description=check.description,
                detail=f"path={path} not found",
                category="file_missing",
                hint=f"file {check.path!r} does not exist; create it first",
            )
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return CheckOutcome(
                passed=False,
                description=check.description,
                detail=f"read error: {e}",
                category="env",
                hint=f"could not read file: {e}",
            )
        match = re.search(check.pattern or "", content)
        if match:
            return CheckOutcome(
                passed=True,
                description=check.description,
                detail=f"pattern={check.pattern!r} matched=True",
                category="none",
            )
        # Прячем «лучшую» близкую строку для debugger-hint'а.
        snippet_hint = _hint_for_pattern_no_match(check.pattern or "", content)
        return CheckOutcome(
            passed=False,
            description=check.description,
            detail=(
                f"pattern={check.pattern!r} did not match. "
                f"file size={len(content)} chars. "
                f"hint={snippet_hint!r}"
                if snippet_hint
                else f"pattern={check.pattern!r} did not match; "
                f"file size={len(content)} chars"
            ),
            category="pattern_no_match",
            hint=(
                f"regex {check.pattern!r} not found in {check.path}. "
                + (
                    f"Closest line: {snippet_hint!r}"
                    if snippet_hint
                    else "No similar line in the file."
                )
                + " Re.DOTALL is OFF — multi-line patterns need explicit "
                + "newline handling. Consider splitting into multiple "
                + "smaller checks."
            ),
        )
    return CheckOutcome(
        passed=False,
        description=check.description,
        detail=f"unknown check type: {check.type}",
        category="unknown",
        hint=f"unsupported acceptance type {check.type!r}",
    )


def _hint_for_pattern_no_match(pattern: str, content: str) -> str | None:
    """Найти строку в файле с максимальным шансом «почти-совпадения».

    Берём первое слово паттерна (без regex-метасимволов) и ищем строку,
    которая его содержит. Это часто помогает debugger'у увидеть, что
    воркер написал близкое имя, но с опечаткой.
    """
    # Извлечь «литеральный» токен из regex'а: первые буквы/цифры/_.
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]+", pattern)
    if not m:
        return None
    token = m.group(0)
    for line in content.splitlines():
        if token in line:
            return line.strip()[:200]
    return None


_ENV_HINT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"Failed to (build|prepare).*jsonschema-rs",
        "ENV: uv не может собрать jsonschema-rs (Rust/PyO3 несовместим с "
        "Python 3.14). Используй уже активированный venv напрямую "
        "(python/python -m pytest), без `uv run`.",
    ),
    (
        r"the configured Python interpreter version \(3\.\d+\) is newer than[\s\S]{0,40}PyO3",
        "ENV: PyO3-пакет не строится на этой версии Python. Acceptance "
        "должен обходиться без пересборки venv (используй `uv run --no-sync` "
        "или прямой `python -m`).",
    ),
    (
        r"command not found: (uv|gh|ruff|pytest|alembic|npm|npx)",
        "ENV: бинарь не найден в PATH. Либо инструмент не установлен, либо "
        "venv не активирован. Acceptance должна использовать инструменты, "
        "которые гарантированно есть.",
    ),
    (
        r"ModuleNotFoundError: No module named ['\"]?(langchain|backend|langgraph|pydantic)",
        "ENV: тяжёлая зависимость не установлена в текущем venv, или "
        "`backend/__init__.py` тащит heavy ML-импорты, которые ломаются "
        "до выполнения проверки. Используй `python -m py_compile` или "
        "`importlib.util.spec_from_file_location` вместо `from backend.X import`.",
    ),
    (
        r"No such file or directory.*(\.venv|venv)",
        "ENV: venv не существует. Активируй venv или используй системный python.",
    ),
)


def _diagnose_output(output: str) -> str:
    """Поискать в выводе типовые «среда сломана»-сигналы и вернуть hint.

    Возвращает пустую строку, если ничего не нашлось.
    """
    for pat, hint in _ENV_HINT_PATTERNS:
        if re.search(pat, output, re.IGNORECASE):
            return hint
    return ""


_SYNTAX_HINT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"SyntaxError",
        "Python SyntaxError — файл не парсится. Поправь синтаксис.",
    ),
    (
        r"IndentationError",
        "Python IndentationError — несогласованные отступы.",
    ),
    (
        r"E\d{3}|F\d{3}|W\d{3}",  # ruff-стиль кода
        "Линтер вернул код-ошибку (ruff/flake8). Запусти линтер и поправь.",
    ),
)


async def _run_command(check: AcceptanceCheck, cwd: Path) -> CheckOutcome:
    cmd = check.command or ""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Real wall-clock timeout: читаем stdout/stderr параллельно (чтобы не
    # упереться в pipe-буферы) и одновременно ждём процесса с
    # `wait_for(proc.wait(), ...)`. На таймауте — kill + сбор partial.
    truncate_at = 64 * 1024  # ~64KB на каждый stream

    async def _read(stream) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        collected = 0
        try:
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    return b"".join(chunks)
                if collected < truncate_at:
                    chunks.append(chunk)
                    collected += len(chunk)
        except asyncio.CancelledError:
            return b"".join(chunks)

    stdout_task = asyncio.create_task(_read(proc.stdout))
    stderr_task = asyncio.create_task(_read(proc.stderr))
    proc_wait = asyncio.create_task(proc.wait())
    try:
        await asyncio.wait_for(proc_wait, timeout=check.timeout_seconds)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        # Соберём то, что успели прочитать.
        for t in (stdout_task, stderr_task):
            t.cancel()
        return CheckOutcome(
            passed=False,
            description=check.description,
            detail=f"timeout after {check.timeout_seconds}s",
            category="timeout",
            hint=(
                f"command timed out after {check.timeout_seconds}s. "
                "Either the command is genuinely slow (raise timeout in "
                "the plan) or it hangs waiting for input/network."
            ),
        )

    async def _safe_await(t: asyncio.Task[bytes]) -> bytes:
        try:
            return await asyncio.wait_for(t, timeout=2.0)
        except TimeoutError, asyncio.CancelledError:
            t.cancel()
            try:
                return await t
            except Exception:  # noqa: BLE001
                return b""

    stdout_b = await _safe_await(stdout_task)
    stderr_b = await _safe_await(stderr_task)
    output = (
        stdout_b.decode(errors="replace") + stderr_b.decode(errors="replace")
    ).strip()
    # Обрезаем длинный вывод для лога.
    snippet = output if len(output) <= 800 else output[:800] + " ...[truncated]"
    detail = f"$ {cmd}\nexit={proc.returncode}\n{snippet}"
    if proc.returncode == 0:
        return CheckOutcome(
            passed=True,
            description=check.description,
            detail=detail,
            category="none",
        )
    # Категоризируем провал.
    env_hint = _diagnose_output(output)
    if env_hint:
        return CheckOutcome(
            passed=False,
            description=check.description,
            detail=f"{detail}\n\n>> HINT: {env_hint}",
            category="env",
            hint=env_hint,
        )
    for pat, syntax_hint in _SYNTAX_HINT_PATTERNS:
        if re.search(pat, output):
            return CheckOutcome(
                passed=False,
                description=check.description,
                detail=detail,
                category="syntax_error",
                hint=syntax_hint,
            )
    return CheckOutcome(
        passed=False,
        description=check.description,
        detail=detail,
        category="cmd_failed",
        hint=(
            f"command exited with {proc.returncode}. "
            f"Read the output above and fix the underlying error."
        ),
    )


async def run_all(checks: tuple[AcceptanceCheck, ...], cwd: Path) -> list[CheckOutcome]:
    """Прогнать все проверки последовательно (порядок важен — следующая может зависеть от предыдущей)."""
    outcomes: list[CheckOutcome] = []
    for check in checks:
        outcome = await run_check(check, cwd)
        outcomes.append(outcome)
        logger.info(
            "[acceptance] %s: %s [%s] — %s",
            "PASS" if outcome.passed else "FAIL",
            outcome.description,
            outcome.category,
            outcome.detail.splitlines()[0] if outcome.detail else "",
        )
    return outcomes


def all_failures_are_env(outcomes: list[CheckOutcome]) -> bool:
    """Все ли провалы среди outcomes — категории `env`?

    Используется replanner'ом: при чисто environment-провалах нет смысла
    переплпнировать (новый план с `uv run` тоже упадёт), нужен exit с
    advisory.
    """
    failed = [o for o in outcomes if not o.passed]
    if not failed:
        return False
    return all(o.category == "env" for o in failed)
