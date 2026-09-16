export function nodeAuthorityStatus(node) {
  const status = String(node?.authority_status || "").trim().toLowerCase();
  return status || "unknown";
}

export function isAuthoritativeNode(node) {
  return nodeAuthorityStatus(node) === "authoritative";
}

export function decisionOverview(nodes, limit = 4) {
  const all = nodes.filter((node) => isAuthoritativeNode(node) && node.node_type === "decision");
  return { all, displayed: all.slice(0, limit), count: all.length };
}
