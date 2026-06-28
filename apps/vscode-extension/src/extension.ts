import { type ChildProcess } from "child_process";
import * as vscode from "vscode";
import { type ExtensionState, type WaggleStatus } from "./types";
import { createContext } from "./services/context";
import {
  runDoctorInternal,
  installWaggle,
  onboardWaggle,
  maybePromptInstall,
} from "./services/install";
import { openGraphStudio } from "./services/studio";
import { exportMemory, openInstallDocs } from "./services/export";

export interface WaggleExtensionApi {
  getState: () => {
    status: WaggleStatus;
    graphStudioProcess: ChildProcess | undefined;
  };
}

export function activate(context: vscode.ExtensionContext): WaggleExtensionApi {
  const output = vscode.window.createOutputChannel("Waggle");
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "waggle.showStatus";
  const state: ExtensionState = {
    graphStudioProcess: undefined,
    status: "not-installed",
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
    vscode.commands.registerCommand("waggle.openInstallDocs", () => openInstallDocs()),
  );

  void maybePromptInstall(ctx);

  return {
    getState: () => ({ ...state }),
  };
}

export function deactivate(): void {}