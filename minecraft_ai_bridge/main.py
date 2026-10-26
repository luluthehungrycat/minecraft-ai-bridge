"""CLI entry point for the Minecraft AI Bridge.

Usage::

    # Run with a goal
    minecraft-ai-bridge "Build a house by the lake"

    # Use a custom config file
    minecraft-ai-bridge --config my_config.yaml "Explore and find diamonds"

    # List providers
    minecraft-ai-bridge --list-providers
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .bridge.orchestrator import Orchestrator
from .config import AppConfig

# Exit code when the run ended in safety-only operation after the Authz
# controller session expired, was revoked, or was fenced. 2 is argparse's.
EXIT_AUTHORITY_LOST = 3


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s" if verbose else "%(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minecraft-ai-bridge",
        description="LLM-powered AI agent that plays Minecraft.",
        epilog="Example: minecraft-ai-bridge 'Build a cobblestone bridge across the river'",
    )
    parser.add_argument(
        "goal",
        nargs="?",
        default=None,
        help="High-level goal for the AI agent (e.g., 'Build a house')",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"minecraft-ai-bridge v{__version__}",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="List supported LLM providers and exit",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Maximum think-act-observe turns before stopping (default: config value, typically 100)"
        ),
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="LLM model name override (e.g., 'openai/gpt-4o-mini', 'anthropic/claude-sonnet-4')",
    )
    return parser


def _list_providers() -> None:
    print("Supported LLM providers:")
    print()
    print("  openai          — OpenAI GPT-4o / GPT-4o-mini etc.")
    print("                    Requires: OPENAI_API_KEY env var")
    print("  anthropic       — Anthropic Claude Sonnet / Haiku")
    print("                    Requires: ANTHROPIC_API_KEY env var")
    print("                    Install:  pip install minecraft-ai-bridge[anthropic]")
    print("  ollama          — Local models (Llama 3, Mistral, etc.)")
    print("                    Requires: ollama server running (default http://localhost:11434)")
    print("  openrouter      — OpenRouter proxy (200+ models)")
    print("                    Requires: OPENROUTER_API_KEY env var")
    print("                    Models: openai/gpt-4o, anthropic/claude-sonnet-4, ...")
    print("  opencode_server — Attachable OpenCode inference server")
    print("                    Requires: opencode server running (default http://localhost:4096)")
    print("                    Default model: big-pickle")
    print()


async def _async_main(args: argparse.Namespace) -> int:
    config_path = Path(args.config)

    if not config_path.exists():
        logging.warning("Config file %s not found; using defaults.", config_path)

    config = AppConfig.from_yaml(str(config_path))

    # Merge CLI overrides
    if args.verbose:
        config.bridge.verbose = True
    if args.max_iterations is not None:
        config.bridge.max_iterations = args.max_iterations
    if args.model is not None:
        config.llm.model = args.model

    _setup_logging(config.bridge.verbose)

    try:
        orch = Orchestrator(config)
        await orch.run(args.goal)
        if orch.strategic_authority_lost:
            logging.error("Shut down after losing controller authority.")
            return EXIT_AUTHORITY_LOST
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    except Exception as exc:
        logging.exception("Fatal error")
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def main() -> None:
    """Synchronous CLI entry point (console_scripts)."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_providers:
        _list_providers()
        sys.exit(0)

    if args.goal:
        print(f"Goal: {args.goal}")
    else:
        print("No goal specified; using default from config.")

    exit_code = asyncio.run(_async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
