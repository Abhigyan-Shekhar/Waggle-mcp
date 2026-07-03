const BOOT_CONFIG_SCHEMA_VERSION = 1;

const DEFAULT_BOOT_CONFIG = Object.freeze({
  schemaVersion: BOOT_CONFIG_SCHEMA_VERSION,
  mode: "edit",
  sampleMode: false,
  scope: {
    project: "",
    agent_id: "",
    session_id: "",
  },
  diagnostics: [],
});

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizeString(value, fieldName, diagnostics) {
  if (value === undefined || value === null) {
    return "";
  }

  if (typeof value !== "string") {
    diagnostics.push(`${fieldName} must be a string; using empty string.`);
    return "";
  }

  return value;
}

function normalizeMode(value, diagnostics) {
  if (value === undefined || value === null) {
    return "edit";
  }

  if (value === "edit" || value === "view") {
    return value;
  }

  diagnostics.push(`mode must be "edit" or "view"; using "edit".`);
  return "edit";
}

function normalizeSampleMode(value, diagnostics) {
  if (value === undefined || value === null) {
    return false;
  }

  if (typeof value === "boolean") {
    return value;
  }

  diagnostics.push("sampleMode must be a boolean; using false.");
  return false;
}

function normalizeScope(config, diagnostics) {
  if (
    Object.prototype.hasOwnProperty.call(config, "scope") &&
    !isPlainObject(config.scope)
  ) {
    diagnostics.push("scope must be an object; falling back to flat scope fields");
  }

  const nestedScope = isPlainObject(config.scope) ? config.scope : {};

  return {
    project: normalizeString(
      nestedScope.project ?? config.project,
      "scope.project",
      diagnostics,
    ),
    agent_id: normalizeString(
      nestedScope.agent_id ?? config.agent_id,
      "scope.agent_id",
      diagnostics,
    ),
    session_id: normalizeString(
      nestedScope.session_id ?? config.session_id,
      "scope.session_id",
      diagnostics,
    ),
  };
}

export function validateBootConfig(rawConfig) {
  const diagnostics = [];

  if (rawConfig === undefined || rawConfig === null) {
    diagnostics.push("Missing window.__WAGGLE_GRAPH_CONFIG__; using defaults.");
    return {
      ...DEFAULT_BOOT_CONFIG,
      diagnostics,
    };
  }

  if (!isPlainObject(rawConfig)) {
    diagnostics.push("Boot config must be an object; using defaults.");
    return {
      ...DEFAULT_BOOT_CONFIG,
      diagnostics,
    };
  }

  const schemaVersion =
    rawConfig.schemaVersion === undefined ? BOOT_CONFIG_SCHEMA_VERSION : rawConfig.schemaVersion;

  if (schemaVersion !== BOOT_CONFIG_SCHEMA_VERSION) {
    diagnostics.push(
      `Unsupported boot config schemaVersion ${String(
        schemaVersion,
      )}; expected ${BOOT_CONFIG_SCHEMA_VERSION}.`,
    );
  }

  return {
    schemaVersion: BOOT_CONFIG_SCHEMA_VERSION,
    mode: normalizeMode(rawConfig.mode, diagnostics),
    sampleMode: normalizeSampleMode(rawConfig.sampleMode, diagnostics),
    scope: normalizeScope(rawConfig, diagnostics),
    diagnostics,
  };
}

export function readBootConfig(globalObject = window) {
  const config = validateBootConfig(globalObject.__WAGGLE_GRAPH_CONFIG__);

  if (config.diagnostics.length > 0 && globalObject.console?.warn) {
    globalObject.console.warn(
      "[waggle] Invalid graph boot config:",
      config.diagnostics,
    );
  }

  return config;
}