import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { curatedSpawnEnv } from "./spawn-env";

const MCP_TIMEOUT_MS = 30_000;

function withTimeout<T>(promise: Promise<T>, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`${label} timed out after ${MCP_TIMEOUT_MS / 1000}s`)),
      MCP_TIMEOUT_MS
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

export async function callStdioTool(
  command: string,
  env: Record<string, string>,
  toolName: string,
  args: Record<string, unknown>
): Promise<unknown> {
  const transport = new StdioClientTransport({
    command,
    args: ["serve", "--transport", "stdio"],
    env: curatedSpawnEnv(env),
    stderr: "pipe"
  });
  const client = new Client({ name: "waggle-vscode", version: "1.0.0" }, { capabilities: {} });
  try {
    await withTimeout(client.connect(transport), "MCP connect");
    return await withTimeout(
      client.callTool({ name: toolName, arguments: args }),
      "MCP callTool"
    );
  } finally {
    await transport.close();
  }
}
