import { spawn, type ChildProcess } from "child_process";
import * as vscode from "vscode";
import { GRAPH_STUDIO_URL } from "../types";
import { type WaggleContext } from "./context";

export function attachProcessLogging(ctx: WaggleContext, child: ChildProcess, label: string): void {
  child.stdout?.on("data", (chunk: Buffer | string) => ctx.output.append(String(chunk)));
  child.stderr?.on("data", (chunk: Buffer | string) => ctx.output.append(String(chunk)));
  child.on("error", (error) => {
    ctx.setStatus("error", "graph studio failed");
    ctx.append(`${label} failed to start: ${String(error)}`);
    if (ctx.state.graphStudioProcess === child) {
      ctx.state.graphStudioProcess = undefined;
    }
    void vscode.window.showErrorMessage("Could not start Waggle Graph Studio.");
  });
  child.on("exit", (code) => {
    ctx.append(`${label} exited with code ${String(code ?? 0)}.`);
    if (ctx.state.graphStudioProcess === child) {
      ctx.state.graphStudioProcess = undefined;
      void ctx.updateStatusFromEnvironment();
    }
  });
}

export async function openGraphStudio(ctx: WaggleContext): Promise<void> {
  const available = await ctx.updateStatusFromEnvironment();
  if (!available) {
    void vscode.window.showErrorMessage("Waggle is not installed. Enable Waggle first.");
    return;
  }
  if (ctx.state.graphStudioProcess) {
    ctx.setStatus("connected", "Graph Studio");
    await vscode.env.openExternal(vscode.Uri.parse(GRAPH_STUDIO_URL));
    return;
  }

  ctx.showOutput();
  ctx.append(`Starting: ${ctx.commandPath()} graph-studio --host 127.0.0.1 --port 8686 --no-open`);
  try {
    const child = spawn(ctx.commandPath(), ["graph-studio", "--host", "127.0.0.1", "--port", "8686", "--no-open"], {
      cwd: ctx.workspaceFolder()?.uri.fsPath,
      detached: false,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true
    });
    ctx.state.graphStudioProcess = child;
    attachProcessLogging(ctx, child, "Graph Studio");
    ctx.setStatus("connected", "Graph Studio");
    setTimeout(() => {
      void vscode.env.openExternal(vscode.Uri.parse(GRAPH_STUDIO_URL));
    }, 1200);
  } catch (error) {
    ctx.setStatus("error", "graph studio failed");
    ctx.append(`Graph Studio failed to start: ${String(error)}`);
    void vscode.window.showErrorMessage("Could not start Waggle Graph Studio.");
  }
}