"""Тесты plugin-registry (P0.2)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orchx.plugins import (
    MemoryPlugin,
    NotifierPlugin,
    RuntimePlugin,
    SCMPlugin,
    TrackerPlugin,
    load_from_config,
    load_plugin,
    registered_plugins,
)
from orchx.plugins.registry import (
    SLOTS,
    PluginNotFound,
    _expand_env_vars,
)


def test_slots_match_entry_points():
    """В pyproject зарегистрированы все 5 slot'ов и хотя бы по 1 плагину."""
    plugins = registered_plugins()
    assert set(plugins.keys()) == set(SLOTS)
    for slot in SLOTS:
        assert plugins[slot], f"slot {slot} has no registered plugins"


def test_load_local_runtime_returns_runtime_plugin():
    rt = load_plugin("runtime", "local")
    assert isinstance(rt, RuntimePlugin)


def test_load_github_tracker():
    t = load_plugin("tracker", "github")
    assert isinstance(t, TrackerPlugin)


def test_load_github_scm():
    scm = load_plugin("scm", "github")
    assert isinstance(scm, SCMPlugin)


def test_load_noop_notifier():
    n = load_plugin("notifier", "noop")
    assert isinstance(n, NotifierPlugin)


def test_load_sqlite_memory():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "x.db"
        m = load_plugin("memory", "sqlite", config={"path": str(db)})
        assert isinstance(m, MemoryPlugin)
        assert db.exists()


def test_unknown_plugin_raises_plugin_not_found():
    with pytest.raises(PluginNotFound):
        load_plugin("runtime", "nonexistent_runtime")


def test_unknown_slot_raises_value_error():
    with pytest.raises(ValueError, match="Unknown plugin slot"):
        load_plugin("totally_invalid_slot", "x")


def test_env_var_expansion_in_config(monkeypatch):
    monkeypatch.setenv("MY_TEST_VAR", "expanded_value")
    out = _expand_env_vars({"x": "${MY_TEST_VAR}", "y": "literal", "z": 42})
    assert out["x"] == "expanded_value"
    assert out["y"] == "literal"
    assert out["z"] == 42


def test_load_from_config_yaml(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
runtime: local
tracker: github
scm: github
notifiers:
  - noop
memory: noop
        """,
        encoding="utf-8",
    )
    bag = load_from_config(cfg)
    assert "runtime" in bag and isinstance(bag["runtime"], RuntimePlugin)
    assert "tracker" in bag and isinstance(bag["tracker"], TrackerPlugin)
    assert "scm" in bag and isinstance(bag["scm"], SCMPlugin)
    assert bag.get("notifiers") and len(bag["notifiers"]) == 1
    assert "memory" in bag and isinstance(bag["memory"], MemoryPlugin)


def test_load_from_missing_config_returns_memory_default():
    """С 0.2.1 memory: sqlite — дефолт, даже без config.yaml."""
    bag = load_from_config(Path("/does/not/exist.yaml"))
    assert "memory" in bag
    assert isinstance(bag["memory"], MemoryPlugin)


def test_load_from_missing_config_with_disabled_memory(tmp_path: Path):
    """Можно явно выключить дефолт через `memory: noop` в config."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory: noop\n", encoding="utf-8")
    bag = load_from_config(cfg)
    # NoopMemory создаётся — это не пустой dict, но и не sqlite.
    assert "memory" in bag
    assert type(bag["memory"]).__name__ == "NoopMemory"


def test_load_from_invalid_yaml_raises(tmp_path: Path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(":\n  - this : is : invalid :", encoding="utf-8")
    with pytest.raises(Exception, match="YAML"):
        load_from_config(cfg)


def test_load_from_config_with_plugin_config(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
memory: sqlite
plugin_config:
  sqlite:
    path: {tmp_path / 'mem.db'}
        """,
        encoding="utf-8",
    )
    bag = load_from_config(cfg)
    assert "memory" in bag
    assert (tmp_path / "mem.db").exists()
