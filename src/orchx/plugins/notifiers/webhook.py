"""Generic JSON-webhook notifier.

POST'ит payload в любой URL. Полезно для интеграции с internal-tooling.

Config:
    url: куда POST'ить.
    headers: dict дополнительных HTTP-headers (например, ``Authorization``).
    auth_token: shortcut: добавит ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import logging
from typing import Any

from ._http import post_json

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """POST {"event": <str>, "payload": <dict>} в произвольный URL."""

    name = "webhook"

    def __init__(
        self,
        *,
        url: str = "",
        headers: dict[str, str] | None = None,
        auth_token: str | None = None,
        **_: Any,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        if not self.url:
            logger.debug("webhook notifier: no url, skipping")
            return
        body = {"event": event, "payload": payload}
        await post_json(self.url, body, headers=self.headers)


__all__ = ["WebhookNotifier"]
