# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
ESS Maker Kit — Planner: the role-attestation seam over the ``weve-plan`` MCP
server.

A *task* can be grounded on a **role** ("whoever holds the Workday
Administrator"); an **attestation** is the human-confirmed claim that binds a
named **person** to that role, scoped to a **plan**. This module is the thin
client the role skill drives to:

  * **attest** a person to a role on the plan (``attest_plan_role``),
  * **list** / **read** the plan's role assignments
    (``list_plan_role_assignments`` / ``get_role_assignment``),
  * **revoke** an assignment (``revoke_role_assignment``), and
  * list the tasks visible to a person once they log in — their directly
    assigned tasks **plus** the pooled tasks for the roles they hold
    (``list_project_plan_tasks_for_caller``).

Everything is validated **locally first** against the role registry
(:data:`planner.roles.DEFAULT_REGISTRY`) so a bad role/provider/oid is caught
with a friendly nudge before the server round-trip, mirroring WeveNova's own
``ValidateAttestationRequest`` rules:

  * ``subjectId`` — the *person's* Entra object id (an OID GUID). Required.
  * ``role`` — must be a registered **attestable** role (exact wire id).
  * ``provider`` — must be the role's owner (``External`` / ``Entra`` /
    ``PowerPlatform``); a right-role / wrong-provider pair is rejected.
  * attestations are **Plan-scoped** (the plan id is supplied here).

The attesting user's identity (tenant + who is attesting) comes from the request
context on the server, never the body — this client only says *who the role
belongs to* (``subjectId``) and *which role/plan*.

The attestation tools key off ``tenantId`` + ``planId``; the caller-task tool
keys off ``projectId`` + ``planId`` (see :mod:`planner.plan_store`
``resolve_plan_binding`` for how the binding is discovered).
"""

from __future__ import annotations

import re
from typing import Any

from planner.mcp_client import McpClient, McpError
from planner.roles import DEFAULT_REGISTRY, RoleDef, RoleRegistry

# An Entra object id is a canonical GUID.
_OID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class AttestationError(RuntimeError):
    """A role attestation was rejected by local validation or the server."""


def is_oid(value: str | None) -> bool:
    """True iff ``value`` is a canonical Entra object-id GUID."""
    return bool(value) and bool(_OID_RE.match(value.strip()))


def validate_attestation(
    subject_id: str,
    role: str,
    provider: str | None = None,
    *,
    registry: RoleRegistry = DEFAULT_REGISTRY,
) -> tuple[str, str]:
    """Validate an attestation request locally and return the canonical
    ``(role_id, provider)`` to send.

    Mirrors the server's ``ValidateAttestationRequest``:

      * ``subject_id`` must be an OID GUID,
      * ``role`` must resolve (free text is accepted via
        :meth:`RoleRegistry.find`) to a **registered attestable** role,
      * ``provider`` (when supplied) must **own** that role; when omitted it is
        derived from the registry.

    Raises :class:`AttestationError` with a field-targeted, re-promptable message.
    """
    if not is_oid(subject_id):
        raise AttestationError(
            "subjectId must be the person's Entra object id (a GUID), e.g. "
            "'11111111-2222-3333-4444-555555555555'."
        )
    resolved: RoleDef | None = registry.find(role)
    if resolved is None or not resolved.attestable:
        allowed = ", ".join(registry.allowed_attestable_names())
        raise AttestationError(f"role must be one of {allowed}.")
    owner = resolved.provider
    if provider is None:
        provider = owner
    elif provider != owner:
        raise AttestationError(
            f"role is not owned by the supplied provider. '{resolved.role}' is owned "
            f"by '{owner}', not '{provider}'."
        )
    return resolved.role, provider


class AttestationClient:
    """Drives the plan role-assignment + caller-task tools for one plan.

    ``tenant_id`` is needed by the attestation tools (attest/list/get/revoke);
    ``project_id`` + ``plan_id`` are needed by the caller-task tool. Build the
    binding with :func:`planner.plan_store.resolve_plan_binding`.
    """

    def __init__(
        self,
        client: McpClient,
        *,
        plan_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        registry: RoleRegistry = DEFAULT_REGISTRY,
    ) -> None:
        if not plan_id:
            raise AttestationError("plan_id is required for role attestations.")
        self.client = client
        self.plan_id = plan_id
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.registry = registry

    def _require_tenant(self) -> str:
        if not self.tenant_id:
            raise AttestationError(
                "tenant_id is required for role attestations but could not be "
                "resolved — set PLANNER_MCP_TENANT_ID or pass it explicitly."
            )
        return self.tenant_id

    def _require_project(self) -> str:
        if not self.project_id:
            raise AttestationError(
                "project_id is required to list a caller's tasks but could not be "
                "resolved — set PLANNER_MCP_PROJECT_ID or pass it explicitly."
            )
        return self.project_id

    # -- attest / read / revoke ------------------------------------------ #

    def attest(
        self,
        subject_id: str,
        role: str,
        *,
        provider: str | None = None,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> dict[str, Any]:
        """Attest ``subject_id`` (a person's OID) to ``role`` on this plan.

        The role is validated (and resolved from free text) against the registry
        and the provider is derived/checked before the call. Idempotent — the
        server returns the existing assignment when the deterministic one already
        exists."""
        role_id, prov = validate_attestation(subject_id, role, provider, registry=self.registry)
        args: dict[str, Any] = {
            "tenantId": self._require_tenant(),
            "planId": self.plan_id,
            "subjectId": subject_id.strip(),
            "role": role_id,
            "provider": prov,
        }
        if etag:
            args["etag"] = etag
        if idempotency_key:
            args["idempotencyKey"] = idempotency_key
        try:
            result = self.client.call_tool("attest_plan_role", args)
        except McpError as exc:
            raise AttestationError(f"attest failed: {exc}") from exc
        return result if isinstance(result, dict) else {"result": result}

    def list_assignments(
        self,
        *,
        subject_id: str | None = None,
        role: str | None = None,
        status: str | None = None,
        top: int | None = None,
        orderby: str | None = None,
        skiptoken: str | None = None,
    ) -> list[dict[str, Any]]:
        """List this plan's role assignments (optionally filtered)."""
        args: dict[str, Any] = {"tenantId": self._require_tenant(), "planId": self.plan_id}
        if subject_id:
            args["subjectId"] = subject_id
        if role:
            resolved = self.registry.find(role)
            args["role"] = resolved.role if resolved else role
        if status:
            args["status"] = status
        if top is not None:
            args["top"] = top
        if orderby:
            args["orderby"] = orderby
        if skiptoken:
            args["skiptoken"] = skiptoken
        try:
            payload = self.client.call_tool("list_plan_role_assignments", args)
        except McpError as exc:
            raise AttestationError(f"list role assignments failed: {exc}") from exc
        return _odata_items(payload)

    def get_assignment(self, assignment_id: str) -> dict[str, Any]:
        try:
            result = self.client.call_tool(
                "get_role_assignment",
                {"tenantId": self._require_tenant(), "assignmentId": assignment_id},
            )
        except McpError as exc:
            raise AttestationError(f"get role assignment failed: {exc}") from exc
        return result if isinstance(result, dict) else {"result": result}

    def revoke(self, assignment_id: str, *, etag: str | None = None) -> dict[str, Any]:
        """Revoke (soft-revoke) a role assignment on this plan."""
        args: dict[str, Any] = {"tenantId": self._require_tenant(), "assignmentId": assignment_id}
        if etag:
            args["etag"] = etag
        try:
            result = self.client.call_tool("revoke_role_assignment", args)
        except McpError as exc:
            raise AttestationError(f"revoke role assignment failed: {exc}") from exc
        return result if isinstance(result, dict) else {"result": result}

    # -- caller tasks (Flow 2) ------------------------------------------- #

    def tasks_for_caller(self, caller_id: str, *, query: str | None = None) -> list[dict[str, Any]]:
        """The tasks a logged-in person sees: their directly-assigned tasks
        **plus** the pooled tasks for every role they are attested to on this
        plan (``list_project_plan_tasks_for_caller``)."""
        args: dict[str, Any] = {
            "projectId": self._require_project(),
            "planId": self.plan_id,
            "callerId": caller_id,
        }
        if query:
            args["query"] = query
        try:
            payload = self.client.call_tool("list_project_plan_tasks_for_caller", args)
        except McpError as exc:
            raise AttestationError(f"list caller tasks failed: {exc}") from exc
        return _odata_items(payload)


def _odata_items(payload: Any) -> list[dict[str, Any]]:
    """Normalise an OData ``{value:[...]}`` collection (or a bare list) to a list
    of dict rows."""
    if isinstance(payload, dict):
        items = payload.get("value", payload.get("Value", []))
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [i for i in items if isinstance(i, dict)]
