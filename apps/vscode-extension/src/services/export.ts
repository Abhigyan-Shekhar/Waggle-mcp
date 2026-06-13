import * as path from "path";
import * as vscode from "vscode";
import { type WaggleContext } from "./context";

export async function exportMemory(ctx: WaggleContext): Promise<void> {
  const folder = ctx.workspaceFolder();
  const defaultUri = folder ? vscode.Uri.file(path.join(folder.uri.fsPath, "waggle-export.abhi")) : undefined;
  const target = await vscode.window.showSaveDialog({
    defaultUri,
    filters: {
      "ABHI Export": ["abhi"]
    },
    saveLabel: "Export Waggle Memory"
  });
  if (!target) {
    return;
  }

  ctx.showOutput();
  ctx.append(`Running: ${ctx.commandPath()} export --output ${target.fsPath}`);
  try {
    const result = await ctx.execFileAsync(ctx.commandPath(), ["export", "--output", target.fsPath], folder?.uri.fsPath);
    ctx.flushResult(result);
    if (result.code !== 0) {
      ctx.setStatus("error", "export failed");
      void vscode.window.showErrorMessage("Waggle export failed. See the output channel for details.");
      return;
    }
    void vscode.window.showInformationMessage(`Waggle memory exported to ${target.fsPath}.`);
  } catch (error) {
    ctx.setStatus("error", "export failed");
    ctx.append(`Export failed: ${String(error)}`);
    void vscode.window.showErrorMessage("Could not export Waggle memory.");
  }
}

export async function openInstallDocs(): Promise<void> {
  await vscode.env.openExternal(
    vscode.Uri.parse("https://github.com/Abhigyan-Shekhar/Waggle-mcp/tree/main/docs/install")
  );
}