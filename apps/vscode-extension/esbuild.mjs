import * as esbuild from "esbuild";

await esbuild.build({
  entryPoints: [
    "src/extension.ts",
    "src/platform.ts",
    "src/server-port.ts",
    "src/cache-version.ts"
  ],
  bundle: true,
  outdir: "dist",
  external: ["vscode"],
  platform: "node",
  format: "cjs",
  target: "node18",
  sourcemap: true,
  minify: false,
  logLevel: "info"
});
