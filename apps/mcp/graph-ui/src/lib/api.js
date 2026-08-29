export const DEMO_SESSION_HEADER = "X-Waggle-Demo-Session";

const DEMO_SESSION_STORAGE_KEY = "waggle:demo-session:v1";
const DEMO_SESSION_PATTERN = /^[A-Za-z0-9_-]{24,96}$/;

let volatileDemoSessionId = "";

function createDemoSessionId() {
  const cryptoProvider = globalThis.crypto;
  if (!cryptoProvider?.getRandomValues) {
    throw new Error("Secure browser randomness is required for the Waggle demo session.");
  }
  const bytes = new Uint8Array(32);
  cryptoProvider.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export function getDemoSessionId() {
  if (typeof window === "undefined" || !window.__WAGGLE_GRAPH_CONFIG__?.demoMode) {
    return "";
  }

  try {
    const stored = window.sessionStorage?.getItem(DEMO_SESSION_STORAGE_KEY) || "";
    if (DEMO_SESSION_PATTERN.test(stored)) {
      volatileDemoSessionId = stored;
      return stored;
    }
  } catch {
    // Some embedded browsers deny storage access. The page-local fallback below
    // still keeps every request from this document in the same demo workspace.
  }

  if (!DEMO_SESSION_PATTERN.test(volatileDemoSessionId)) {
    volatileDemoSessionId = createDemoSessionId();
  }

  try {
    window.sessionStorage?.setItem(DEMO_SESSION_STORAGE_KEY, volatileDemoSessionId);
  } catch {
    // Keep using the page-local token when sessionStorage is unavailable.
  }
  return volatileDemoSessionId;
}

export async function apiRequest(path, options = {}) {
  const configuredBase = String(
    (typeof window !== "undefined" && window.__WAGGLE_GRAPH_CONFIG__?.apiBaseUrl) || "",
  ).replace(/\/$/, "");
  const requestUrl = configuredBase && path.startsWith("/")
    ? `${configuredBase}${path}`
    : path;
  const demoSessionId = getDemoSessionId();
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
    ...(demoSessionId ? { [DEMO_SESSION_HEADER]: demoSessionId } : {}),
  };
  const response = await fetch(requestUrl, {
    ...options,
    headers,
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
