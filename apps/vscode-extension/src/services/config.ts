import * as fs from "fs/promises";
import * as path from "path";
import * as vscode from "vscode";
import {
  type JsonObject,
  type McpRootKey,
  WAGGLE_SERVER_NAME,
  isJsonObject,
} from "../types";
import { type WaggleContext } from "./context";

export async function parseJsonFile(filePath: string): Promise<JsonObject> {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    const parsed: unknown = JSON.parse(raw);
    if (!isJsonObject(parsed)) {
      throw new Error("Expected JSON object at root of mcp.json");
    }
    return parsed;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return {};
    }
    throw error;
  }
}

export function determineRootKey(ctx: WaggleContext, payload: JsonObject): McpRootKey {
  if (isJsonObject(payload.servers)) {
    return "servers";
  }
  if (isJsonObject(payload.mcpServers)) {
    return "mcpServers";
  }
  return ctx.config().get<McpRootKey>("mcpConfigScope", "servers");
}

export async function writeWorkspaceConfig(ctx: WaggleContext): Promise<boolean> {
  const folder = ctx.workspaceFolder();
  if (!folder) {
    void vscode.window.showWarningMessage("Open a workspace folder before enabling Waggle for this workspace.");
    return false;
  }

  const filePath = path.join(folder.uri.fsPath, ".vscode", "mcp.json");
  let existing: JsonObject;
  try {
    existing = await parseJsonFile(filePath);
  } catch (error) {
    void vscode.window.showErrorMessage(`Cannot parse existing .vscode/mcp.json: ${String(error)}`);
    ctx.setStatus("error", "invalid mcp.json");
    return false;
  }

  const rootKey = determineRootKey(ctx, existing);
  const currentRoot = isJsonObject(existing[rootKey]) ? { ...(existing[rootKey] as JsonObject) } : {};
  const waggleConfig = ctx.buildWorkspaceServerConfig();
  const previousWaggle = currentRoot[WAGGLE_SERVER_NAME];
  const actionLabel = previousWaggle ? "Update Waggle Config" : "Write Waggle Config";
  const previewPayload: JsonObject = {
    [rootKey]: {
      [WAGGLE_SERVER_NAME]: waggleConfig
    }
  };
  const detail = JSON.stringify(previewPayload, null, 2);

  ctx.append(`Prepared ${actionLabel.toLowerCase()} for ${filePath}`);
  const choice = await vscode.window.showInformationMessage(
    previousWaggle
      ? "Waggle already exists in .vscode/mcp.json. Update only the Waggle block?"
      : "Review the Waggle MCP config before writing it to .vscode/mcp.json.",
    { modal: true, detail },
    actionLabel
  );
  if (choice !== actionLabel) {
    return false;
  }

  currentRoot[WAGGLE_SERVER_NAME] = waggleConfig;
  existing[rootKey] = currentRoot;

  try {
    await fs.mkdir(path.dirname(filePath), { recursive: true });
    const serialized = `${JSON.stringify(existing, null, 2)}\n`;
    await fs.writeFile(filePath, serialized, "utf8");
  } catch (err) {
    ctx.append(`Failed to write ${filePath}: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  }

  ctx.append(`Wrote ${filePath}`);
  ctx.setStatus("ready", folder.name);
  return true;
}