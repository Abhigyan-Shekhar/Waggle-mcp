import * as vscode from "vscode";
import { type WaggleContext } from "./context";
import { writeWorkspaceConfig } from "./config";
import { openInstallDocs } from "./export";

export async function runDoctorInternal(ctx: WaggleContext, showSuccessMessage = true): Promise<boolean> {
  ctx.showOutput();
  ctx.append(`Running: ${ctx.commandPath()} doctor`);
  try {
    const result = await ctx.execFileAsync(ctx.commandPath(), ["doctor"], ctx.workspaceFolder()?.uri.fsPath);
    ctx.flushResult(result);
    if (result.code === 0) {
      ctx.setStatus(ctx.state.graphStudioProcess ? "connected" : "ready", "doctor ok");
      if (showSuccessMessage) {
        void vscode.window.showInformationMessage("Waggle doctor completed successfully.");
      }
      return true;
    }
    ctx.setStatus("error", "doctor warnings");
    void vscode.window.showWarningMessage("Waggle doctor reported issues. See the Waggle output channel for details.");
    return false;
  } catch (error) {
    ctx.setStatus("error", "doctor failed");
    ctx.append(`Doctor failed: ${String(error)}`);
    void vscode.window.showErrorMessage("Could not run waggle-mcp doctor.");
    return false;
  }
}

export async function installWaggle(ctx: WaggleContext, showPostInstallMessage = true): Promise<boolean> {
  const method = ctx.config().get<string>("installMethod", "pipx");
  if (method !== "pipx") {
    void vscode.window.showInformationMessage("Binary install support is reserved for a later Waggle extension update.");
    return false;
  }

  ctx.showOutput();
  ctx.append("Running: pipx install waggle-mcp");
  try {
    const result = await ctx.execFileAsync("pipx", ["install", "waggle-mcp"], ctx.workspaceFolder()?.uri.fsPath);
    ctx.flushResult(result);
    if (result.code !== 0) {
      ctx.setStatus("error", "install failed");
      void vscode.window.showErrorMessage("Waggle install failed. See the Waggle output channel for details.");
      return false;
    }
    ctx.append("Waggle installed successfully.");
    await ctx.updateStatusFromEnvironment();
    if (showPostInstallMessage) {
      void vscode.window.showInformationMessage("Waggle installed successfully.");
    }
    return true;
  } catch (error) {
    ctx.setStatus("error", "install failed");
    ctx.append(`Install failed: ${String(error)}`);
    void vscode.window.showErrorMessage("Waggle install failed. Ensure pipx is installed and available on PATH.");
    return false;
  }
}

export async function onboardWaggle(ctx: WaggleContext): Promise<void> {
  const folder = ctx.workspaceFolder();
  if (!folder) {
    void vscode.window.showWarningMessage("Open a workspace folder before running Waggle setup.");
    return;
  }

  const proceed = await vscode.window.showInformationMessage(
    "Enable Waggle for this workspace? This will install the Waggle CLI if needed, write .vscode/mcp.json after confirmation, and run waggle-mcp doctor.",
    { modal: true },
    "Enable Waggle"
  );
  if (proceed !== "Enable Waggle") {
    return;
  }

  const available = await ctx.updateStatusFromEnvironment();
  if (!available) {
    const installed = await installWaggle(ctx, false);
    if (!installed) {
      return;
    }
  }

  const configured = await writeWorkspaceConfig(ctx);
  if (!configured) {
    return;
  }

  const doctorOk = await runDoctorInternal(ctx, false);
  if (doctorOk) {
    ctx.setStatus("connected", folder.name);
    void vscode.window.showInformationMessage("Waggle is installed, configured, and ready for this workspace.");
    return;
  }
  void vscode.window.showWarningMessage("Waggle was installed and configured, but doctor reported issues. See the Waggle output channel.");
}

export async function maybePromptInstall(ctx: WaggleContext): Promise<void> {
  const available = await ctx.updateStatusFromEnvironment();
  if (available) {
    return;
  }
  const choice = await vscode.window.showInformationMessage(
    "Waggle is not set up in this VS Code workspace. Enable it now?",
    "Enable Waggle",
    "Open Docs"
  );
  if (choice === "Enable Waggle") {
    await onboardWaggle(ctx);
    return;
  }
  if (choice === "Open Docs") {
    await openInstallDocs();
  }
}