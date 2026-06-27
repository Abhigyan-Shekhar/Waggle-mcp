import { defineConfig } from "@vscode/test-cli";

export default defineConfig({
  files: "dist/test/**/*.test.js",
  version: "1.124.2",
  workspaceFolder: "./test-workspace",
  mocha: {
    ui: "bdd"
  }
});
