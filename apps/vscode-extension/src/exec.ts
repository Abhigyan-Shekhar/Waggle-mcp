import { execFile, type ExecFileOptions } from "child_process";
import { curatedSpawnEnv } from "./spawn-env";

export interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

export async function execFileAsync(
  command: string,
  args: string[],
  options?: ExecFileOptions
): Promise<CommandResult> {
  const envOverrides: Record<string, string> = {};
  if (options?.env) {
    for (const [key, value] of Object.entries(options.env)) {
      if (typeof value === "string") {
        envOverrides[key] = value;
      }
    }
  }
  const mergedOptions: ExecFileOptions = {
    windowsHide: true,
    ...options,
    env: curatedSpawnEnv(envOverrides)
  };
  return await new Promise((resolve, reject) => {
    execFile(command, args, mergedOptions, (error, stdout, stderr) => {
      const numericCode = (error as NodeJS.ErrnoException | null)?.code;
      const code = typeof numericCode === "number" ? numericCode : 0;
      if (error && typeof (error as NodeJS.ErrnoException).code !== "number") {
        reject(error);
        return;
      }
      resolve({ code, stdout: String(stdout), stderr: String(stderr) });
    });
  });
}
