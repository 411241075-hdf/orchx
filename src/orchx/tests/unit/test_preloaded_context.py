"""Тесты для :mod:`orchx.preloaded_context`."""

from __future__ import annotations

from pathlib import Path

from orchx.preloaded_context import render_preloaded_context


def _make_file(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_render_empty_inputs(tmp_path: Path) -> None:
    out = render_preloaded_context(tmp_path, tmp_path, inputs=())
    assert out == ""


def test_render_small_file_inlines_whole(tmp_path: Path) -> None:
    """Файл < SMALL_FILE_LINE_LIMIT строк — встраивается целиком."""
    _make_file(tmp_path, "src/foo.py", "def hello():\n    return 1\n")
    out = render_preloaded_context(tmp_path, tmp_path, inputs=("src/foo.py",))
    assert "## Pre-loaded context" in out
    assert "### `src/foo.py`" in out
    assert "def hello()" in out
    assert "```python" in out


def test_render_explicit_range_string_format(tmp_path: Path) -> None:
    """Формат ``path:start-end`` — выдержка только этих строк."""
    lines = "\n".join(f"line{i}" for i in range(1, 101))
    _make_file(tmp_path, "src/big.py", lines)
    out = render_preloaded_context(
        tmp_path, tmp_path, inputs=("src/big.py:50-55",)
    )
    assert "### `src/big.py:50-55`" in out
    # Должны быть строки 40-65 (range + RANGE_PADDING=10) — проверяем граничные.
    assert "line50" in out
    assert "line55" in out
    assert "line40" in out  # padding снизу
    assert "line65" in out  # padding сверху


def test_render_explicit_range_dict_format(tmp_path: Path) -> None:
    """Формат ``{"path": ..., "lines": [start, end]}``."""
    lines = "\n".join(f"line{i}" for i in range(1, 51))
    _make_file(tmp_path, "src/big.py", lines)
    out = render_preloaded_context(
        tmp_path,
        tmp_path,
        inputs=[{"path": "src/big.py", "lines": [10, 12]}],
    )
    assert "src/big.py" in out
    assert "line10" in out
    assert "line12" in out


def test_render_large_file_without_range_truncated(tmp_path: Path) -> None:
    """Большой файл без range — head + notice."""
    lines = "\n".join(f"line{i}" for i in range(1, 501))
    _make_file(tmp_path, "src/huge.py", lines)
    out = render_preloaded_context(tmp_path, tmp_path, inputs=("src/huge.py",))
    assert "src/huge.py" in out
    # Должны быть первые SMALL_FILE_LINE_LIMIT=200 строк.
    assert "line1\n" in out
    assert "line200" in out
    # И notice про обрезку.
    assert "500 lines" in out


def test_render_missing_file_doesnt_crash(tmp_path: Path) -> None:
    """Несуществующий файл — пропускаем без ошибки."""
    out = render_preloaded_context(
        tmp_path, tmp_path, inputs=("does/not/exist.py",)
    )
    # Либо пусто, либо notice — но не raise.
    assert isinstance(out, str)


def test_render_uses_cache(tmp_path: Path) -> None:
    """Один и тот же ``(path, range)`` читается ровно один раз."""
    _make_file(tmp_path, "src/foo.py", "x = 1\n")
    cache: dict = {}
    out1 = render_preloaded_context(
        tmp_path, tmp_path, inputs=("src/foo.py",), cache=cache
    )
    assert len(cache) == 1
    out2 = render_preloaded_context(
        tmp_path, tmp_path, inputs=("src/foo.py",), cache=cache
    )
    # Тот же кэш использован.
    assert len(cache) == 1
    assert out1 == out2


def test_render_prefers_worktree_over_repo_root(tmp_path: Path) -> None:
    """Если файл есть в worktree — берём оттуда (он свежее)."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    _make_file(repo, "src/foo.py", "REPO_VERSION\n")
    _make_file(wt, "src/foo.py", "WORKTREE_VERSION\n")
    out = render_preloaded_context(repo, wt, inputs=("src/foo.py",))
    assert "WORKTREE_VERSION" in out
    assert "REPO_VERSION" not in out


def test_render_multiple_inputs(tmp_path: Path) -> None:
    """Несколько inputs — несколько секций, каждая со своим заголовком."""
    _make_file(tmp_path, "a.py", "alpha = 1\n")
    _make_file(tmp_path, "b.py", "beta = 2\n")
    out = render_preloaded_context(tmp_path, tmp_path, inputs=("a.py", "b.py"))
    assert "### `a.py`" in out
    assert "### `b.py`" in out
    assert "alpha = 1" in out
    assert "beta = 2" in out
