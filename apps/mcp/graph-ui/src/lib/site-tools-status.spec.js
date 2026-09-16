import { describe, expect, it } from "vitest";
import { resolveSiteToolsStatus } from "./site-tools-status.js";

describe("Site tools registration status", () => {
  it("reports ready only when every Waggle tool registers", () => {
    expect(resolveSiteToolsStatus([true, true, true, true])).toEqual({
      kind: "ready",
      registeredCount: 4,
    });
  });

  it("reports an unsupported browser when registration is unavailable", () => {
    expect(resolveSiteToolsStatus([false, false, false, false])).toEqual({
      kind: "unavailable",
      registeredCount: 0,
    });
    expect(resolveSiteToolsStatus([true, true, false, true])).toEqual({
      kind: "unavailable",
      registeredCount: 3,
    });
  });
});
