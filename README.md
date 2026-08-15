# chameleon

Stateful three-agent browser automation (Planner, Navigator, Guardian) that pauses on ambiguity or irreversible steps and resumes from disk.

## Install

Requires Python 3.11+, Node.js 18+ (`npx`), and an LLM key: `CLAUDE_API_KEY` / `ANTHROPIC_API_KEY` (Claude Sonnet 4.6) or `XAI_API_KEY` (Grok 4.6).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # set CLAUDE_API_KEY or XAI_API_KEY
```

Chromium is downloaded on first Playwright MCP run.

## Run

Smoke the headed browser (no agents, Sauce Demo login only):

```bash
python scripts/smoke_mcp.py
```

Run a task in the terminal (headed OS browser):

```bash
chameleon --site saucedemo --task "buy me a t-shirt" --task-id demo1
python -m chameleon --site greenhouse --task "apply to this job for me" --task-id demo-gh
```

Open the two-pane web console (live page on the left, chat on the right):

```bash
chameleon ui
```

Then visit `http://127.0.0.1:8765`. Each **New chat** is its own `task_id` (resume, cookies, and transcript stay with that chat). Guardian questions are answered in the same composer. Type `stop` to halt a run.

## Docs

- [docs/requirements.md](docs/requirements.md) — acceptance contract
- [agent-build-spec.md](agent-build-spec.md) — original build spec
