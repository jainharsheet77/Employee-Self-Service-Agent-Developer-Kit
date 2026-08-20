# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
ESS Maker Kit — Roles CLI.

The deterministic surface the ``/roles`` skill calls to manage **people and
roles** on a plan, decoupled from the planner (which only *grounds* a role onto
a task). Everything here is WeveNova-backed (the ``weve-plan`` MCP server):

    python scripts/planner/roles_cli.py current-user
    python scripts/planner/roles_cli.py roles [--live]
    python scripts/planner/roles_cli.py attest --person <oid> --role WorkdayAdmin
    python scripts/planner/roles_cli.py assignments [--person <oid>] [--role <id>]
    python scripts/planner/roles_cli.py revoke --assignment <id>
    python scripts/planner/roles_cli.py caller-tasks --caller <oid>

A *task* is grounded on a **role** by the planner; an **attestation** binds a
named **person** (their Entra object id) to that role, scoped to the plan, so the
platform can later show that person the role's tasks (``caller-tasks``, Flow 2).

The current caller's OID is resolved via ``current-user`` from the same
``weve-plan`` MCP the plan lives on. ``attest`` accepts the resolved OID.

Role strings are validated locally against the registry
(:data:`planner.roles.DEFAULT_REGISTRY`) and again by WeveNova (ordinal,
case-sensitive), so emit the exact wire id (run ``roles`` to see them).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure scripts/ is on the path so ``import planner...`` resolves when this
# file is run directly (mirrors scripts/planner/cli.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _attest_client(args: argparse.Namespace):
    """Build the role-attestation client (WeveNova only). Resolves the
    project/plan/tenant binding from args, env, or discovery."""
    from planner.attest import AttestationClient
    from planner.mcp_client import McpError, client_from_config
    from planner.plan_store import PlanStoreError, resolve_plan_binding

    try:
        client = client_from_config(
            getattr(args, "mcp_server", "weve-plan"),
            getattr(args, "mcp_config", os.path.join(".vscode", "mcp.json")),
        )
        pid, plid, tid = resolve_plan_binding(
            client,
            project_id=getattr(args, "project_id", None),
            plan_id=getattr(args, "plan_id", None),
        )
    except (McpError, PlanStoreError) as exc:
        raise SystemExit(f"cannot reach the WeveNova plan: {exc}")
    return AttestationClient(client, plan_id=plid, tenant_id=tid, project_id=pid)


def _weve_client(args: argparse.Namespace):
    """Build a bare WeveNova MCP client for current-user lookup."""
    from planner.mcp_client import McpError, client_from_config

    try:
        return client_from_config(
            getattr(args, "mcp_server", "weve-plan"),
            getattr(args, "mcp_config", os.path.join(".vscode", "mcp.json")),
        )
    except McpError as exc:
        raise SystemExit(f"cannot reach the WeveNova MCP server: {exc}")


def _user_aad_id(user: dict) -> str:
    """The person's Entra object id (``aadId``) from a directory record."""
    return user.get("aadId") or user.get("AadId") or user.get("id") or user.get("Id") or ""


def _user_display_name(user: dict) -> str:
    return user.get("displayName") or user.get("DisplayName") or user.get("name") or ""


def _resolve_caller_id(args: argparse.Namespace) -> str | None:
    """The caller's own Entra object id for the caller-scoped task query.

    Precedence: explicit ``--caller`` → ``PLANNER_MCP_CALLER_ID`` env → the
    ``get_current_user_context`` MCP tool (the default). This has to be the
    **authenticated** caller — the identity the ``weve-plan`` tunnel token signs
    in as — because WeveNova expands role-pooled tasks for the caller's *own* OID
    only (self-only). ``get_current_user_context`` returns exactly that
    authenticated caller, so its OID surfaces only that caller's own pooled tasks;
    a different, hand-supplied OID would not."""
    caller = getattr(args, "caller", None)
    if caller and caller.strip():
        return caller.strip()
    env = os.environ.get("PLANNER_MCP_CALLER_ID")
    if env and env.strip():
        return env.strip()
    from planner.mcp_client import McpError

    try:
        payload = _weve_client(args).call_tool("get_current_user_context", {})
    except McpError:
        return None
    return _user_aad_id(payload) if isinstance(payload, dict) else None


def cmd_current_user(args: argparse.Namespace) -> int:
    """Return the authenticated WeveNova/TDS caller and its AAD object ID."""
    from planner.mcp_client import McpError

    client = _weve_client(args)
    try:
        payload = client.call_tool("get_current_user_context", {})
    except McpError as exc:
        print(f"cannot resolve the current WeveNova user: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if not isinstance(payload, dict) or not _user_aad_id(payload):
        print("The current WeveNova user context is unavailable.", file=sys.stderr)
        return 1
    print(f"{_user_display_name(payload) or '?'}   <{_user_aad_id(payload)}>")
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    """List the valid WeveNova roles (the exact wire ids a task/attestation must
    use). Offline from the static catalogue by default; ``--live`` refreshes from
    the server."""
    from planner.roles import DEFAULT_REGISTRY, RoleRegistry

    registry = DEFAULT_REGISTRY
    if getattr(args, "live", False):
        from planner.mcp_client import McpError, client_from_config

        try:
            client = client_from_config(
                getattr(args, "mcp_server", "weve-plan"),
                getattr(args, "mcp_config", os.path.join(".vscode", "mcp.json")),
            )
            registry = RoleRegistry.from_mcp(client)
        except McpError as exc:
            print(f"warning: could not refresh roles from the server ({exc}); using the static catalogue", file=sys.stderr)

    if args.json:
        print(json.dumps(
            [
                {
                    "role": r.role,
                    "provider": r.provider,
                    "displayName": r.display_name,
                    "attestable": r.attestable,
                }
                for r in registry._by_id.values()  # noqa: SLF001 — CLI dump
            ],
            indent=2,
        ))
        return 0
    print("Task-groundable roles (use the exact id verbatim):\n")
    for r in registry._by_id.values():  # noqa: SLF001 — CLI dump
        tag = "  [attestable]" if r.attestable else ""
        label = r.role if r.display_name == r.role else f"{r.role}  ({r.display_name})"
        print(f"    {label}   <{r.provider}>{tag}")
    print("\nAttestable roles can be bound to a person with `roles attest`.")
    return 0


def cmd_attest(args: argparse.Namespace) -> int:
    """Attest a person (their Entra object id) to an attestable role on the plan."""
    from planner.attest import AttestationError

    client = _attest_client(args)
    try:
        rec = client.attest(
            args.person,
            args.role,
            provider=args.provider,
            idempotency_key=args.idempotency_key,
            etag=args.etag,
        )
    except AttestationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rec, indent=2))
    else:
        aid = rec.get("AssignmentId") or rec.get("Id") or "?"
        print(f"Attested {args.person} to {rec.get('Role', args.role)} (assignment {aid}).")
    return 0


def cmd_assignments(args: argparse.Namespace) -> int:
    """List the plan's role assignments (who is attested to which role)."""
    from planner.attest import AttestationError

    client = _attest_client(args)
    try:
        items = client.list_assignments(
            subject_id=args.person, role=args.role, status=args.status
        )
    except AttestationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("No role assignments on this plan.")
        return 0
    print(f"{len(items)} role assignment(s):\n")
    for a in items:
        aid = a.get("AssignmentId") or a.get("Id") or "?"
        print(f"    {a.get('Role', '?')}  ->  {a.get('SubjectId', '?')}  "
              f"[{a.get('Status', '?')}]  ({aid})")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """Revoke a role assignment on the plan."""
    from planner.attest import AttestationError

    client = _attest_client(args)
    try:
        client.revoke(args.assignment, etag=args.etag)
    except AttestationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Revoked assignment {args.assignment}.")
    return 0


def cmd_caller_tasks(args: argparse.Namespace) -> int:
    """Show the tasks a logged-in person sees on the plan: their directly-assigned
    tasks **plus** the pooled tasks for the roles they are attested to (Flow 2,
    server-resolved via WeveNova).

    Caller-scoped and **self-only**: the caller id must be the *authenticated*
    identity (the tunnel-signed-in user), resolved from ``--caller``,
    ``PLANNER_MCP_CALLER_ID``, or ``get_current_user_context``. WeveNova only
    expands role-pooled tasks for the caller's own OID."""
    from planner.attest import AttestationError, is_oid

    caller = _resolve_caller_id(args)
    if not caller:
        print(
            "could not resolve your caller id — get_current_user_context returned "
            "nothing and neither --caller nor PLANNER_MCP_CALLER_ID is set. It must "
            "be YOUR authenticated identity (self-only).",
            file=sys.stderr,
        )
        return 2
    if not is_oid(caller):
        print(
            f"caller id must be an Entra object id (a GUID), got {caller!r}. "
            "This is the caller's own authenticated OID, not a display name.",
            file=sys.stderr,
        )
        return 2

    client = _attest_client(args)
    try:
        tasks = client.tasks_for_caller(caller, odata_filter=getattr(args, "filter", None))
    except AttestationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(tasks, indent=2))
        return 0
    if not tasks:
        print("No tasks are waiting on you right now.")
        return 0
    print(f"{len(tasks)} task(s) visible to you:\n")
    for t in tasks:
        tid = t.get("TaskId") or t.get("Id") or "?"
        print(f"    - {tid}  {t.get('Title', '')}  [{t.get('State', '?')}]")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roles", description="ESS Maker Kit roles CLI (people ↔ roles on a plan)")
    parser.add_argument("--mcp-server", dest="mcp_server", default="weve-plan", help="MCP server name in .vscode/mcp.json")
    parser.add_argument("--mcp-config", dest="mcp_config", default=os.path.join(".vscode", "mcp.json"), help="path to the MCP config")
    parser.add_argument("--project-id", dest="project_id", default=None, help="WeveNova project id; else PLANNER_MCP_PROJECT_ID or discovery")
    parser.add_argument("--plan-id", dest="plan_id", default=None, help="WeveNova plan id; else PLANNER_MCP_PLAN_ID or discovery")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("current-user", help="get the authenticated WeveNova caller and AAD object id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_current_user)

    p = sub.add_parser("roles", help="list the valid WeveNova roles (exact wire ids for tasks/attestations)")
    p.add_argument("--live", action="store_true", help="refresh the catalogue from the weve-plan server (else static)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("attest", help="attest a person to an attestable role on the plan")
    p.add_argument("--person", required=True, help="the current person's Entra object id (a GUID) — resolve it via `current-user`")
    p.add_argument("--role", required=True, help="an attestable role (id or display name; see `roles`)")
    p.add_argument("--provider", help="the role's owner (External/Entra/PowerPlatform); derived when omitted")
    p.add_argument("--idempotency-key", dest="idempotency_key", help="optional idempotency key for replay-safe attest")
    p.add_argument("--etag", help="optional If-Match etag for convergence")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_attest)

    p = sub.add_parser("assignments", help="list the plan's role assignments")
    p.add_argument("--person", help="filter by subject (person) oid")
    p.add_argument("--role", help="filter by role (id or display name)")
    p.add_argument("--status", help="filter by status, e.g. Active/Revoked")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_assignments)

    p = sub.add_parser("revoke", help="revoke a role assignment on the plan")
    p.add_argument("--assignment", required=True, help="the assignment id to revoke")
    p.add_argument("--etag", help="optional If-Match etag")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("caller-tasks", help="show YOUR tasks: direct + pooled-for-your-roles (Flow 2, self-only)")
    p.add_argument(
        "--caller",
        help="YOUR own authenticated Entra object id (a GUID); defaults to "
        "PLANNER_MCP_CALLER_ID, then get_current_user_context. Must be the "
        "tunnel-authenticated caller because role expansion is self-only",
    )
    p.add_argument(
        "--filter",
        dest="filter",
        help="optional extra OData $filter; the caller scope is applied "
        "automatically, so don't repeat an assignedToId predicate",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_caller_tasks)

    return parser


def _configure_io() -> None:
    """Best-effort UTF-8 stdout/stderr so labels (em-dashes, etc.) print on
    Windows consoles without crashing on cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
