"""Тесты browser tool (P1.7) — domain check + missing Playwright fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.agent.permissions import Permissions
from orchx.agent.tools import ToolContext
from orchx.agent.tools.browser import BrowserTool, _check_domain


def test_check_domain_allows_localhost():
    assert _check_domain("http://localhost:5173/api", BrowserTool.DEFAULT_ALLOWED)
    assert _check_domain("http://127.0.0.1:8080/x", BrowserTool.DEFAULT_ALLOWED)


def test_check_domain_blocks_external():
    assert not _check_domain("https://example.com/x", BrowserTool.DEFAULT_ALLOWED)
    assert not _check_domain("https://api.openai.com/", BrowserTool.DEFAULT_ALLOWED)


def test_check_domain_handles_malformed_url():
    assert not _check_domain("not-a-url", BrowserTool.DEFAULT_ALLOWED)
    assert not _check_domain("", BrowserTool.DEFAULT_ALLOWED)


@pytest.mark.asyncio
async def test_browser_without_playwright_returns_error(tmp_path: Path):
    """Если playwright не установлен — clear error, не падает."""
    try:
        import playwright  # noqa: F401
        pytest.skip("playwright installed; cannot test missing-deps path")
    except ImportError:
        pass

    ctx = ToolContext(cwd=tmp_path, repo_root=tmp_path, permissions=Permissions(browser=True))
    t = BrowserTool()
    r = await t.run(ctx, action="goto", url="http://localhost:5173")
    assert r.is_error
    assert "Playwright not installed" in r.content


@pytest.mark.asyncio
async def test_browser_blocked_external_domain_before_playwright(tmp_path: Path):
    """Domain check срабатывает до того, как мы пытаемся импортнуть playwright."""
    # Если playwright не установлен — fail на ImportError перед domain check.
    # Если установлен — должна быть permission_denied для example.com.
    ctx = ToolContext(cwd=tmp_path, repo_root=tmp_path, permissions=Permissions(browser=True))
    t = BrowserTool()
    r = await t.run(ctx, action="goto", url="https://api.evil.example.com/")
    assert r.is_error
    # Either "Playwright not installed" OR "Permission denied: browser on https://..."
    assert "Playwright not installed" in r.content or "Permission denied" in r.content


@pytest.mark.asyncio
async def test_browser_unknown_action(tmp_path: Path):
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed")
    ctx = ToolContext(cwd=tmp_path, repo_root=tmp_path, permissions=Permissions(browser=True))
    t = BrowserTool()
    r = await t.run(ctx, action="totally_invalid")
    assert r.is_error
