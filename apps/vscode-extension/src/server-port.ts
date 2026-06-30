import { createHash } from "crypto";
import type { ChildProcess } from "child_process";

export const DEFAULT_HTTP_PORT = 18_765;
export const MAX_PORT_ATTEMPTS = 20;

export function workspacePortStateKey(workspacePath?: string): string {
  if (!workspacePath) {
    return "waggle.httpPort.default";
  }
  const digest = createHash("sha256").update(workspacePath).digest("hex").slice(0, 16);
  return `waggle.httpPort.${digest}`;
}

export function isManagedChildAlive(child: ChildProcess | undefined): boolean {
  return Boolean(child && child.exitCode === null && child.signalCode === null);
}

export function canReuseManagedServer(child: ChildProcess | undefined, portHealthy: boolean): boolean {
  return isManagedChildAlive(child) && portHealthy;
}

/** Pick the next port when the preferred one is occupied by a foreign process. */
export function nextPortIfOccupied(
  preferredPort: number,
  occupiedPorts: Set<number>,
  maxAttempts = MAX_PORT_ATTEMPTS
): number | undefined {
  for (let offset = 0; offset < maxAttempts; offset++) {
    const candidate = preferredPort + offset;
    if (!occupiedPorts.has(candidate)) {
      return candidate;
    }
  }
  return undefined;
}
