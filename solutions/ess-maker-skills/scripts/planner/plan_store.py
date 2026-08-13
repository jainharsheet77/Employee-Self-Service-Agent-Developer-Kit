# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
ESS Maker Kit — Planner: the Plan persistence seam (local file vs WeveNova MCP).

The planner reads and writes the Plan through a **store**. Two implementations:

  * :class:`LocalPlanStore` — the default. ``plan.json`` on disk plus the
    rendered ``ESS-scenario-plan.md`` beside it (the original behaviour).
  * :class:`McpPlanStore` — persists to a **WeveNova project plan** over the
    ``weve-plan`` MCP server instead of ``plan.json``: it reads the plan +
    project + tasks from WeveNova and reconciles task changes back to it. The
    human ``ESS-scenario-plan.md`` view is still rendered locally.

Both stores validate the Plan before persisting and always (re)render the
Markdown view, so the ``.md`` file is present regardless of backend.

WeveNova today exposes **task** CRUD plus a read of the plan (context, outputs,
status, acceptance criteria). Plan-level context/outputs are therefore read from
WeveNova and reflected in the ``.md``; task create/update/delete are persisted.
When a plan-level edit can't be pushed (no upstream plan-update operation), the
store says so rather than silently dropping it.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from planner import weve_mapping as wm
from planner.mcp_client import McpClient, McpError, client_from_config
from planner.plan_model import SUMMARY_FILENAME, Plan


class PlanStoreError(RuntimeError):
    """A store could not load or persist the plan."""


class PlanStore(Protocol):
    def load(self) -> Plan: ...
    def save(self, plan: Plan) -> list[str]: ...
    @property
    def summary_path(self) -> str: ...


def _summary_beside(plan_path: str) -> str:
    return os.path.join(os.path.dirname(plan_path) or ".", SUMMARY_FILENAME)


class LocalPlanStore:
    """The default file-backed store: ``plan.json`` + ``ESS-scenario-plan.md``."""

    def __init__(self, plan_path: str) -> None:
        self.plan_path = plan_path

    @property
    def summary_path(self) -> str:
        return _summary_beside(self.plan_path)

    def load(self) -> Plan:
        return Plan.load_or_new(self.plan_path)

    def save(self, plan: Plan) -> list[str]:
        """Validate then atomically write ``plan.json`` and re-render the ``.md``.
        Returns any non-fatal notices (none for the local store)."""
        plan.save_all(self.plan_path)
        return []


class McpPlanStore:
    """A WeveNova-backed store over the ``weve-plan`` MCP server — the **source
    of truth** for the plan of the project/agent being configured.

    WeveNova is authoritative: ``load`` always **fetches** the plan (context,
    outputs, status, acceptance criteria) and its tasks from WeveNova; ``save``
    reconciles task changes back to WeveNova and then renders the human
    ``ESS-scenario-plan.md`` **from the re-fetched WeveNova state**. A local
    ``plan.json`` is written only as an optional cache/mirror (``cache_path``) —
    never read as truth.
    """

    def __init__(self, client: McpClient, summary_path: str, cache_path: str | None = None) -> None:
        self.client = client
        self._summary_path = summary_path
        self.cache_path = cache_path
        self.warnings: list[str] = []

    @property
    def summary_path(self) -> str:
        return self._summary_path

    def _cache(self, plan: Plan) -> None:
        """Mirror the authoritative WeveNova plan to a local ``plan.json`` cache
        (best-effort; the cache is never the source of truth)."""
        if self.cache_path:
            try:
                plan.save(self.cache_path)
            except OSError:
                pass

    # -- read ------------------------------------------------------------- #

    def _list_tasks(self) -> list[dict[str, Any]]:
        raw = self.client.call_tool("list_project_plan_tasks", {})
        # OData collections come back as {"value": [...]}, but tolerate a bare list.
        if isinstance(raw, dict):
            items = raw.get("value", raw.get("Value", []))
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        return [wm.task_from_weve(t) for t in items if isinstance(t, dict)]

    def load(self) -> Plan:
        """Fetch the authoritative plan (+ tasks) from WeveNova and mirror it to
        the local cache. WeveNova is always the source read here — never the
        local cache."""
        self.warnings = []
        try:
            doc = self.client.call_tool("get_project_plan", {})
        except McpError as exc:
            raise PlanStoreError(f"cannot read the WeveNova project plan: {exc}") from exc
        if not isinstance(doc, dict):
            raise PlanStoreError(f"unexpected project-plan payload: {doc!r:.120}")
        try:
            tasks = self._list_tasks()
        except McpError as exc:
            # The plan itself read fine — degrade to a plan-level view with no
            # tasks and warn, rather than making MCP mode unusable while the
            # tasks collection is unavailable. ``save`` re-lists independently and
            # refuses to reconcile (never deletes) if it still can't read tasks.
            self.warnings.append(
                f"WeveNova tasks unavailable ({exc}); showing plan context/outputs only. "
                "Task changes will not persist until the tasks endpoint is reachable."
            )
            tasks = []
        plan = Plan(wm.plan_from_weve(doc, tasks=tasks))
        self._cache(plan)
        return plan

    # -- write ------------------------------------------------------------ #

    def _list_tasks_raw(self) -> dict[str, dict[str, Any]]:
        """Server tasks as ``{TaskId: raw WeveNova task}`` for change detection."""
        raw = self.client.call_tool("list_project_plan_tasks", {})
        if isinstance(raw, dict):
            items = raw.get("value", raw.get("Value", []))
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        out: dict[str, dict[str, Any]] = {}
        for t in items:
            if isinstance(t, dict):
                tid = t.get("TaskId") or t.get("Id")
                if tid:
                    out[tid] = t
        return out

    def save(self, plan: Plan) -> list[str]:
        """Reconcile the plan's tasks to WeveNova, then render the ``.md``.

        Diff by task id against the live server set: a local task not on the
        server is **created**; a local task whose writable fields differ from the
        server is **patched**; unchanged tasks are left alone (no no-op writes);
        a server task no longer in the plan is **deleted**. Plan-level
        context/outputs are read-only over the current MCP surface — a notice is
        returned when the local plan holds such data that this store cannot push.
        """
        notices: list[str] = []
        try:
            server = self._list_tasks_raw()
        except McpError as exc:
            raise PlanStoreError(f"cannot reconcile tasks (list failed): {exc}") from exc

        local_ids: set[str] = set()
        try:
            for task in plan.tasks:
                tid = task.get("id") or ""
                local_ids.add(tid)
                body = wm.task_to_weve(task, include_id=False)
                if tid in server:
                    # Only patch when the writable projection actually changed.
                    current = wm.task_to_weve(wm.task_from_weve(server[tid]), include_id=False)
                    if body != current:
                        self.client.call_tool(
                            "update_project_plan_task", {"taskId": tid, "patch": body}
                        )
                else:
                    self.client.call_tool("create_project_plan_task", {"task": body})
            for stale in set(server) - local_ids:
                self.client.call_tool("delete_project_plan_task", {"taskId": stale})
        except McpError as exc:
            raise PlanStoreError(f"cannot persist tasks to WeveNova: {exc}") from exc

        if plan.outputs:
            notices.append(
                f"{len(plan.outputs)} pinned output(s) are shown in the plan view but "
                "are owned by WeveNova upstream (no plan-level write over MCP yet)."
            )

        # WeveNova is the source of truth: render the human view (and refresh the
        # local cache) from the **re-fetched** authoritative plan — so the .md
        # reflects WeveNova (including any server-assigned task ids), not just the
        # in-memory copy. Fall back to the in-memory plan if the re-fetch fails.
        authoritative = plan
        try:
            authoritative = self.load()
            notices.extend(
                w for w in self.warnings if "unavailable" in w
            )
        except PlanStoreError:
            pass
        authoritative.write_summary(self._summary_path)
        self._cache(authoritative)
        return notices


def make_store(
    *,
    backend: str,
    plan_path: str,
    mcp_server: str = "weve-plan",
    mcp_config: str = os.path.join(".vscode", "mcp.json"),
    mcp_cache: bool = True,
) -> PlanStore:
    """Build the requested store. ``backend`` is ``"local"`` (default) or
    ``"mcp"``. For MCP, the endpoint comes from ``.vscode/mcp.json`` (the
    ``weve-plan`` server) or the ``PLANNER_MCP_URL`` env override; WeveNova is the
    source of truth and ``plan_path`` is written only as a local cache/mirror
    (disable with ``mcp_cache=False``)."""
    if backend == "local":
        return LocalPlanStore(plan_path)
    if backend == "mcp":
        try:
            client = client_from_config(mcp_server, mcp_config)
        except McpError as exc:
            raise PlanStoreError(str(exc)) from exc
        cache_path = plan_path if mcp_cache else None
        return McpPlanStore(client, _summary_beside(plan_path), cache_path=cache_path)
    raise PlanStoreError(f"unknown plan store backend: {backend!r}")
