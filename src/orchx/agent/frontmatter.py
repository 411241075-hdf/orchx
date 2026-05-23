"""Парсер ``.kilo/agent/orchX-<role>.md`` — YAML-frontmatter + markdown body.

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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .permissions import Permissions, parse_permissions

KILO_AGENT_PREFIX = "orchX-"
"""Все agent-файлы лежат как ``.kilo/agent/orchX-<role>.md``."""


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

    max_steps: int = 80
    permissions: Permissions = field(default_factory=Permissions)


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


def parse_agent_markdown(text: str, *, role: str, name: str) -> AgentSpec:
    """Превратить содержимое .md-файла в :class:`AgentSpec`.

    Args:
        text: Полное содержимое файла (UTF-8).
        role: Короткое имя роли (например, ``implementer``).
        name: Полное имя агента (например, ``orchX-implementer``).
    """
    import yaml  # ленивый импорт — у нас он есть транзитивно через langchain

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
    return AgentSpec(
        name=name,
        role=role,
        description=description,
        body=body,
        max_steps=max_steps,
        permissions=perms,
    )


def load_agent_spec(role: str, repo_root: Path) -> AgentSpec:
    """Загрузить ``.kilo/agent/orchX-<role>.md`` и распарсить его.

    Raises:
        FileNotFoundError: Если файл агента не найден.
    """
    name = f"{KILO_AGENT_PREFIX}{role}"
    path = repo_root / ".kilo" / "agent" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"orchX agent spec not found: {path}. Expected {name}.md under .kilo/agent/."
        )
    return parse_agent_markdown(
        path.read_text(encoding="utf-8"),
        role=role,
        name=name,
    )
