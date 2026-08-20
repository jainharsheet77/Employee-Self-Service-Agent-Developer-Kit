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

PID = "proj-test-1"
PLID = "plan-test-1"


class FakeWeveClient:
    """Emulates the weve-plan MCP tools over an in-memory plan + task table.

    Mirrors the 3.x multi-plan surface: every plan/task tool takes ``projectId``
    /``planId`` (ignored here — one plan in the fake), plus the role-assigned
    create and dedicated state-transition tools.
    """

    def __init__(self, plan_doc: dict, tasks: list[dict] | None = None) -> None:
        self.plan_doc = plan_doc
        self.tasks: dict[str, dict] = {}
        self._etag_seq = 0
        for t in tasks or []:
            tid = t.get("TaskId") or str(uuid.uuid4())
            self.tasks[tid] = {**t, "TaskId": tid, "ETag": self._bump_etag()}
        self.calls: list[str] = []
        self.call_log: list[tuple[str, dict]] = []

    def _bump_etag(self) -> str:
        self._etag_seq += 1
        return f'W/"{self._etag_seq}"'

    def call_tool(self, name: str, arguments=None):
        arguments = arguments or {}
        self.calls.append(name)
        self.call_log.append((name, arguments))
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
            self.tasks[tid] = {**arguments["task"], "TaskId": tid, "ETag": self._bump_etag()}
            return self.tasks[tid]
        if name == "create_role_assigned_project_plan_task":
            tid = str(uuid.uuid4())
            role = arguments["role"]
            self.tasks[tid] = {
                "TaskId": tid,
                "Title": arguments.get("title", ""),
                "Description": arguments.get("description", ""),
                "State": "NotStarted",
                "Produces": list(arguments.get("produces") or []),
                "Consumes": list(arguments.get("consumes") or []),
                "AssignedToType": "Role",
                "AssignedToId": role,
                "AssignedToRoleId": role,
                "ETag": self._bump_etag(),
            }
            return self.tasks[tid]
        if name == "update_project_plan_task":
            tid = arguments["taskId"]
            self._check_etag(tid, arguments)
            self.tasks[tid] = {
                **self.tasks.get(tid, {}), **arguments["patch"],
                "TaskId": tid, "ETag": self._bump_etag(),
            }
            return self.tasks[tid]
        if name == "set_project_plan_task_state":
            tid = arguments["taskId"]
            self._check_etag(tid, arguments)
            self.tasks[tid] = {
                **self.tasks.get(tid, {}), "State": arguments["state"],
                "TaskId": tid, "ETag": self._bump_etag(),
            }
            return self.tasks[tid]
        if name == "delete_project_plan_task":
            self._check_etag(arguments["taskId"], arguments)
            self.tasks.pop(arguments["taskId"], None)
            return {"deleted": arguments["taskId"]}
        raise McpError(f"unknown tool {name}")

    def _check_etag(self, tid: str, arguments: dict) -> None:
        """Model If-Match: a mutation must send the entity's current ETag."""
        current = self.tasks.get(tid, {}).get("ETag")
        if current and arguments.get("etag") != current:
            raise McpError(f"precondition failed: stale ETag for {tid}")


def _mcp_store(client, summary_path, **kw):
    """Construct an McpPlanStore bound to the fake's single (project, plan)."""
    return McpPlanStore(client, summary_path, project_id=PID, plan_id=PLID, **kw)


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
    store = _mcp_store(client, str(tmp_path / "ESS-scenario-plan.md"))
    plan = store.load()
    assert plan.data["planId"] == doc["PlanId"]
    assert plan.output_value_or_context("scenario") == "HR-Ticketing"  # context read-through
    assert [t["title"] for t in plan.tasks] == ["Set up Workday SSO"]
    assert plan.tasks[0]["assignedTo"]["role"]["roleId"] == "App/Cloud App Admin"


# --- mcp store: task reconcile ----------------------------------------------- #

def test_mcp_store_save_creates_new_task(tmp_path):
    client = FakeWeveClient(_fixture_doc(), tasks=[])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
    plan = Plan(wm.plan_from_weve(client.plan_doc, tasks=[]))
    plan.add_task(new_task("T1", "Run setup", assigned_to=principal_pool("Environment Maker"),
                           produces=["primaryEnvironment"]))
    notices = store.save(plan)
    # A pooled-role task is created through the dedicated role-assigned tool.
    assert "create_role_assigned_project_plan_task" in client.calls
    assert len(client.tasks) == 1
    created = next(iter(client.tasks.values()))
    assert created["Title"] == "Run setup"
    assert created["AssignedToRoleId"] == "Environment Maker"
    assert os.path.exists(store.summary_path)          # .md still rendered
    assert any("owned by WeveNova" in n for n in notices)  # outputs read-only notice


def test_mcp_store_save_creates_plain_user_task(tmp_path):
    from planner.plan_model import principal_person
    client = FakeWeveClient(_fixture_doc(), tasks=[])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
    plan = Plan(wm.plan_from_weve(client.plan_doc, tasks=[]))
    plan.add_task(new_task("T1", "Do it", assigned_to=principal_person("11111111-1111-1111-1111-111111111111")))
    store.save(plan)
    # A person-assigned (non-pooled) task uses the generic create.
    assert "create_project_plan_task" in client.calls
    assert "create_role_assigned_project_plan_task" not in client.calls


def test_mcp_store_save_skips_unchanged_tasks(tmp_path):
    server_task = wm.task_to_weve(new_task("keep", "Same", assigned_to=principal_pool("WorkdayAdmin"),
                                           produces=["x"]), include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[server_task])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
    plan = store.load()
    client.calls.clear()
    store.save(plan)                    # nothing changed
    assert "update_project_plan_task" not in client.calls   # no no-op write
    assert "create_project_plan_task" not in client.calls
    assert "create_role_assigned_project_plan_task" not in client.calls
    assert "delete_project_plan_task" not in client.calls


def test_mcp_store_load_writes_local_cache(tmp_path):
    cache = tmp_path / "plan.json"
    client = FakeWeveClient(_fixture_doc(), tasks=[])
    store = _mcp_store(client, str(tmp_path / "plan.md"), cache_path=str(cache))
    plan = store.load()
    assert cache.exists()                                    # WeveNova mirrored to local cache
    cached = json.loads(cache.read_text(encoding="utf-8"))
    assert cached["planId"] == plan.data["planId"]


def test_mcp_store_save_renders_md_from_weve_state(tmp_path):
    # After creating a task locally, the .md must reflect the WeveNova state —
    # i.e. the server-assigned TaskId, not the local placeholder id.
    md = tmp_path / "plan.md"
    client = FakeWeveClient(_fixture_doc(), tasks=[])
    store = _mcp_store(client, str(md), cache_path=str(tmp_path / "plan.json"))
    plan = Plan(wm.plan_from_weve(client.plan_doc, tasks=[]))
    plan.add_task(new_task("LOCAL-TMP", "Run setup", assigned_to=principal_pool("Environment Maker")))
    store.save(plan)
    server_id = next(iter(client.tasks))                     # uuid the fake assigned
    rendered = md.read_text(encoding="utf-8")
    assert server_id in rendered                             # md generated from WeveNova
    assert "LOCAL-TMP" not in rendered                       # not the local placeholder


def test_mcp_store_save_state_change_uses_state_tool(tmp_path):
    server_task = wm.task_to_weve(new_task("keep", "Same", assigned_to=principal_pool("WorkdayAdmin")),
                                  include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[server_task])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
    plan = store.load()
    task = next(t for t in plan.tasks if t["id"] == "keep")
    task["state"] = "Completed"
    client.calls.clear()
    store.save(plan)
    # A pure state transition is routed through the dedicated state tool, not a patch.
    assert "set_project_plan_task_state" in client.calls
    assert "update_project_plan_task" not in client.calls
    assert client.tasks["keep"]["State"] == "Completed"


def test_mcp_store_save_title_and_state_change_reads_fresh_etag(tmp_path):
    # Both a content field and the state change: PATCH first, then re-read the
    # fresh ETag before set-state, so the second call is not a stale If-Match.
    server_task = wm.task_to_weve(new_task("keep", "Old", assigned_to=principal_pool("WorkdayAdmin")),
                                  include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[server_task])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
    plan = store.load()
    task = next(t for t in plan.tasks if t["id"] == "keep")
    task["title"] = "New"
    task["state"] = "Completed"
    client.calls.clear()
    store.save(plan)                    # must not raise a stale-ETag McpError
    assert "update_project_plan_task" in client.calls
    assert "get_project_plan_task" in client.calls          # re-read for a fresh etag
    assert "set_project_plan_task_state" in client.calls
    assert client.tasks["keep"]["Title"] == "New"
    assert client.tasks["keep"]["State"] == "Completed"


def test_mcp_store_save_updates_existing_task(tmp_path):
    server_task = wm.task_to_weve(new_task("keep", "Old title",
                                           assigned_to=principal_pool("WorkdayAdmin")), include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[server_task])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
    plan = store.load()
    # the server task loaded with id "keep"; retitle it and save
    task = next(t for t in plan.tasks if t["id"] == "keep")
    task["title"] = "New title"
    store.save(plan)
    assert "update_project_plan_task" in client.calls
    assert client.tasks["keep"]["Title"] == "New title"
    # the PATCH carried the current ETag as If-Match
    patches = [a for (n, a) in client.call_log if n == "update_project_plan_task"]
    assert patches and patches[0].get("etag")


def test_mcp_store_save_deletes_removed_task(tmp_path):
    a = wm.task_to_weve(new_task("a", "A", assigned_to=principal_pool("WorkdayAdmin")), include_id=True)
    b = wm.task_to_weve(new_task("b", "B", assigned_to=principal_pool("WorkdayAdmin")), include_id=True)
    client = FakeWeveClient(_fixture_doc(), tasks=[a, b])
    store = _mcp_store(client, str(tmp_path / "plan.md"))
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

    store = _mcp_store(TasksDown(_fixture_doc()), str(tmp_path / "plan.md"))
    plan = store.load()
    assert plan.data["planId"]           # plan-level still read
    assert plan.tasks == []
    assert any("tasks unavailable" in w for w in store.warnings)


def test_mcp_store_load_raises_when_plan_unreadable(tmp_path):
    class PlanDown:
        def call_tool(self, name, arguments=None):
            raise McpError("Upstream GET ... returned 503")

    from planner.plan_store import PlanStoreError
    store = _mcp_store(PlanDown(), str(tmp_path / "plan.md"))
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
