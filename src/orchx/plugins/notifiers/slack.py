"""Slack notifier (incoming webhook).

Config:
    webhook_url: Slack incoming webhook URL (https://hooks.slack.com/...).
        Поддерживает ``${SLACK_WEBHOOK_URL}`` подстановку env.
    channel: опциональный канал override (если webhook это позволяет).
    username: имя бота (default: orchX).
"""

from __future__ import annotations

import logging
from typing import Any

from ._http import post_json

logger = logging.getLogger(__name__)


class SlackNotifier:
    """POST'ит формат Slack incoming webhook."""

    name = "slack"

    def __init__(
        self,
        *,
        webhook_url: str = "",
        channel: str | None = None,
        username: str = "orchX",
        **_: Any,
    ) -> None:
        self.webhook_url = webhook_url
        self.channel = channel
        self.username = username

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        if not self.webhook_url:
            logger.debug("slack notifier: no webhook_url, skipping")
            return
        body: dict[str, Any] = {
            "username": self.username,
            "text": self._format(event, payload),
        }
        if self.channel:
            body["channel"] = self.channel
        await post_json(self.webhook_url, body)

    @staticmethod
    def _format(event: str, payload: dict[str, Any]) -> str:
        # Простой markdown-format. Можно расширить blocks-кадрами.
        emoji = {
            "run_started": ":rocket:",
            "phase_completed": ":white_check_mark:",
            "phase_failed": ":x:",
            "replan_triggered": ":arrows_counterclockwise:",
            "pr_opened": ":pencil:",
            "cost_alert": ":warning:",
            "budget_exceeded": ":no_entry:",
            "wall_budget_exceeded": ":no_entry:",
            "ci_failed": ":fire:",
            "changes_requested": ":speech_balloon:",
            "approved_and_green": ":sparkles:",
            "run_finished": ":checkered_flag:",
        }.get(event, ":bell:")
        lines = [f"{emoji} *orchX [{event}]*"]
        task_id = payload.get("task_id")
        if task_id:
            lines.append(f"*task_id*: `{task_id}`")
        for k, v in payload.items():
            if k == "task_id":
                continue
            lines.append(f"*{k}*: `{v}`")
        return "\n".join(lines)


__all__ = ["SlackNotifier"]
