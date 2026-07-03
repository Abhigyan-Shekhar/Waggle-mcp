import * as vscode from "vscode";
import { type ExtensionState } from "./types";
import { createContext } from "./services/context";
import {
  runDoctorInternal,
  installWaggle,
  onboardWaggle,
  maybePromptInstall,
} from "./services/install";
import { openGraphStudio } from "./services/studio";
import { exportMemory, openInstallDocs } from "./services/export";

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Waggle");
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "waggle.showStatus";
  const state: ExtensionState = {
    graphStudioProcess: undefined,
    status: "not-installed"
  };
  context.subscriptions.push(output, statusBar, new vscode.Disposable(() => state.graphStudioProcess?.kill()));

  const ctx = createContext(output, statusBar, state);

  const showStatus = async (): Promise<void> => {
    await ctx.updateStatusFromEnvironment();
    ctx.showOutput();
    ctx.append(`Status: ${statusBar.text}`);
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("waggle.enableWorkspace", () => onboardWaggle(ctx)),
    vscode.commands.registerCommand("waggle.install", () => installWaggle(ctx)),
    vscode.commands.registerCommand("waggle.doctor", () => runDoctorInternal(ctx)),
    vscode.commands.registerCommand("waggle.openGraphStudio", () => openGraphStudio(ctx)),
    vscode.commands.registerCommand("waggle.showStatus", showStatus),
    vscode.commands.registerCommand("waggle.exportMemory", () => exportMemory(ctx)),
    vscode.commands.registerCommand("waggle.openInstallDocs", () => openInstallDocs())
  );

  void maybePromptInstall(ctx);
}

export function deactivate(): void {}