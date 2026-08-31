import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { build } from "esbuild";

const outputPath = "src/cli_consumption/dashboard_calculations.js";
const result = await build({
  absWorkingDir: resolve("packages/offline"),
  bundle: true,
  entryPoints: ["src/index.ts"],
  format: "iife",
  legalComments: "none",
  outfile: "../../src/cli_consumption/dashboard_calculations.js",
  platform: "browser",
  target: "es2020",
  write: false,
});
const [generated] = result.outputFiles;
const tracked = await readFile(outputPath);

if (generated === undefined || !tracked.equals(generated.contents)) {
  process.stderr.write(
    "The offline browser asset is stale; run `npm run build:offline`.\n",
  );
  process.exitCode = 1;
}
