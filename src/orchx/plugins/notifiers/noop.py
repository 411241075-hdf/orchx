"""No-op notifier (default). Silently игнорирует все события."""

from __future__ import annotations

from typing import Any


class NoopNotifier:
    """Безопасный default — ничего не делает.

    Используется, когда в конфиге нет ``notifiers``. Позволяет orchestrator'у
    звать ``ctx.notifier.notify(...)`` без if-then проверок.
    """

    name = "noop"

    def __init__(self, **_: Any) -> None:
        pass

    async def notify(self, event: str, payload: dict[str, Any]) -> None:  # noqa: ARG002
        return None


__all__ = ["NoopNotifier"]
