import * as fs from "fs/promises";
import * as path from "path";
import * as vscode from "vscode";

export type McpRootKey = "servers" | "mcpServers";
export type JsonObject = Record<string, unknown>;

const WAGGLE_SERVER_NAME = "waggle";

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function resolveTenantId(folder: vscode.WorkspaceFolder | undefined, config: vscode.WorkspaceConfiguration): string {
  const configured = config.get<string>("tenantId", "${workspaceFolderBasename}");
  if (configured !== "${workspaceFolderBasename}") {
    return configured;
  }
  return folder?.name ?? "default";
}

export function buildWorkspaceServerConfig(
  command: string,
  tenantId: string,
  dbPath: string
): JsonObject {
  return {
    type: "stdio",
    command,
    args: ["serve", "--transport", "stdio"],
    env: {
      WAGGLE_DEFAULT_TENANT_ID: tenantId,
      WAGGLE_DB_PATH: dbPath,
      WAGGLE_STARTUP_MODE: "fast"
    }
  };
}

export async function parseJsonFile(filePath: string): Promise<JsonObject> {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    const parsed: unknown = JSON.parse(raw);
    if (!isJsonObject(parsed)) {
      throw new Error(`${filePath} must contain a JSON object at the top level.`);
    }
    return parsed;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return {};
    }
    throw error;
  }
}

export function determineRootKey(payload: JsonObject, preferred: McpRootKey): McpRootKey {
  if (isJsonObject(payload.servers)) {
    return "servers";
  }
  if (isJsonObject(payload.mcpServers)) {
    return "mcpServers";
  }
  return preferred;
}

export async function writeWorkspaceMcpConfig(options: {
  folder: vscode.WorkspaceFolder;
  command: string;
  tenantId: string;
  dbPath: string;
  mcpConfigScope: McpRootKey;
  appendLog: (message: string) => void;
}): Promise<boolean> {
  const filePath = path.join(options.folder.uri.fsPath, ".vscode", "mcp.json");
  const existing = await parseJsonFile(filePath);
  const rootKey = determineRootKey(existing, options.mcpConfigScope);
  const currentRoot = isJsonObject(existing[rootKey]) ? { ...(existing[rootKey] as JsonObject) } : {};
  const waggleConfig = buildWorkspaceServerConfig(options.command, options.tenantId, options.dbPath);
  const previousWaggle = currentRoot[WAGGLE_SERVER_NAME];
  const actionLabel = previousWaggle ? "Update Waggle Config" : "Write Waggle Config";
  const previewPayload: JsonObject = {
    [rootKey]: {
      [WAGGLE_SERVER_NAME]: waggleConfig
    }
  };

  options.appendLog(`Prepared ${actionLabel.toLowerCase()} for ${filePath}`);
  const choice = await vscode.window.showInformationMessage(
    previousWaggle
      ? "Waggle already exists in .vscode/mcp.json. Update only the Waggle block?"
      : "Review the Waggle MCP config before writing it to .vscode/mcp.json.",
    { modal: true, detail: JSON.stringify(previewPayload, null, 2) },
    actionLabel
  );
  if (choice !== actionLabel) {
    return false;
  }

  currentRoot[WAGGLE_SERVER_NAME] = waggleConfig;
  existing[rootKey] = currentRoot;
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const serialized = `${JSON.stringify(existing, null, 2)}\n`;
  JSON.parse(serialized);
  await fs.writeFile(filePath, serialized, "utf8");
  options.appendLog(`Wrote ${filePath}`);
  return true;
}
