"""No-op memory backend (default)."""

from __future__ import annotations

from typing import Any


class NoopMemory:
    """Безопасный default — ничего не помнит и ничего не возвращает."""

    name = "noop"

    def __init__(self, **_: Any) -> None:
        pass

    async def remember(
        self, namespace: str, key: str, value: dict[str, Any]  # noqa: ARG002
    ) -> None:
        return None

    async def recall(
        self, namespace: str, query: str, k: int = 5  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        return []

    async def forget_old(self, days: int = 90) -> int:  # noqa: ARG002
        return 0


__all__ = ["NoopMemory"]
