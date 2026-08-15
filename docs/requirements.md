# Chameleon requirements

Acceptance contract for this build. The contest prompt lives in [`agent-build-spec.md`](../agent-build-spec.md). This file is what the code must satisfy.

## 1. Problem

Browser agents usually succeed silently or fail silently. Chameleon pauses when it hits genuine ambiguity or an irreversible step, asks a specific question, and can be killed mid-task and resumed from disk with no re-login and no re-deciding of answers already given.

Pitch: an agent that knows when **not** to act alone.

## 2. Goals

- Live headed demo against two site profiles: `saucedemo` (shopping/checkout) and `greenhouse` (job application form-fill).
- Three Grok 4.6 agents: Planner, Navigator, Guardian. Same code for both sites; only the profile changes.
- State written to disk after every action, not only at pause points.
- Kill/resume from `task_id`.
- Site-specific knowledge lives in YAML profiles. Agents are profile-agnostic.

## 3. Non-goals (this build)

- Anti-bot / stealth plugins.
- A generic multi-site crawler.
- Docker / Kubernetes (layout is reserved; images are later).
- Web UI / CDP screencast.
- Dynamic Planner (one Grok call to invent the checklist) — stretch only.

## 4. Functional requirements

### FR1 — CLI

```bash
chameleon --site <id> --task "<natural language>" --task-id <id>
python -m chameleon --site <id> --task "<natural language>" --task-id <id>
```

`--site` must match a file in `configs/sites/<id>.yaml`. Unknown sites fail cleanly.

### FR2 — Profiles

YAML under `configs/sites/`. Agents must not branch on site id. Adding a site is a data change.

### FR3 — Planner

`planner_plan(task, profile) -> checklist` copies `checklist_template` from the profile. No Grok call in this build. Prints the ordered sub-goals at start of a new task. Not re-run on resume.

### FR4 — Navigator

One Grok 4.6 call per turn.

Input: `{sub_goal, browser_snapshot, action_history, guardian_answers}` plus profile login credentials when `requires_login` is true (credentials are context, not a scripted click sequence).

Output: exactly one proposed tool from:

- `browser_navigate`
- `browser_click`
- `browser_type`
- `browser_snapshot`

Navigator does **not** execute. It returns a proposal. May also set `subgoal_complete` when the current sub-goal is done.

The orchestrator executes the tool, takes a snapshot, and persists state.

### FR5 — Guardian

Invoked only when the current sub-goal `risk != none`.

Input: `{proposed_action, sub_goal, risk_reason}`.

Output: `PROCEED` or `ASK(question)`.

On `ASK`:

1. Set `status=paused_ask`, persist, print the question, block on CLI `input()`.
2. Write the answer into `guardian_answers`, persist, resume Navigator with that answer in context.
3. Do not execute the blocked proposal until Guardian later says `PROCEED`.

Irreversible sub-goals (confirm order, submit application) must ASK before confirm/submit.

### FR6 — State

Write JSON after every executed action and after every Guardian answer.

Schema:

| Field | Meaning |
|---|---|
| `site` | Profile id |
| `task` | User task text |
| `task_id` | Resume key |
| `planner_checklist` | Ordered `{goal, risk}` items |
| `current_subgoal_index` | Index into checklist |
| `navigator_action_history` | Tool calls already executed |
| `guardian_answers` | `{subgoal_index, question, answer}` |
| `current_url` | Last known page URL |
| `storage_state_path` | Playwright storage dump |
| `status` | `running` \| `paused_ask` \| `completed` \| `failed` |
| `pending_question` | Unanswered Guardian question, if any |

Paths:

- `data/tasks/{task_id}.json`
- `data/sessions/{task_id}/` (user-data-dir + `storage_state.json`)

Writes are atomic (temp file + replace).

### FR7 — Resume

If `data/tasks/{task_id}.json` exists:

- Do **not** re-run Planner.
- Do **not** re-login as a scripted first step.
- Spawn Playwright MCP with `--user-data-dir data/sessions/{task_id}`.
- Restore via `browser_set_storage_state` when a dump exists.
- `browser_navigate` to saved `current_url` when present.
- Continue the Navigator loop at `current_subgoal_index`.
- If `status=paused_ask` and `pending_question` is set, prompt that question first.

### FR8 — Narration

Print which agent is acting and why, so handoffs are visible next to the headed browser. Color-code Planner / Navigator / Guardian / questions / answers.

## 5. Non-functional requirements

- **NFR1** Browser is headed. Never pass `--headless`. No stealth plugins.
- **NFR2** Navigator and Guardian call a real LLM at each decision — Claude Sonnet 4.6 (`CLAUDE_API_KEY` / `ANTHROPIC_API_KEY`) or Grok 4.6 (`XAI_API_KEY`). No hardcoded click sequences standing in for those decisions.
- **NFR3** Navigator is the only agent that proposes browser tools. `planner.py` and `guardian.py` must not import `mcp_client` or `mcp`. Storage dump/restore is orchestrator-owned (`browser_storage_state` / `browser_set_storage_state`); the LLM never receives those tools.
- **NFR4** `.env` is gitignored. Sauce Demo credentials may live in the public demo YAML (`standard_user` / `secret_sauce`).

## 6. Site profiles

Risk tags: `none` | `ambiguous_choice` | `needs_user_info` | `irreversible`.

### saucedemo

- Base URL: `https://www.saucedemo.com/`
- Login required: `standard_user` / `secret_sauce`
- Task type: shopping
- Checklist: log in; find matching item; add to cart; checkout/shipping; confirm order

### greenhouse

- Live public GitLab application page (see YAML)
- No login
- Task type: form_fill
- Checklist: open form; fill personal info; screening questions; resume/cover letter; submit

## 7. Demo acceptance

Must pass twice:

1. `chameleon --site saucedemo --task "buy me a t-shirt" --task-id demo1` — Guardian asks which shirt; user answers; checkout asks for missing info (e.g. zip); finish.
2. Kill (Ctrl+C) on a second run right after a Guardian question; restart the same `--task-id`; resumes without re-login or re-asking answered questions.
3. `chameleon --site greenhouse --task "apply to this job for me" --task-id demo-gh` — same three agents, different YAML, no code branch on site id.

## 8. Later (out of this build)

- `deploy/Dockerfile`: Python 3.11 + Node 20; `ENTRYPOINT ["chameleon"]`; volume for `/data`; MCP via HTTP sidecar or in-process stdio.
- Headed-in-Docker: Xvfb + noVNC. Not before the local headed demo is solid.
- Stretch web UI (browser screencast + chat for Guardian ASK).
