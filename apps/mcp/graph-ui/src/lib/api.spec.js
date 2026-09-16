import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRequest, DEMO_SESSION_HEADER, getDemoSessionId } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("browser-scoped demo API sessions", () => {
  it("generates one first-party token and sends it on every API request", async () => {
    const values = new Map();
    const sessionStorage = {
      getItem: vi.fn((key) => values.get(key) || null),
      setItem: vi.fn((key, value) => values.set(key, value)),
    };
    vi.stubGlobal("window", {
      __WAGGLE_GRAPH_CONFIG__: {
        apiBaseUrl: "https://waggle-webmcp-api.onrender.com",
        demoMode: true,
      },
      sessionStorage,
    });
    vi.stubGlobal("crypto", {
      getRandomValues: vi.fn((bytes) => {
        bytes.fill(7);
        return bytes;
      }),
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const sessionId = getDemoSessionId();
    await apiRequest("/api/webmcp/recall-memory", { method: "POST", body: "{}" });
    await apiRequest("/api/webmcp/proposals", { method: "POST", body: "{}" });

    expect(sessionId).toBe("07".repeat(32));
    expect(sessionStorage.setItem).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][1].headers[DEMO_SESSION_HEADER]).toBe(sessionId);
    expect(fetchMock.mock.calls[1][1].headers[DEMO_SESSION_HEADER]).toBe(sessionId);
  });

  it("does not attach demo identity outside demo mode", async () => {
    vi.stubGlobal("window", {
      __WAGGLE_GRAPH_CONFIG__: { apiBaseUrl: "https://api.example.test", demoMode: false },
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/api/graph");

    expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty(DEMO_SESSION_HEADER);
  });
});
