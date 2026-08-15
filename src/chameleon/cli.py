from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from chameleon.llm import has_llm_key
from chameleon.loop import run_task
from chameleon.paths import repo_root
from chameleon.profiles import UnknownSiteError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chameleon",
        description="Stateful multi-agent browser automation that knows when not to act alone.",
    )
    parser.add_argument("--site", required=True, help="Profile id (configs/sites/<id>.yaml)")
    parser.add_argument("--task", required=True, help="Natural-language task")
    parser.add_argument("--task-id", required=True, dest="task_id", help="Resume key")
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv(repo_root() / ".env")
    load_dotenv()
    if not has_llm_key():
        print(
            "No LLM key set. Add CLAUDE_API_KEY (or ANTHROPIC_API_KEY) or XAI_API_KEY to .env",
            file=sys.stderr,
        )
        raise SystemExit(2)
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run_task(args.site, args.task, args.task_id))
    except UnknownSiteError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
