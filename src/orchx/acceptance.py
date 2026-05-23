"""Проверка acceptance-критериев задачи."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .models import AcceptanceCheck

logger = logging.getLogger(__name__)


@dataclass
class CheckOutcome:
    """Результат одной acceptance-проверки."""

    passed: bool
    description: str
    detail: str


async def run_check(check: AcceptanceCheck, cwd: Path) -> CheckOutcome:
    """Прогнать одну проверку в контексте worktree."""
    if check.type == "command":
        return await _run_command(check, cwd)
    if check.type == "file_exists":
        path = cwd / (check.path or "")
        ok = path.is_file()
        return CheckOutcome(
            passed=ok,
            description=check.description,
            detail=f"path={path} exists={ok}",
        )
    if check.type == "file_contains":
        path = cwd / (check.path or "")
        if not path.is_file():
            return CheckOutcome(
                passed=False,
                description=check.description,
                detail=f"path={path} not found",
            )
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return CheckOutcome(
                passed=False, description=check.description, detail=f"read error: {e}"
            )
        match = re.search(check.pattern or "", content)
        return CheckOutcome(
            passed=match is not None,
            description=check.description,
            detail=f"pattern={check.pattern!r} matched={bool(match)}",
        )
    return CheckOutcome(
        passed=False,
        description=check.description,
        detail=f"unknown check type: {check.type}",
    )


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


async def _run_command(check: AcceptanceCheck, cwd: Path) -> CheckOutcome:
    cmd = check.command or ""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=check.timeout_seconds
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return CheckOutcome(
            passed=False,
            description=check.description,
            detail=f"timeout after {check.timeout_seconds}s",
        )
    output = (stdout_b.decode(errors="replace") + stderr_b.decode(errors="replace")).strip()
    # Обрезаем длинный вывод для лога.
    snippet = output if len(output) <= 800 else output[:800] + " ...[truncated]"
    detail = f"$ {cmd}\nexit={proc.returncode}\n{snippet}"
    if proc.returncode != 0:
        hint = _diagnose_output(output)
        if hint:
            detail = f"{detail}\n\n>> HINT: {hint}"
    return CheckOutcome(
        passed=proc.returncode == 0,
        description=check.description,
        detail=detail,
    )


async def run_all(checks: tuple[AcceptanceCheck, ...], cwd: Path) -> list[CheckOutcome]:
    """Прогнать все проверки последовательно (порядок важен — следующая может зависеть от предыдущей)."""
    outcomes: list[CheckOutcome] = []
    for check in checks:
        outcome = await run_check(check, cwd)
        outcomes.append(outcome)
        logger.info(
            "[acceptance] %s: %s — %s",
            "PASS" if outcome.passed else "FAIL",
            outcome.description,
            outcome.detail.splitlines()[0] if outcome.detail else "",
        )
    return outcomes
