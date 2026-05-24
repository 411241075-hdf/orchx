"""WebFetchTool — read-only fetch публичных URL для документации.

По умолчанию выключен (``permission.webfetch: deny`` во всех frontmatter'ах).
Включается вручную для роли, которой реально нужны внешние доки (например,
debugger на новой ошибке или planner на незнакомой технологии).

**Безопасность.**

- Только HTTPS (HTTP-схема → upgrade до HTTPS).
- Hostname резолвится → IP проверяется против блок-листа RFC1918 /
  link-local / loopback — это закрывает доступ к cloud metadata endpoint'ам
  (например, 169.254.169.254 у AWS/GCP/Azure) и внутренним сервисам.
- Размер ответа жёстко ограничен 256KB.
- Timeout жёсткий — 60s.

**Формат.**

HTML конвертируется в Markdown через библиотеку ``markdownify`` (зрелый
HTML→Markdown конвертер на базе BeautifulSoup). Это даёт качественный
результат: сохраняются таблицы, code blocks, заголовки, ссылки, списки.
Перед конвертацией удаляются ``<script>``/``<style>``/``<nav>``/``<footer>``
для уменьшения шума.

``markdownify`` объявлен в ``orchx`` extras pyproject.toml; импорт
ленивый — если библиотека не установлена, tool вернёт понятную ошибку
с инструкцией установки.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse

from . import Tool, ToolContext, ToolResult, permission_denied

# Жёсткие лимиты — не настраиваются через параметры tool'а, чтобы LLM не
# смогла их обойти запросив 100MB ответ.
_MAX_BYTES = 256 * 1024
_DEFAULT_TIMEOUT_S = 60.0


def _ip_is_blocked(ip_str: str) -> bool:
    """Проверить IP против блок-листа private / link-local / loopback."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # параноим: не валидный IP — блокируем
    # Все «небезопасные» категории сразу из stdlib.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_check_host(host: str) -> tuple[bool, str]:
    """Резолвить hostname в IP-адреса и проверить против блок-листа.

    Returns:
        ``(allowed, reason)``. ``allowed=False`` означает, что хотя бы один
        IP в результате попал в блок-лист (DNS rebinding-protection).
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        # IPv4: ('1.2.3.4', port); IPv6: ('::1', port, flow, scope).
        if not sockaddr:
            continue
        ip_str = sockaddr[0]
        # IPv6 scoped — отрежем '%scope'.
        ip_str = ip_str.split("%", 1)[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        if _ip_is_blocked(ip_str):
            return False, f"resolved IP {ip_str} is private/loopback/link-local"
    if not seen:
        return False, "no IPs resolved for host"
    return True, "ok"


_NOISE_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript")


def _html_to_markdown(html_str: str) -> tuple[str, str | None]:
    """Сконвертировать HTML в Markdown через ``markdownify``.

    Перед конвертацией удаляются шумные блоки (script/style/nav/footer/…),
    чтобы LLM получала только смысловой контент документации.

    Returns:
        ``(markdown, error)``. Если ``markdownify`` не установлен или упал,
        ``markdown=""`` и ``error`` — текст ошибки с инструкцией. Иначе
        ``error=None``.
    """
    try:
        from markdownify import markdownify as _md  # type: ignore[import-not-found]
    except ImportError:
        return "", (
            "webfetch markdown conversion requires the `markdownify` "
            "package. Install via `pip install markdownify` (or "
            "`pip install -e .[orchx]`) and retry, or call webfetch with "
            "`format='text'` to skip conversion."
        )

    # Грубо снимаем noise-теги вместе с содержимым — markdownify сам по себе
    # этого не делает, а LLM-у не нужны меню/футеры/скрипты в выдаче.
    cleaned = html_str
    for tag in _NOISE_TAGS:
        cleaned = re.sub(
            rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        # Самозакрывающиеся варианты на всякий случай.
        cleaned = re.sub(rf"<{tag}\b[^>]*/>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", cleaned)

    try:
        md = _md(
            cleaned,
            heading_style="ATX",  # # H1, ## H2 — а не подчёркивание
            bullets="-",
            strip=["script", "style"],
        )
    except Exception as e:  # noqa: BLE001
        return "", f"markdownify failed: {e!r}"

    # Markdownify нередко оставляет хвосты пустых строк/пробелов.
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md, None


class WebFetchTool(Tool):
    """Скачать публичный URL и вернуть содержимое в Markdown или plain text."""

    name = "webfetch"
    description = (
        "Fetch a public HTTPS URL and return its content (HTML → Markdown by "
        "default, or raw `text`). Private/loopback/link-local IPs are "
        "blocked (no cloud metadata endpoints, no LAN). Max 256KB; 30s "
        "timeout. NEVER guess URLs — use only URLs given in the task or "
        "found via cited references."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full https:// URL.",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "text"],
                "description": "Output format. Default 'markdown'.",
            },
        },
        "required": ["url"],
    }
    permission_attr = "webfetch"

    async def run(
        self,
        ctx: ToolContext,
        *,
        url: str,
        format: str = "markdown",  # noqa: A002 — это OpenAI-имя аргумента
    ) -> ToolResult:
        """Скачать URL с проверкой безопасности (см. описание класса)."""
        ctx.activity(f"webfetch {url[:80]}")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return permission_denied(
                tool="webfetch",
                target=url,
                reason=f"unsupported scheme {parsed.scheme!r}; only http(s) is allowed",
            )
        # HTTP → HTTPS upgrade.
        if parsed.scheme == "http":
            url = url.replace("http://", "https://", 1)
            parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host:
            return permission_denied(
                tool="webfetch",
                target=url,
                reason="URL has no hostname",
            )
        # Резолвим и блокируем private-сети.
        allowed, reason = _resolve_and_check_host(host)
        if not allowed:
            return permission_denied(
                tool="webfetch",
                target=url,
                reason=reason,
                hint=(
                    "Cloud metadata endpoints (169.254.169.254), LAN hosts "
                    "(10.x/192.168.x/172.16.x), and loopback are blocked."
                ),
            )

        # Ленивый импорт httpx — не делаем его hard-dep пакета.
        try:
            import httpx
        except ImportError:
            return ToolResult(
                content=(
                    "webfetch requires the `httpx` package. Install via "
                    "`pip install httpx` and retry."
                ),
                is_error=True,
            )

        # Качаем стримом, обрезая на лимите.
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=_DEFAULT_TIMEOUT_S,
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return ToolResult(
                            content=(
                                f"HTTP {resp.status_code} for {url}: "
                                f"{resp.reason_phrase}"
                            ),
                            is_error=True,
                        )
                    chunks: list[bytes] = []
                    collected = 0
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        if collected + len(chunk) > _MAX_BYTES:
                            chunks.append(chunk[: _MAX_BYTES - collected])
                            collected = _MAX_BYTES
                            break
                        chunks.append(chunk)
                        collected += len(chunk)
                    body_b = b"".join(chunks)
                    content_type = resp.headers.get("content-type", "")
        except TimeoutError:
            return ToolResult(
                content=f"webfetch timed out after {_DEFAULT_TIMEOUT_S}s",
                is_error=True,
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                content=f"webfetch network error: {e!r}",
                is_error=True,
            )

        text = body_b.decode("utf-8", errors="replace")
        is_html = "html" in content_type.lower() or text.lstrip().startswith("<")
        if format == "markdown" and is_html:
            md, err = _html_to_markdown(text)
            if err is not None:
                return ToolResult(content=err, is_error=True)
            text = md
        truncated = collected == _MAX_BYTES
        suffix = (
            f"\n\n... (response truncated at {_MAX_BYTES // 1024}KB)"
            if truncated
            else ""
        )
        return ToolResult(content=text + suffix)


def _maybe_call(cb: Any, arg: Any) -> None:  # pragma: no cover
    """Unused; placeholder для будущего streaming-callback'а."""
    if cb is None:
        return
    try:
        cb(arg)
    except Exception:  # noqa: BLE001
        pass
