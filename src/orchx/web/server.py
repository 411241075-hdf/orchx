"""FastAPI web dashboard для orchX (P1.4).

Эндпойнты:

* ``GET /api/runs`` — список прогонов в ``orchx/runs/``.
* ``GET /api/runs/<task_id>`` — полный summary.json (если ещё нет — live state).
* ``GET /api/runs/<task_id>/plan`` — plan.json.
* ``GET /api/runs/<task_id>/log`` — orchx.log (tail).
* ``GET /api/runs/<task_id>/tasks/<task_id>/logs`` — лог attempt'ов задачи.
* ``GET /api/runs/<task_id>/events`` — Server-Sent Events про изменения state'а.
* ``GET /`` — статика (vanilla HTML+HTMX, без сборки).

Запуск:

.. code-block:: bash

   orchx dashboard --port 8421 --host 127.0.0.1

   # или внутри прогона:
   orchx all "..." --dashboard 127.0.0.1:8421

Архитектура: orchestrator опубликовывает state-обновления через
``DashboardBus`` (in-memory pub/sub). Web-сервер subscriptions
форвардит как Server-Sent Events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DashboardBus — in-memory pub/sub для live updates.
# ---------------------------------------------------------------------------


class DashboardBus:
    """Простой in-memory broadcast: orchestrator pub'ит события, SSE-клиенты sub'ят.

    Singleton используется одним процессом. Web-сервер регистрирует
    subscribers, orchestrator (через ``ctx.notifier``) выкидывает
    события. Никаких external deps (Redis / nats / etc.) — для one-machine
    deployment'а этого достаточно.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        msg = {"event": event, "payload": payload}
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # медленный клиент — drop, не блокируем orchestrator.
                pass

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


# Global singleton (per-process).
_BUS = DashboardBus()


def get_bus() -> DashboardBus:
    """Возвращает singleton :class:`DashboardBus`."""
    return _BUS


class DashboardNotifier:
    """NotifierPlugin-совместимая обёртка над :class:`DashboardBus`.

    Используется когда orchx запускается с ``--dashboard``: события из
    orchestrator'а форвардятся в bus, оттуда → SSE → браузер.
    """

    name = "dashboard"

    def __init__(self, **_: Any) -> None:
        self._bus = get_bus()

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(event, payload)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def create_app(repo_root: Path) -> Any:
    """Создать :class:`fastapi.FastAPI` instance для dashboard'а.

    Args:
        repo_root: корень репо для resolve ``orchx/runs/...``.

    Returns:
        FastAPI app или :class:`ImportError` если ``orchx[server]`` не установлен.
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise ImportError(
            "FastAPI not installed. Run: pip install 'orchx[server]'"
        ) from e

    from .. import paths

    app = FastAPI(title="orchX dashboard", docs_url="/api/docs")
    bus = get_bus()
    static_dir = Path(__file__).parent / "static"

    @app.get("/api/runs")
    async def list_runs() -> JSONResponse:
        runs_dir = paths.runs_dir(repo_root)
        if not runs_dir.exists():
            return JSONResponse([])
        items: list[dict[str, Any]] = []
        for d in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            summary_p = d / "summary.json"
            plan_p = d / "plan.json"
            item: dict[str, Any] = {
                "task_id": d.name,
                "mtime": d.stat().st_mtime,
                "has_summary": summary_p.exists(),
                "has_plan": plan_p.exists(),
            }
            if summary_p.exists():
                try:
                    s = json.loads(summary_p.read_text(encoding="utf-8"))
                    item["counts"] = s.get("counts", {})
                    item["wall_seconds"] = s.get("wall_seconds")
                    item["cost"] = s.get("cost", {}).get("total_usd")
                    item["aborted"] = s.get("aborted", False)
                except (json.JSONDecodeError, OSError):
                    pass
            items.append(item)
        return JSONResponse(items)

    @app.get("/api/runs/{task_id}")
    async def get_run(task_id: str) -> JSONResponse:
        d = paths.run_dir(repo_root, task_id)
        if not d.exists():
            raise HTTPException(404, f"run {task_id} not found")
        summary_p = d / "summary.json"
        if summary_p.exists():
            return JSONResponse(json.loads(summary_p.read_text(encoding="utf-8")))
        # Live run — возвращаем хоть что-то.
        return JSONResponse({"task_id": task_id, "live": True})

    @app.get("/api/runs/{task_id}/plan")
    async def get_plan(task_id: str) -> JSONResponse:
        plan_p = paths.run_dir(repo_root, task_id) / "plan.json"
        if not plan_p.exists():
            raise HTTPException(404, "plan not found")
        return JSONResponse(json.loads(plan_p.read_text(encoding="utf-8")))

    @app.get("/api/runs/{task_id}/log")
    async def get_log(task_id: str, tail: int = 200) -> JSONResponse:
        log_p = paths.orchx_log_path(repo_root, task_id)
        if not log_p.exists():
            raise HTTPException(404, "log not found")
        lines = log_p.read_text(encoding="utf-8", errors="replace").splitlines()
        return JSONResponse({"task_id": task_id, "tail": lines[-tail:] if tail > 0 else lines})

    @app.get("/api/runs/{task_id}/tasks/{subtask_id}/logs")
    async def get_subtask_log(
        task_id: str, subtask_id: str, attempt: int | None = None
    ) -> JSONResponse:
        logs_dir = paths.run_dir(repo_root, task_id) / "logs"
        if not logs_dir.exists():
            raise HTTPException(404, "no logs dir")
        if attempt is not None:
            log_p = logs_dir / f"{subtask_id}.attempt{attempt}.log"
        else:
            # latest
            candidates = sorted(logs_dir.glob(f"{subtask_id}*.log"))
            if not candidates:
                raise HTTPException(404, f"no logs for subtask {subtask_id}")
            log_p = candidates[-1]
        if not log_p.exists():
            raise HTTPException(404, str(log_p))
        return JSONResponse(
            {
                "path": str(log_p),
                "content": log_p.read_text(encoding="utf-8", errors="replace"),
            }
        )

    @app.get("/api/events")
    async def sse_events() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            q = await bus.subscribe()
            try:
                # Сразу шлём heartbeat чтобы клиент знал что соединение live.
                yield b": connected\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=30.0)
                        body = (
                            f"event: {msg['event']}\n"
                            f"data: {json.dumps(msg['payload'])}\n\n"
                        ).encode()
                        yield body
                    except TimeoutError:
                        yield b": heartbeat\n\n"
            finally:
                await bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        index = static_dir / "index.html"
        if index.exists():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>orchX dashboard</h1><p>static/index.html missing</p>")

    # P2.3: federation REST endpoints (spawn / status / abort).
    try:
        from .federation import add_federation_routes

        add_federation_routes(app, repo_root)
    except Exception as e:  # noqa: BLE001
        logger.warning("federation routes not registered: %s", e)

    # Static assets (CSS / JS).
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


async def serve(
    *,
    repo_root: Path,
    host: str = "127.0.0.1",
    port: int = 8421,
) -> None:
    """Поднять dashboard через uvicorn (для CLI ``orchx dashboard``).

    Блокирующий — нужно запускать в отдельной задаче, если хочется
    параллельно гонять orchestrator.
    """
    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            "uvicorn not installed. Run: pip install 'orchx[server]'"
        ) from e
    app = create_app(repo_root)
    config = uvicorn.Config(
        app, host=host, port=port, log_level=os.environ.get("ORCHX_WEB_LOG", "info")
    )
    server = uvicorn.Server(config)
    await server.serve()


__all__ = ["DashboardBus", "DashboardNotifier", "create_app", "get_bus", "serve"]
