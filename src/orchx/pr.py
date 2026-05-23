"""Пуш интеграционной ветки и создание GitHub PR.

Стратегия трёхступенчатая, с автоматическим fallback'ом без прерывания пайплайна:

1. ``gh pr create`` — если ``gh`` есть в PATH **и** авторизован
   (``gh auth status`` rc=0). Это даёт самый красивый вывод и нативный
   репорт ошибок.
2. ``gh`` + ``GH_TOKEN`` из ``git credential fill`` — если ``gh`` есть, но
   не авторизован, но машина уже умеет пушить в этот remote (значит,
   токен лежит в credential helper / keychain). Подсовываем токен через
   env, ``gh`` использует его как Bearer.
3. **GitHub REST API напрямую** через ``curl`` — последний рубеж, если
   ``gh`` нет вообще. Берём токен через ``git credential fill`` и
   делаем ``POST /repos/{owner}/{repo}/pulls``.

Если ни один путь не сработал, диспетчер всё равно успешно делает push,
формирует **compare URL** и возвращает его пользователю для ручного
создания PR в один клик.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote

from . import paths

logger = logging.getLogger(__name__)


async def _run(
    *args: str, cwd: Path, env: dict[str, str] | None = None, stdin: bytes | None = None
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        env=env,
    )
    stdout_b, stderr_b = await proc.communicate(input=stdin)
    return proc.returncode or 0, (
        stdout_b.decode("utf-8", errors="replace")
        + stderr_b.decode("utf-8", errors="replace")
    )


async def _git_credential_token(*, cwd: Path) -> str | None:
    """Достать GitHub-токен через ``git credential fill`` без интерактива.

    Если git настроен с credential helper (osxkeychain/manager-core/cache),
    это работает даже когда ``gh`` не авторизован. Возвращаем None на любой
    сбой — вызывающий код просто пойдёт следующим путём.
    """
    helper_input = b"protocol=https\nhost=github.com\n\n"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "credential",
            "fill",
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Никакого интерактивного промпта — если токена нет, fail быстро.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout_b, _ = await asyncio.wait_for(
            proc.communicate(input=helper_input), timeout=5.0
        )
    except (TimeoutError, OSError):
        return None
    if proc.returncode != 0:
        return None
    for line in stdout_b.decode("utf-8", errors="replace").splitlines():
        if line.startswith("password="):
            token = line[len("password=") :].strip()
            return token or None
    return None


async def _gh_authenticated() -> bool:
    """Проверить, что ``gh`` готов делать API-запросы без подсказок."""
    if not shutil.which("gh"):
        return False
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "auth",
        "status",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    return rc == 0


_GITHUB_REMOTE_RE = re.compile(
    r"(?:git@|https?://)github\.com[:/]([^/]+)/([^/.\s]+?)(?:\.git)?\s*$"
)


async def _github_compare_url(
    *, integration_worktree: Path, integration_branch: str, base_branch: str
) -> str | None:
    """Сформировать URL вида https://github.com/<owner>/<repo>/compare/<base>...<head>.

    Возвращает None, если remote не определяется как GitHub. В compare URL
    автоматически добавлен ``?expand=1`` — GitHub откроет форму создания PR.
    """
    code, out = await _run(
        "git", "remote", "get-url", "origin", cwd=integration_worktree
    )
    if code != 0:
        return None
    remote = out.strip().splitlines()[0] if out.strip() else ""
    m = _GITHUB_REMOTE_RE.search(remote)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return (
        f"https://github.com/{owner}/{repo}/compare/"
        f"{quote(base_branch, safe='/')}...{quote(integration_branch, safe='/')}?expand=1"
    )


# Пути, которые игнорируются при оценке «значимости» дельты ветки.
# Это служебные файлы роя; их одних недостаточно, чтобы оправдать PR.
# Источник правды — `paths.ORCHX_ARTEFACT_PREFIXES`.
_ORCHX_ARTEFACT_PREFIXES = paths.ORCHX_ARTEFACT_PREFIXES


async def diff_against_base(
    *, integration_worktree: Path, base_branch: str
) -> dict[str, list[str]]:
    """Вернуть набор изменённых файлов интеграционной ветки vs base_branch.

    Группируем по «значимости»:
    - ``meaningful``: реальные правки кода/тестов/документации;
    - ``orchX_artefacts``: служебные файлы роя.

    Используется, чтобы не открывать «пустой» PR, если воркеры написали
    только свои `result.json`.
    """
    code, out = await _run(
        "git",
        "diff",
        "--name-only",
        f"{base_branch}...HEAD",
        cwd=integration_worktree,
    )
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if code != 0:
        # Не смогли посчитать дельту — относим всё к meaningful, чтобы не
        # потерять реальные правки молча.
        return {"meaningful": files, "orchX_artefacts": []}
    meaningful: list[str] = []
    artefacts: list[str] = []
    for f in files:
        if any(f.startswith(p) for p in _ORCHX_ARTEFACT_PREFIXES):
            artefacts.append(f)
        else:
            meaningful.append(f)
    return {"meaningful": meaningful, "orchX_artefacts": artefacts}


async def push_and_open_pr(
    *,
    repo_root: Path,
    integration_worktree: Path,
    integration_branch: str,
    base_branch: str,
    title: str,
    body: str,
    skip_if_empty: bool = True,
) -> dict[str, str]:
    """Запушить интеграционную ветку и открыть PR в base_branch.

    Args:
        repo_root: корень репозитория (где работает `gh`).
        integration_worktree: worktree, на котором лежит интеграционная ветка.
        integration_branch: имя ветки.
        base_branch: целевая ветка PR.
        title, body: содержимое PR.
        skip_if_empty: если True (по умолчанию) и в дельте нет ни одного
            «значимого» файла (только `.orchx/results/*.json` и т.п.) —
            push и PR не делаются. Это спасает от истории «PR создан, но
            смержив его, ничего не изменится».

    Returns:
        ``{"push": ..., "pr_url": ..., "error": ...}``. При skip даёт
        ``"error": "no meaningful changes — PR skipped"``.
    """
    diff_groups = await diff_against_base(
        integration_worktree=integration_worktree, base_branch=base_branch
    )
    meaningful = diff_groups["meaningful"]
    artefacts = diff_groups["orchX_artefacts"]
    if skip_if_empty and not meaningful:
        msg = (
            "в интеграционной ветке нет значимых изменений кода — PR пропущен. "
            f"Изменены только служебные файлы роя ({len(artefacts)} шт.: "
            f"{', '.join(artefacts[:3])}{'…' if len(artefacts) > 3 else ''}). "
            "Обычно это означает, что рой остановился до того, как хотя бы "
            "один воркер смержил реальный код (провалилась первая фаза или "
            "все задачи были пропущены)."
        )
        return {
            "push": "",
            "pr_url": "",
            "error": msg,
            "diff_meaningful": meaningful,
            "diff_artefacts": artefacts,
        }

    code, push_output = await _run(
        "git", "push", "-u", "origin", integration_branch, cwd=integration_worktree
    )
    if code != 0:
        return {
            "push": push_output,
            "pr_url": "",
            "error": "push failed",
            "diff_meaningful": meaningful,
            "diff_artefacts": artefacts,
        }

    # Заранее посчитаем compare URL — он пригодится и при наличии gh
    # (открыть в браузере), и как fallback.
    compare_url = await _github_compare_url(
        integration_worktree=integration_worktree,
        integration_branch=integration_branch,
        base_branch=base_branch,
    )

    # Соберём результирующий «короб», который будем дополнять по ходу.
    result: dict[str, str | list[str]] = {
        "push": push_output,
        "pr_url": "",
        "compare_url": compare_url or "",
        "error": "",
        "diff_meaningful": meaningful,
        "diff_artefacts": artefacts,
    }

    # --- Путь 1: gh CLI авторизован → штатный create.
    has_gh = bool(shutil.which("gh"))
    gh_ready = await _gh_authenticated() if has_gh else False
    if gh_ready:
        pr_url, err = await _gh_pr_create(
            repo_root=repo_root,
            base_branch=base_branch,
            integration_branch=integration_branch,
            title=title,
            body=body,
        )
        if pr_url:
            result["pr_url"] = pr_url
            return result
        # gh запустился, но упал — попробуем достать токен и пройти REST.
        result["error"] = err

    # --- Путь 2: gh есть, но не авторизован → подсунем токен через GH_TOKEN.
    token = await _git_credential_token(cwd=repo_root)
    if has_gh and not gh_ready and token:
        env_with_token = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}
        pr_url, err = await _gh_pr_create(
            repo_root=repo_root,
            base_branch=base_branch,
            integration_branch=integration_branch,
            title=title,
            body=body,
            env=env_with_token,
        )
        if pr_url:
            result["pr_url"] = pr_url
            result["error"] = ""
            return result
        # gh + токен не сработал — спускаемся на REST.
        result["error"] = err

    # --- Путь 3: REST API напрямую (gh нет / упал и есть токен).
    if token:
        pr_url, err = await _create_pr_via_rest(
            integration_worktree=integration_worktree,
            base_branch=base_branch,
            integration_branch=integration_branch,
            title=title,
            body=body,
            token=token,
        )
        if pr_url:
            result["pr_url"] = pr_url
            result["error"] = ""
            return result
        result["error"] = err

    # --- Все пути исчерпаны: даём compare URL, чтобы пользователь
    # открыл его в один клик.
    if not result["error"]:
        result["error"] = (
            "нет учётных данных GitHub — ветка запушена; откройте compare URL "
            "ниже, чтобы создать PR в один клик (установите gh: brew install gh, "
            "затем gh auth login — после этого диспетчер создаст PR автоматически)"
        )
    return result


async def _gh_pr_create(
    *,
    repo_root: Path,
    base_branch: str,
    integration_branch: str,
    title: str,
    body: str,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Запустить ``gh pr create`` и вернуть (pr_url, error_message).

    На успехе ``error_message`` пустой. На ошибке ``pr_url`` пустой.
    """
    code, output = await _run(
        "gh",
        "pr",
        "create",
        "--base",
        base_branch,
        "--head",
        integration_branch,
        "--title",
        title,
        "--body",
        body,
        cwd=repo_root,
        env=env,
    )
    if code == 0:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("https://"):
                return line, ""
        # gh успешно завершился, но URL не нашли — что-то странное.
        return "", f"gh pr create rc=0 but no URL in output: {output.strip()}"
    return "", f"gh pr create failed: {output.strip()}"


async def _create_pr_via_rest(
    *,
    integration_worktree: Path,
    base_branch: str,
    integration_branch: str,
    title: str,
    body: str,
    token: str,
) -> tuple[str, str]:
    """Создать PR через ``POST /repos/{owner}/{repo}/pulls``.

    Используется как fallback когда ``gh`` недоступен. Берёт ``owner/repo``
    из remote, шлёт JSON через ``curl`` (он есть на macOS/Linux всегда,
    отдельной HTTP-зависимости в Python не тащим).
    """
    code, out = await _run(
        "git", "remote", "get-url", "origin", cwd=integration_worktree
    )
    if code != 0:
        return "", f"REST: cannot read origin URL: {out.strip()}"
    remote = out.strip().splitlines()[0] if out.strip() else ""
    m = _GITHUB_REMOTE_RE.search(remote)
    if not m:
        return "", f"REST: origin is not a github.com remote: {remote!r}"
    owner, repo = m.group(1), m.group(2)

    payload = _json.dumps(
        {
            "title": title,
            "head": integration_branch,
            "base": base_branch,
            "body": body,
            "maintainer_can_modify": True,
            "draft": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    if not shutil.which("curl"):
        return "", "REST: curl not found in PATH; cannot reach api.github.com"

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    code, output = await _run(
        "curl",
        "-sS",
        "-X",
        "POST",
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-H",
        "User-Agent: orchX",
        "--data-binary",
        "@-",
        api_url,
        cwd=integration_worktree,
        stdin=payload,
    )
    if code != 0:
        return "", f"REST: curl failed (exit={code}): {output.strip()}"
    try:
        data = _json.loads(output)
    except _json.JSONDecodeError as e:
        return "", f"REST: invalid JSON response: {e}; body={output[:200]!r}"
    if isinstance(data, dict) and data.get("html_url"):
        return data["html_url"], ""
    # Типичные ошибки: 422 «pull request already exists», 401 bad creds.
    msg = ""
    if isinstance(data, dict):
        msg = data.get("message") or ""
        errors = data.get("errors") or []
        if errors:
            details = "; ".join(
                e.get("message") or _json.dumps(e, ensure_ascii=False)
                for e in errors
                if isinstance(e, dict)
            )
            msg = f"{msg}: {details}" if msg else details
        # Если PR уже существует — найдём и вернём его URL, это «успех».
        if "already exists" in (msg or "").lower():
            existing = await _find_existing_pr(
                owner=owner,
                repo=repo,
                integration_branch=integration_branch,
                token=token,
                cwd=integration_worktree,
            )
            if existing:
                return existing, ""
    return "", f"REST: GitHub API rejected PR creation: {msg or output.strip()[:300]}"


async def _find_existing_pr(
    *,
    owner: str,
    repo: str,
    integration_branch: str,
    token: str,
    cwd: Path,
) -> str | None:
    """Найти открытый PR из ``integration_branch``. Возвращает html_url или None."""
    if not shutil.which("curl"):
        return None
    head_filter = f"{owner}:{integration_branch}"
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/pulls"
        f"?state=open&head={quote(head_filter, safe=':/')}"
    )
    code, output = await _run(
        "curl",
        "-sS",
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "User-Agent: orchX",
        api_url,
        cwd=cwd,
    )
    if code != 0:
        return None
    try:
        data = _json.loads(output)
    except _json.JSONDecodeError:
        return None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first.get("html_url")
    return None


def render_pr_body(summary: dict) -> str:
    """Сгенерировать PR-описание из summary роя."""
    lines = [
        f"# orchX: {summary['task_id']}",
        "",
    ]
    if summary.get("summary"):
        lines += [summary["summary"], ""]

    counts = summary.get("counts", {})
    succ = counts.get("success", 0)
    fail = counts.get("failed", 0)
    skip = counts.get("skipped", 0)
    total = succ + fail + skip + counts.get("pending", 0) + counts.get("running", 0)

    # Прозрачный warning сразу в начале PR, если рой не дошёл до конца —
    # иначе пользователь увидит «PR создан» и не поймёт, что 80% работы
    # просто не сделано (как в предыдущем прогоне ts-03-modularity).
    halt_reason = summary.get("halt_reason") or summary.get("abort_reason")
    if halt_reason or fail > 0 or skip > 0:
        lines.append("> [!WARNING]")
        if halt_reason:
            lines.append(
                f"> **orchX остановлен до завершения работы.** {halt_reason}"
            )
        if fail > 0 or skip > 0:
            lines.append(
                f"> Задачи: {succ} успешно, {fail} провалено, {skip} пропущено "
                f"из {total} всего. Изменения кода из проваленных/пропущенных "
                f"задач **не включены** в этот PR — в интеграционную ветку "
                f"смержены только успешные задачи."
            )
        lines.append(
            "> "
            "Изучите таблицу проваленных задач ниже и решите: исправить вручную "
            "и перезапустить рой или закрыть этот PR и отказаться от прогона."
        )
        lines.append("")

    lines += [
        f"- **Интеграционная ветка:** `{summary['integration_branch']}`",
        f"- **Базовая ветка:** `{summary['base_branch']}`",
        f"- **Время выполнения:** {summary['wall_seconds']}с",
        f"- **Перепланирований:** {summary.get('replan_count', 0)}",
    ]
    if summary.get("aborted"):
        lines.append(f"- **Прерван:** {summary.get('abort_reason') or 'да'}")
    if summary.get("halt_reason"):
        lines.append(f"- **Остановлен:** {summary['halt_reason']}")
    if summary.get("spec_files"):
        lines.append("- **Файлы спецификации:**")
        for sf in summary["spec_files"]:
            lines.append(f"  - `{sf}`")
    lines.append("")

    # Phases section (only if phased plan).
    phases = summary.get("phases", [])
    show_phases = len(phases) > 1 or (
        phases and phases[0].get("id") != "main"
    )
    if show_phases:
        lines += [
            "## Фазы работы",
            "",
            "| Фаза | Статус | Задач | Длительность | Цель |",
            "| --- | --- | --- | --- | --- |",
        ]
        for ph in phases:
            goal = (ph.get("goal") or "").replace("|", "\\|").replace("\n", " ")
            if len(goal) > 300:
                goal = goal[:297] + "..."
            duration = (
                f"{ph['duration_s']}с" if ph.get("duration_s") else "-"
            )
            lines.append(
                f"| `{ph['id']}` | {ph['status']} | {ph['task_count']} | "
                f"{duration} | {goal} |"
            )
        lines.append("")

    # Replan history (only if there were replans).
    replan_history = summary.get("replan_history") or []
    if replan_history:
        lines += [
            "## История перепланирований",
            "",
        ]
        for entry in replan_history:
            lines.append(
                f"- **Попытка {entry['attempt']}** на фазе `{entry['failed_phase']}` "
                f"(проваленные задачи: {', '.join(entry.get('failed_tasks', [])) or '-'}) "
                f"→ {entry['outcome']}"
            )
            if entry.get("new_phases"):
                lines.append(
                    f"  - Новые фазы: {', '.join(entry['new_phases'])}"
                )
            if entry.get("error"):
                lines.append(f"  - Ошибка: {entry['error']}")
        lines.append("")

    # Группируем задачи по статусу, чтобы failed/skipped было видно сразу.
    tasks = summary.get("tasks") or []
    failed_tasks = [t for t in tasks if t.get("status") == "failed"]
    skipped_tasks = [t for t in tasks if t.get("status") == "skipped"]
    success_tasks = [t for t in tasks if t.get("status") == "success"]

    if failed_tasks:
        lines += [
            "## ✗ Проваленные задачи",
            "",
            "Эти задачи провалили acceptance после всех retry'ев. Их правки **не смержены**.",
            "",
            "| ID | Агент | Попыток | Причина |",
            "| --- | --- | --- | --- |",
        ]
        for t in failed_tasks:
            notes = (t.get("notes") or "").replace("|", "\\|").replace("\n", " ")
            if len(notes) > 300:
                notes = notes[:297] + "..."
            lines.append(
                f"| `{t['id']}` | {t['agent']} | {t['attempts']} | {notes} |"
            )
        lines.append("")

    if skipped_tasks:
        lines += [
            "## ⊘ Пропущенные задачи",
            "",
            "Эти задачи не запускались из-за провалившихся зависимостей или halt-а роя.",
            "",
            "| ID | Агент | Причина |",
            "| --- | --- | --- |",
        ]
        for t in skipped_tasks:
            notes = (t.get("notes") or "").replace("|", "\\|").replace("\n", " ")
            if len(notes) > 300:
                notes = notes[:297] + "..."
            lines.append(f"| `{t['id']}` | {t['agent']} | {notes} |")
        lines.append("")

    if success_tasks:
        lines += [
            "## ✓ Успешные задачи",
            "",
            "| ID | Агент | Попыток | Заметки |",
            "| --- | --- | --- | --- |",
        ]
        for t in success_tasks:
            notes = (t.get("notes") or "").replace("|", "\\|").replace("\n", " ")
            if len(notes) > 300:
                notes = notes[:297] + "..."
            lines.append(
                f"| `{t['id']}` | {t['agent']} | {t['attempts']} | {notes} |"
            )
        lines.append("")

    lines += [
        "Создано с помощью **orchX**.",
        f"Полный лог: `{summary['log_file']}`",
    ]
    return "\n".join(lines)
