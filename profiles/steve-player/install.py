#!/usr/bin/env python3
"""Install the secret-free Steve profile through Hermes' supported CLI."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import yaml

PLUGIN_SHA = "6313fad5ddf2c060e7b16782f4860c0d5cdbfcac"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure-existing", action="store_true")
    parser.add_argument("--skip-plugin", action="store_true", help="Plugin already installed")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    target = Path.home() / ".hermes" / "profiles" / "steve-player"
    if target.exists() and not args.configure_existing:
        parser.error("profile exists; explicitly pass --configure-existing to apply this template")
    if not target.exists():
        subprocess.run(
            [
                "hermes",
                "profile",
                "create",
                "steve-player",
                "--no-skills",
                "--description",
                "Minecraft-only controller; no host tools or delegation.",
            ],
            check=True,
        )
    config = yaml.safe_load((source / "config.yaml").read_text())
    for key, value in config.items():
        subprocess.run(
            ["hermes", "-p", "steve-player", "config", "set", "--force", key, json.dumps(value)],
            check=True,
        )
    for name in ("SOUL.md", "distribution.yaml"):
        shutil.copyfile(source / name, target / name)
    if not args.skip_plugin:
        subprocess.run(
            [
                "hermes",
                "-p",
                "steve-player",
                "plugins",
                "install",
                "claude-subscription-directsdk",
                "--ref",
                PLUGIN_SHA,
                "--yes-deps",
                "--enable",
            ],
            check=True,
        )
    # `profile create` already writes the wrapper; this regenerates it when the
    # template is applied to an existing profile (--configure-existing).
    subprocess.run(["hermes", "profile", "alias", "steve-player"], check=True)
    print("Template applied. Set MAB_MCP_URL and MAB_CONTROLLER_TOKEN in the profile secret scope.")
    print("Authenticate Codex with Hermes and Claude with the official Claude CLI if needed.")


if __name__ == "__main__":
    main()
