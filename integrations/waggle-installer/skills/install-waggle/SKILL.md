---
name: install-waggle
description: Install the official Waggle plugin locally for Codex from the latest stable Abhigyan-Shekhar/Waggle-mcp GitHub Release. Use for explicit requests such as "Install Waggle", "Set up Waggle", "Install Waggle for Codex", or "Enable Waggle". Do not use for Waggle operation, repair, updates, configuration, or memory workflows.
---

# Install Waggle

Install Waggle only; do not implement, configure, repair, or operate Waggle itself.

1. Confirm that this is a local Codex environment with shell access. If the `codex` executable or local filesystem is unavailable, stop and explain that Waggle must be installed from a supported local Codex environment. Do not attempt a ChatGPT Web installation.
2. Tell the user: "I’ll download the official Waggle Codex plugin from the Abhigyan-Shekhar/Waggle-mcp GitHub Releases page and install it through Codex’s normal permission flow."
3. Run `python3 scripts/install.py` from this skill directory. Let Codex request its normal network, filesystem, and command approvals; do not bypass the sandbox or use `sudo`.
4. Report the script's result exactly:
   - If it says Waggle is already installed, stop.
   - If it succeeds, confirm installation and tell the user to start a new Codex task so the plugin loads.
   - If it fails, report the error clearly and do not claim that Waggle was installed.

The installer checks `codex plugin list --json`, discovers the latest non-draft, non-prerelease GitHub Release, downloads its `waggle-codex-marketplace-<tag>.zip` asset, extracts it safely, locates the marketplace root, and runs the existing Codex commands. Do not substitute PyPI, a hosted MCP server, or another installation path.
