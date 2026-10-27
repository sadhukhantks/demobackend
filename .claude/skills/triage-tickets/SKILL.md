---
name: triage-tickets
description: Triage the VOW support-ticket queue like a senior full-stack developer — read unresolved tickets from vowconsole3, re-classify each (data issue / bug / new feature / migration issue), decide self-fix vs team dispatch with a model tier (sonnet/opus/fable), implement easy fixes on isolated branches, and post [AI-TRIAGE] internal verdicts back on the tickets. Use when the user says "triage the tickets", "look at the open support tickets", "what's in the ticket queue", or names a specific ticket to assess. Dispatches the ticket-triage agent.
---

# Skill: triage-tickets

Thin entry point — the **`ticket-triage`** agent carries the full methodology (queue SQL,
classification rubric, self-vs-team heuristic, model tiering, write-back format, guardrails).

## Inputs (all optional)

- **Ticket id(s) / ticket no(s)** — triage only these; without them the agent takes the whole
  unresolved queue (`status_id IN (1,2,3,4)`, minus already-triaged).
- **`--force`** — re-triage tickets that already carry an `[AI-TRIAGE]` comment.
- **`report-only`** — classify and plan but skip self-implementation and the DB write-back.

## Steps

1. Dispatch the **`ticket-triage`** agent (Agent tool) with the inputs above verbatim. One
   agent, sequential — it manages its own worktrees for fixes; don't fan out parallel triagers
   over the same queue (they'd race on the dedupe marker).
2. Relay the agent's report: the triage table, branches created, dispatch plans, and open
   questions.
3. Surface decisions the agent flagged — sls repair scripts awaiting approval, ambiguous
   tickets needing reporter input, reject candidates — as the first thing the user sees.
4. If the user approves a dispatch plan, launch the named agents at the named tiers (that
   approval is the gate; don't auto-launch from triage).

## Notes

- DB = vowconsole3 via pymysql, creds `env/database.env`. The agent writes ONLY internal
  `[AI-TRIAGE]` comments — status moves, assignment, and reporter replies stay human.
- Fixes land on `ticket-<id>-fix` branches via git worktrees; nothing is pushed or merged.
