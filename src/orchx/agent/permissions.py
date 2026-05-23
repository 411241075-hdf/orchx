"""Permission-модель для воркера orchX.

Зеркалит то, что раньше делал kilo на основе frontmatter ``permission:``-блока.

Модель преднамеренно простая:
- Скаляр ``allow`` / ``deny`` для tool'ов без под-параметров (read, glob, …).
- Allow/deny + opt-glob-словарь для ``edit`` (path-gating).
- Allow-list-словарь команд для ``bash`` (default ``"*": deny``).

Формат словаря-allow-list — порядок не важен: матчер сортирует правила
по «специфичности» (длина паттерна, отсутствие ``*``).

Bash sandbox матчит **полную строку команды**, не разбивая её по ``|``/``;``.
Это совпадает с поведением kilo и под это писались agent-файлы.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any


def _truthy(val: Any, default: bool) -> bool:
    """Прочитать allow/deny-скаляр (str) или bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "allow"
    return default


@dataclass
class Permissions:
    """Разрешения, выводящиеся из frontmatter agent-файла."""

    read: bool = True
    glob: bool = True
    grep: bool = True
    semantic_search: bool = False
    codesearch: bool = True
    webfetch: bool = False
    websearch: bool = False
    task: bool = False
    edit: bool | dict[str, str] = True
    """Если ``True`` — разрешён любой путь. Если dict — glob → ``allow``/``deny``.

    В dict-форме порядок значений — самые специфичные паттерны первыми;
    ``"*"`` идёт последним fallback'ом.
    """

    bash: dict[str, str] = field(default_factory=lambda: {"*": "deny"})
    """Allow-list bash-команд в формате glob → allow/deny."""

    def edit_allowed(self, rel_path: str) -> bool:
        """Разрешено ли редактировать файл по относительному пути."""
        if isinstance(self.edit, bool):
            return self.edit
        rules = sorted(
            self.edit.items(),
            key=lambda kv: ("*" in kv[0], -len(kv[0])),
        )
        for pattern, action in rules:
            if fnmatch.fnmatchcase(rel_path, pattern):
                return action == "allow"
        return False

    def bash_allowed(self, command: str) -> tuple[bool, str | None]:
        """Разрешена ли bash-команда.

        Returns:
            (allowed, matched_pattern). Если ни одно правило не сматчилось —
            ``(False, None)`` (deny by default).
        """
        rules = sorted(
            self.bash.items(),
            key=lambda kv: ("*" == kv[0], -len(kv[0])),
        )
        for pattern, action in rules:
            if fnmatch.fnmatchcase(command, pattern):
                return (action == "allow", pattern)
        return (False, None)


def parse_permissions(raw: dict[str, Any]) -> Permissions:
    """Сконвертировать frontmatter-словарь в :class:`Permissions`."""
    p = Permissions()
    p.read = _truthy(raw.get("read", "allow"), True)
    p.glob = _truthy(raw.get("glob", "allow"), True)
    p.grep = _truthy(raw.get("grep", "allow"), True)
    p.semantic_search = _truthy(raw.get("semantic_search", "deny"), False)
    p.codesearch = _truthy(raw.get("codesearch", "allow"), True)
    p.webfetch = _truthy(raw.get("webfetch", "deny"), False)
    p.websearch = _truthy(raw.get("websearch", "deny"), False)
    p.task = _truthy(raw.get("task", "deny"), False)

    edit_raw = raw.get("edit", "allow")
    if isinstance(edit_raw, dict):
        # Сохраняем как есть: glob → "allow"/"deny" (строки).
        p.edit = {str(k): str(v).strip().lower() for k, v in edit_raw.items()}
    else:
        p.edit = _truthy(edit_raw, True)

    bash_raw = raw.get("bash")
    if isinstance(bash_raw, dict):
        p.bash = {str(k): str(v).strip().lower() for k, v in bash_raw.items()}
    elif isinstance(bash_raw, (str, bool)):
        # bash: allow|deny — превратим в один шаблон "*".
        allowed = _truthy(bash_raw, False)
        p.bash = {"*": "allow" if allowed else "deny"}
    else:
        p.bash = {"*": "deny"}
    return p


def describe_permissions(p: Permissions) -> str:
    """Человекочитаемое описание для system-prompt'а."""
    lines: list[str] = []
    flags = [
        ("read", p.read),
        ("glob", p.glob),
        ("grep", p.grep),
        ("codesearch", p.codesearch),
    ]
    if p.semantic_search:
        flags.append(("semantic_search", True))
    lines.append("- Read tools: " + ", ".join(name for name, ok in flags if ok))
    if isinstance(p.edit, bool):
        lines.append(f"- edit: {'allowed' if p.edit else 'DENIED'}")
    else:
        allowed = [g for g, a in p.edit.items() if a == "allow"]
        denied = [g for g, a in p.edit.items() if a == "deny"]
        lines.append(
            "- edit: path-gated (allowed: "
            + (", ".join(allowed) or "—")
            + (f"; denied: {', '.join(denied)}" if denied else "")
            + ")"
        )
    allowed_bash = [g for g, a in p.bash.items() if a == "allow"]
    if allowed_bash:
        lines.append("- bash allow-list: " + ", ".join(allowed_bash))
    else:
        lines.append("- bash: no commands allowed")
    return "\n".join(lines)
