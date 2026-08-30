export function resolveSiteToolsStatus(results) {
  const registeredCount = results.filter(Boolean).length;
  return {
    kind: registeredCount === results.length && results.length > 0 ? "ready" : "unavailable",
    registeredCount,
  };
}
