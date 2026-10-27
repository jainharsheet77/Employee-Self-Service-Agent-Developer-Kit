# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Skill contracts for the maker-facing Planner orchestration."""

import json
import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "solutions" / "ess-maker-skills" / "src" / "skills"


def _text(name):
    return (SKILLS / name).read_text(encoding="utf-8")


def test_plan_orientation_precedes_questions_and_permissions():
    text = _text("planner/SKILL.md")
    assert text.index("## Orient the maker") < text.index("## Gate")
    for phrase in ("A plan is your ESS rollout checklist", "not just", "resume", "share and activate", "pending"):
        assert phrase in text
    assert "tool approval" in _text("planner/sync.md").lower() or "Host trust" in text


def test_scenario_and_system_choices_explain_consequences_and_deferral():
    text = _text("planner/interview.md")
    forms = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.S)]
    assert len(forms) == 2
    for form in forms:
        question = form[0]
        assert question["multiSelect"] is True
        assert question["allowFreeformInput"] is True
        assert any(option["label"] == "Not sure yet" for option in question["options"])
        assert all(len(option["description"]) > 50 for option in question["options"])
    assert "add-system --area <scenario-id> --pending" in text
    assert "do not substitute SharePoint" in text
    assert "already-clear choices instead of reconfirming them" in text


def test_experience_is_inferred_after_scenarios_not_selected_up_front():
    expected = {
        "planner/SKILL.md": "Infer the experience from the scenarios",
        "planner/interview.md": "## Infer the experience from the scenarios",
        "planner/sync.md": "Reuse the scenario-inferred target",
    }
    for path, phrase in expected.items():
        assert phrase in " ".join(_text(path).split())
    prompt = (SKILLS.parents[1] / ".github" / "prompts" / "planner.prompt.md").read_text(encoding="utf-8")
    assert "Never open with an ESS experience picker" in " ".join(prompt.split())
    interview = _text("planner/interview.md")
    assert "Pick a focus" not in interview
    assert "ask only if the target agent" not in interview
    assert "sponsor still picks" not in _text("planner/sync.md")


def test_scenario_domains_ground_hr_and_it_inference():
    interview = _text("planner/interview.md")
    for name, examples in (
        ("EmployeeSelfServiceHRDA", ("profile", "HR cases", "manager")),
        ("EmployeeSelfServiceITDA", ("IT knowledge", "troubleshooting", "IT tickets")),
    ):
        row = next(line for line in interview.splitlines() if line.startswith("|") and name in line)
        assert all(example in row for example in examples)
    prose = " ".join(interview.split())
    assert "scenario_catalogue.md" in prose
    assert "Do not infer from a connector alone" in prose
    assert "Unknown systems do not delay" in prose


def test_experience_confirmation_is_part_of_draft_review():
    prose = " ".join(_text("planner/interview.md").split())
    assert "This looks like an ESS HR rollout" in prose
    assert "This looks like an ESS IT rollout" in prose
    assert "not a separate approval turn" in prose
    assert "Preserve an explicit or saved target" in prose
    assert "Do not silently retarget a shared plan" in prose


def test_unclear_or_mixed_experience_keeps_planning_open():
    prose = " ".join(_text("planner/interview.md").split())
    assert "mixed HR/IT" in prose
    assert "Facilities" in prose
    assert "--key experience" in prose
    assert "--group pendingDecision" in prose
    assert "remove-context --key experience" in prose
    assert "Keep all requested scenarios" in prose
    assert "local draft without a target" in prose


def test_planner_evaluations_generate_in_flow_with_existing_safety_gates():
    text = _text("planner/evaluate.md")
    assert "--group evaluationCase" in text
    for path in ("topics/create-eval-driven/SKILL.md", "evaluations/create/SKILL.md"):
        assert f"src/skills/{path}" in text
        assert (SKILLS / path).is_file()
    assert "push approval" in " ".join(text.split())
    assert "not a test run" in _text("planner/SKILL.md")
    assert "Generate only missing coverage" in text
    handoff = _text("evaluations/create/SKILL.md")
    assert "Planner handoff: reuse the supplied scope" in handoff
    assert "Do not ask the maker to invoke `/evaluate` separately" in handoff


def test_shared_completion_requires_state_output_readback_and_explicit_activation():
    text = _text("planner/capture.md")
    assert "state **and the expected output" in text
    assert "Never publish or activate" in text
    assert "no partial-output write endpoint" in text
    assert "set_project_plan_task_state" in text
    assert "preserve the original ETag" in text


def test_next_steps_explain_share_and_activate_with_separate_prompts():
    text = _text("planner/interview.md").split("## After the plan is shown", 1)[1]
    readout = text.split("Then **end the turn.**", 1)[0]
    steps = re.findall(r"^> (\d+)\. (.*?)(?=^> \d+\.|\Z)", readout, re.M | re.S)
    assert [number for number, _ in steps] == ["1", "2", "3", "4"]
    share = " ".join(steps[1][1].replace(">", "").split())
    activate = " ".join(steps[2][1].replace(">", "").split())
    for phrase in ("shared draft", "colleagues with access can review", "read-only until activation"):
        assert phrase in share
    assert '**Prompt:** "Share this plan."' in share
    assert "Activate this plan." not in share
    for phrase in ("active rollout plan", "start ready tasks", "record progress"):
        assert phrase in activate
    assert "does not run tasks or deploy ESS automatically" in activate
    assert '**Prompt:** "Activate this plan."' in activate
    assert "Share this plan." not in activate
    assert "displaying an example is not permission to perform it" in " ".join(readout.split())
    main = " ".join(_text("planner/SKILL.md").split())
    assert "Share and Activate are separate steps" in main
    assert '"Share this plan."' in main
    assert '"Activate this plan."' in main


def test_shared_draft_follow_up_explains_activation_and_waits_for_request():
    text = _text("planner/sync.md").split("5. **Confirm it's saved", 1)[1]
    follow_up = " ".join(text.split("6. **Activate when", 1)[0].split())
    assert "active rollout plan" in follow_up
    assert "does not run tasks or deploy ESS automatically" in follow_up
    assert '**Prompt:** "Activate this plan."' in follow_up
    assert "only when the sponsor asks to activate" in follow_up


@pytest.mark.parametrize(
    ("state", "prompt"),
    [
        ("Local draft", "Share this plan."),
        ("Shared draft", "Activate this plan."),
        ("Active", "Assign people to the roles."),
    ],
)
def test_maker_status_next_step_matches_plan_lifecycle(state, prompt):
    rows = _text("planner/status.md").splitlines()
    row = next((line for line in rows if line.startswith(f"| Maker / sponsor | {state} |")), "")
    assert row, f"Missing maker next-step policy for {state}"
    assert prompt in row
    assert "/setup" not in row
    if state == "Shared draft":
        assert "plan owner" in row
        assert "does not run tasks or deploy ESS automatically" in row


def test_status_separates_team_readiness_from_caller_execution():
    text = " ".join(_text("planner/status.md").split())
    for phrase in (
        "**Team readiness**",
        "**Your next step**",
        "not the first ready task in the overall plan",
        "list_project_plan_tasks_for_caller",
        "InProgress",
        "NotStarted",
        "`blockedBy`",
        "shared draft remains read-only",
        "never substitute another person's ready work",
    ):
        assert phrase in text
    row = next(
        line
        for line in _text("planner/status.md").splitlines()
        if line.startswith("| Assignee | Active, own in-progress or ready task |")
    )
    assert "/setup" in row
    assert "Start my environment setup task." in row
    assert "Continue setup." in row


def test_status_reuses_context_without_forcing_sharing_or_stealing_work():
    text = " ".join(_text("planner/status.md").split())
    for phrase in (
        "Reuse the known maker/sponsor context",
        "Do not ask for a role that is already known",
        "local-only drafting",
        "do not keep prompting them to share",
        "no shared plan ID",
        "403/access denied",
        "do not guess who the requester is",
        "Never share, activate, assign, claim, or run a skill",
        "no unfinished work remains",
        "Completed or Archived",
    ):
        assert phrase in text


@pytest.mark.parametrize("entry", ["skill", "prompt"])
def test_status_entrypoints_require_the_requesting_persons_next_step(entry):
    if entry == "skill":
        text = _text("planner/SKILL.md")
    else:
        path = SKILLS.parents[1] / ".github" / "prompts" / "planner.prompt.md"
        text = path.read_text(encoding="utf-8")
    prose = " ".join(text.split())
    assert "**Your next step**" in prose
    assert "requesting person" in prose
    assert "plan lifecycle" in prose
    assert "status.md" in prose


def test_foundation_task_handoff_uses_setup_for_environment_and_installation():
    text = _text("planner/mytasks.md")
    for phrase in ("primaryEnvironment", "provisionedEnvironment", "essAgent"):
        assert phrase in text
    assert "src/skills/foundation-setup/SKILL.md" in text
    assert "Continue `/setup`" in text
    prose = " ".join(text.split())
    assert "`kitSetup` because this task needs the pinned environment" in prose
    assert "do not invent an environment dependency" in prose
    assert "description or the foundation outputs" in prose
    manual = text.split("2. **The task is a portal / manual step**", 1)[1]
    assert "provision the Power Platform environment" not in manual


def test_progress_refreshes_next_action_from_current_state_for_the_same_person():
    text = _text("planner/status.md")
    assert "## Refresh after progress" in text
    refresh = " ".join(text.split("## Refresh after progress", 1)[1].split("## Step 2", 1)[0].split())
    for phrase in (
        "same requester",
        "Do not reuse the previous recommendation",
        "list_project_plan_tasks_for_caller",
        "pendingSync",
        "partial",
        "no open work",
        "do not automatically start",
    ):
        assert phrase in refresh
    for path in ("planner/SKILL.md", "planner/capture.md", "planner/sync.md", "planner/assign.md"):
        assert "Refresh after progress" in _text(path)


def test_native_planner_preserves_explicit_sharing_and_activation():
    path = SKILLS.parents[1] / ".github" / "skills" / "planner" / "SKILL.md"
    text = " ".join(path.read_text(encoding="utf-8").split())
    assert "local draft" in text
    assert "explicitly asks to share" in text
    assert "explicit activation" in text
    assert "declined shared access" in text
    assert "once built, it is published" not in text


def test_intent_capture_precedes_scoped_research():
    main = _text("planner/SKILL.md")
    assert main.index("| 1. Interview |") < main.index("| 2. Research |") < main.index("| 3. Model |")
    research = " ".join(_text("planner/research.md").split())
    assert "Capture intent before research" in research
    assert "unknown systems" in research
    interview = " ".join(_text("planner/interview.md").split())
    assert "one batched `vscode_askQuestions` call" in interview
    assert "Do not ask each area in a separate turn" in interview
    assert "Ask once per captured area" not in interview


def test_research_reuses_fetched_content_and_exposes_freshness():
    text = " ".join(_text("planner/research.md").split())
    for phrase in ("research-context.json", "24 hours", "--refresh", "Do not fetch selected pages again"):
        assert phrase in text
    assert "no persisted" not in text
    assert "Read the selected pages** with your fetch tool" not in text


def test_servicenow_grounding_uses_its_existing_connect_checklist():
    text = _text("planner/model.md")
    assert "setup/servicenow/tasks.md" not in text
    assert "src/skills/connect/servicenow/steps.md" in text
    assert (SKILLS / "connect" / "servicenow" / "steps.md").is_file()
    row = next(line for line in text.splitlines() if line.startswith("| Connect ServiceNow |"))
    assert "Learn" in row
    assert "role:` items" not in row


def test_workspace_collapses_display_without_changing_models():
    path = SKILLS.parents[1] / ".vscode" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["chat.agent.thinkingStyle"] == "collapsed"
    assert settings["chat.agent.thinking.collapsedTools"] == "always"
    assert settings["chat.experimental.persistentProgress"] == "off"
    assert not any("model" in key.lower() or "reasoningEffort" in key for key in settings)


def test_native_planner_discovers_existing_plan_lifecycle_requests():
    path = SKILLS.parents[1] / ".github" / "skills" / "planner" / "SKILL.md"
    metadata = " ".join(path.read_text(encoding="utf-8").split("---", 2)[1].split())
    for phrase in (
        "VS Code", "share and activate the plan", "resume", "status", "before setup",
        "import", "edit", "assign owners", "record task completion",
    ):
        assert phrase in metadata


def test_existing_draft_and_visible_tools_do_not_skip_workspace_bootstrap():
    text = _text("planner/SKILL.md")
    gate, resume = text.split("## Gate", 1)[1].split("## First", 1)
    assert "After completing the Gate above" in resume
    prose = " ".join(gate.split())
    assert gate.index("materialize-defaults") < gate.index("validate --server ess-planner")
    for phrase in (
        "first Planner use in each chat",
        "existing local draft",
        "tools are already visible",
        ".vscode/mcp.json",
        "configured",
        "earlier session",
    ):
        assert phrase in prose


def test_planner_bootstrap_is_reused_across_flows_but_not_across_chats():
    gate = _text("planner/SKILL.md").split("## Gate", 1)[1].split("## First", 1)[0]
    prose = " ".join(gate.split())
    for phrase in (
        "same chat and workspace",
        "later turns or phases",
        "workspace or MCP configuration changes",
        "no bootstrap attempt is recorded",
        "failed bootstrap",
        "validate --server workiq-preview",
        "does not grant shared access",
    ):
        assert phrase in prose
    assert "current interaction" not in gate
    assert "once per Planner entry" not in gate


@pytest.mark.parametrize(
    "phase",
    sorted(path.name for path in (SKILLS / "planner").glob("*.md") if path.name != "SKILL.md"),
)
def test_every_planner_phase_enters_through_the_shared_chat_bootstrap(phase):
    entry = " ".join(_text(f"planner/{phase}").splitlines()[:18])
    assert "src/skills/planner/SKILL.md" in entry
    assert "Gate" in entry
    assert "workspace bootstrap" in entry
    assert "first Planner use" in entry
    assert "same chat and workspace" in entry


@pytest.mark.parametrize("entry", ["native", "prompt", "sync"])
def test_lifecycle_entrypoints_bootstrap_before_shared_calls(entry):
    if entry == "native":
        path = SKILLS.parents[1] / ".github" / "skills" / "planner" / "SKILL.md"
    elif entry == "prompt":
        path = SKILLS.parents[1] / ".github" / "prompts" / "planner.prompt.md"
    else:
        path = SKILLS / "planner" / "sync.md"
    prose = " ".join(path.read_text(encoding="utf-8").split())
    assert "workspace bootstrap" in prose
    assert "before any shared tool call" in prose
