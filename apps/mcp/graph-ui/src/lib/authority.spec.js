import { describe, expect, it } from "vitest";
import { decisionOverview, isAuthoritativeNode, nodeAuthorityStatus } from "./authority";

describe("server-projected memory authority", () => {
  it.each(["future", "expired", "historical", "superseded", "rejected"])(
    "does not treat %s memory as authoritative",
    (status) => {
      expect(isAuthoritativeNode({ authority_status: status })).toBe(false);
    },
  );

  it("does not guess authority when the server projection is absent", () => {
    expect(nodeAuthorityStatus({ valid_to: null })).toBe("unknown");
    expect(isAuthoritativeNode({ valid_to: null })).toBe(false);
  });

  it("accepts only the explicit authoritative projection", () => {
    expect(isAuthoritativeNode({ authority_status: "authoritative" })).toBe(true);
  });

  it("counts all decisions while bounding only the displayed subset", () => {
    const decisions = Array.from({ length: 8 }, (_, index) => ({
      id: `decision-${index}`,
      node_type: "decision",
      authority_status: "authoritative",
    }));
    const overview = decisionOverview(decisions, 5);
    expect(overview.count).toBe(8);
    expect(overview.displayed).toHaveLength(5);
  });
});
