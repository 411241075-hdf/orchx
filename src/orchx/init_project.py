"""Реализация команды ``orchx init``.

Создаёт ``.orchx/`` в корне git-репозитория пользователя, разворачивая туда
дефолтные ресурсы из ``<package>/templates/``: пример ``.env``, шаблон
``PROJECT.md``, копии ролевых промптов (опционально), README.

Также добавляет в ``<project>/.gitignore`` блок ``.orchx/runs/``,
``.orchx/_pending/``, ``.orchx/.env`` (идемпотентно — повторный вызов не
дублирует).

Команда безопасна по умолчанию: НЕ перезаписывает существующие файлы,
только добавляет недостающие. С ``--force`` перезаписывает всё.
С ``--minimal`` создаёт только ``.env``/``PROJECT.md``/README, без копии
дефолтных промптов (тогда орch будет использовать промпты из пакета).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .runtime import RUNTIME_DIR_NAME, ensure_gitignore


@dataclass
class InitReport:
    """Что было создано/пропущено в результате ``orchx init``."""

    runtime_dir: Path
    created: list[Path]
    skipped: list[Path]
    overwritten: list[Path]
    gitignore_updated: bool

    def describe(self) -> str:
        """Сообщение для вывода в TTY."""
        lines = [f"orchX initialized at {self.runtime_dir}"]
        for p in self.created:
            lines.append(f"  + {p.relative_to(self.runtime_dir.parent)}")
        for p in self.overwritten:
            lines.append(f"  ~ {p.relative_to(self.runtime_dir.parent)} (overwritten)")
        for p in self.skipped:
            lines.append(f"  · {p.relative_to(self.runtime_dir.parent)} (kept)")
        if self.gitignore_updated:
            lines.append("  + .gitignore (added orchX block)")
        return "\n".join(lines)


def _package_templates_dir() -> Path:
    """Каталог ``<package>/templates/`` со всеми дефолтами."""
    return Path(__file__).resolve().parent / "templates"


def _copy_file(
    src: Path,
    dst: Path,
    *,
    force: bool,
    report: InitReport,
) -> None:
    """Скопировать ``src`` → ``dst``, обновив отчёт.

    Поведение:
      - dst не существует → копируем, кладём в ``report.created``;
      - dst существует, force=False → пропускаем, кладём в ``report.skipped``;
      - dst существует, force=True → перезаписываем, кладём в ``report.overwritten``.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if not force:
            report.skipped.append(dst)
            return
        shutil.copyfile(src, dst)
        report.overwritten.append(dst)
        return
    shutil.copyfile(src, dst)
    report.created.append(dst)


def _copy_tree(
    src_dir: Path,
    dst_dir: Path,
    *,
    force: bool,
    report: InitReport,
) -> None:
    """Скопировать содержимое директории (только верхний уровень файлов)."""
    if not src_dir.is_dir():
        return
    for entry in sorted(src_dir.iterdir()):
        if entry.is_file():
            _copy_file(entry, dst_dir / entry.name, force=force, report=report)
        elif entry.is_dir():
            _copy_tree(entry, dst_dir / entry.name, force=force, report=report)


def init_project(
    project_root: Path,
    *,
    force: bool = False,
    minimal: bool = False,
) -> InitReport:
    """Развернуть ``.orchx/`` в ``project_root``.

    Args:
        project_root: Корень git-репозитория пользователя (там создаётся
            ``.orchx/``).
        force: Перезаписать существующие файлы (.env, PROJECT.md, prompts/).
        minimal: Не копировать промпты — пусть рой использует дефолты пакета.
            Полезно когда ничего кастомизировать не нужно: меньше шумовых
            файлов в репо пользователя.

    Returns:
        :class:`InitReport` с подробностями.
    """
    runtime_dir = project_root / RUNTIME_DIR_NAME
    runtime_dir.mkdir(parents=True, exist_ok=True)

    report = InitReport(
        runtime_dir=runtime_dir,
        created=[],
        skipped=[],
        overwritten=[],
        gitignore_updated=False,
    )

    templates = _package_templates_dir()

    # 1. Корневые файлы: .env.example, PROJECT.md, README.md, config.yaml.
    for entry in ("env.example", "PROJECT.md", "README.md", "config.yaml"):
        src = templates / entry
        if not src.exists():
            continue
        # env.example лежит в шаблонах под обычным именем — но в .orchx/
        # его принято класть как .env.example (со скрытой точкой). Имя
        # PROJECT.md/README.md/config.yaml оставляем как есть.
        target_name = ".env.example" if entry == "env.example" else entry
        _copy_file(src, runtime_dir / target_name, force=force, report=report)

    # 2. Прометы ролей (опционально).
    if not minimal:
        prompts_src = templates / "prompts"
        prompts_dst = runtime_dir / "prompts"
        _copy_tree(prompts_src, prompts_dst, force=force, report=report)

    # 3. Гарантируем .gitignore-блок.
    report.gitignore_updated = ensure_gitignore(project_root)

    return report
