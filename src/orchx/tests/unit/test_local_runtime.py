"""Тесты LocalRuntime (P0.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.plugins.runtimes.local import LocalRuntime


@pytest.mark.asyncio
async def test_local_runtime_requires_llm():
    rt = LocalRuntime()
    with pytest.raises(ValueError, match="llm"):
        await rt.spawn_worker(
            cwd=Path("/tmp"),
            repo_root=Path("/tmp"),
            role="implementer",
            prompt="...",
            timeout_s=10.0,
            log_file=Path("/tmp/x.log"),
            effort=None,
        )


def test_local_runtime_has_name():
    rt = LocalRuntime()
    assert rt.name == "local"


def test_local_runtime_constructor_accepts_extra_kwargs():
    # Не должно ломаться при «лишних» kwarg'ах из config.
    rt = LocalRuntime(extra="ignored", another=42)
    assert rt.name == "local"
