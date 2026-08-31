import { describe, expect, it } from "vitest";
import { strToU8, zipSync } from "fflate";

import {
  clearSessionWorkspace,
  createSessionApi,
  loadAbhiIntoSession,
  readSessionWorkspace,
} from "./session-abhi";

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, value); }
  removeItem(key) { this.values.delete(key); }
}

function fixtureBase64() {
  const manifest = { schema_version: "2.0.0", project: "private-project", encryption: { enabled: false } };
  const nodes = [
    { id: "goal", label: "Project goal", content: "Keep the graph private.", node_type: "note", tags: ["goal"] },
    { id: "storage", label: "Storage architecture", content: "Use SQLite locally.", node_type: "decision", tags: ["storage"] },
  ];
  const archive = zipSync({
    "manifest.json": strToU8(JSON.stringify(manifest)),
    "nodes.jsonl": strToU8(nodes.map((node) => JSON.stringify(node)).join("\n")),
    "edges.jsonl": strToU8(""),
  });
  const file = new Uint8Array(archive.length + 4);
  file.set([0x57, 0x47, 0x4c, 0x01]);
  file.set(archive, 4);
  return Buffer.from(file).toString("base64");
}

describe("private .abhi browser sessions", () => {
  it("loads into session storage and powers recall without a server", async () => {
    const storage = new MemoryStorage();
    const result = await loadAbhiIntoSession({
      contentBase64: fixtureBase64(),
      fileName: "private.abhi",
      project: "waggle-webmcp",
      storage,
    });

    expect(result.node_count).toBe(2);
    expect(result.privacy).toContain("no Waggle server upload");
    expect(readSessionWorkspace("waggle-webmcp", storage).original_project).toBe("private-project");
    const recall = createSessionApi("waggle-webmcp", storage).recallMemory({ query: "storage", limit: 5 });
    expect(recall.memories[0].content).toBe("Use SQLite locally.");
    expect(recall.storage).toBe("browser_session_only");

    clearSessionWorkspace("waggle-webmcp", storage);
    expect(readSessionWorkspace("waggle-webmcp", storage)).toBeNull();
  });

  it("rejects non-Waggle and encrypted archives", async () => {
    const storage = new MemoryStorage();
    await expect(loadAbhiIntoSession({ contentBase64: Buffer.from("nope").toString("base64"), fileName: "bad.abhi", project: "waggle-webmcp", storage })).rejects.toThrow("missing Waggle WGL");
  });
});
