# Build Spec: Stateful Multi-Agent Browser Automation System
**Paste this entire document into Cursor as the task prompt. Model: Grok 4.6.**

## 0. What you're building

A three-agent system (Planner, Navigator, Guardian) that takes a natural-language task, automates a real website via Playwright MCP, pauses to ask the user a specific question whenever it hits genuine ambiguity or an irreversible step, and can be killed mid-task and resumed later from exactly where it stopped — no re-login, no re-deciding what's already decided.

The system must run against **any** configured site via a modular `SITE_PROFILES` config — not hardcoded to one target. Build and demo against two profiles: `saucedemo` (shopping/checkout) and `greenhouse` (job application form-fill). Same three agents, same code, different profile passed at launch.

Judging rubric this is built for: **It works (40)** — must run live, reliably, twice. **Taste (30)** — visible browser + narrated agent handoffs + a live kill/resume moment. **Business use case (30)** — pitch this as "an agent that knows when NOT to act alone," not as a generic browser bot.

## 1. Non-negotiables (do not skip these)

- Grok 4.6 must do real reasoning work at every decision point — no hardcoded click sequences standing in for agent decisions.
- Navigator is the **only** agent with MCP/browser tool access. Planner and Guardian never touch the browser directly — enforce this in code, don't just assume it.
- State must be written to disk after **every** action, not only at pause points.
- Browser runs headed (`headless=False`), visible on screen, for the entire demo.
- Do not attempt any anti-bot/stealth evasion. Both target sites have no bot protection — if you find yourself adding stealth plugins, stop, you're solving the wrong problem.

## 2. Architecture

```
User task (CLI arg) + --site profile
        │
        ▼
   [ PLANNER ]  (Grok 4.6, no tools)
   Reads task + site profile's checklist_template.
   Outputs ordered sub-goals, each tagged with a risk level.
        │
        ▼  (one sub-goal at a time)
   [ NAVIGATOR ] (Grok 4.6, MCP tools: browser_navigate,
                  browser_click, browser_type, browser_snapshot)
   Reads current sub-goal + browser_snapshot.
   Proposes ONE next action. Does not execute it directly —
   passes the proposal to the Guardian first if the sub-goal
   is risk-tagged.
        │
        ▼
   [ GUARDIAN ]  (Grok 4.6, no tools)
   Only invoked on risk-tagged sub-goals.
   Given {proposed_action, sub_goal, risk_reason}, returns
   either PROCEED or ASK(question).
   If ASK: pause the whole loop, print the question, block
   for user input, write the answer into state, resume
   Navigator with that answer in context.
        │
        ▼
   [ STATE STORE ]  (JSON file per task_id, written after
                     every single action — not just pauses)
   { site, task, planner_checklist, current_subgoal_index,
     navigator_action_history, guardian_answers,
     current_url, storage_state_path, status }
```

**Resume path**: on launch, check for an existing state file for the given `task_id`. If found: reload Playwright context from the saved `storage_state` (cookies/session), `goto()` the saved `current_url`, rebuild Planner's checklist position and Guardian's answers from state, and continue the Navigator loop from `current_subgoal_index`. Do not re-run the Planner or re-login.

## 3. Modular site profiles

Define profiles as data, not code branches. The Planner, Navigator, and Guardian must be 100% profile-agnostic — all site-specific knowledge lives in this config.

```python
SITE_PROFILES = {
    "saucedemo": {
        "name": "Sauce Demo",
        "base_url": "https://www.saucedemo.com/",
        "requires_login": True,
        "login": {"username": "standard_user", "password": "secret_sauce"},
        "task_type": "shopping",
        "checklist_template": [
            {"goal": "log in with the provided credentials", "risk": "none"},
            {"goal": "find the item matching the user's request", "risk": "ambiguous_choice"},
            {"goal": "add the chosen item to the cart", "risk": "none"},
            {"goal": "go to checkout and fill shipping info", "risk": "needs_user_info"},
            {"goal": "confirm and finish the order", "risk": "irreversible"},
        ],
    },
    "greenhouse": {
        "name": "Greenhouse Job Application",
        # Pick any live public application page, e.g.
        # https://boards.greenhouse.io/<company>/jobs/<id>#app
        "base_url": "<PASTE A LIVE GREENHOUSE APPLICATION URL HERE>",
        "requires_login": False,
        "task_type": "form_fill",
        "checklist_template": [
            {"goal": "open the application form", "risk": "none"},
            {"goal": "fill personal info fields (name, email, phone)", "risk": "needs_user_info"},
            {"goal": "answer screening questions", "risk": "ambiguous_choice"},
            {"goal": "attach resume/cover letter fields if required", "risk": "needs_user_info"},
            {"goal": "submit the application", "risk": "irreversible"},
        ],
    },
}
```

Why these two: they're different task *shapes* (transactional cart/checkout vs. open-ended form-fill), which proves the architecture generalizes instead of being hardcoded to one flow — this is a stronger technical claim than two shopping sites.

Launch with: `python agent.py --site saucedemo --task "buy me a t-shirt" --task-id demo1`

## 4. File structure

```
/agent.py              # CLI entry point, ties everything together
/agents/planner.py      # planner_plan(task, site_profile) -> checklist
/agents/navigator.py    # navigator_step(subgoal, snapshot, history) -> action
/agents/guardian.py     # guardian_check(action, subgoal, reason) -> verdict
/state.py               # save_state(), load_state()
/site_profiles.py       # SITE_PROFILES dict above
/mcp_client.py           # thin wrapper around Playwright MCP tool calls
```

## 5. Grok 4.6 API basics (for reference while wiring calls)

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user

client = Client(api_key=os.getenv("XAI_API_KEY"))
chat = client.chat.create(model="grok-4.6")
chat.append(user(prompt))
response = chat.sample()
```
Base URL if using the OpenAI-compatible client instead: `https://api.x.ai/v1`. Set `XAI_API_KEY` in the environment before running.

## 6. Time-boxed build order — 130 minutes, core scope

| Phase | Task | Time |
|---|---|---|
| 1 | Stand up Playwright MCP server locally, confirm you can call `browser_navigate`/`browser_click`/`browser_snapshot` manually against Sauce Demo before wiring any agent to it | 20 min |
| 2 | `site_profiles.py` with both profiles above; `state.py` save/load functions — get the schema right first, everything else depends on it | 15 min |
| 3 | Planner: **hardcode** the checklist read directly from `checklist_template` for this build — skip dynamic generation to protect time. One Grok call only if time allows later (see stretch) | 10 min |
| 4 | Navigator: one Grok 4.6 call per turn — `{sub_goal, browser_snapshot, action_history}` → one MCP tool call. This is the only agent with tool access | 35 min |
| 5 | Guardian: one Grok 4.6 call, invoked only on risk-tagged sub-goals — `{proposed_action, sub_goal, risk_reason}` → `PROCEED` or `ASK(question)`. On `ASK`: block on CLI `input()`, write answer to state | 25 min |
| 6 | Wire resume path: on launch, check for existing state file, reload `storage_state`, `goto()` saved URL, continue loop from saved `current_subgoal_index` | 20 min |
| 7 | Rehearse: run `saucedemo` task end to end once. Run again, Ctrl+C right after Guardian asks a question, restart, confirm it resumes through checkout without re-login | 20 min |
| 8 | Buffer / fix whatever broke in rehearsal | 5 min |

**Total: 130 min.** This is the floor — a working two-site, three-agent, resumable system with a visible browser. Do not start stretch items until phase 7 has passed cleanly twice in a row.

## 7. Stretch goals — only if phase 7 is solid with time to spare

**If ~20–30 min remain:**
- Color-code terminal output by agent (Planner/Navigator/Guardian each a distinct color; questions/answers highlighted separately) so the reasoning trace reads clearly on screen next to the browser window. Cheap, high payoff.
- Position the browser window and terminal side by side deliberately (`--window-size` / `--window-position` launch args) rather than leaving default placement.
- Make the Planner dynamic (one Grok call generating the checklist from raw task text) instead of the hardcoded template, if you want to show it handling a task shape the template didn't anticipate.

**If ~45–60+ min remain (real stretch — do not attempt unless everything above is done and rehearsed):**
- Replace the native OS browser window + terminal with a single local web page: left pane streams the browser live (CDP screencast or polled `page.screenshot()` over a websocket), right pane is a chat log with an input box that feeds directly into the Guardian's `ASK` pause instead of a terminal prompt. This looks more like a finished product but is a real, separate build — budget it as its own phase, not a UI polish pass, and only start it with a full rehearsed fallback (the terminal+window version) still working underneath.

## 8. Demo script

1. Open with the problem in one sentence: most browser agents either succeed silently or fail silently — this one knows when it isn't sure, and it doesn't lose its place if you have to step away.
2. Run `--site saucedemo --task "buy me a t-shirt"`. Narrate the handoff live: Planner hands off the checklist, Navigator proposes adding a t-shirt, Guardian intercepts (ambiguous — two t-shirts exist) and asks which one. Answer it. Watch it proceed to checkout, ask for a zip code, answer, finish.
3. Kill the process mid-task on a second run (right after a Guardian question, before answering). Restart the script. Show it resume from saved state — no re-login, no repeated questions — then answer and let it finish.
4. Switch profiles: `--site greenhouse --task "apply to this job for me"`. Same three agents, no code changes, different site shape — this is the modularity proof.
5. Close on the business framing: this is the difference between an agent you'd trust with a task that has real stakes (paying, submitting, applying) and one that guesses and hopes.
