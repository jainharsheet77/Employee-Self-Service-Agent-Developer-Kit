# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""ESS planner MCP server.

Exposes the WeveNova AgentConfiguration beta surface — projects, plans, tasks,
and plan role attestation — as MCP tools for the planner skill. Identity and
tenant come from the access token (never tool arguments); the shared client
core (auth, token decode, httpx session, retrying ``_request``) is inherited
from the landing-page ``AgentConfigClient`` via ``PlannerClient``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from planner_client import PlannerClient
from roles import ATTESTABLE_ROLES


_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_UPDATE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_DELETE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_CREATE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


mcp = FastMCP(
    "ess-planner",
    instructions=(
        "Drive WeveNova AgentConfiguration projects, plans, tasks, and plan "
        "role attestation. Identity and tenant come from the access token and "
        "are never tool arguments. For any PATCH/DELETE, read the exact entity "
        "first and pass its current ETag as ``etag`` (sent as If-Match). The "
        "server self-heals the two conflicts this surface produces: on a stale "
        "ETag it re-reads the entity and retries the mutation once with the "
        "fresh ETag, and when a task mutation is blocked because its parent "
        "plan is not Active it returns an actionable message telling you to "
        "activate the plan first. Projects and plans have no DELETE route — "
        "archive them instead; only tasks can be deleted."
    ),
)

_client: Optional[PlannerClient] = None


def get_client() -> PlannerClient:
    global _client
    if _client is None:
        _client = PlannerClient()
    return _client


def _format(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ----------------------------------------------------------------------
# AgentConfiguration project / plan / task tools (WeveNova beta surface).
# Identity and tenant come from the access token; they are never tool args.
# For any PATCH/DELETE, read the exact entity first and pass its current
# ETag as ``etag`` (sent as If-Match). The client re-reads and retries the
# mutation once automatically on a stale-ETag (412) conflict, and turns the
# "parent plan not Active" task conflict (409) into an actionable message.
# ----------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def list_agent_configuration_projects(
    query: Optional[dict[str, Any]] = None,
) -> str:
    """List the caller's AgentConfiguration projects (optional OData query)."""
    return _format(await get_client().list_agent_configuration_projects(query))


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def get_agent_configuration_project(
    projectId: str,
    query: Optional[dict[str, Any]] = None,
) -> str:
    """Get one project and its current ETag before archiving or updating it."""
    return _format(
        await get_client().get_agent_configuration_project(projectId, query)
    )


@mcp.tool(annotations=_CREATE_ANNOTATIONS)
async def create_agent_configuration_project(
    project: dict[str, Any],
    idempotencyKey: Optional[str] = None,
) -> str:
    """Get-or-create a project by name, e.g. {"name": "Employee Self Serve"}."""
    return _format(
        await get_client().create_agent_configuration_project(project, idempotencyKey)
    )


@mcp.tool(annotations=_DELETE_ANNOTATIONS)
async def archive_agent_configuration_project(projectId: str, etag: str) -> str:
    """Archive a project (projects have no DELETE route). Cascades to the active
    plan and cancels in-flight tasks. Pass the project's current ETag."""
    return _format(
        await get_client().archive_agent_configuration_project(projectId, etag)
    )


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def list_project_plans(
    projectId: str,
    query: Optional[dict[str, Any]] = None,
) -> str:
    """List the plans in a project."""
    return _format(await get_client().list_project_plans(projectId, query))


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def get_project_plan(
    projectId: str,
    planId: str,
    query: Optional[dict[str, Any]] = None,
) -> str:
    """Get a plan and its current ETag before archiving or updating it."""
    return _format(await get_client().get_project_plan(projectId, planId, query))


@mcp.tool(annotations=_CREATE_ANNOTATIONS)
async def create_project_plan(
    projectId: str,
    plan: dict[str, Any],
    idempotencyKey: Optional[str] = None,
) -> str:
    """Create a plan in a project (body: ownedById?, acceptanceCriteria?,
    context?, tasks?). New plans start in Draft."""
    return _format(
        await get_client().create_project_plan(projectId, plan, idempotencyKey)
    )


@mcp.tool(annotations=_UPDATE_ANNOTATIONS)
async def update_project_plan(
    projectId: str,
    planId: str,
    patch: dict[str, Any],
    etag: str,
) -> str:
    """Patch plan fields or dispatch a lifecycle change. Activate a Draft plan
    with patch {"status": "Active"}; also supports {"status": "Completed"},
    {"ownedById": ...}, {"acceptanceCriteria": [...]}, or {"context": [...]}.
    Only the plan owner may change status. Requires the plan's current ETag; a
    stale ETag is re-read and retried once automatically."""
    return _format(
        await get_client().update_project_plan(projectId, planId, patch, etag)
    )


@mcp.tool(annotations=_DELETE_ANNOTATIONS)
async def archive_project_plan(projectId: str, planId: str, etag: str) -> str:
    """Archive a plan (plans have no DELETE route). Cancels its in-flight tasks.
    Pass the plan's current ETag."""
    return _format(await get_client().archive_project_plan(projectId, planId, etag))


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def list_project_plan_tasks(
    projectId: str,
    planId: str,
    query: Optional[dict[str, Any]] = None,
) -> str:
    """List the tasks in a plan."""
    return _format(
        await get_client().list_project_plan_tasks(projectId, planId, query)
    )


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def list_project_plan_tasks_for_caller(
    projectId: str,
    planId: str,
    query: Optional[dict[str, Any]] = None,
) -> str:
    """List the plan's tasks assigned to the authenticated caller. The caller
    Entra id is taken from the access token, not an argument."""
    return _format(
        await get_client().list_project_plan_tasks_for_caller(
            projectId, planId, query
        )
    )


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def get_project_plan_task(
    projectId: str,
    planId: str,
    taskId: str,
    query: Optional[dict[str, Any]] = None,
) -> str:
    """Get a task and its current ETag before updating, completing, or
    deleting it."""
    return _format(
        await get_client().get_project_plan_task(projectId, planId, taskId, query)
    )


@mcp.tool(annotations=_CREATE_ANNOTATIONS)
async def create_project_plan_task(
    projectId: str,
    planId: str,
    task: dict[str, Any],
    idempotencyKey: Optional[str] = None,
) -> str:
    """Create a task (body: title required; description?, assignedToId?,
    assignedToType? User|Role, assignedToRoleId?, produces?, consumes?)."""
    return _format(
        await get_client().create_project_plan_task(
            projectId, planId, task, idempotencyKey
        )
    )


@mcp.tool(annotations=_CREATE_ANNOTATIONS)
async def create_role_assigned_project_plan_task(
    projectId: str,
    planId: str,
    role: str,
    title: str,
    description: Optional[str] = None,
    produces: Optional[list[str]] = None,
    consumes: Optional[list[str]] = None,
    idempotencyKey: Optional[str] = None,
) -> str:
    """Create a pooled task assigned to whoever holds an attestable role
    (WorkdayAdmin, ServiceNowAdmin, ServiceNowKnowledgeManager)."""
    return _format(
        await get_client().create_role_assigned_project_plan_task(
            projectId,
            planId,
            role,
            title,
            description,
            produces,
            consumes,
            idempotencyKey,
        )
    )


@mcp.tool(annotations=_UPDATE_ANNOTATIONS)
async def update_project_plan_task(
    projectId: str,
    planId: str,
    taskId: str,
    patch: dict[str, Any],
    etag: str,
) -> str:
    """Patch task content or claim a pooled task. Accepts only title,
    description, assignedToId, produces, consumes. To claim a role-pooled task,
    send patch {"assignedToId": "<user-aad-id>"}. Use set_project_plan_task_state
    or complete_project_plan_task for lifecycle changes. Requires the task's
    current ETag; a stale ETag is re-read and retried once, and a non-Active
    parent plan yields an actionable "activate the plan first" message."""
    return _format(
        await get_client().update_project_plan_task(
            projectId, planId, taskId, patch, etag
        )
    )


@mcp.tool(annotations=_UPDATE_ANNOTATIONS)
async def set_project_plan_task_state(
    projectId: str,
    planId: str,
    taskId: str,
    state: str,
    etag: str,
) -> str:
    """Transition a task's lifecycle state without outputs (NotStarted,
    InProgress, Completed, Cancelled). A task must be InProgress before it can
    be Completed, and the parent plan must be Active. Use
    complete_project_plan_task when completion must capture outputs. Requires
    the task's current ETag; a stale ETag is re-read and retried once, and a
    non-Active parent plan yields an actionable "activate the plan first"
    message."""
    return _format(
        await get_client().set_project_plan_task_state(
            projectId, planId, taskId, state, etag
        )
    )


@mcp.tool(annotations=_UPDATE_ANNOTATIONS)
async def complete_project_plan_task(
    projectId: str,
    planId: str,
    taskId: str,
    outputs: list[dict[str, Any]],
    etag: str,
) -> str:
    """Complete an InProgress task and persist its outputs into the parent plan
    ledger. Each output needs key, kind (Custom|Environment|Connection|
    KnowledgeSource), and attributes [{key, value, description?}]; Environment
    outputs require a non-empty environmentId attribute. Requires the task's
    current ETag; a stale ETag is re-read and retried once, and a non-Active
    parent plan yields an actionable "activate the plan first" message."""
    return _format(
        await get_client().complete_project_plan_task(
            projectId, planId, taskId, outputs, etag
        )
    )


@mcp.tool(annotations=_DELETE_ANNOTATIONS)
async def delete_project_plan_task(
    projectId: str,
    planId: str,
    taskId: str,
    etag: str,
) -> str:
    """Permanently delete one task (the only project-plan resource with a DELETE
    route). Requires the task's current ETag; a stale ETag is re-read and
    retried once, and a non-Active parent plan yields an actionable "activate
    the plan first" message."""
    return _format(
        await get_client().delete_project_plan_task(projectId, planId, taskId, etag)
    )


# ----------------------------------------------------------------------
# WeveNova role attestation tools. The tenant is taken from the access
# token; only the provider-owned attestable roles are valid and the
# attestation provider is always External.
# ----------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def list_attestable_roles() -> str:
    """List the provider-owned role identifiers accepted by plan attestation."""
    return _format(list(ATTESTABLE_ROLES))


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def list_plan_role_assignments(
    planId: str,
    subjectId: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    top: Optional[int] = None,
    orderby: Optional[str] = None,
    skiptoken: Optional[str] = None,
) -> str:
    """List active or revoked role assignments for a plan (plan-scoped;
    optionally filter by subjectId, role, or status Active|Revoked)."""
    return _format(
        await get_client().list_plan_role_assignments(
            planId, subjectId, role, status, top, orderby, skiptoken
        )
    )


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
async def get_role_assignment(assignmentId: str) -> str:
    """Get one role assignment by its opaque assignment ID (returns its ETag)."""
    return _format(await get_client().get_role_assignment(assignmentId))


@mcp.tool(annotations=_CREATE_ANNOTATIONS)
async def attest_plan_role(
    planId: str,
    subjectId: str,
    role: str,
    etag: Optional[str] = None,
    idempotencyKey: Optional[str] = None,
) -> str:
    """Attest that a subject holds an attestable role (WorkdayAdmin,
    ServiceNowAdmin, ServiceNowKnowledgeManager) for a plan; the provider is
    always External. Omit etag for a first attestation; pass an existing
    assignment's strong ETag to converge (never the plan's weak ETag)."""
    return _format(
        await get_client().attest_plan_role(
            planId, subjectId, role, etag=etag, idempotency_key=idempotencyKey
        )
    )


@mcp.tool(annotations=_DELETE_ANNOTATIONS)
async def revoke_role_assignment(
    assignmentId: str,
    etag: Optional[str] = None,
) -> str:
    """Revoke a plan role assignment by opaque assignment ID (soft-revoke to
    Status=Revoked)."""
    return _format(await get_client().revoke_role_assignment(assignmentId, etag))


if __name__ == "__main__":
    mcp.run()
