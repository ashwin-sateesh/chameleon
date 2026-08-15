from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from chameleon.llm import has_llm_key
from chameleon.loop import MissingTaskError, run_task
from chameleon.paths import repo_root
from chameleon.profiles import UnknownSiteError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chameleon",
        description="Stateful multi-agent browser automation that knows when not to act alone.",
        epilog="Web console: chameleon ui",
    )
    parser.add_argument("--site", required=True, help="Profile id (configs/sites/<id>.yaml)")
    parser.add_argument(
        "--task",
        default=None,
        help="Natural-language task (optional for copilot sites; required for execute sites)",
    )
    parser.add_argument("--task-id", required=True, dest="task_id", help="Resume key")
    return parser


def build_ui_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chameleon ui", description="Open the local web console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser tab")
    return parser


def _require_key() -> None:
    if not has_llm_key():
        print(
            "No LLM key set. Add CLAUDE_API_KEY (or ANTHROPIC_API_KEY) or XAI_API_KEY to .env",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(repo_root() / ".env")
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "ui":
        _require_key()
        args = build_ui_parser().parse_args(argv[1:])
        from chameleon.ui.server import serve

        serve(host=args.host, port=args.port, open_browser=not args.no_open)
        return
    _require_key()
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run_task(args.site, args.task, args.task_id))
    except (UnknownSiteError, MissingTaskError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
