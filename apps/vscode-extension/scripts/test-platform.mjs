import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const assetMap = JSON.parse(readFileSync(join(scriptDir, "asset-map.json"), "utf8"));

assert.equal(assetMap["win32-x64"], "waggle-mcp-windows-x64.exe");
assert.equal(assetMap["linux-x64"], "waggle-mcp-linux-x64");
assert.equal(assetMap["darwin-arm64"], "waggle-mcp-darwin-arm64");
assert.equal(assetMap["darwin-x64"], "waggle-mcp-darwin-x64");

const platformModule = await import(pathToFileURL(join(scriptDir, "..", "dist", "platform.js")).href);
const { resolvePlatformAssetKey, platformAssetKey } = platformModule;

assert.equal(resolvePlatformAssetKey("darwin", "arm64"), "darwin-arm64");
assert.equal(resolvePlatformAssetKey("linux", "x64"), "linux-x64");
assert.throws(() => resolvePlatformAssetKey("win32", "arm64"), /Windows ARM64/);
assert.throws(() => resolvePlatformAssetKey("linux", "arm64"), /Linux ARM64/);
assert.throws(() => resolvePlatformAssetKey("linux", "ia32"), /Unsupported architecture/);
assert.equal(typeof platformAssetKey(), "string");

console.log("platform asset map ok");
