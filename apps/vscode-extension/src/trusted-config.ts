import * as vscode from "vscode";

const SENSITIVE_KEYS = new Set([
  "installMethod",
  "commandPath",
  "binaryReleaseRepo",
  "binaryVersion",
  "binaryAllowLatestFallback"
]);

export function isWorkspaceTrusted(): boolean {
  return vscode.workspace.isTrusted;
}

export async function requireWorkspaceTrust(action: string): Promise<boolean> {
  if (vscode.workspace.isTrusted) {
    return true;
  }
  const choice = await vscode.window.showWarningMessage(
    `Waggle cannot ${action} in an untrusted workspace. Trust this folder first.`,
    "Manage Workspace Trust"
  );
  if (choice === "Manage Workspace Trust") {
    await vscode.commands.executeCommand("workbench.action.manageTrust");
  }
  return false;
}

/** User/global-only for settings that affect binary download/spawn (workspace overrides ignored). */
export function getTrustedSetting<T>(key: string, defaultValue: T): T {
  const inspect = vscode.workspace.getConfiguration("waggle").inspect<T>(key);
  if (SENSITIVE_KEYS.has(key)) {
    if (inspect?.globalValue !== undefined) {
      return inspect.globalValue;
    }
    return (inspect?.defaultValue as T | undefined) ?? defaultValue;
  }
  if (!vscode.workspace.isTrusted) {
    if (inspect?.globalValue !== undefined) {
      return inspect.globalValue;
    }
    if (inspect?.workspaceValue !== undefined) {
      return defaultValue;
    }
  }
  return vscode.workspace.getConfiguration("waggle").get<T>(key, defaultValue);
}

export function workspaceOverridesSensitiveSetting(key: string): boolean {
  if (!SENSITIVE_KEYS.has(key)) {
    return false;
  }
  const inspect = vscode.workspace.getConfiguration("waggle").inspect(key);
  return inspect?.workspaceValue !== undefined;
}
