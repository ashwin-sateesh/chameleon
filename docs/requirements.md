# Chameleon requirements

Acceptance contract for this build. The contest prompt lives in [`agent-build-spec.md`](../agent-build-spec.md). This file is what the code must satisfy.

## 1. Problem

Browser agents usually succeed silently or fail silently. Chameleon pauses when it hits genuine ambiguity or an irreversible step, asks a specific question, and can be killed mid-task and resumed from disk with no re-login and no re-deciding of answers already given.

Pitch: an agent that knows when **not** to act alone.

## 2. Goals

- Live headed demo against execute profiles (`saucedemo`, `greenhouse`) and copilot profiles (`maps`, with `osm` fallback and `airbnb` as a second exploratory site).
- Three agents: Planner, Navigator, Guardian. Same code for all sites; only the profile changes.
- Execute mode: state after every action; kill/resume; pauses on ambiguity.
- Copilot mode: terminal-led — confirm once, agent acts, then suggest unused on-page options.
- Site-specific knowledge lives in YAML profiles. Agents are profile-agnostic.

## 3. Non-goals (this build)

- Anti-bot / stealth plugins.
- A generic multi-site crawler.
- Docker / Kubernetes (layout is reserved; images are later).
- Web UI / CDP screencast.
- Dynamic Planner inventing an execute checklist from raw task text — stretch only. Copilot uses `planner_observe` to interpret page state (required).

## 4. Functional requirements

### FR1 — CLI

```bash
chameleon --site <id> --task "<natural language>" --task-id <id>
chameleon --site maps --task-id <id>
python -m chameleon --site <id> --task "<natural language>" --task-id <id>
```

`--site` must match a file in `configs/sites/<id>.yaml`. Unknown sites fail cleanly. `--task` is required for `interaction_mode: execute` and optional for `copilot` (falls back to `default_task`).

### FR2 — Profiles

YAML under `configs/sites/`. Agents must not branch on site id. Adding a site is a data change.

`interaction_mode` is `execute` (default) or `copilot`. Copilot profiles may set `intent_hints`, `end_phrases`, and `default_task`.

### FR3 — Planner

`planner_plan(task, profile) -> checklist` copies `checklist_template` from the profile for execute sites. Prints the ordered sub-goals at start of a new task. Not re-run on resume.

Copilot: `planner_observe` reads URL + truncated snapshot + `intent_hints` after each agent burst and after the user navigates in the headed browser (debounced fingerprint change). Returns unused on-page options the user would likely miss, or `should_ask false` so the loop can nudge them to click, type, or `done`. No LLM call on idle polls of the same page. Do not re-ask constraints already applied.

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
| `phase` | Copilot only: `observing` \| `asking` \| `acting` |
| `user_state` | Copilot: last interpreted page summary |
| `last_fingerprint` / `interpreted_fingerprint` | Copilot observe debounce |
| `consented_goal` | Copilot micro-goal after user consent |
| `observed_events` | Copilot: recent `{url, summary}` |

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

### FR9 — Copilot

Profiles with `interaction_mode: copilot` open the site and wait for the **terminal**, not for the user to click the page.

- On open: one crisp line asking what they want. If `--task` is a real request (not `default_task`), immediately ASK `I'll {task}. OK?`
- A concrete instruction or “yes” is consent — one confirm, then the agent acts. Do not double-ask “or will you?”
- Core search (query, dates, guests, submit) is one burst. Optional filters (stars, chips, Guest favorite, layers) are applied only if already visible — one try. If a requested filter is not on the page, tell the user and ASK; do not hunt.
- After each burst and after the user clicks in the headed browser: Planner analyzes THIS page, suggests at most 2 simple visible extras, and asks a follow-up. If a click opens a new tab, switch to that tab and analyze it. If nothing useful: nudge to click, type, or `done`.
- Same copilot loop for `maps`, `osm`, and `airbnb` (profile data only; no site-id branches).
- Idle polls of the same page do not call the LLM. Cookie/CAPTCHA: ASK the user to handle it in the browser. No stealth.
- Per-click Guardian is a heuristic (ASK only for leave-site / book / pay / share / submit). Navigator bursts run back-to-back.
- User types `done` / `quit` / `that's all` to finish. Kill/resume still works mid-question.

## 5. Non-functional requirements

- **NFR1** Browser is headed. Never pass `--headless`. No stealth plugins.
- **NFR2** Navigator, Guardian, and copilot Planner call a real LLM — Claude Opus 4.8 (`CLAUDE_API_KEY` / `ANTHROPIC_API_KEY`) or Grok 4.6 (`XAI_API_KEY`). No hardcoded click sequences standing in for those decisions. Copilot per-click safety is a small heuristic after the user already consented to the burst.
- **NFR3** Navigator is the only agent that proposes browser tools. `planner.py` and `guardian.py` must not import `mcp_client` or `mcp`. Storage dump/restore is orchestrator-owned (`browser_storage_state` / `browser_set_storage_state`); the LLM never receives those tools.
- **NFR4** `.env` is gitignored. Sauce Demo credentials may live in the public demo YAML (`standard_user` / `secret_sauce`).

## 6. Site profiles

Risk tags: `none` | `ambiguous_choice` | `needs_user_info` | `irreversible`.

### saucedemo

- Base URL: `https://www.saucedemo.com/`
- Login required: `standard_user` / `secret_sauce`
- Task type: shopping
- Checklist: log in; find matching item; add to cart; checkout/shipping; confirm order
- `interaction_mode`: execute

### greenhouse

- Live public GitLab application page (see YAML)
- No login
- Task type: form_fill
- Checklist: open form; fill personal info; screening questions; resume/cover letter; submit
- `interaction_mode`: execute

### maps

- Base URL: `https://www.google.com/maps`
- `interaction_mode`: copilot
- Intent hints: restaurants / hotels / things to do filters, directions, similar places
- Do not click the map canvas

### osm

- Base URL: `https://www.openstreetmap.org`
- `interaction_mode`: copilot
- Fallback if Maps snapshots or bot checks make the live demo unreliable

### airbnb

- Base URL: `https://www.airbnb.com`
- `interaction_mode`: copilot
- Second exploratory shape: search → dates/guests → unused filters (Guest favorite, Luxury, Instant Book) → listing → ASK before book/contact

## 7. Demo acceptance

Must pass twice:

1. `chameleon --site saucedemo --task "buy me a t-shirt" --task-id demo1` — Guardian asks which shirt; user answers; checkout asks for missing info (e.g. zip); finish.
2. Kill (Ctrl+C) on a second run right after a Guardian question; restart the same `--task-id`; resumes without re-login or re-asking answered questions.
3. `chameleon --site greenhouse --task "apply to this job for me" --task-id demo-gh` — same three agents, different YAML, no code branch on site id.
4. `chameleon --site maps --task "restaurants in San Francisco" --task-id demo-maps` — agent confirms the task, acts, then suggests on-page extras; `done` ends. Kill/resume mid-ask still works.

## 8. Later (out of this build)

- `deploy/Dockerfile`: Python 3.11 + Node 20; `ENTRYPOINT ["chameleon"]`; volume for `/data`; MCP via HTTP sidecar or in-process stdio.
- Headed-in-Docker: Xvfb + noVNC. Not before the local headed demo is solid.
- Stretch web UI (browser screencast + chat for Guardian ASK).
