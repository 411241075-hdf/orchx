"""Тесты web dashboard (P1.4) — DashboardBus + DashboardNotifier.

NB: FastAPI app тестируется только при наличии extras ``orchx[server]``;
TestClient skip'ается если fastapi не установлен.
"""

from __future__ import annotations

import asyncio

import pytest

from orchx.web.server import (
    DashboardBus,
    DashboardNotifier,
    get_bus,
)

# NB: pytestmark.asyncio убран ради совместимости с sync-тестом
# test_create_app_requires_server_extras в конце файла. async-тесты
# помечены вручную ниже.


@pytest.mark.asyncio
async def test_bus_publish_to_no_subscribers_no_raise():
    bus = DashboardBus()
    await bus.publish("foo", {"x": 1})  # должно тихо отработать


@pytest.mark.asyncio
async def test_bus_publish_to_subscriber():
    bus = DashboardBus()
    q = await bus.subscribe()
    await bus.publish("foo", {"x": 1})
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg["event"] == "foo"
    assert msg["payload"] == {"x": 1}


@pytest.mark.asyncio
async def test_bus_unsubscribe_stops_receiving():
    bus = DashboardBus()
    q = await bus.subscribe()
    await bus.unsubscribe(q)
    await bus.publish("foo", {})
    # Не должно быть сообщений в очереди.
    assert q.empty()


@pytest.mark.asyncio
async def test_bus_multiple_subscribers_all_receive():
    bus = DashboardBus()
    q1 = await bus.subscribe()
    q2 = await bus.subscribe()
    await bus.publish("foo", {"x": 1})
    m1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    m2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert m1["event"] == m2["event"] == "foo"


@pytest.mark.asyncio
async def test_dashboard_notifier_publishes_to_singleton_bus():
    n = DashboardNotifier()
    bus = get_bus()
    q = await bus.subscribe()
    try:
        await n.notify("test_event", {"y": 42})
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg["event"] == "test_event"
        assert msg["payload"] == {"y": 42}
    finally:
        await bus.unsubscribe(q)


def test_create_app_requires_server_extras():
    """Без fastapi — ImportError."""
    try:
        import fastapi  # noqa: F401
    except ImportError:
        from pathlib import Path

        from orchx.web.server import create_app

        with pytest.raises(ImportError, match="server"):
            create_app(Path("."))
    else:
        pytest.skip("fastapi installed; cannot test missing-deps path")
