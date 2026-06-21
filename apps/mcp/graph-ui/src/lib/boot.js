export function getBootConfig() {
  const config = window.__WAGGLE_GRAPH_CONFIG__ || {};
  return {
    mode: config.mode === "view" ? "view" : "edit",
    sampleMode: Boolean(config.sampleMode),
    scope: {
      project: config.project || "",
      agent_id: config.agent_id || "",
      session_id: config.session_id || ""
    }
  };
}
