import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { build } from "esbuild";

const execute = promisify(execFile);
const assets = [["src/cli_consumption/dashboard_react.js", "src/app.tsx", true]];

for (const [path, entryPoint, react] of assets) {
  const result = await build({
    absWorkingDir: resolve("packages/offline"),
    bundle: true,
    define: react ? { "process.env.NODE_ENV": '"production"' } : undefined,
    entryPoints: [entryPoint],
    format: "iife",
    legalComments: "none",
    jsx: react ? "automatic" : undefined,
    minify: react,
    platform: "browser",
    target: "es2020",
    write: false,
  });
  let contents = result.outputFiles[0]?.text;
  if (react) {
    contents = contents?.replaceAll(
      "https://react.dev/errors/",
      "about:blank#react-error-",
    );
  }
  const tracked = await readFile(path, "utf8");
  if (contents === undefined || tracked !== contents) {
    process.stderr.write(
      `The offline browser asset ${path} is stale; run \`npm run build:offline\`.\n`,
    );
    process.exitCode = 1;
  }
}

const temporary = await mkdtemp(join(tmpdir(), "cli-consumption-css-"));
try {
  const output = join(temporary, "dashboard_react.css");
  await execute(
    resolve("node_modules/.bin/tailwindcss"),
    ["-i", "src/styles.css", "-o", output, "--minify"],
    { cwd: resolve("packages/offline") },
  );
  const generated = (await readFile(output, "utf8"))
    .replaceAll("https://tailwindcss.com", "tailwindcss.com")
    .replace(/\n?$/, "\n");
  const tracked = await readFile("src/cli_consumption/dashboard_react.css", "utf8");
  if (generated !== tracked) {
    process.stderr.write(
      "The offline browser asset dashboard_react.css is stale; run `npm run build:offline`.\n",
    );
    process.exitCode = 1;
  }
} finally {
  await rm(temporary, { recursive: true });
}
