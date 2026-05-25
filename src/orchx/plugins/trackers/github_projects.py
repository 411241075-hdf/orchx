"""GitHub Projects v2 tracker plugin.

Поддерживает Kanban-workflow:

* ``list_ready_tasks()`` — все items в колонке ``Ready`` (имя настраивается).
* ``pick_next_ready_task()`` — взять первую задачу из Ready и сразу
  передвинуть её в ``In Progress`` (атомарная операция, чтобы 2 агента
  не схватили одну и ту же задачу).
* ``move_task(task_id, column)`` — передвинуть карточку в любую колонку.
* ``update_status(task_id, status, details)`` — оставить комментарий на
  связанном issue (через ``GithubTracker`` под капотом) + двинуть в
  соответствующую колонку.
* ``fetch_task_description(task_id)`` — body issue.

Реализация работает через ``gh api graphql`` (Projects v2 REST API нет —
только GraphQL). Требуется ``gh`` CLI с правами scope ``project,repo``.

Формат task_id:

* Простой ``"<issue_number>"`` принимается ``fetch_task_description`` /
  ``update_status`` (для обратной совместимости с GithubTracker).
* Композитный ``"<project_item_id>:<issue_number>"`` возвращается
  ``list_ready_tasks`` / ``pick_next_ready_task`` и принимается
  ``move_task`` (нужен project_item_id для GraphQL мутаций).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from .github import GithubTracker

logger = logging.getLogger(__name__)


@dataclass
class GithubTask:
    """Concrete implementation of :class:`orchx.plugins.contracts.TaskHandle`."""

    id: str
    """``<project_item_id>:<issue_number>`` (composite)."""

    title: str
    body: str
    url: str | None = None
    project_item_id: str = ""
    issue_number: str = ""

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> GithubTask:
        content = item.get("content") or {}
        issue_number = str(content.get("number") or "")
        project_item_id = str(item.get("id") or "")
        composite = (
            f"{project_item_id}:{issue_number}"
            if project_item_id and issue_number
            else (project_item_id or issue_number or "")
        )
        return cls(
            id=composite,
            title=str(content.get("title") or ""),
            body=str(content.get("body") or ""),
            url=content.get("url"),
            project_item_id=project_item_id,
            issue_number=issue_number,
        )


# ---------------------------------------------------------------------------
# gh GraphQL helpers
# ---------------------------------------------------------------------------


async def _gh_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Запустить ``gh api graphql`` с переданными query/variables.

    Returns: JSON-ответ ``{"data": ..., "errors": [...]}``.
    Бросает :class:`subprocess.CalledProcessError` если gh упал.
    """
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if isinstance(v, int):
            args.extend(["-F", f"{k}={v}"])
        else:
            args.extend(["-f", f"{k}={v}"])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode or 1, args, stdout_b, stderr_b
        )
    try:
        data: dict[str, Any] = json.loads(stdout_b.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh api graphql returned invalid JSON: {e}") from e
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class GithubProjectsTracker:
    """GitHub Projects v2 backend для orchX TrackerPlugin.

    Config (через ``plugin_config.github-projects`` в config.yaml):

    * ``owner_repo``: ``owner/repo``. По умолчанию резолвится из git remote.
    * ``project_owner``: ``orgs/<org>`` или ``users/<user>``. По умолчанию
      берётся из ``owner_repo`` (первая часть). Для organization-level
      projects надо явно указать ``orgs/...``.
    * ``project_number``: int, номер Projects v2 (см. URL проекта в UI).
    * ``status_field``: имя поля статуса (default ``"Status"``).
    * ``ready_column``: имя колонки "готово к работе" (default ``"Ready"``).
    * ``in_progress_column``: default ``"In Progress"``.
    * ``done_column``: default ``"Done"``.

    Lazy-loaded поля (резолвятся при первом вызове):

    * ``_project_id`` — GraphQL global id проекта.
    * ``_status_field_id`` — global id поля Status.
    * ``_option_ids`` — mapping имя_колонки → option id.
    """

    name = "github-projects"

    def __init__(
        self,
        *,
        owner_repo: str | None = None,
        project_owner: str | None = None,
        project_number: int | None = None,
        status_field: str = "Status",
        ready_column: str = "Ready",
        in_progress_column: str = "In progress",
        done_column: str = "Done",
        **_: Any,
    ) -> None:
        self.owner_repo = owner_repo
        # project_owner типа ``orgs/<org>`` или ``users/<user>``.
        if project_owner:
            self.project_owner = project_owner.rstrip("/")
        elif owner_repo:
            # Угадаем по owner_repo. Тип (user/org) определим лениво.
            self.project_owner = owner_repo.split("/", 1)[0]
        else:
            self.project_owner = ""
        self.project_number = project_number
        self.status_field = status_field
        self.ready_column = ready_column
        self.in_progress_column = in_progress_column
        self.done_column = done_column

        # Для базового API (issues comment / view) переиспользуем GithubTracker.
        self._issue_tracker = GithubTracker(owner_repo=owner_repo)

        # Lazy-cache:
        self._project_id: str | None = None
        self._status_field_id: str | None = None
        self._option_ids: dict[str, str] = {}

    # ----------------------------------------------------------------
    # Lazy resolvers
    # ----------------------------------------------------------------

    async def _ensure_project_meta(self) -> None:
        """Резолвим project id, status field id, option ids (один раз)."""
        if self._project_id is not None:
            return
        if not self.project_owner or not self.project_number:
            raise RuntimeError(
                "github-projects: project_owner и project_number обязательны "
                "(задайте в .orchx/config.yaml → plugin_config.github-projects)."
            )

        # Пробуем org, потом user — что найдётся.
        last_err: Exception | None = None
        for kind in ("organization", "user"):
            query = """
            query($owner: String!, $number: Int!) {
              %s(login: $owner) {
                projectV2(number: $number) {
                  id
                  fields(first: 50) {
                    nodes {
                      ... on ProjectV2SingleSelectField {
                        id
                        name
                        options { id name }
                      }
                    }
                  }
                }
              }
            }
            """ % kind  # noqa: UP031
            try:
                data = await _gh_graphql(
                    query,
                    {"owner": self.project_owner.split("/")[-1],
                     "number": int(self.project_number)},
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
            root = (data.get("data") or {}).get(kind)
            if not root:
                continue
            project = root.get("projectV2")
            if not project:
                continue
            self._project_id = project["id"]
            for field in project.get("fields", {}).get("nodes") or []:
                if not field:
                    continue
                if field.get("name") == self.status_field:
                    self._status_field_id = field["id"]
                    for opt in field.get("options") or []:
                        self._option_ids[opt["name"]] = opt["id"]
                    break
            if not self._status_field_id:
                raise RuntimeError(
                    f"github-projects: status field {self.status_field!r} not "
                    f"found in project #{self.project_number}. Configured "
                    f"fields: only single-select supported."
                )
            return

        raise RuntimeError(
            f"github-projects: cannot find project {self.project_owner}#{self.project_number} "
            f"(tried as org and user). Last error: {last_err}"
        )

    def _option_id_for(self, column: str) -> str:
        if column not in self._option_ids:
            raise RuntimeError(
                f"github-projects: column {column!r} not found in field "
                f"{self.status_field!r}. Available: {list(self._option_ids)}"
            )
        return self._option_ids[column]

    # ----------------------------------------------------------------
    # Helpers: parse task_id
    # ----------------------------------------------------------------

    @staticmethod
    def _split_task_id(task_id: str) -> tuple[str, str]:
        """Разобрать composite ``"<project_item_id>:<issue_number>"``.

        Если в task_id нет ":" — это просто issue number, project_item_id
        пустой (используется для fetch/comment, но не для move).
        """
        if ":" in task_id:
            pid, _, num = task_id.partition(":")
            return pid, num
        return "", task_id

    # ----------------------------------------------------------------
    # TrackerPlugin minimal API
    # ----------------------------------------------------------------

    async def fetch_task_description(self, task_id: str) -> str | None:
        _, issue_num = self._split_task_id(task_id)
        return await self._issue_tracker.fetch_task_description(issue_num)

    async def update_status(
        self,
        task_id: str,
        status: str,
        details: str = "",
    ) -> None:
        # 1. Комментарий на issue (как в GithubTracker).
        _, issue_num = self._split_task_id(task_id)
        if issue_num:
            await self._issue_tracker.update_status(issue_num, status, details)
        # 2. Двинуть карточку, если task_id содержит project_item_id.
        column_map = {
            "running": self.in_progress_column,
            "done": self.done_column,
            "failed": self.in_progress_column,  # оставляем в работе для retry
            "replanned": self.in_progress_column,
        }
        target = column_map.get(status)
        if target:
            try:
                await self.move_task(task_id, target)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "github-projects: move_task(%s, %s) failed", task_id, target,
                    exc_info=True,
                )

    # ----------------------------------------------------------------
    # TrackerPlugin Kanban API
    # ----------------------------------------------------------------

    async def list_ready_tasks(self, limit: int = 20) -> list[GithubTask]:
        await self._ensure_project_meta()
        assert self._project_id and self._status_field_id
        ready_opt = self._option_id_for(self.ready_column)

        # Берём первые 100 items, фильтруем по status==ready_opt.
        query = """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100) {
                nodes {
                  id
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        optionId
                        field { ... on ProjectV2SingleSelectField { id } }
                      }
                    }
                  }
                  content {
                    ... on Issue {
                      number title body url state
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await _gh_graphql(query, {"projectId": self._project_id})
        items = (
            ((data.get("data") or {}).get("node") or {})
            .get("items", {}).get("nodes")
        ) or []

        result: list[GithubTask] = []
        for item in items:
            if not item or not item.get("content"):
                continue
            # Проверим что статус == ready_opt.
            in_ready = False
            for fv in (item.get("fieldValues", {}).get("nodes") or []):
                if not fv or "optionId" not in fv:
                    continue
                field = fv.get("field") or {}
                if (
                    field.get("id") == self._status_field_id
                    and fv.get("optionId") == ready_opt
                ):
                    in_ready = True
                    break
            if not in_ready:
                continue
            # Пропустим закрытые issues.
            content = item.get("content") or {}
            if content.get("state") and content["state"].upper() == "CLOSED":
                continue
            result.append(GithubTask.from_item(item))
            if len(result) >= limit:
                break
        return result

    async def pick_next_ready_task(self) -> GithubTask | None:
        tasks = await self.list_ready_tasks(limit=1)
        if not tasks:
            return None
        task = tasks[0]
        # Атомарно двинем в In Progress прежде чем вернуть.
        await self.move_task(task.id, self.in_progress_column)
        return task

    async def move_task(self, task_id: str, column: str) -> None:
        await self._ensure_project_meta()
        assert self._project_id and self._status_field_id
        option_id = self._option_id_for(column)
        project_item_id, _ = self._split_task_id(task_id)
        if not project_item_id:
            raise RuntimeError(
                f"github-projects: cannot move task {task_id!r} — task_id "
                "does not include project_item_id. Use the composite id "
                "returned by list_ready_tasks/pick_next_ready_task."
            )

        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId,
            itemId: $itemId,
            fieldId: $fieldId,
            value: { singleSelectOptionId: $optionId }
          }) {
            projectV2Item { id }
          }
        }
        """
        await _gh_graphql(
            mutation,
            {
                "projectId": self._project_id,
                "itemId": project_item_id,
                "fieldId": self._status_field_id,
                "optionId": option_id,
            },
        )


__all__ = ["GithubProjectsTracker", "GithubTask"]
