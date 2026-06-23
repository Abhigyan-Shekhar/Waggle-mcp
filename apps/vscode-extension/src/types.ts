import { type ChildProcess } from "child_process";

export type WaggleStatus = "not-installed" | "ready" | "connected" | "error";
export type McpRootKey = "servers" | "mcpServers";
export type JsonObject = Record<string, unknown>;

export const OUTPUT_CHANNEL = "Waggle";
export const WAGGLE_SERVER_NAME = "waggle";
export const DEFAULT_COMMAND = "waggle-mcp";
export const DEFAULT_DB_PATH = "~/.waggle/waggle.db";
export const GRAPH_STUDIO_URL = "http://127.0.0.1:8686/graph?mode=edit";

export interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface ExtensionState {
  graphStudioProcess: ChildProcess | undefined;
  status: WaggleStatus;
}

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}