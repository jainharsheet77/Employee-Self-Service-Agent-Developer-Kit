# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for planner.roles_cli — the decoupled /roles CLI surface (pure logic /
in-memory fake, no network).

Verifies the extracted commands are wired on the roles CLI (not the planner CLI),
that the offline `roles` listing renders the verbatim ids, and that `attest`
threads the person/role through to the attestation client.
"""

from __future__ import annotations

import pytest

from planner import roles_cli
from planner.attest import AttestationClient
from planner.mcp_client import McpError

PLAN = "17aeb22e-02bb-4729-8097-4adeae4313a1"
PROJECT = "003ab3c7-544f-435d-89c2-7970b7c2e6bf"
TENANT = "af8c5344-6ea5-443d-8d17-11df9512ae7c"
SUBJECT = "11111111-2222-3333-4444-555555555555"


class _FakeToolClient:
    """Minimal MCP tool emulation for the attest path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name == "attest_plan_role":
            return {
                "AssignmentId": "assign-1",
                "Role": (arguments or {})["role"],
                "SubjectId": (arguments or {})["subjectId"],
                "Status": "Active",
            }
        raise McpError(f"unexpected tool {name}")


def test_parser_wires_role_commands():
    parser = roles_cli.build_parser()
    for cmd, func in [
        ("roles", roles_cli.cmd_roles),
        ("attest", roles_cli.cmd_attest),
        ("assignments", roles_cli.cmd_assignments),
        ("revoke", roles_cli.cmd_revoke),
        ("caller-tasks", roles_cli.cmd_caller_tasks),
    ]:
        # every subcommand parses and binds its handler
        if cmd == "attest":
            args = parser.parse_args([cmd, "--person", SUBJECT, "--role", "WorkdayAdmin"])
        elif cmd == "revoke":
            args = parser.parse_args([cmd, "--assignment", "assign-1"])
        elif cmd == "caller-tasks":
            args = parser.parse_args([cmd, "--caller", SUBJECT])
        else:
            args = parser.parse_args([cmd])
        assert args.func is func


def test_roles_listing_offline(capsys):
    rc = roles_cli.main(["roles"])
    out = capsys.readouterr().out
    assert rc == 0
    # verbatim ids, with the External display rendered alongside the compact id
    assert "WorkdayAdmin  (Workday Administrator)" in out
    assert "Power Platform Administrator" in out
    assert "[attestable]" in out


def test_attest_threads_person_and_role(monkeypatch, capsys):
    fake = _FakeToolClient()
    client = AttestationClient(fake, plan_id=PLAN, tenant_id=TENANT, project_id=PROJECT)
    monkeypatch.setattr(roles_cli, "_attest_client", lambda args: client)

    rc = roles_cli.main(["attest", "--person", SUBJECT, "--role", "Workday Administrator"])
    out = capsys.readouterr().out
    assert rc == 0
    name, args = fake.calls[-1]
    assert name == "attest_plan_role"
    # display name resolved to the compact wire id; provider derived
    assert args["role"] == "WorkdayAdmin"
    assert args["provider"] == "External"
    assert args["subjectId"] == SUBJECT
    assert "Attested" in out


def test_attest_rejects_non_attestable_role(monkeypatch, capsys):
    fake = _FakeToolClient()
    client = AttestationClient(fake, plan_id=PLAN, tenant_id=TENANT, project_id=PROJECT)
    monkeypatch.setattr(roles_cli, "_attest_client", lambda args: client)

    rc = roles_cli.main(["attest", "--person", SUBJECT, "--role", "AgentOwner"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "must be one of" in err
    # no server call was made on a local-validation failure
    assert fake.calls == []
