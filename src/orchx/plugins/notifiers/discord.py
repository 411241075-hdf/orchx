"""Discord notifier (incoming webhook).

Config:
    webhook_url: Discord webhook URL (https://discord.com/api/webhooks/...).
    username: имя бота (default: orchX).
"""

from __future__ import annotations

import logging
from typing import Any

from ._http import post_json

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """POST'ит формат Discord incoming webhook."""

    name = "discord"

    def __init__(
        self,
        *,
        webhook_url: str = "",
        username: str = "orchX",
        **_: Any,
    ) -> None:
        self.webhook_url = webhook_url
        self.username = username

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        if not self.webhook_url:
            logger.debug("discord notifier: no webhook_url, skipping")
            return
        embed = {
            "title": f"orchX [{event}]",
            "description": self._format_description(payload),
            "color": self._color_for_event(event),
        }
        body = {"username": self.username, "embeds": [embed]}
        await post_json(self.webhook_url, body)

    @staticmethod
    def _color_for_event(event: str) -> int:
        # Discord использует int colors (0xRRGGBB).
        green = 0x2ECC71
        red = 0xE74C3C
        amber = 0xF39C12
        blue = 0x3498DB
        mapping = {
            "run_started": blue,
            "phase_completed": green,
            "phase_failed": red,
            "replan_triggered": amber,
            "pr_opened": blue,
            "cost_alert": amber,
            "budget_exceeded": red,
            "wall_budget_exceeded": red,
            "ci_failed": red,
            "changes_requested": amber,
            "approved_and_green": green,
            "run_finished": green,
        }
        return mapping.get(event, blue)

    @staticmethod
    def _format_description(payload: dict[str, Any]) -> str:
        if not payload:
            return "(no details)"
        return "\n".join(f"**{k}**: `{v}`" for k, v in payload.items())


__all__ = ["DiscordNotifier"]
