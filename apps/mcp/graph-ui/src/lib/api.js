export async function apiRequest(path, options = {}) {
  const configuredBase = String(
    (typeof window !== "undefined" && window.__WAGGLE_GRAPH_CONFIG__?.apiBaseUrl) || "",
  ).replace(/\/$/, "");
  const requestUrl = configuredBase && path.startsWith("/")
    ? `${configuredBase}${path}`
    : path;
  const response = await fetch(requestUrl, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    },
    ...options,
    ...(configuredBase ? { credentials: "include" } : {}),
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      message = payload.message || payload.error || message;
    } catch {
      // Ignore non-JSON error bodies.
    }
    throw new Error(message);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

export function buildScopeQuery(scope) {
  const params = new URLSearchParams();
  if (scope.project) {
    params.set("project", scope.project);
  }
  if (scope.agent_id) {
    params.set("agent_id", scope.agent_id);
  }
  if (scope.session_id) {
    params.set("session_id", scope.session_id);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}
