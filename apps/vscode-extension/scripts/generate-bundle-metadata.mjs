#!/usr/bin/env node
/**
 * Writes bundle-metadata.json for GitHub Releases (invoked from release-binaries CI).
 * Usage: node generate-bundle-metadata.mjs <version> [output-path] [artifacts-dir]
 */
import { createHash } from "node:crypto";
import { createReadStream, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const version = process.argv[2];
if (!version) {
  console.error("usage: node generate-bundle-metadata.mjs <version> [output-path] [artifacts-dir]");
  process.exit(1);
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const assetMap = JSON.parse(readFileSync(join(scriptDir, "asset-map.json"), "utf8"));
const outputPath = process.argv[3] || join(scriptDir, "..", "resources", "bundle-metadata.json");
const artifactsDir = process.argv[4];

function findArtifactFile(rootDir, assetName) {
  const direct = join(rootDir, assetName);
  try {
    if (statSync(direct).isFile()) {
      return direct;
    }
  } catch {
    // continue search
  }

  let found;
  for (const entry of readdirSync(rootDir)) {
    const entryPath = join(rootDir, entry);
    const stats = statSync(entryPath);
    if (stats.isFile() && entry === assetName) {
      return entryPath;
    }
    if (stats.isDirectory()) {
      const nested = findArtifactFile(entryPath, assetName);
      if (nested) {
        found = nested;
      }
    }
  }
  return found;
}

function sha256File(filePath) {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    createReadStream(filePath)
      .on("data", (chunk) => hash.update(chunk))
      .on("end", () => resolve(`sha256:${hash.digest("hex")}`))
      .on("error", reject);
  });
}

const checksums = {};
if (artifactsDir) {
  for (const [platformKey, assetName] of Object.entries(assetMap)) {
    const artifactPath = findArtifactFile(artifactsDir, assetName);
    if (!artifactPath) {
      console.error(`missing artifact for ${platformKey}: ${assetName}`);
      process.exit(1);
    }
    checksums[platformKey] = await sha256File(artifactPath);
    console.log(`${platformKey}: ${checksums[platformKey]}`);
  }
}

const payload = {
  version,
  repository: "Abhigyan-Shekhar/Waggle-mcp",
  assets: assetMap,
  ...(Object.keys(checksums).length > 0 ? { checksums } : {})
};

mkdirSync(dirname(outputPath), { recursive: true });
writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
console.log(`wrote ${outputPath}`);
