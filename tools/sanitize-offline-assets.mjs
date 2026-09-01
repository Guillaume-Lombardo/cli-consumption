import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(
  new URL("../src/cli_consumption/dashboard_react.js", import.meta.url),
);
const stylesheetPath = fileURLToPath(
  new URL("../src/cli_consumption/dashboard_react.css", import.meta.url),
);
const script = (await readFile(scriptPath, "utf8")).replaceAll(
  "https://react.dev/errors/",
  "about:blank#react-error-",
);
const stylesheet = (await readFile(stylesheetPath, "utf8"))
  .replaceAll("https://tailwindcss.com", "tailwindcss.com")
  .replace(/\n?$/, "\n");

await Promise.all([
  writeFile(scriptPath, script),
  writeFile(stylesheetPath, stylesheet),
]);
