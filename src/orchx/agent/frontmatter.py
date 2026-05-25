"""Парсер ``orchX-<role>.md`` — YAML-frontmatter + markdown body.

Формат файла::

    ---
    description: ...
    mode: all
    steps: 80
    permission:
      read: allow
      bash:
        "git status*": allow
        "*": deny
      edit: allow
    ---

    <markdown body — system prompt роли>

Парсер вытаскивает frontmatter (через PyYAML), маппит в :class:`AgentSpec`
+ :class:`Permissions`. Body отдаётся как plain-строка.

Поиск файла промпта каскадный (см. :class:`orchx.runtime.OrchXRuntime`):

1. ``<project>/.orchx/prompts/orchX-<role>.md`` — переопределение под проект.
2. ``<package>/templates/prompts/orchX-<role>.md`` — дефолт пакета.

Это позволяет пользователю кастомизировать prompts для своего стека
(например, заменить примеры под backend/frontend layout своего проекта)
не редактируя сам пакет и не теряя апдейты при upgrade'е orchx.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..runtime import OrchXRuntime
from .permissions import Permissions, parse_permissions

AGENT_PREFIX = "orchX-"
"""Все agent-файлы лежат как ``orchX-<role>.md``."""


@dataclass
class AgentSpec:
    """Распарсенная роль воркера."""

    name: str
    """Полное имя файла-агента, например ``orchX-implementer``."""

    role: str
    """Короткое имя роли (``implementer``, ``planner``, ...)."""

    description: str
    body: str
    """Markdown-тело файла после закрывающего ``---``. Идёт в system prompt."""

    source_path: Path | None = None
    """Откуда был загружен файл (для диагностики/логов)."""

    max_steps: int = 80
    permissions: Permissions = field(default_factory=Permissions)
    mcp_servers: list[dict[str, object]] = field(default_factory=list)
    """P1.1: список MCP-серверов для подключения. Каждый элемент:
    ``{"name": ..., "command": ..., "args": [...], "env": {...}}``."""


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Разделить файл на (yaml_text, body).

    Если нет frontmatter (файл не начинается с ``---``), возвращаем ("", text).
    """
    if not text.startswith("---"):
        return "", text
    # Ищем закрывающий разделитель: строка, состоящая ровно из ``---``.
    lines = text.splitlines(keepends=True)
    # Первая строка — открывающий ``---`` (с любым trailing whitespace).
    if lines[0].rstrip() != "---":
        return "", text
    yaml_lines: list[str] = []
    body_start = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            body_start = i + 1
            break
        yaml_lines.append(line)
    if body_start < 0:
        # Нет закрывающего --- → не валидный frontmatter.
        return "", text
    yaml_text = "".join(yaml_lines)
    body = "".join(lines[body_start:])
    # Снимаем ведущую пустую строку у body (косметика).
    body = body.lstrip("\n")
    return yaml_text, body


def parse_agent_markdown(
    text: str,
    *,
    role: str,
    name: str,
    source_path: Path | None = None,
) -> AgentSpec:
    """Превратить содержимое .md-файла в :class:`AgentSpec`.

    Args:
        text: Полное содержимое файла (UTF-8).
        role: Короткое имя роли (например, ``implementer``).
        name: Полное имя агента (например, ``orchX-implementer``).
        source_path: Откуда загружен файл (для диагностики).
    """
    import yaml  # ленивый импорт — у нас он есть транзитивно через openai

    yaml_text, body = _split_frontmatter(text)
    fm: dict = {}
    if yaml_text.strip():
        try:
            loaded = yaml.safe_load(yaml_text)
            if isinstance(loaded, dict):
                fm = loaded
        except yaml.YAMLError:
            fm = {}
    description = str(fm.get("description") or "").strip()
    try:
        max_steps = int(fm.get("steps", 80))
    except (TypeError, ValueError):
        max_steps = 80
    perms_raw = fm.get("permission") or {}
    perms = (
        parse_permissions(perms_raw) if isinstance(perms_raw, dict) else Permissions()
    )
    mcp_raw = fm.get("mcp_servers") or []
    mcp_servers: list[dict[str, object]] = []
    if isinstance(mcp_raw, list):
        for entry in mcp_raw:
            if isinstance(entry, dict) and entry.get("name"):
                mcp_servers.append(dict(entry))
    return AgentSpec(
        name=name,
        role=role,
        description=description,
        body=body,
        source_path=source_path,
        max_steps=max_steps,
        permissions=perms,
        mcp_servers=mcp_servers,
    )


def _find_agent_file(role: str, runtime: OrchXRuntime) -> Path:
    """Найти файл промпта по каскаду ``runtime.prompts_dirs``.

    Возвращает первый существующий ``orchX-<role>.md``.

    Raises:
        FileNotFoundError: ни в одной директории файл не найден.
    """
    name = f"{AGENT_PREFIX}{role}"
    filename = f"{name}.md"
    tried: list[Path] = []
    for d in runtime.prompts_dirs:
        candidate = d / filename
        tried.append(candidate)
        if candidate.exists():
            return candidate
    tried_lines = "\n  ".join(str(p) for p in tried)
    raise FileNotFoundError(
        f"orchX agent spec not found: {filename}.\n"
        f"Searched (in priority order):\n  {tried_lines}\n"
        f"Hint: run `orchx init` in the project root to create the default "
        f".orchx/prompts/, or copy a template from the orchx package."
    )


def load_agent_spec(role: str, runtime: OrchXRuntime) -> AgentSpec:
    """Загрузить ``orchX-<role>.md`` по каскаду из ``runtime``.

    Args:
        role: Короткое имя роли (``implementer``, ``planner``, ...).
        runtime: Runtime-конфигурация (см. :class:`orchx.runtime.OrchXRuntime`).

    Raises:
        FileNotFoundError: Если ни в одной из ``runtime.prompts_dirs`` нет
            файла ``orchX-<role>.md``.
    """
    path = _find_agent_file(role, runtime)
    name = f"{AGENT_PREFIX}{role}"
    return parse_agent_markdown(
        path.read_text(encoding="utf-8"),
        role=role,
        name=name,
        source_path=path,
    )
