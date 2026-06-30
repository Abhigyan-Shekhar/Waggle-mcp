import { execFile, spawn, type ChildProcess } from "child_process";
import * as fs from "fs/promises";
import * as http from "http";
import * as path from "path";
import * as vscode from "vscode";
import { BinaryResolver } from "./binary-resolver";
import { curatedSpawnEnv, spawnNotFoundMessage } from "./spawn-env";
import {
  canReuseManagedServer,
  DEFAULT_HTTP_PORT,
  isManagedChildAlive,
  nextPortIfOccupied,
  workspacePortStateKey
} from "./server-port";

export interface ServerRuntime {
  baseUrl: string;
  port: number;
}

const STOP_TIMEOUT_MS = 8_000;

export class ServerManager {
  private child: ChildProcess | undefined;
  private port = 0;
  private startPromise: Promise<ServerRuntime> | undefined;
  private readonly onDidChangeEmitter = new vscode.EventEmitter<ServerRuntime | undefined>();
  readonly onDidChange = this.onDidChangeEmitter.event;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly resolver: BinaryResolver,
    private readonly log: (message: string) => void
  ) {}

  get runtime(): ServerRuntime | undefined {
    if (!this.port || !this.child) {
      return undefined;
    }
    return {
      baseUrl: `http://127.0.0.1:${this.port}`,
      port: this.port
    };
  }

  async start(env: Record<string, string>, cwd?: string): Promise<ServerRuntime> {
    if (!vscode.workspace.isTrusted) {
      throw new Error("Cannot start Waggle server in an untrusted workspace. Trust this folder first.");
    }
    if (this.startPromise) {
      return this.startPromise;
    }

    this.startPromise = this.startInternal(env, cwd);
    try {
      return await this.startPromise;
    } finally {
      this.startPromise = undefined;
    }
  }

  private async startInternal(env: Record<string, string>, cwd?: string): Promise<ServerRuntime> {
    if (canReuseManagedServer(this.child, await this.probe(this.port))) {
      return { baseUrl: `http://127.0.0.1:${this.port}`, port: this.port };
    }

    if (this.child) {
      await this.stop();
    }

    const command = await this.resolver.resolveCommandPath();
    await this.validateCommandPath(command);

    this.port = await this.allocatePort(cwd);
    const args = ["edit-graph", "--host", "127.0.0.1", "--port", String(this.port), "--no-open"];
    this.log(`Starting ${command} ${args.join(" ")}`);

    const child = await this.spawnManagedProcess(command, args, env, cwd);
    this.child = child;

    const healthy = await this.waitForHealthy(this.port, child, 60_000);
    if (!healthy) {
      await this.stop();
      const exitCode = child.exitCode;
      if (exitCode !== null && exitCode !== 0) {
        throw new Error(`Waggle server exited before becoming healthy (code ${exitCode})`);
      }
      throw new Error(`Waggle server did not become healthy on port ${this.port}`);
    }

    await this.context.workspaceState.update(workspacePortStateKey(cwd), this.port);
    const runtime = { baseUrl: `http://127.0.0.1:${this.port}`, port: this.port };
    this.onDidChangeEmitter.fire(runtime);
    return runtime;
  }

  private async allocatePort(cwd?: string): Promise<number> {
    const stateKey = workspacePortStateKey(cwd);
    const preferred = this.context.workspaceState.get<number>(stateKey, DEFAULT_HTTP_PORT);
    const occupied = new Set<number>();

    for (let offset = 0; offset < 20; offset++) {
      const candidate = preferred + offset;
      if (await this.probe(candidate)) {
        if (isManagedChildAlive(this.child) && this.port === candidate) {
          return candidate;
        }
        occupied.add(candidate);
      }
    }

    const resolved = nextPortIfOccupied(preferred, occupied);
    if (resolved === undefined) {
      throw new Error(`No free port found near ${preferred} for Waggle server.`);
    }
    return resolved;
  }

  async restart(env: Record<string, string>, cwd?: string): Promise<ServerRuntime> {
    await this.stop();
    return this.start(env, cwd);
  }

  async stop(): Promise<void> {
    const child = this.child;
    this.child = undefined;
    if (!child?.pid) {
      this.onDidChangeEmitter.fire(undefined);
      return;
    }

    if (process.platform === "win32") {
      await this.stopWindowsProcess(child);
    } else {
      child.kill("SIGTERM");
      await this.waitForChildExit(child, STOP_TIMEOUT_MS);
    }
    this.onDidChangeEmitter.fire(undefined);
  }

  private async validateCommandPath(command: string): Promise<void> {
    if (path.isAbsolute(command) || command.includes(path.sep) || command.includes("/")) {
      await fs.access(command);
    }
  }

  private spawnManagedProcess(
    command: string,
    args: string[],
    env: Record<string, string>,
    cwd?: string
  ): Promise<ChildProcess> {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (handler: () => void) => {
        if (settled) {
          return;
        }
        settled = true;
        handler();
      };

      const child = spawn(command, args, {
        cwd,
        env: curatedSpawnEnv(env),
        detached: false,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true
      });

      child.stdout?.on("data", (chunk) => this.log(String(chunk).trimEnd()));
      child.stderr?.on("data", (chunk) => this.log(String(chunk).trimEnd()));

      child.once("spawn", () => {
        finish(() => resolve(child));
      });

      child.once("error", (error) => {
        if (this.child === child) {
          this.child = undefined;
          this.onDidChangeEmitter.fire(undefined);
        }
        finish(() => {
          const message = error.message.includes("ENOENT")
            ? spawnNotFoundMessage(command)
            : `Failed to start Waggle server (${command}): ${error.message}`;
          reject(new Error(message));
        });
      });

      child.on("exit", (code) => {
        this.log(`edit-graph exited (${String(code ?? 0)}).`);
        if (this.child === child) {
          this.child = undefined;
          this.onDidChangeEmitter.fire(undefined);
        }
      });
    });
  }

  private waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<void> {
    if (child.exitCode !== null || child.signalCode !== null) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(), timeoutMs);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  private stopWindowsProcess(child: ChildProcess): Promise<void> {
    return new Promise((resolve) => {
      execFile(
        "taskkill",
        ["/pid", String(child.pid), "/T", "/F"],
        { windowsHide: true },
        () => {
          void this.waitForChildExit(child, STOP_TIMEOUT_MS).then(() => resolve());
        }
      );
    });
  }

  private async waitForHealthy(port: number, child: ChildProcess, timeoutMs = 30_000): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (!isManagedChildAlive(child)) {
        return false;
      }
      if (await this.probe(port)) {
        return isManagedChildAlive(child);
      }
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    return false;
  }

  private probe(port: number): Promise<boolean> {
    return new Promise((resolve) => {
      const request = http.get(`http://127.0.0.1:${port}/health/live`, (response) => {
        resolve(response.statusCode === 200);
        response.resume();
      });
      request.on("error", () => resolve(false));
      request.setTimeout(2000, () => {
        request.destroy();
        resolve(false);
      });
    });
  }

  async readPortFile(): Promise<number | undefined> {
    try {
      const filePath = path.join(this.context.globalStorageUri.fsPath, "waggle-port.json");
      const raw = await fs.readFile(filePath, "utf8");
      const payload = JSON.parse(raw) as { port?: number };
      return typeof payload.port === "number" ? payload.port : undefined;
    } catch {
      return undefined;
    }
  }

  async writePortFile(port: number): Promise<void> {
    const filePath = path.join(this.context.globalStorageUri.fsPath, "waggle-port.json");
    await fs.mkdir(path.dirname(filePath), { recursive: true });
    await fs.writeFile(filePath, `${JSON.stringify({ port })}\n`, "utf8");
  }
}
