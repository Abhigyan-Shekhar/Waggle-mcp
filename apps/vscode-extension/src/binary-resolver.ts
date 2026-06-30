import { createHash } from "crypto";
import { createWriteStream } from "fs";
import * as fs from "fs/promises";
import * as https from "https";
import * as path from "path";
import * as vscode from "vscode";
import { getTrustedSetting } from "./trusted-config";
import { shouldScanFallbackCacheVersions } from "./cache-version";
import {
  assetFileNameForCurrentPlatform,
  buildAssetNameToPlatformKeyMap,
  platformAssetKey,
  type PlatformAssetKey
} from "./platform";

export interface BundleMetadata {
  version: string;
  repository: string;
  assets: Record<string, string>;
  checksums?: Record<string, string>;
}

const DEFAULT_REPO = "Abhigyan-Shekhar/Waggle-mcp";
const NETWORK_TIMEOUT_MS = 60_000;
const MAX_REDIRECTS = 5;
const MAX_BINARY_BYTES = 300 * 1024 * 1024;

export class BinaryResolver {
  constructor(private readonly context: vscode.ExtensionContext) {}

  private cacheRoot(): string {
    return path.join(this.context.globalStorageUri.fsPath, "waggle-bin");
  }

  async resolveCommandPath(): Promise<string> {
    const method = getTrustedSetting<string>("installMethod", "binary");
    const configured = getTrustedSetting<string>("commandPath", "waggle-mcp");
    if (method === "pipx" || method === "commandPath") {
      return configured;
    }
    if (await this.hasCachedBinary()) {
      return await this.cachedBinaryPath();
    }
    if (path.isAbsolute(configured)) {
      try {
        await fs.access(configured);
        return configured;
      } catch {
        // fall through to download
      }
    }
    return this.ensureBinary();
  }

  async hasCachedBinary(): Promise<boolean> {
    return (await this.resolveCachedBinaryPath()) !== undefined;
  }

  private async cachedBinaryPathForVersion(version: string): Promise<string> {
    const assetName = assetFileNameForCurrentPlatform();
    return path.join(this.cacheRoot(), version, assetName);
  }

  private async cachedBinaryPath(): Promise<string> {
    const discovered = await this.resolveCachedBinaryPath();
    if (discovered) {
      return discovered;
    }
    return this.cachedBinaryPathForVersion(await this.resolveRequestedVersion());
  }

  private metadataSidecarPath(binaryPath: string): string {
    return `${binaryPath}.metadata.json`;
  }

  private async readMetadataSidecar(binaryPath: string): Promise<BundleMetadata | undefined> {
    try {
      const raw = await fs.readFile(this.metadataSidecarPath(binaryPath), "utf8");
      const parsed = JSON.parse(raw) as BundleMetadata;
      if (parsed?.version && parsed.checksums) {
        return parsed;
      }
    } catch {
      return undefined;
    }
    return undefined;
  }

  private async writeMetadataSidecar(binaryPath: string, metadata: BundleMetadata): Promise<void> {
    await fs.writeFile(
      this.metadataSidecarPath(binaryPath),
      JSON.stringify({
        version: metadata.version,
        repository: metadata.repository,
        assets: metadata.assets,
        checksums: metadata.checksums
      }),
      "utf8"
    );
  }

  private async resolveCachedBinaryPath(): Promise<string | undefined> {
    const requested = await this.resolveRequestedVersion();
    const requestedPath = await this.cachedBinaryPathForVersion(requested);
    try {
      await fs.access(requestedPath);
      await this.verifyCachedBinary(requestedPath);
      return requestedPath;
    } catch {
      // fall through — binary may be cached under a fallback release version
    }

    if (!shouldScanFallbackCacheVersions(getTrustedSetting<boolean>("binaryAllowLatestFallback", false))) {
      return undefined;
    }

    try {
      const versions = await fs.readdir(this.cacheRoot());
      const assetName = assetFileNameForCurrentPlatform();
      for (const version of versions) {
        const candidate = path.join(this.cacheRoot(), version, assetName);
        try {
          await fs.access(candidate);
          await this.verifyCachedBinary(candidate);
          return candidate;
        } catch {
          // try next cached version
        }
      }
    } catch {
      return undefined;
    }
    return undefined;
  }

  async ensureBinary(): Promise<string> {
    if (!vscode.workspace.isTrusted) {
      throw new Error("Cannot download Waggle binary in an untrusted workspace. Trust this folder first.");
    }
    const metadata = await this.fetchBundleMetadata();
    const destPath = await this.cachedBinaryPathForVersion(metadata.version);
    try {
      await fs.access(destPath);
      await this.verifyCachedBinary(destPath, metadata);
      await this.writeMetadataSidecar(destPath, metadata);
      return destPath;
    } catch {
      // download below
    }

    const assetName = metadata.assets[platformAssetKey()];
    if (!assetName) {
      throw new Error(`Release v${metadata.version} has no asset for ${platformAssetKey()}`);
    }

    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Waggle",
        cancellable: false
      },
      async () => {
        await fs.mkdir(path.dirname(destPath), { recursive: true });
        const url = `https://github.com/${metadata.repository}/releases/download/v${metadata.version}/${assetName}`;
        await this.downloadFile(url, destPath);
        await this.verifyCachedBinary(destPath, metadata);
        await this.writeMetadataSidecar(destPath, metadata);
        if (process.platform !== "win32") {
          await fs.chmod(destPath, 0o755);
        }
      }
    );

    return destPath;
  }

  private async resolveRequestedVersion(): Promise<string> {
    const override = getTrustedSetting<string>("binaryVersion", "").trim();
    if (override) {
      return override;
    }
    return this.context.extension.packageJSON.version as string;
  }

  private async fetchBundleMetadata(): Promise<BundleMetadata> {
    const repo = getTrustedSetting<string>("binaryReleaseRepo", DEFAULT_REPO).trim() || DEFAULT_REPO;
    const requestedVersion = await this.resolveRequestedVersion();

    const fromRelease = await this.tryFetchReleaseMetadata(repo, requestedVersion);
    if (fromRelease) {
      return fromRelease;
    }

    if (getTrustedSetting<boolean>("binaryAllowLatestFallback", false)) {
      const latest = await this.tryFetchLatestMetadata(repo);
      if (latest) {
        return latest;
      }
    }

    throw new Error(
      `No GitHub release v${requestedVersion} with Waggle binaries for ${platformAssetKey()}. ` +
        "Set waggle.commandPath, waggle.installMethod to pipx, enable waggle.binaryAllowLatestFallback, or wait for a maintainer release."
    );
  }

  private async tryFetchReleaseMetadata(repo: string, version: string): Promise<BundleMetadata | undefined> {
    try {
      const release = await this.fetchJson<{
        tag_name: string;
        assets: { name: string; browser_download_url: string }[];
      }>(`https://api.github.com/repos/${repo}/releases/tags/v${version}`);
      const fromAsset = await this.tryParseBundleMetadataAsset(release.assets);
      if (fromAsset) {
        return fromAsset;
      }
      return this.metadataFromRelease(release, repo);
    } catch {
      return undefined;
    }
  }

  private async tryFetchLatestMetadata(repo: string): Promise<BundleMetadata | undefined> {
    try {
      const release = await this.fetchJson<{
        tag_name: string;
        assets: { name: string; browser_download_url: string }[];
      }>(`https://api.github.com/repos/${repo}/releases/latest`);
      const fromAsset = await this.tryParseBundleMetadataAsset(release.assets);
      if (fromAsset) {
        return fromAsset;
      }
      return this.metadataFromRelease(release, repo);
    } catch {
      return undefined;
    }
  }

  private async tryParseBundleMetadataAsset(
    assets: { name: string; browser_download_url: string }[]
  ): Promise<BundleMetadata | undefined> {
    const metadataAsset = assets.find((asset) => asset.name === "bundle-metadata.json");
    if (!metadataAsset) {
      return undefined;
    }
    try {
      const parsed = await this.fetchJson<BundleMetadata>(metadataAsset.browser_download_url);
      if (parsed?.version && parsed.assets) {
        return {
          ...parsed,
          repository: parsed.repository || DEFAULT_REPO
        };
      }
    } catch {
      return undefined;
    }
    return undefined;
  }

  private metadataFromRelease(
    release: { tag_name: string; assets: { name: string }[] },
    repo: string
  ): BundleMetadata {
    const version = release.tag_name.replace(/^v/, "");
    const nameToKey = buildAssetNameToPlatformKeyMap();
    const assets: Partial<Record<PlatformAssetKey, string>> = {};

    for (const asset of release.assets) {
      const key = nameToKey.get(asset.name);
      if (key) {
        assets[key] = asset.name;
      }
    }

    const platform = platformAssetKey();
    const expectedName = assetFileNameForCurrentPlatform();
    const actualName = assets[platform];
    if (!actualName) {
      throw new Error(
        `Release v${version} is missing ${expectedName} for ${platform}. Available: ${release.assets.map((a) => a.name).join(", ") || "(none)"}`
      );
    }

    return { version, repository: repo, assets: assets as Record<string, string> };
  }

  private async verifyCachedBinary(filePath: string, metadata?: BundleMetadata): Promise<void> {
    const resolved =
      metadata ?? (await this.readMetadataSidecar(filePath)) ?? (await this.fetchBundleMetadata());
    const platform = platformAssetKey();
    const expectedRaw = resolved.checksums?.[platform];
    if (!expectedRaw) {
      throw new Error(
        `Missing SHA256 checksum for ${platform} in bundle metadata. Refusing to run unverified binary.`
      );
    }
    const expected = expectedRaw.replace(/^sha256:/i, "").toLowerCase();
    const actual = await this.sha256File(filePath);
    if (actual !== expected) {
      await fs.unlink(filePath).catch(() => undefined);
      await fs.unlink(this.metadataSidecarPath(filePath)).catch(() => undefined);
      throw new Error(
        `Downloaded binary checksum mismatch for ${platform}. The file was removed; try again or report a compromised release.`
      );
    }
  }

  private async sha256File(filePath: string): Promise<string> {
    const hash = createHash("sha256");
    const data = await fs.readFile(filePath);
    hash.update(data);
    return hash.digest("hex");
  }

  private fetchJson<T>(url: string, redirectDepth = 0): Promise<T> {
    if (redirectDepth > MAX_REDIRECTS) {
      return Promise.reject(new Error(`Too many redirects while fetching ${url}`));
    }
    return new Promise((resolve, reject) => {
      const headers: Record<string, string> = {
        Accept: "application/vnd.github+json",
        "User-Agent": "waggle-vscode-extension"
      };
      const request = https.get(url, { headers }, (response) => {
        if (response.statusCode === 302 || response.statusCode === 301) {
          const location = response.headers.location;
          if (location) {
            void this.fetchJson<T>(location, redirectDepth + 1).then(resolve, reject);
            return;
          }
        }
        if ((response.statusCode ?? 0) >= 400) {
          reject(new Error(`GitHub API ${response.statusCode ?? 0} for ${url}`));
          return;
        }
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")) as T);
          } catch (error) {
            reject(error);
          }
        });
      });
      request.on("error", reject);
      request.setTimeout(NETWORK_TIMEOUT_MS, () => {
        request.destroy();
        reject(new Error(`GitHub API request timed out for ${url}`));
      });
    });
  }

  private downloadFile(url: string, destPath: string, redirectDepth = 0): Promise<void> {
    if (redirectDepth > MAX_REDIRECTS) {
      return Promise.reject(new Error(`Too many redirects while downloading ${url}`));
    }
    return new Promise((resolve, reject) => {
      const request = https.get(url, { headers: { "User-Agent": "waggle-vscode-extension" } }, (response) => {
        if (response.statusCode === 302 || response.statusCode === 301) {
          const location = response.headers.location;
          if (!location) {
            reject(new Error(`Redirect without location for ${url}`));
            return;
          }
          void this.downloadFile(location, destPath, redirectDepth + 1).then(resolve, reject);
          return;
        }
        if ((response.statusCode ?? 0) >= 400) {
          reject(new Error(`Download failed (${response.statusCode ?? 0}) for ${url}`));
          return;
        }

        const contentLength = Number(response.headers["content-length"] ?? 0);
        if (contentLength > MAX_BINARY_BYTES) {
          reject(new Error(`Binary exceeds maximum size (${MAX_BINARY_BYTES} bytes).`));
          return;
        }

        const tmpPath = `${destPath}.download`;
        const writeStream = createWriteStream(tmpPath);
        let bytes = 0;
        let settled = false;

        const fail = (error: Error): void => {
          if (settled) {
            return;
          }
          settled = true;
          writeStream.destroy();
          void fs.unlink(tmpPath).catch(() => undefined);
          reject(error);
        };

        const finish = (): void => {
          if (settled) {
            return;
          }
          settled = true;
          writeStream.end(async () => {
            try {
              await fs.rename(tmpPath, destPath);
              resolve();
            } catch (error) {
              reject(error);
            }
          });
        };

        response.on("data", (chunk: Buffer) => {
          bytes += chunk.length;
          if (bytes > MAX_BINARY_BYTES) {
            response.destroy();
            fail(new Error(`Binary exceeds maximum size (${MAX_BINARY_BYTES} bytes).`));
            return;
          }
          if (!writeStream.write(chunk)) {
            response.pause();
            writeStream.once("drain", () => response.resume());
          }
        });

        response.on("end", () => finish());
        response.on("error", (error) => fail(error));
        writeStream.on("error", (error) => fail(error));
      });
      request.on("error", reject);
      request.setTimeout(NETWORK_TIMEOUT_MS, () => {
        request.destroy();
        reject(new Error(`Binary download timed out for ${url}`));
      });
    });
  }
}
