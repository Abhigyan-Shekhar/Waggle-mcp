import { execFile } from "child_process";
import * as vscode from "vscode";
import {
  type WaggleStatus,
  type JsonObject,
  type CommandResult,
  type ExtensionState,
  DEFAULT_COMMAND,
  DEFAULT_DB_PATH,
} from "../types";

/**
 * Shared context passed to every service. Holds the output channel, status
 * bar, mutable extension state, and the common helpers that were previously
 * closures inside activate(). Receiving this as a parameter is the test seam:
 * a fake context lets services be exercised without the extension host.
 */
export interface WaggleContext {
  readonly output: vscode.OutputChannel;
  readonly statusBar: vscode.StatusBarItem;
  readonly state: ExtensionState;
  append(message: string): void;
  config(): vscode.WorkspaceConfiguration;
  workspaceFolder(): vscode.WorkspaceFolder | undefined;
  commandPath(): string;
  resolveTenantId(): string;
  setStatus(status: WaggleStatus, detail?: string): void;
  buildWorkspaceServerConfig(): JsonObject;
  execFileAsync(command: string, args: string[], cwd?: string): Promise<CommandResult>;
  showOutput(): void;
  flushResult(result: CommandResult): void;
  updateStatusFromEnvironment(): Promise<boolean>;
}

export function createContext(
  output: vscode.OutputChannel,
  statusBar: vscode.StatusBarItem,
  state: ExtensionState
): WaggleContext {
  const append = (message: string): void => {
    output.appendLine(`[waggle] ${message}`);
  };

  const config = (): vscode.WorkspaceConfiguration => vscode.workspace.getConfiguration("waggle");
  const workspaceFolder = (): vscode.WorkspaceFolder | undefined => vscode.workspace.workspaceFolders?.[0];
  const commandPath = (): string => config().get<string>("commandPath", DEFAULT_COMMAND);

  const resolveTenantId = (): string => {
    const configured = config().get<string>("tenantId", "${workspaceFolderBasename}");
    if (configured !== "${workspaceFolderBasename}") {
      return configured;
    }
    return workspaceFolder()?.name ?? "default";
  };

  const setStatus = (status: WaggleStatus, detail = ""): void => {
    state.status = status;
    const suffix = detail ? `: ${detail}` : "";
    const labels: Record<WaggleStatus, string> = {
      "not-installed": `Waggle: Not Installed${suffix}`,
      ready: `Waggle: Ready${suffix}`,
      connected: `Waggle: Connected${suffix}`,
      error: `Waggle: Error${suffix}`
    };
    statusBar.text = labels[status];
    statusBar.show();
  };

  const buildWorkspaceServerConfig = (): JsonObject => ({
    type: "stdio",
    command: commandPath(),
    args: ["serve", "--transport", "stdio"],
    env: {
      WAGGLE_DEFAULT_TENANT_ID: resolveTenantId(),
      WAGGLE_DB_PATH: config().get<string>("dbPath", DEFAULT_DB_PATH)
    }
  });

  const execFileAsync = async (command: string, args: string[], cwd?: string): Promise<CommandResult> =>
    await new Promise((resolve, reject) => {
      execFile(command, args, { cwd, windowsHide: true, timeout: 15000 }, (error, stdout, stderr) => {
        const numericCode = (error as NodeJS.ErrnoException | null)?.code;
        const code = typeof numericCode === "number" ? numericCode : 0;
        if (error && typeof (error as NodeJS.ErrnoException).code !== "number") {
          reject(error);
          return;
        }
        resolve({ code, stdout, stderr });
      });
    });

  const showOutput = (): void => output.show(true);

  const flushResult = (result: CommandResult): void => {
    if (result.stdout.trim()) {
      output.append(result.stdout);
    }
    if (result.stderr.trim()) {
      output.append(result.stderr);
    }
  };

  const updateStatusFromEnvironment = async (): Promise<boolean> => {
    try {
      const result = await execFileAsync(commandPath(), ["--version"]);
      if (result.code === 0) {
        setStatus(state.graphStudioProcess ? "connected" : "ready", result.stdout.trim());
        return true;
      }
      setStatus("error", "version check failed");
      flushResult(result);
      return false;
    } catch (error) {
      setStatus("not-installed");
      append(`CLI not available: ${String(error)}`);
      return false;
    }
  };

  return {
    output,
    statusBar,
    state,
    append,
    config,
    workspaceFolder,
    commandPath,
    resolveTenantId,
    setStatus,
    buildWorkspaceServerConfig,
    execFileAsync,
    showOutput,
    flushResult,
    updateStatusFromEnvironment,
  };
}