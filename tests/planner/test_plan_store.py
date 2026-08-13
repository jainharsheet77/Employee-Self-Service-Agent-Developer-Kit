# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for planner.plan_store — the local vs WeveNova-MCP persistence seam.

The MCP-backed store is exercised against a small in-memory fake that emulates
the ``weve-plan`` tool surface (plan read + task CRUD), so no network is needed.
An opt-in ``@pytest.mark.live`` test hits the real server when ``--run-live`` is
passed and ``PLANNER_MCP_URL`` is set.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from planner import weve_mapping as wm
from planner.mcp_client import McpError
from planner.plan_model import Plan, new_task, principal_pool
from planner.plan_store import LocalPlanStore, McpPlanStore, make_store

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "weve_project_plan.json")


class FakeWeveClient:
    """Emulates the weve-plan MCP tools over an in-memory plan + task table."""

    def __init__(self, plan_doc: dict, tasks: list[dict] | None = None) -> None:
        self.plan_doc = plan_doc
        self.tasks: dict[str, dict] = {}
        for t in tasks or []:
            tid = t.get("TaskId") or str(uuid.uuid4())
            self.tasks[tid] = {**t, "TaskId": tid}
        self.calls: list[str] = []

    def call_tool(self, name: str, arguments=None):
        arguments = arguments or {}
        self.calls.append(name)
        if name == "get_project_plan":
            return self.plan_doc
        if name == "list_project_plan_tasks":
            return {"value": list(self.tasks.values())}
        if name == "get_project_plan_task":
            tid = arguments["taskId"]
            if tid not in self.tasks:
                raise McpError(f"task {tid} not found")
            return self.tasks[tid]
        if name == "create_project_plan_task":
            tid = str(uuid.uuid4())
            self.tasks[tid] = {**arguments["task"], "TaskId": tid}
            return self.tasks[tid]
        if name == "update_project_plan_task":
            tid = arguments["taskId"]
            self.tasks[tid] = {**self.tasks.get(tid, {}), **arguments["patch"], "TaskId": tid}
            return self.tasks[tid]
        if name == "delete_project_plan_task":
            self.tasks.pop(arguments["taskId"], None)
            return {"deleted": arguments["taskId"]}
        raise McpError(f"unknown tool {name}")


def _fixture_doc() -> dict:
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --- local store ------------------------------------------------------------- #

def test_local_store_round_trip(tmp_path):
    plan_path = str(tmp_path / "plan.json")
    store = LocalPlanStore(plan_path)
    plan = Plan.new(objective="ESS HR ticketing")
    store.save(plan)
    assert os.path.exists(plan_path)
    assert os.path.exists(store.summary_path)          # .md rendered
    assert store.load().output_value_or_context("objective") == "ESS HR ticketing"


def test_make_store_selects_local(tmp_path):
    store = make_store(backend="local", plan_path=str(tmp_path / "plan.json"))
    assert isinstance(store, LocalPlanStore)


# --- mcp store: read --------------------------------------------------------- #

def test_mcp_store_load_maps_plan_and_tasks(tmp_path):
    doc = _fixture_doc()
    server_task = wm.task_to_weve(
        new_task("srv-1", "Set up Workday SSO", assigned_to=principal_pool("App/Cloud App Admin"),
                 produces=["workdayEntraApp"]),
        include_id=True,
    )
    client = FakeWeveClient(doc, tasks=[server_task])
    store = McpPlanStore(client, str(tmp_path / "ESS-scenario-plan.md"))
    plan = store.load()
    assert plan.data["planId"] == doc["PlanId"]
    assert plan.output_value_or_context("scenario") == "HR-Ticketing"  # context read-through
    assert [t["title"] for t in plan.tasks] == ["Set up Workday SSO"]
    assert plan.tasks[0]["assignedTo"]["role"]["roleId"] == "App/Cloud App Admin"


# --- mcp store: task reconcile ----------------------------------------------- #

def test_mcp_store_save_creates_new_task(tmp_path):
    client = FakeWeveClient(_fixture_doc(), tasks=[])
    store = McpPlanStore(client, str(tmp_path / "plan.md"))
    plan = Plan(wm.plan_from_weve(client.plan_doc, tasks=[]))
    plan.add_task(new_task("T1", "Run setup", assigned_to=principal_pool("power-platform-admin"),
                           produces=["primaryEnvironment"]))
    notices = store.save(plan)
    assert "create_project_plan_task" in client.calls
    assert len(client.tasks) == 1
    assert next(iter(client.tasks.values()))["Title"] == "Run setup"
    assert os.path.exists(store.summary_path)          # .md still rendered
    assert any("owned by WeveNova" in n for n in notices)  # outputs read-only notice


def test_mcp_store_save_skips_unchanged_tasks(tmp_path):
    server_task = wm.task_to_weve(new_task("keep", "Same", assigned_to=principal_pool("maker"),
                                           produces=["x"]), include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[server_task])
    store = McpPlanStore(client, str(tmp_path / "plan.md"))
    plan = store.load()
    client.calls.clear()
    store.save(plan)                    # nothing changed
    assert "update_project_plan_task" not in client.calls   # no no-op write
    assert "create_project_plan_task" not in client.calls
    assert "delete_project_plan_task" not in client.calls


def test_mcp_store_save_updates_existing_task(tmp_path):
    server_task = wm.task_to_weve(new_task("keep", "Old title",
                                           assigned_to=principal_pool("maker")), include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[server_task])
    store = McpPlanStore(client, str(tmp_path / "plan.md"))
    plan = store.load()
    # the server task loaded with id "keep"; retitle it and save
    task = next(t for t in plan.tasks if t["id"] == "keep")
    task["title"] = "New title"
    store.save(plan)
    assert "update_project_plan_task" in client.calls
    assert client.tasks["keep"]["Title"] == "New title"


def test_mcp_store_save_deletes_removed_task(tmp_path):
    a = wm.task_to_weve(new_task("a", "A", assigned_to=principal_pool("maker")), include_id=True)
    b = wm.task_to_weve(new_task("b", "B", assigned_to=principal_pool("maker")), include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[a, b])
    store = McpPlanStore(client, str(tmp_path / "plan.md"))
    plan = store.load()
    plan.data["tasks"] = [t for t in plan.tasks if t["id"] == "a"]   # drop b
    store.save(plan)
    assert "delete_project_plan_task" in client.calls
    assert set(client.tasks) == {"a"}


def test_mcp_store_load_degrades_when_tasks_unavailable(tmp_path):
    # Plan read works but the tasks collection is down -> load the plan-level
    # view with no tasks + a warning, rather than failing outright.
    class TasksDown:
        def __init__(self, doc):
            self.doc = doc
        def call_tool(self, name, arguments=None):
            if name == "get_project_plan":
                return self.doc
            raise McpError("Upstream GET ... /tasks returned 404 Not Found")

    store = McpPlanStore(TasksDown(_fixture_doc()), str(tmp_path / "plan.md"))
    plan = store.load()
    assert plan.data["planId"]           # plan-level still read
    assert plan.tasks == []
    assert any("tasks unavailable" in w for w in store.warnings)


def test_mcp_store_load_raises_when_plan_unreadable(tmp_path):
    class PlanDown:
        def call_tool(self, name, arguments=None):
            raise McpError("Upstream GET ... returned 503")

    from planner.plan_store import PlanStoreError
    store = McpPlanStore(PlanDown(), str(tmp_path / "plan.md"))
    with pytest.raises(PlanStoreError):
        store.load()


# --- opt-in live smoke ------------------------------------------------------- #

@pytest.mark.live
def test_mcp_store_live_reads_plan(tmp_path):
    """Opt-in (``--run-live`` + ``PLANNER_MCP_URL``): the real weve-plan server
    returns a plan the store can map and render."""
    if not os.environ.get("PLANNER_MCP_URL"):
        pytest.skip("set PLANNER_MCP_URL (and PLANNER_MCP_HEADERS) to run the live MCP smoke")
    store = make_store(backend="mcp", plan_path=str(tmp_path / "plan.json"))
    plan = store.load()
    assert plan.data["planId"]                       # plan-level read from WeveNova
    assert plan.data["projectId"]
    # When the tasks collection is reachable the plan is fully valid; while it is
    # unavailable, load degrades (empty tasks) and outputs may reference unloaded
    # tasks — so assert the store surfaced that rather than requiring validity.
    if getattr(store, "warnings", []):
        assert any("tasks unavailable" in w for w in store.warnings)
    else:
        assert plan.validate() == []
    md = plan.render_summary()
    assert "## " in md
