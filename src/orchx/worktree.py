"""Управление git worktree-ами для изоляции воркеров роя."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    """Ошибка при выполнении git-команды."""


async def _git(*args: str, cwd: Path) -> str:
    """Запустить git с заданными аргументами в указанной директории.

    Returns:
        stdout как строка.

    Raises:
        GitError: Если git завершился с ненулевым кодом.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} (cwd={cwd}) failed [{proc.returncode}]: {stderr}"
        )
    return stdout


class DirtyWorkingTreeError(GitError):
    """Сигнал, что репо не чист — диспетчер откажется стартовать.

    Раздельный класс — чтобы CLI мог отличить «грязный workdir» от прочих
    git-ошибок и показать пользователю осмысленный совет (commit/stash/auto-stash)
    вместо traceback'а.
    """

    def __init__(self, status: str) -> None:
        """Сохранить вывод ``git status --porcelain`` для последующего показа."""
        self.status = status
        super().__init__(self._format_message(status))

    @staticmethod
    def _format_message(status: str) -> str:
        files = []
        for line in status.splitlines():
            line = line.strip()
            if not line:
                continue
            # формат `XY path` от --porcelain=v1
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                files.append(parts[1])
            else:
                files.append(line)
        files_block = "\n".join(f"  - {f}" for f in files[:20])
        if len(files) > 20:
            files_block += f"\n  ... and {len(files) - 20} more"
        return (
            "Repo has uncommitted changes to tracked files — refusing to start orchX.\n\n"
            "Why this matters: worker worktrees are created from committed refs, "
            "not from your working tree. If orchX runs while files are dirty, "
            "workers will silently use the OLD committed version of those files — "
            "and any final merge into the integration branch may conflict with "
            "your uncommitted edits.\n\n"
            f"Files with uncommitted changes ({len(files)}):\n{files_block}\n\n"
            "Choose one:\n"
            "  1) git stash push -u -m 'pre-orchX' && orchx all '...'  "
            "(then `git stash pop` after orchX finishes)\n"
            "  2) git add -A && git commit -m '...'  (commit your changes)\n"
            "  3) orchx all --auto-stash '...'  (orchX stashes for you "
            "and pops automatically at the end)\n"
            "  4) orchx all --allow-dirty '...'  (UNSAFE: ignore the warning)"
        )


async def ensure_clean(repo_root: Path, *, allow_dirty: bool = False) -> str:
    """Убедиться, что в репозитории нет незакоммиченных правок отслеживаемых файлов.

    Untracked файлы и игнорируемые игнорируются — они не повлияют на worktrees
    роя (новые worktrees наследуют только tracked содержимое от base ref).

    Args:
        repo_root: корень репозитория.
        allow_dirty: если True, грязное состояние не считается ошибкой (вернётся
            непустая строка с порцеляновым статусом). Использовать только если
            ты на 100% понимаешь, что делаешь.

    Returns:
        Порцелянов статус (пустой, если чисто).

    Raises:
        DirtyWorkingTreeError: если состояние грязное и ``allow_dirty=False``.
    """
    status = await _git(
        "status", "--porcelain=v1", "--untracked-files=no", cwd=repo_root
    )
    if status and not allow_dirty:
        raise DirtyWorkingTreeError(status)
    return status


async def auto_stash(repo_root: Path, label: str) -> str | None:
    """Засташить грязные tracked-правки, если они есть. Вернуть имя stash entry.

    Args:
        repo_root: корень репозитория.
        label: человеко-читаемая метка stash entry.

    Returns:
        ``stash@{0}`` имя stash-entry, если стэш был создан. ``None``, если
        стэшить было нечего.
    """
    status = await _git(
        "status", "--porcelain=v1", "--untracked-files=no", cwd=repo_root
    )
    if not status:
        return None
    # `git stash push` без -u: untracked не трогаем (они не блокируют рой).
    await _git("stash", "push", "-m", label, cwd=repo_root)
    return "stash@{0}"


async def stash_pop(repo_root: Path) -> None:
    """Снять верхний stash entry.

    Игнорирует ошибки конфликтов — пользователь увидит их в обычном
    ``git status`` потом.
    """
    try:
        await _git("stash", "pop", cwd=repo_root)
    except GitError as e:  # noqa: BLE001
        logger.warning("git stash pop failed (manual resolution required): %s", e)


async def create_integration_branch(
    repo_root: Path, base_branch: str, integration_branch: str
) -> None:
    """Создать интеграционную ветку из base_branch (если ещё не существует).

    Если ветка уже есть — проверяем, что её tip потомок base_branch, иначе ошибка.
    """
    # Проверим существование локальной ветки.
    branch_exists = True
    try:
        await _git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{integration_branch}",
            cwd=repo_root,
        )
    except GitError:
        branch_exists = False
    if branch_exists:
        merge_base = await _git(
            "merge-base", integration_branch, base_branch, cwd=repo_root
        )
        base_sha = await _git("rev-parse", base_branch, cwd=repo_root)
        if merge_base != base_sha:
            raise GitError(
                f"Integration branch {integration_branch} has diverged from {base_branch}. "
                "Delete or merge manually before re-running."
            )
        logger.info("Reusing integration branch %s", integration_branch)
        return
    await _git("branch", integration_branch, base_branch, cwd=repo_root)
    logger.info(
        "Created integration branch %s from %s", integration_branch, base_branch
    )


async def add_worktree(
    repo_root: Path, worktree_path: Path, branch: str, base_ref: str
) -> Path:
    """Создать новый worktree и новую ветку из base_ref.

    Args:
        repo_root: Корень основного репозитория.
        worktree_path: Куда положить worktree.
        branch: Имя новой ветки воркера.
        base_ref: Ref, от которого создаётся ветка (обычно интеграционная).

    Returns:
        Путь к созданному worktree.
    """
    if worktree_path.exists():
        raise GitError(f"Worktree path already exists: {worktree_path}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await _git(
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree_path),
        base_ref,
        cwd=repo_root,
    )
    return worktree_path


async def remove_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Удалить worktree (без force, с force как fallback)."""
    if not worktree_path.exists():
        return
    try:
        await _git("worktree", "remove", str(worktree_path), cwd=repo_root)
    except GitError:
        logger.warning(
            "worktree remove failed, retrying with --force: %s", worktree_path
        )
        try:
            await _git(
                "worktree", "remove", "--force", str(worktree_path), cwd=repo_root
            )
        except GitError as e:
            logger.error("worktree --force remove also failed: %s", e)
            # Last resort: rmtree, then prune.
            shutil.rmtree(worktree_path, ignore_errors=True)
            await _git("worktree", "prune", cwd=repo_root)


async def delete_branch(repo_root: Path, branch: str) -> None:
    """Принудительно удалить локальную ветку. Не падает, если её нет."""
    try:
        await _git("branch", "-D", branch, cwd=repo_root)
    except GitError:
        # Ветка отсутствует или checked-out где-то — это ок, пробуем дальше.
        pass


async def commit_all(
    worktree_path: Path, message: str, author_name: str, author_email: str
) -> str | None:
    """Закоммитить все изменения в worktree. Возвращает SHA или None если коммитить нечего."""
    status = await _git("status", "--porcelain", cwd=worktree_path)
    if not status:
        return None
    await _git("add", "-A", cwd=worktree_path)
    await _git(
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "-m",
        message,
        "--no-verify",  # хуки могут отбить коммит на половине задачи; диспетчер сам прогонит acceptance
        cwd=worktree_path,
    )
    return await _git("rev-parse", "HEAD", cwd=worktree_path)


async def merge_branch_into(
    integration_worktree: Path,
    source_branch: str,
    *,
    no_ff: bool = True,
) -> tuple[bool, str]:
    """Смержить source_branch в текущую ветку integration_worktree.

    Args:
        integration_worktree: Worktree, который чек-аутнут на интеграционную ветку.
        source_branch: Ветка воркера, которую мерджим.
        no_ff: Использовать --no-ff merge commit (рекомендуется для трассировки).

    Returns:
        (success, output). success=False, если merge оставил конфликты или
        завершился с ошибкой.
    """
    args = ["merge", "--no-edit"]
    if no_ff:
        args.append("--no-ff")
    args.append(source_branch)
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(integration_worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    output = (stdout_b.decode() + stderr_b.decode()).strip()
    return proc.returncode == 0, output


async def add_integration_worktree(
    repo_root: Path, worktree_path: Path, branch: str
) -> Path:
    """Создать worktree, чек-аутнутый на уже существующую интеграционную ветку.

    Используется для merge-операций, чтобы не трогать основной checkout пользователя.
    """
    if worktree_path.exists():
        raise GitError(f"Integration worktree already exists: {worktree_path}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await _git("worktree", "add", str(worktree_path), branch, cwd=repo_root)
    return worktree_path
