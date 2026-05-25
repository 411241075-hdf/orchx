"""Federation REST API (P2.3).

Расширяет web-dashboard endpoint'ами для cross-machine оркестрации:

* ``POST /api/runs/spawn`` — принять plan.json от remote orchX-instance,
  запустить локально. Возвращает task_id.
* ``GET /api/runs/<task_id>/status`` — статус для remote poll'а.
* ``DELETE /api/runs/<task_id>`` — abort.

Аутентификация: ``Authorization: Bearer <ORCHX_FEDERATION_TOKEN>``.
Токен задаётся через env ``ORCHX_FEDERATION_TOKEN``. Если не задан —
endpoints доступны без auth (только для local-dev — не используйте в
public networks).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def add_federation_routes(app: Any, repo_root: Path) -> None:
    """Зарегистрировать federation-endpoints на существующем FastAPI app'е."""
    try:
        from fastapi import Body, Header, HTTPException
        from fastapi.responses import JSONResponse
    except ImportError as e:
        raise ImportError(
            "FastAPI not installed. Run: pip install 'orchx[server]'"
        ) from e

    from .. import paths

    def _check_auth(authorization: str | None) -> None:
        token = os.environ.get("ORCHX_FEDERATION_TOKEN", "").strip()
        if not token:
            # No auth configured — open access (dev mode).
            return
        if not authorization:
            raise HTTPException(401, "missing Authorization header")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise HTTPException(401, "invalid Authorization scheme; expected Bearer")
        given = authorization.removeprefix(prefix).strip()
        # Constant-time comparison чтобы не утечь длиной по timing.
        if not secrets.compare_digest(given, token):
            raise HTTPException(401, "invalid token")

    @app.post("/api/runs/spawn")
    async def spawn_run(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Принять plan.json и зарегистрировать его как pending run.

        Body: ``{"plan": {...plan.json...}}``. orchestrator не запускается
        синхронно (это блочный процесс) — план сохраняется в
        ``orchx/runs/<task_id>/plan.json`` и клиент должен сам запустить
        ``orchx run <task_id>`` или дёрнуть spawn-hook (TODO: P3).
        """
        _check_auth(authorization)
        plan = body.get("plan")
        if not isinstance(plan, dict):
            raise HTTPException(400, "expected body['plan'] as JSON object")
        task_id = plan.get("task_id")
        if not task_id or not isinstance(task_id, str):
            raise HTTPException(400, "plan.task_id required")

        run_dir = paths.run_dir(repo_root, task_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.json"
        plan_path.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(
            "federation: accepted plan for task_id=%s -> %s", task_id, plan_path
        )
        return JSONResponse(
            {
                "task_id": task_id,
                "plan_path": str(plan_path),
                "next_step": (
                    f"Run: orchx run {plan_path} (federation does not auto-execute)"
                ),
            }
        )

    @app.get("/api/runs/{task_id}/status")
    async def federation_status(
        task_id: str, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        _check_auth(authorization)
        d = paths.run_dir(repo_root, task_id)
        if not d.exists():
            raise HTTPException(404, f"run {task_id} not found")
        summary_p = d / "summary.json"
        if summary_p.exists():
            try:
                s = json.loads(summary_p.read_text(encoding="utf-8"))
                return JSONResponse(
                    {
                        "task_id": task_id,
                        "state": "done",
                        "counts": s.get("counts", {}),
                        "aborted": s.get("aborted", False),
                        "halt_reason": s.get("halt_reason"),
                        "cost": s.get("cost", {}),
                    }
                )
            except (OSError, json.JSONDecodeError) as e:
                return JSONResponse(
                    {"task_id": task_id, "state": "unknown", "error": str(e)}
                )
        return JSONResponse({"task_id": task_id, "state": "pending"})

    @app.delete("/api/runs/{task_id}")
    async def federation_abort(
        task_id: str, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        """Создать abort-маркер. Orchestrator проверяет его на каждом шаге.

        NB: фактическая остановка — best effort (зависит от того, как часто
        orchestrator опрашивает abort-marker; v1 P2.3 — marker'а ещё нет
        в orchestrator-loop'е, это TODO).
        """
        _check_auth(authorization)
        marker = paths.run_dir(repo_root, task_id) / ".abort"
        if not marker.parent.exists():
            raise HTTPException(404, f"run {task_id} not found")
        marker.write_text("federation abort\n", encoding="utf-8")
        return JSONResponse({"task_id": task_id, "abort_marker": str(marker)})


__all__ = ["add_federation_routes"]
