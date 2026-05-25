"""Browser tool на основе Playwright (P1.7).

Опциональный tool для UI-тестирования / web-скрапинга. Требует
``pip install 'orchx[browser]'`` + ``playwright install chromium``.

Sub-tools (одна tool-функция, dispatch по action-arg, чтобы не плодить
5 разных tool-имён в OpenAI registry — экономим context):

* ``browser(action="goto", url=...)`` — открыть страницу.
* ``browser(action="click", selector=...)`` — клик.
* ``browser(action="fill", selector=..., text=...)`` — заполнить input.
* ``browser(action="screenshot", path=...)`` — сделать скриншот.
* ``browser(action="evaluate", script=...)`` — выполнить JS.
* ``browser(action="text", selector=...)`` — получить text content элемента.
* ``browser(action="close")`` — закрыть страницу.

Security:

* ``allowed_domains`` — белый список host'ов (по умолчанию: localhost/127.0.0.1
  любой порт). Запросы за пределы — отказ.
* Singleton page per ToolContext (worker reuse'ит ту же страницу
  между ходами LLM).
"""

from __future__ import annotations

import fnmatch
import json
from urllib.parse import urlparse

from . import Tool, ToolContext, ToolResult, permission_denied


class BrowserTool(Tool):
    """Управление headless-браузером через Playwright."""

    name = "browser"
    description = (
        "Control a headless browser (Playwright/Chromium) for UI testing or web "
        "scraping. Dispatch via 'action' arg: goto/click/fill/screenshot/"
        "evaluate/text/close. Returns text/JSON depending on action."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["goto", "click", "fill", "screenshot", "evaluate", "text", "close"],
            },
            "url": {"type": "string", "description": "URL for action=goto"},
            "selector": {
                "type": "string",
                "description": "CSS selector for click/fill/text",
            },
            "text": {"type": "string", "description": "Text to fill (action=fill)"},
            "script": {"type": "string", "description": "JS to evaluate (action=evaluate)"},
            "path": {
                "type": "string",
                "description": (
                    "Output path for action=screenshot (relative to cwd). "
                    "Default: .orchx/screenshots/<timestamp>.png"
                ),
            },
            "timeout_ms": {"type": "integer", "description": "Per-action timeout (ms)."},
        },
        "required": ["action"],
    }
    permission_attr = None  # gated через _check_domain + browser permission

    # Стандартные allowed_domains — только локалка. Можно расширить
    # через permission frontmatter:
    # browser:
    #   allowed_domains: ["localhost:*", "127.0.0.1:*", "staging.example.com"]
    DEFAULT_ALLOWED = ("localhost:*", "127.0.0.1:*", "127.0.0.1", "localhost")

    async def run(
        self,
        ctx: ToolContext,
        *,
        action: str,
        url: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        script: str | None = None,
        path: str | None = None,
        timeout_ms: int = 15000,
    ) -> ToolResult:
        ctx.activity(f"browser {action}")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ToolResult(
                content=(
                    "Playwright not installed. Run: pip install 'orchx[browser]' "
                    "&& playwright install chromium"
                ),
                is_error=True,
            )

        # Per-ctx singleton: создаём ленивую страницу.
        store = getattr(ctx, "_browser_store", None)
        if store is None:
            store = {"playwright": None, "browser": None, "page": None}
            ctx._browser_store = store  # type: ignore[attr-defined]

        async def _ensure_page():
            if store["page"] is None:
                store["playwright"] = await async_playwright().start()
                store["browser"] = await store["playwright"].chromium.launch(headless=True)
                store["page"] = await store["browser"].new_page()
            return store["page"]

        async def _close():
            try:
                if store["page"] is not None:
                    await store["page"].close()
                if store["browser"] is not None:
                    await store["browser"].close()
                if store["playwright"] is not None:
                    await store["playwright"].stop()
            finally:
                store["page"] = None
                store["browser"] = None
                store["playwright"] = None

        try:
            if action == "goto":
                if not url:
                    return ToolResult(content="action=goto requires url", is_error=True)
                if not _check_domain(url, self.DEFAULT_ALLOWED):
                    return permission_denied(
                        tool="browser",
                        target=url,
                        reason=f"host not in allowed domains {self.DEFAULT_ALLOWED}",
                        hint="Browser tool is restricted to localhost by default for safety.",
                    )
                page = await _ensure_page()
                resp = await page.goto(url, timeout=timeout_ms)
                status = resp.status if resp else "unknown"
                return ToolResult(content=f"goto {url} → status {status}")

            if action == "click":
                if not selector:
                    return ToolResult(content="action=click requires selector", is_error=True)
                page = await _ensure_page()
                await page.click(selector, timeout=timeout_ms)
                return ToolResult(content=f"clicked {selector}")

            if action == "fill":
                if not selector or text is None:
                    return ToolResult(
                        content="action=fill requires selector + text", is_error=True
                    )
                page = await _ensure_page()
                await page.fill(selector, text, timeout=timeout_ms)
                return ToolResult(content=f"filled {selector}")

            if action == "screenshot":
                page = await _ensure_page()
                if path:
                    target = (ctx.cwd / path).resolve()
                else:
                    import time as _time

                    sdir = ctx.cwd / ".orchx" / "screenshots"
                    sdir.mkdir(parents=True, exist_ok=True)
                    target = sdir / f"shot-{int(_time.time())}.png"
                # Safety: write только в cwd.
                try:
                    target.resolve().relative_to(ctx.cwd.resolve())
                except ValueError:
                    return permission_denied(
                        tool="browser",
                        target=str(target),
                        reason="screenshot path outside cwd",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(target), full_page=True)
                return ToolResult(content=f"screenshot saved: {target.relative_to(ctx.cwd)}")

            if action == "evaluate":
                if not script:
                    return ToolResult(content="action=evaluate requires script", is_error=True)
                page = await _ensure_page()
                result = await page.evaluate(script)
                return ToolResult(content=json.dumps(result, default=str)[:8000])

            if action == "text":
                if not selector:
                    return ToolResult(content="action=text requires selector", is_error=True)
                page = await _ensure_page()
                t = await page.text_content(selector, timeout=timeout_ms)
                return ToolResult(content=(t or "")[:8000])

            if action == "close":
                await _close()
                return ToolResult(content="browser closed")

            return ToolResult(content=f"unknown action: {action}", is_error=True)

        except Exception as e:  # noqa: BLE001
            return ToolResult(
                content=f"browser {action} failed: {e}",
                is_error=True,
            )


def _check_domain(url: str, allowed_patterns: tuple[str, ...]) -> bool:
    """Проверить URL host против глоб-паттернов."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    host = parsed.netloc.lower()
    if not host:
        return False
    for pat in allowed_patterns:
        if fnmatch.fnmatchcase(host, pat.lower()):
            return True
    return False


__all__ = ["BrowserTool"]
