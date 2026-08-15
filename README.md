# chameleon

Stateful three-agent browser automation (Planner, Navigator, Guardian) that pauses on ambiguity or irreversible steps and resumes from disk.

## Install

Requires Python 3.11+, Node.js 18+ (`npx`), and an LLM key: `CLAUDE_API_KEY` / `ANTHROPIC_API_KEY` (Claude Sonnet 4.6) or `XAI_API_KEY` (Grok 4.6).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set CLAUDE_API_KEY or XAI_API_KEY
```

Chromium is downloaded on first Playwright MCP run.

## Run

Smoke the headed browser (no agents, Sauce Demo login only):

```bash
python scripts/smoke_mcp.py
```

Run a task:

```bash
chameleon --site saucedemo --task "buy me a t-shirt" --task-id demo1
python -m chameleon --site greenhouse --task "apply to this job for me" --task-id demo-gh
```

Resume is automatic when `data/tasks/{task_id}.json` already exists.

## Docs

- [docs/requirements.md](docs/requirements.md) — acceptance contract
- [agent-build-spec.md](agent-build-spec.md) — original build spec
