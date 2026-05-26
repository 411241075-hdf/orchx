"""Предзагрузка фрагментов кода в task.md (ANALYSIS.md §5.1.C).

Воркеры в orchX-worktree запускаются с холодным контекстом. На больших
файлах (`backend/api/endpoints.py` ~6000 строк, `frontend/src/App.jsx`
~3000) каждый воркер заново делает 3-5 grep'ов, чтобы найти уже известное
место — это >30% потерянных LLM-итераций.

Этот модуль вырезает из `inputs`-путей нужные фрагменты и встраивает их
прямо в `task.md` под секцией `## Pre-loaded context`. Воркер видит код
сразу, без тулинговой разведки.

Поддерживает три формата `inputs[i]`:

1. ``"backend/api/endpoints.py"`` — путь без range. Если файл маленький
   (<= ``SMALL_FILE_LINE_LIMIT``), вставляем целиком; иначе оставляем
   только путь (воркер сам прочитает нужное место через `read`).
2. ``"backend/api/endpoints.py:4880-5010"`` — путь + range «start-end»
   (1-индексированные строки, включительно). Вставляем выдержку.
3. ``{"path": "...", "lines": [start, end]}`` — то же что (2), но в JSON.

Кеш: одинаковый ``(repo_root, path, range)`` рендерится один раз за
прогон (см. ``_excerpt_cache``). Если две задачи в плане ссылаются на
один и тот же фрагмент, второй раз файл не читается.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Максимальный размер «маленького» файла, который встраиваем целиком.
# Большие файлы оставляем на чтение через `read`-tool, чтобы task.md не
# раздулся до десятков KB и не съел весь контекстный бюджет воркера.
SMALL_FILE_LINE_LIMIT = 200

# Если range запрошен, расширяем его на N строк в обе стороны для
# дополнительного контекста (сигнатуры функции выше, конец блока ниже).
# Берём ±10 строк по умолчанию, но не выходим за границы файла.
RANGE_PADDING = 10

# Жёсткий лимит на размер вставки (в строках) — чтобы один inputs[]
# случайно не съел весь task.md контекст.
MAX_EXCERPT_LINES = 400


@dataclass
class _Excerpt:
    """Распарсенный input + извлечённый текст файла."""

    raw: str
    """Исходное значение из ``inputs[]`` (для отображения в task.md)."""
    path: Path
    start_line: int | None  # 1-based; None = весь файл
    end_line: int | None
    text: str  # содержимое выдержки
    truncated: bool
    notice: str = ""
    """Доп. сообщение (например, «file too large, showing head»)."""


_INPUT_RANGE_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)-(?P<end>\d+)$")


def _parse_input(raw_input: object) -> tuple[str, int | None, int | None] | None:
    """Распарсить один элемент ``inputs[]`` в (path, start, end).

    Возвращает None, если формат не распознан (тогда не пытаемся
    встраивать).
    """
    if isinstance(raw_input, dict):
        path = raw_input.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        lines = raw_input.get("lines")
        if isinstance(lines, (list, tuple)) and len(lines) == 2:
            try:
                return path, int(lines[0]), int(lines[1])
            except (TypeError, ValueError):
                return path, None, None
        return path, None, None
    if isinstance(raw_input, str):
        s = raw_input.strip()
        if not s:
            return None
        m = _INPUT_RANGE_RE.match(s)
        if m:
            try:
                return m.group("path"), int(m.group("start")), int(m.group("end"))
            except ValueError:
                return s, None, None
        return s, None, None
    return None


def _detect_lang(path: Path) -> str:
    """Угадать language tag для markdown-блока кода."""
    suffix = path.suffix.lstrip(".")
    mapping = {
        "py": "python",
        "pyi": "python",
        "ts": "typescript",
        "tsx": "tsx",
        "js": "javascript",
        "jsx": "jsx",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "kt": "kotlin",
        "rb": "ruby",
        "sh": "bash",
        "yaml": "yaml",
        "yml": "yaml",
        "json": "json",
        "toml": "toml",
        "md": "markdown",
        "sql": "sql",
        "html": "html",
        "css": "css",
        "scss": "scss",
    }
    return mapping.get(suffix, "")


def _read_excerpt(
    full_path: Path,
    start: int | None,
    end: int | None,
) -> tuple[str, bool, str]:
    """Прочитать выдержку из файла.

    Returns:
        (text, truncated, notice) — текст выдержки, флаг обрезки и
        опциональное сообщение для пользователя.
    """
    if not full_path.exists() or not full_path.is_file():
        return "", False, "file not found"
    try:
        all_text = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return "", False, f"failed to read: {e}"
    lines = all_text.splitlines()
    total = len(lines)
    if start is None or end is None:
        # Без range: маленькие файлы — целиком, большие — head с пометкой.
        if total <= SMALL_FILE_LINE_LIMIT:
            return all_text, False, ""
        head = "\n".join(lines[:SMALL_FILE_LINE_LIMIT])
        return (
            head,
            True,
            f"file is {total} lines; showing first {SMALL_FILE_LINE_LIMIT}. "
            "Use `read` tool with offset/limit for the rest.",
        )
    # С range: расширяем ±RANGE_PADDING строк, но не за пределы файла.
    actual_start = max(1, start - RANGE_PADDING)
    actual_end = min(total, end + RANGE_PADDING)
    span = actual_end - actual_start + 1
    if span > MAX_EXCERPT_LINES:
        # Обрезаем сверху и снизу симметрично: фокус — оригинальный range.
        actual_start = max(start - (MAX_EXCERPT_LINES // 2), 1)
        actual_end = min(end + (MAX_EXCERPT_LINES // 2), total)
        if actual_end - actual_start + 1 > MAX_EXCERPT_LINES:
            actual_end = actual_start + MAX_EXCERPT_LINES - 1
    excerpt_lines = lines[actual_start - 1 : actual_end]
    notice = ""
    expected_start = max(1, start - RANGE_PADDING)
    expected_end = min(total, end + RANGE_PADDING)
    if actual_start != expected_start or actual_end != expected_end:
        notice = (
            f"requested {start}-{end}, showing {actual_start}-{actual_end} "
            f"(capped at {MAX_EXCERPT_LINES} lines)."
        )
    return "\n".join(excerpt_lines), False, notice


def render_preloaded_context(
    repo_root: Path,
    worktree_root: Path,
    inputs: tuple[str, ...] | list[object],
    *,
    cache: dict[tuple[str, int | None, int | None], _Excerpt] | None = None,
) -> str:
    """Сгенерировать markdown-блок с предзагруженным кодом.

    Args:
        repo_root: корень репозитория (используется для резолва путей,
            которые не попали в worktree, например runbook'и из соседней
            ветки — но это редкий кейс).
        worktree_root: корень worktree воркера. Прежде чем читать из
            ``repo_root``, проверяем здесь — у воркера есть локальный чек-аут.
        inputs: значения из ``TaskSpec.inputs``. Поддерживаемые форматы
            см. в module docstring.
        cache: per-run-кэш (опционально). Если передан, повторные
            ссылки на один и тот же ``(path, range)`` не читают файл.
            Ключ — ``(rel_path, start, end)``.

    Returns:
        Готовый markdown-фрагмент (без trailing newline). Пустая строка,
        если в inputs нет ничего, что можно встроить.
    """
    if not inputs:
        return ""
    parts: list[str] = []
    for raw in inputs:
        parsed = _parse_input(raw)
        if parsed is None:
            continue
        rel_path, start, end = parsed
        cache_key = (rel_path, start, end)
        if cache is not None and cache_key in cache:
            excerpt = cache[cache_key]
        else:
            # Сначала пробуем worktree (там свежая ветка задачи); fallback —
            # repo_root. Это важно, когда задача создаёт новый файл и
            # ссылается на него же в inputs (редкий кейс, но возможный).
            candidate_paths = [worktree_root / rel_path, repo_root / rel_path]
            full_path = next((p for p in candidate_paths if p.exists()), candidate_paths[0])
            text, truncated, notice = _read_excerpt(full_path, start, end)
            excerpt = _Excerpt(
                raw=raw if isinstance(raw, str) else f"{rel_path}:{start}-{end}",
                path=Path(rel_path),
                start_line=start,
                end_line=end,
                text=text,
                truncated=truncated,
                notice=notice,
            )
            if cache is not None:
                cache[cache_key] = excerpt
        rendered = _render_one_excerpt(excerpt)
        if rendered:
            parts.append(rendered)
    if not parts:
        return ""
    header = (
        "## Pre-loaded context\n\n"
        "Эти выдержки кода уже прочитаны для тебя — НЕ нужно делать `read`/`grep` "
        "для них заново. Файлы доступны через обычный `read` если нужны другие "
        "участки.\n"
    )
    return header + "\n".join(parts)


def _render_one_excerpt(exc: _Excerpt) -> str:
    """Один блок ``### path[:lines]`` с code-fence."""
    if not exc.text:
        if exc.notice:
            return f"### `{exc.raw}`\n\n_({exc.notice})_\n"
        return ""
    title_path = str(exc.path)
    if exc.start_line and exc.end_line:
        title_path += f":{exc.start_line}-{exc.end_line}"
    lang = _detect_lang(exc.path)
    fence = "```" + lang
    body = [f"### `{title_path}`"]
    if exc.notice:
        body.append("")
        body.append(f"_{exc.notice}_")
    body.append("")
    body.append(fence)
    body.append(exc.text)
    body.append("```")
    return "\n".join(body) + "\n"


__all__ = ["render_preloaded_context"]
