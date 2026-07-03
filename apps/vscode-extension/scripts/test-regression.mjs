import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const distDir = join(scriptDir, "..", "dist");

const cacheVersion = await import(pathToFileURL(join(distDir, "cache-version.js")).href);
const serverPort = await import(pathToFileURL(join(distDir, "server-port.js")).href);

const { shouldScanFallbackCacheVersions, matchesRequestedCacheVersion } = cacheVersion;
const {
  canReuseManagedServer,
  isManagedChildAlive,
  nextPortIfOccupied,
  workspacePortStateKey
} = serverPort;

assert.equal(shouldScanFallbackCacheVersions(false), false);
assert.equal(shouldScanFallbackCacheVersions(true), true);
assert.equal(matchesRequestedCacheVersion("0.1.0", "0.1.0"), true);
assert.equal(matchesRequestedCacheVersion("0.1.1", "0.1.0"), false);

const deadChild = { exitCode: 1, signalCode: null };
assert.equal(isManagedChildAlive(deadChild), false);
assert.equal(canReuseManagedServer(deadChild, true), false);

const aliveChild = { exitCode: null, signalCode: null };
assert.equal(canReuseManagedServer(aliveChild, true), true);
assert.equal(canReuseManagedServer(aliveChild, false), false);

assert.equal(nextPortIfOccupied(18765, new Set([18765, 18766])), 18767);
assert.equal(nextPortIfOccupied(18765, new Set(Array.from({ length: 20 }, (_, i) => 18765 + i))), undefined);

const keyA = workspacePortStateKey("/workspace/a");
const keyB = workspacePortStateKey("/workspace/b");
assert.equal(keyA, workspacePortStateKey("/workspace/a"));
assert.notEqual(keyA, keyB);

console.log("regression helpers ok");
