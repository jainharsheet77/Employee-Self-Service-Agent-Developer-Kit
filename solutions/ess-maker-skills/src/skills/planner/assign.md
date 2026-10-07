# Planner — Phase 4: Assign each Task (Flow 1) — a follow-up after the plan is shown

**Default to pooled; don't interview per task.** By the time you get here the plan
is modelled with every Task grounded to a role. **Do not** walk the sponsor through a
per-task "who does this?" questionnaire before they've seen the plan — that's the
over-interrogation the flow avoids. Instead, **pool every Task to its grounded role**
(automatic, no questions), publish, and **show the plan** (download link + checklist).
Naming people to those roles is a **follow-up the sponsor opts into** — suggested in
one short line after the plan is shown (`src/skills/planner/interview.md` → *After the
plan is shown*), never a blocking question you fire and never run before the sponsor
asks. Leaving every role pooled is a complete, valid end state.

When the sponsor asks to assign (now or later), the role for each Task is already
grounded from the Learn docs (Phase 3) — their only job is to pick **who**. For each
role they want to name:

1. **State the role** (don't ask for it): "This task is a `{role}` task."
2. **List the people who hold that role.** The roles source is a separate,
   currently-unbuilt system, so this is best-effort:
   - If a roles source is wired, show its holders of `{role}` and let the
     sponsor pick one.
   - If not, ask the sponsor for the person's name/identifier, or offer to
     **leave it open to the role** (a pool any holder can later claim).
3. **Record the choice.** For a specific person, `--person` takes their directory
   **object id**, not a name — so once the sponsor names someone, turn that name
   into their `oid` with the shared person-resolution step
   (`src/skills/roles/resolve-person.md`; it handles sign-in, disambiguation, and
   the WorkIQ fallback, then hands back the `oid`). Skip that when leaving the
   task open to the role.

   ```
   # assign a specific person, keeping the grounded role
   python scripts/planner/cli.py assign --task <T#> --role <role> --person <oid>

   # or leave it open to the role (pooled)
   python scripts/planner/cli.py assign --task <T#> --role <role>
   ```

The person is stored *acting as* the role — both facts are kept, so the Task
still shows up under that role in Flow 2, and provenance survives.

## The soft assumption

For the MVP, the sponsor often also holds Power Platform admin access, so they
may take the setup Task themselves. That's a convenience, not a rule — assign to
whoever actually holds the role; a different admin running the Task produces the
same result and the same capture (Phase 6).

Pool every Task to its grounded role, then **show the summary**. (The eval
**preview** was already rendered eagerly during the interview — Phase 5,
`src/skills/planner/evaluate.md`; re-render it here if the scope changed. It is
render-only and generates nothing.)

**Persist the plan automatically — the instant the task set is modelled and pooled,
not after a naming interview.** Publishing is the trigger that follows *modelling*,
not person-assignment; do not wait for the sponsor to name anyone. Follow
`src/skills/planner/sync.md` → *Push* now (name the configuring agent →
`export-remote-plan` → `create_project_plan` → re-hydrate). That publishes the plan
as **Draft** (roles pooled) and shows it back for review. **Naming people to the
pooled roles is the follow-up** — route it through the roles attestation flow
(`src/skills/roles/nudge.md` → `src/skills/roles/resolve-person.md` →
`src/skills/roles/attest.md`), which records each person as holding their role for
the plan (a create — no re-publish needed). A Draft's tasks are read-only until it's
**Active**, and the backend never auto-activates — so once the sponsor is happy,
**ask them to activate** the plan to put the work in motion (see `sync.md` steps
5–6). Never leave a freshly built plan local. If the service is unreachable, fall
back to the local cache and carry on (never mention any of this to the sponsor), and
push on a later turn once it's reachable — a summary that still shows the plan as
`(local, not synced)` has **not** been persisted.

The Plan is ready to run: each assignee runs the Task's skill (or does the
manual/portal step), and you capture what it produced in Phase 6.

## Nudge — put real people on the attestable roles

Some Tasks are pooled to an **attestable role** (e.g. `ServiceNowAdmin`,
`WorkdayAdmin`, `EntraPowerPlatformAdministrator`) rather than a named person. Pooled
role work is **invisible** until a real person is recorded as holding that role
for the plan — so once the plan is published (`src/skills/planner/sync.md`), nudge
the sponsor to assign those roles to real people. That is what makes the work show
up in each person's "what am I assigned?" view. Hand off to the roles attestation
flow to do it — it resolves the person and records the assignment
(`src/skills/roles/nudge.md`, then `src/skills/roles/resolve-person.md` →
`src/skills/roles/attest.md`). This is **assignment only**; it never answers "what
are my tasks?".

## Future — resolve the person from an external roles API

For an **attestable** role (e.g. `ServiceNowAdmin`, `WorkdayAdmin`,
`EntraPowerPlatformAdministrator`) the person side is already wired: the roles skill
resolves a name to a directory object id and records the holder against the plan
(`src/skills/roles/SKILL.md`). The seam below still describes the **general** roles
source for every *other* grounded role, which remains best-effort until built.

Today a Task carries a Learn-grounded **role** (pooled), and the sponsor picks the
person by hand. The next step wires a real **roles source** behind the same seam
(`scripts/planner/roles.py` → `RoleSource`), so the planner can:

1. **List holders of a role** from an external API — `list_holders(role)` returns
   the people who hold `{role}`, and the sponsor picks from that list instead of
   typing a name (Flow 1).
2. **Assign a user together with the role** — the choice is stored as
   `assign --task <T#> --role <role> --person <oid>` (person owns it, role
   retained), so the Task still groups under the role in Flow 2.
3. **Resolve "what am I assigned?"** — `roles_of(person)` maps the caller to their
   role(s) so Flow 2 needs no manual role entry.

The Plan schema and skills already support user+role assignment; only the backing
`RoleSource` is unbuilt. The role stays **Learn-grounded** regardless of who fills
it — the external API resolves *people*, never the *role* a Task needs.
