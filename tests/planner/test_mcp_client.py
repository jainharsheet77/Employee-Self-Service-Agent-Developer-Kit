# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for planner.mcp_client — the ``--ping`` diagnostic.

The 3.x ``get_project_plan`` tool requires ``{"projectId","planId"}``; ``--ping``
used to call it with an empty ``{}`` (always rejected). ``_ping_project_plan``
must first resolve the project/plan binding and then call the tool with those
ids. Exercised against a small in-memory fake — no network.
"""

from __future__ import annotations

import pytest

from planner import mcp_client
from planner.mcp_client import McpError


class _FakePingClient:
    def __init__(self, plan_doc: dict, projects: list[dict], plans: list[dict]) -> None:
        self.plan_doc = plan_doc
        self.projects = projects
        self.plans = plans
        self.calls: list[tuple[str, dict]] = []
        self.get_args: dict | None = None

    def call_tool(self, name: str, arguments: dict | None = None):
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "list_agent_configuration_projects":
            return {"value": self.projects}
        if name == "get_agent_configuration_project":
            return self.projects[0]
        if name == "list_project_plans":
            return {"value": self.plans}
        if name == "get_project_plan":
            self.get_args = arguments
            return self.plan_doc
        raise McpError(f"unexpected tool {name}")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in ("PLANNER_MCP_PROJECT_ID", "PLANNER_MCP_PLAN_ID", "PLANNER_MCP_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)


def test_ping_project_plan_resolves_ids_before_get():
    client = _FakePingClient(
        plan_doc={"PlanId": "pl-1", "ProjectId": "pr-1", "Context": [], "AcceptanceCriteria": []},
        projects=[{"ProjectId": "pr-1", "Name": "P", "ActivePlanId": "pl-1", "TenantId": "t"}],
        plans=[{"PlanId": "pl-1"}],
    )
    msg = mcp_client._ping_project_plan(client)
    # The tool was called WITH the resolved ids — not the old empty {}.
    assert client.get_args == {"projectId": "pr-1", "planId": "pl-1"}
    assert "get_project_plan OK" in msg
    assert "pr-1" in msg and "pl-1" in msg


def test_ping_project_plan_uses_env_binding(monkeypatch):
    # An explicit env binding is honored without any discovery calls.
    monkeypatch.setenv("PLANNER_MCP_PROJECT_ID", "pr-env")
    monkeypatch.setenv("PLANNER_MCP_PLAN_ID", "pl-env")
    client = _FakePingClient(
        plan_doc={"PlanId": "pl-env", "ProjectId": "pr-env", "Context": []},
        projects=[{"ProjectId": "pr-env", "Name": "P"}],
        plans=[],
    )
    mcp_client._ping_project_plan(client)
    assert client.get_args == {"projectId": "pr-env", "planId": "pl-env"}
    # With both ids pinned, no plan discovery is needed.
    assert not any(n == "list_project_plans" for n, _ in client.calls)
