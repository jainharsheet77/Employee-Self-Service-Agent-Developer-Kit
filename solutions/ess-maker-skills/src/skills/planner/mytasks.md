# Planner — Flow 2: "What am I assigned?"

When a person asks what work is waiting on them, show their Tasks **grouped by
each role they hold** — which naturally covers a person with more than one role.

## Steps

**0. Fetch the live plan from WeveNova first.** "What am I assigned?" is a read of
the *live* plan, so before anything else pull it — **even if you routed straight
here** and think no backend is configured:

```
python scripts/planner/cli.py --store mcp pull
```

- Returns a plan → continue to step 1 (resolve roles, show tasks).
- Returns an **empty plan / "no plans yet"** → there genuinely are no assignments
  because no plan has been authored yet; say so and offer to **build** one (hand
  back to the planner's plan-creation flow). Do **not** claim "no plan exists"
  without having pulled.
- Errors that WeveNova is unreachable/unconfigured → fall back to the local plan
  (`summary`) and the best-effort role resolution below.

**Never answer "nothing is assigned / no plan exists" until this pull has run.**

1. **Find the person's roles.** The roles source is a separate, unbuilt system,
   so this is best-effort:
   - If a roles source is wired, look up the roles this person holds.
   - If not, resolve the caller's identity by name via the WeveNova people
     directory (`find-users`) — or just ask — and/or ask them to confirm which of
     the plan's roles are theirs.

   > **MCP store — "my tasks" is self-only.** Against WeveNova (`--store mcp`),
   > don't resolve or ask for the roles — hand off to the `/roles` skill's
   > `caller-tasks`, which resolves them server-side from the caller's
   > attestations. The caller id it needs is the **authenticated** person's *own*
   > OID (the tunnel-signed-in user, or `PLANNER_MCP_CALLER_ID`) — WeveNova only
   > expands role-pooled tasks for the caller's own identity. **Never** pass a
   > name looked up via `find-users` (e.g. "primary") as the caller — that's for
   > `attest`, and as a caller it returns none of their pooled tasks.
2. **Show their Tasks, grouped by role:**

   ```
   python scripts/planner/cli.py mine --person <oid> --roles <role,role,...>
   ```

   This lists, under each role:
   - Tasks **assigned to them** directly ("assigned to you"), and
   - Open **pools** for a role they hold ("open to your role"), which they can
     pick up.

   > **MCP store:** against WeveNova (`--store mcp`), "what am I assigned?" is
   > owned by the **`/roles` skill** (`src/skills/roles/SKILL.md`): its
   > `caller-tasks --caller <oid>` resolves the person's roles from their
   > **attestations** server-side (no manual `--roles`) and returns their direct
   > tasks **and** the pooled tasks for every role they're attested to. Hand off
   > to `/roles`. The `mine --roles …` form above is the offline equivalent when
   > no roles source is wired.

3. **Claiming a pooled Task.** If they take a pooled Task, record them as the
   owner (the role is retained):

   ```
   python scripts/planner/cli.py claim --task <T#> --person <oid>
   ```

4. From there, they do the Task (as its description says) and you capture its
   output (Phase 6, `src/skills/planner/capture.md`).

## Before they start — connect their kit to the plan's environment

A task after setup runs against the environment the Power Platform admin
established (they run `/setup`, **decide or create** the environment, and the
planner pins its id as `primaryEnvironment`). Each *other* persona still has to
connect their own kit to that same environment first. So when someone picks up a
task, run `task-brief` and honour its nudge:

```
python scripts/planner/cli.py task-brief --task <T#>
```

- **Plan has an environment pinned** → `task-brief` prints "First connect your
  kit: run /setup and choose environment `<envId>`". Nudge them to `/setup` into
  **that** environment (don't let them pick or create a different one), then they
  do their task.
- **No environment pinned yet** → the admin's `/setup` task is the prerequisite;
  the environment hasn't been decided. Don't nudge this person to setup — tell
  them their task is blocked until setup runs, and who owns it.

Present the result in plain language — role headings with their tasks beneath —
not as raw output.

## Brief the task in detail — enrich from Learn

When an assignee engages a task — "what do I do?", or they claim it — do **not**
just echo the one-line description. Give a **detailed, actionable how-to**, the
same depth `/connect` or `/setup` gives. **Mantra: enrich from Learn.** Start from
the structured brief, then enrich the "how":

```
python scripts/planner/cli.py task-brief --task <T#>
```

`task-brief` gives the role, the values the task **consumes** (resolved off the
plan — e.g. the environment id to use), the outputs to **capture** when done, and
the `/setup`-into-the-pinned-environment nudge. On top of that, render the "how"
by the task's kind:

1. **The task is done by running a kit skill** (its description says run `/setup`,
   `/connect`, `/create`, `/evaluate`): **hand off to that skill** — it owns the
   detailed, current, per-tenant steps (that's exactly why `/connect` and `/setup`
   are rich). Read that skill's `SKILL.md` and follow it; don't re-summarise its
   steps from memory. The skill *is* the how-to. (E.g. "create a topic" → `/create`.)

2. **The task is a portal / manual step** with no kit skill (register an Entra app,
   provision the Power Platform environment, publish the agent): **fetch the how-to
   from Microsoft Learn at render time** and render a **task walkthrough**:
   - **Role** — taken *verbatim* from the step's Learn page (never relabelled or
     invented; if Learn lists alternatives, name them as Learn does).
   - **What it accomplishes** — a line or two of context.
   - **Steps** — numbered; each step ends with an inline `learn.microsoft.com` link
     to the exact page/section it came from.
   - **Help & resources** — a short list of the relevant Learn links.

   Fetch from the task's grounding Learn anchor kept in the research context
   (§7.6, `prerequisites[].sourceUrl`) — **never** rely only on the terse stored
   description, and **never** fabricate a step, role, or link. The description is
   the scannable summary; the Learn fetch (or the kit skill) is the detailed how.

Always enrich **on start**, freshly from Learn, so the steps and links are current
rather than baked into (and drifting from) the stored plan.
