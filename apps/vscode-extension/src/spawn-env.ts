const ALLOWED_ENV_KEYS = new Set([
  "PATH",
  "Path",
  "PATHEXT",
  "HOME",
  "USERPROFILE",
  "HOMEDRIVE",
  "HOMEPATH",
  "TEMP",
  "TMP",
  "TMPDIR",
  "SYSTEMROOT",
  "WINDIR",
  "COMSPEC",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "PYTHONUTF8",
  "WAGGLE_DEFAULT_TENANT_ID",
  "WAGGLE_DB_PATH",
  "WAGGLE_STARTUP_MODE",
  "WAGGLE_MODEL"
]);

export function curatedSpawnEnv(overrides: Record<string, string> = {}): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined && ALLOWED_ENV_KEYS.has(key)) {
      env[key] = value;
    }
  }
  for (const [key, value] of Object.entries(overrides)) {
    env[key] = value;
  }
  return env;
}

export function spawnNotFoundMessage(command: string): string {
  return (
    `${command} not found on PATH. Set waggle.commandPath, switch waggle.installMethod to binary, or install via pipx.`
  );
}
