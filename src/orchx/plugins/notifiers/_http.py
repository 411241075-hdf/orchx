"""Общий HTTP-helper для notifier'ов: POST JSON с retry'ями."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 10.0,
    retries: int = 2,
    headers: dict[str, str] | None = None,
) -> bool:
    """POST JSON. Returns True если хоть одна попытка успешна (2xx).

    Все исключения проглатываются и логируются — notifier не должен
    влиять на orchestrator (он "fire-and-forget").
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed; notifier disabled")
        return False
    headers = headers or {}
    headers.setdefault("Content-Type", "application/json")
    body = json.dumps(payload).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    return True
                logger.warning(
                    "notifier POST %s -> HTTP %s: %s",
                    url,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning("notifier POST attempt %s failed: %s", attempt + 1, e)
        await asyncio.sleep(min(2 ** attempt, 5))
    if last_exc:
        logger.warning("notifier POST gave up: %s", last_exc)
    return False
