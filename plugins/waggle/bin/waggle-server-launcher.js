#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const TARGETS = {
  "darwin:arm64": ["darwin-arm64", "waggle-server"],
  "darwin:x64": ["darwin-x86_64", "waggle-server"],
  "linux:x64": ["linux-x86_64", "waggle-server"],
  "linux:arm64": ["linux-aarch64", "waggle-server"],
  "win32:x64": ["win32-x86_64", "waggle-server.exe"],
};

function resolveBinary() {
  const target = TARGETS[`${process.platform}:${process.arch}`];
  if (!target) {
    throw new Error(`Unsupported Waggle bundled runtime target: ${process.platform}/${process.arch}`);
  }

  const pluginRoot = path.resolve(__dirname, "..");
  const binaryPath = path.join(pluginRoot, "runtime", target[0], target[1]);
  if (!fs.existsSync(binaryPath)) {
    throw new Error(
      [
        `Missing bundled Waggle server binary for ${target[0]}.`,
        `Expected: ${binaryPath}`,
        "Upgrade or reinstall the Waggle Codex plugin. This plugin does not use waggle-mcp from PATH.",
      ].join("\n")
    );
  }
  return binaryPath;
}

function main() {
  let binaryPath;
  try {
    binaryPath = resolveBinary();
  } catch (error) {
    console.error(error.message);
    process.exit(78);
  }

  const child = spawn(binaryPath, process.argv.slice(2), {
    cwd: path.dirname(binaryPath),
    env: {
      WAGGLE_BACKEND: "sqlite",
      WAGGLE_DB_PATH: "~/.waggle/waggle.db",
      WAGGLE_DEFAULT_TENANT_ID: "local-default",
      WAGGLE_MODEL: "all-MiniLM-L6-v2",
      WAGGLE_BUNDLED_RUNTIME: "1",
      WAGGLE_TRANSPORT: "stdio",
      ...process.env,
    },
    stdio: "inherit",
  });

  child.on("error", (error) => {
    console.error(`Failed to launch bundled Waggle server: ${error.message}`);
    process.exit(78);
  });

  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

main();
