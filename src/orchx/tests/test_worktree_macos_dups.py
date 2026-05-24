# -*- coding: utf-8 -*-
"""Тесты для cleanup_macos_duplicates / cleanup_git_internal_duplicates.

В прошлом прогоне (orchx/runs/admin-subdomain bba6422) macOS APFS под
параллельной нагрузкой создал ``<file> 2`` дубликаты внутри worktree
воркера. ``commit_all`` слепо сделал ``git add -A`` и в integration ветку
ушёл коммит, удаляющий половину репозитория. Эти тесты гарантируют, что
после фикса:

1. ``_is_macos_duplicate`` корректно классифицирует имена.
2. ``cleanup_macos_duplicates`` удаляет дубликаты, не трогая обычные файлы.
3. ``cleanup_git_internal_duplicates`` чистит ``.git/worktrees/<name>/``,
   но не трогает живые ref'ы.
"""

from __future__ import annotations

from pathlib import Path

from orchx.worktree import (
    _is_macos_duplicate,
    cleanup_git_internal_duplicates,
    cleanup_macos_duplicates,
)


def test_is_macos_duplicate_basic() -> None:
    # Положительные случаи — реальные примеры из логов прошлого прогона.
    assert _is_macos_duplicate("foo 2.py")
    assert _is_macos_duplicate(".env 3.example")
    assert _is_macos_duplicate("HEAD 2")
    assert _is_macos_duplicate("settings 2.json")
    assert _is_macos_duplicate("packages/shared 2.json")
    assert _is_macos_duplicate("backend 2/foo.py")
    assert _is_macos_duplicate("README 2.md")
    # Отрицательные — обычные имена с цифрами и пробелами не должны
    # триггериться.
    assert not _is_macos_duplicate("foo.py")
    assert not _is_macos_duplicate("file_2.py")  # подчёркивание, не пробел
    assert not _is_macos_duplicate("foo-2.py")  # дефис
    assert not _is_macos_duplicate("README.md")
    # Имя с числом и пробелом, но число — часть базового имени, не суффикс.
    assert not _is_macos_duplicate("v 1 release notes.txt")  # тут 1 — не последний токен пути
    # «foo bar.py» (обычный пробел в имени без цифрового суффикса) — нет.
    assert not _is_macos_duplicate("foo bar.py")


def test_cleanup_macos_duplicates_removes_only_dups(tmp_path: Path) -> None:
    """Реальный сценарий: в worktree смешаны нормальные файлы и macOS-копии."""
    # Нормальные файлы.
    (tmp_path / "real.py").write_text("ok")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "inner.py").write_text("ok")
    # macOS-style дубликаты (имитация APFS race-condition).
    (tmp_path / "real 2.py").write_text("dup")
    (tmp_path / "settings 3.json").write_text("dup")
    (tmp_path / ".env 2.example").write_text("dup")
    (tmp_path / "subdir" / "inner 2.py").write_text("dup")
    # Дубликат-директория.
    (tmp_path / "subdir 2").mkdir()
    (tmp_path / "subdir 2" / "x.py").write_text("dup")

    removed = cleanup_macos_duplicates(tmp_path)

    # Все дубликаты удалены.
    assert not (tmp_path / "real 2.py").exists()
    assert not (tmp_path / "settings 3.json").exists()
    assert not (tmp_path / ".env 2.example").exists()
    assert not (tmp_path / "subdir" / "inner 2.py").exists()
    assert not (tmp_path / "subdir 2").exists()
    # Нормальные файлы целы.
    assert (tmp_path / "real.py").read_text() == "ok"
    assert (tmp_path / "subdir" / "inner.py").read_text() == "ok"
    # Список removed содержит все 5 удалённых entries.
    assert len(removed) == 5
    assert any("real 2.py" in r for r in removed)
    assert any("subdir 2" in r for r in removed)


def test_cleanup_macos_duplicates_skips_dotgit(tmp_path: Path) -> None:
    """Чистка worktree-папки не должна лезть в .git — для него отдельная функция."""
    (tmp_path / "real.py").write_text("ok")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD 2").write_text("dup-but-skipped")
    (git_dir / "config").write_text("[core]")

    removed = cleanup_macos_duplicates(tmp_path)

    # .git внутренности НЕ тронуты.
    assert (git_dir / "HEAD 2").exists()
    assert (git_dir / "config").exists()
    # На уровне worktree'а никаких дубликатов не было.
    assert removed == []


def test_cleanup_git_internal_duplicates_targets_known_subdirs(tmp_path: Path) -> None:
    """cleanup_git_internal_duplicates работает только в .git/{worktrees,refs,logs}."""
    # Имитируем структуру .git.
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main")
    # «Чужие» дубликаты — они НЕ должны быть удалены (вне whitelist).
    (git_dir / "HEAD 2").write_text("stale")
    # А вот эти должны.
    wt_dir = git_dir / "worktrees" / "foo"
    wt_dir.mkdir(parents=True)
    (wt_dir / "HEAD").write_text("ok")
    (wt_dir / "HEAD 2").write_text("stale")
    (wt_dir / "index 2").write_text("stale")
    refs_heads = git_dir / "refs" / "heads"
    refs_heads.mkdir(parents=True)
    (refs_heads / "main").write_text("sha")
    (refs_heads / "main 2").write_text("stale-sha")

    removed = cleanup_git_internal_duplicates(tmp_path)

    # Вне whitelist'а .git/HEAD 2 — НЕ удаляем (защита от чрезмерной чистки).
    assert (git_dir / "HEAD 2").exists()
    # А внутри worktrees/refs — удалили.
    assert not (wt_dir / "HEAD 2").exists()
    assert not (wt_dir / "index 2").exists()
    assert not (refs_heads / "main 2").exists()
    # Нормальные файлы целы.
    assert (wt_dir / "HEAD").read_text() == "ok"
    assert (refs_heads / "main").read_text() == "sha"
    # 3 удаления отрапортованы.
    assert len(removed) == 3
