import * as fs from "fs";
import * as path from "path";

export type PlatformAssetKey = "win32-x64" | "darwin-arm64" | "darwin-x64" | "linux-x64";

const ASSET_MAP_PATH = path.join(__dirname, "..", "scripts", "asset-map.json");

function normalizeArch(arch: string): "x64" | "arm64" {
  if (arch === "x64" || arch === "arm64") {
    return arch;
  }
  throw new Error(`Unsupported architecture: ${arch}. Only x64 and arm64 are supported.`);
}

export function resolvePlatformAssetKey(platform: string, arch: string): PlatformAssetKey {
  const normalizedArch = normalizeArch(arch);
  if (platform === "win32") {
    if (normalizedArch === "arm64") {
      throw new Error(
        "Windows ARM64 is not supported yet. Use waggle.installMethod pipx with waggle.commandPath, or an x64 Windows build."
      );
    }
    return "win32-x64";
  }
  if (platform === "darwin") {
    return normalizedArch === "arm64" ? "darwin-arm64" : "darwin-x64";
  }
  if (platform === "linux") {
    if (normalizedArch === "arm64") {
      throw new Error(
        "Linux ARM64 is not supported yet. Use waggle.installMethod pipx with waggle.commandPath, or an x64 Linux build."
      );
    }
    return "linux-x64";
  }
  throw new Error(`Unsupported platform: ${platform} ${arch}`);
}

export function platformAssetKey(): PlatformAssetKey {
  return resolvePlatformAssetKey(process.platform, process.arch);
}

export function loadAssetMap(): Record<PlatformAssetKey, string> {
  const raw = fs.readFileSync(ASSET_MAP_PATH, "utf8");
  return JSON.parse(raw) as Record<PlatformAssetKey, string>;
}

export function assetFileNameForCurrentPlatform(): string {
  const key = platformAssetKey();
  const map = loadAssetMap();
  const name = map[key];
  if (!name) {
    throw new Error(`No release binary mapped for ${key}`);
  }
  return name;
}

export function buildAssetNameToPlatformKeyMap(): Map<string, PlatformAssetKey> {
  const map = loadAssetMap();
  const inverse = new Map<string, PlatformAssetKey>();
  for (const [key, name] of Object.entries(map)) {
    inverse.set(name, key as PlatformAssetKey);
  }
  return inverse;
}
