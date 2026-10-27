---
name: planner
description: >-
  Create, import, edit, assign, resume, share, activate, or track an ESS rollout plan in VS Code or Copilot CLI. Use for explicit Planner requests: "plan a rollout", "create a plan", "import my plan", "edit this plan", "assign owners", "what am I assigned?", "start my Planner task", "record task completion", "review the plan", "what is the plan status?", "share this plan", "activate this plan", "share and activate the plan", "resume my plan", or /planner. A saved local plan in a fresh chat is enough context; the user need not start a new planning interview. Open-ended "set up ESS for the first time / where do I start" is NOT a direct trigger: it gets the first-run welcome. The planner is the one experience allowed before setup.
---

# ESS Maker Kit — Planner

Entry point for explicit planning and existing-plan requests. This skill and the
VS Code `/planner` prompt use the same canonical flow. Whichever Planner flow
runs first in a chat must prepare this workspace, not assume a previous session
already prepared its connections. Later flows reuse that successful bootstrap.

This skill ships **inside** the kit; the kit root is the `ess-maker-skills` folder
that contains this skill's `.github/skills/` directory. Make it the working
directory before running anything.

## Steps

1. **Honor the kit rules first.** Read `.github/copilot-instructions.md` at the kit
   root and follow it (persona, communication rules — never narrate files/commands).
2. **Complete the workspace bootstrap before any shared tool call.** Read
   `src/skills/planner/SKILL.md` and apply its **Gate** before the first Planner
   flow in this chat, including direct status, assignment, editing, or completion.
   A saved plan or an already-visible Planner tool is not evidence that
   `.vscode/mcp.json` exists. The gate materializes defaults and validates the
   Planner and person-resolution entries, without requiring `/setup` or
   `.local/config.json`. Reuse a successful gate result in the same chat and
   workspace for later turns or phases; never assume one from an earlier session.
   Follow the gate's recheck/failure rules, not a separate bootstrap policy here.
   If the file disappears or the MCP connection is unavailable after a previous
   check, run the gate's bounded materialize/validate recovery before shared work.
   Backend API errors do not trigger configuration repair.
3. **Resume, then honor the requested action.** Follow the canonical planner's
   resume check: reuse the saved local draft or refresh the shared plan when
   access is accepted. Respect local-only drafting and declined shared access.
   Do not re-run the interview or re-ask the objective for an existing plan.
   For a general resume, show its current state and the requesting person's next
   action. Only start a new plan if none exists or the maker explicitly confirms
   starting over.
   A new plan remains a **local draft** until the maker **explicitly asks to share**
   it. Sharing creates a shared Draft; **explicit activation** is a separate
   action. Never publish or activate automatically after modelling. Run
   `python scripts/planner/cli.py ...` from the kit root.
   Carry an explicit share/activate request through the resume check; do not stop
   at a status response or restart the interview. If both were requested, confirm
   sharing succeeded before attempting activation, following `sync.md`.
   Reuse an already-shared/active plan rather than creating a duplicate.
4. **Keep the handoff visible.** Apply the canonical planner's **Communication
   rules** to every reply: a prominent next-step heading, bold action, and
   separate prompt or command. Required questions belong in the main chat.

## What the planner does

It authors a local, structured **Plan** for an ESS rollout: captures scenario and
system intent first, grounds that scope on Microsoft Learn, emits atomic tasks
with responsible roles and known or pending owners, and captures what each task produced. Its first task is
usually the **Power Platform admin running `/setup`**; once that pins the
environment, other personas are nudged to run `/setup` to connect their own kit to
it. To run environment setup directly (not plan), use the **setup** skill.
