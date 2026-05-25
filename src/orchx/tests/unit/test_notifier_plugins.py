"""Тесты notifier plugins (P0.2 / P1.5)."""

from __future__ import annotations

import pytest

from orchx.plugins.notifiers.discord import DiscordNotifier
from orchx.plugins.notifiers.noop import NoopNotifier
from orchx.plugins.notifiers.slack import SlackNotifier
from orchx.plugins.notifiers.webhook import WebhookNotifier


@pytest.mark.asyncio
async def test_noop_notifier_swallows_everything():
    n = NoopNotifier()
    await n.notify("anything", {"x": 1})  # no raise, no return


@pytest.mark.asyncio
async def test_slack_notifier_without_webhook_url_is_silent():
    n = SlackNotifier(webhook_url="")
    await n.notify("pr_opened", {"pr_url": "https://x"})  # no raise


@pytest.mark.asyncio
async def test_discord_notifier_without_webhook_url_is_silent():
    n = DiscordNotifier(webhook_url="")
    await n.notify("ci_failed", {"task_id": "T1"})


@pytest.mark.asyncio
async def test_webhook_notifier_without_url_is_silent():
    n = WebhookNotifier(url="")
    await n.notify("anything", {})


def test_slack_format_includes_emoji_for_known_event():
    text = SlackNotifier._format("pr_opened", {"task_id": "T1", "pr_url": "https://x"})
    assert ":pencil:" in text
    assert "T1" in text


def test_slack_format_unknown_event_falls_back_to_bell():
    text = SlackNotifier._format("totally_new_event", {"k": "v"})
    assert ":bell:" in text


def test_discord_color_for_event_known():
    assert DiscordNotifier._color_for_event("phase_failed") != DiscordNotifier._color_for_event(
        "phase_completed"
    )


def test_discord_format_description_handles_empty():
    assert DiscordNotifier._format_description({}) == "(no details)"


def test_webhook_notifier_auth_token_sets_header():
    n = WebhookNotifier(url="http://x", auth_token="secret")
    assert n.headers["Authorization"] == "Bearer secret"


def test_webhook_notifier_custom_headers_preserved():
    n = WebhookNotifier(url="http://x", headers={"X-Foo": "bar"})
    assert n.headers["X-Foo"] == "bar"
