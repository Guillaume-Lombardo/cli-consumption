import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { sanitizeOfflineScript, sanitizeOfflineStylesheet } from "./offline-assets.mjs";

const scriptPath = fileURLToPath(
  new URL("../src/cli_consumption/dashboard_react.js", import.meta.url),
);
const stylesheetPath = fileURLToPath(
  new URL("../src/cli_consumption/dashboard_react.css", import.meta.url),
);
const script = sanitizeOfflineScript(await readFile(scriptPath, "utf8"));
const stylesheet = await sanitizeOfflineStylesheet(
  await readFile(stylesheetPath, "utf8"),
);

await Promise.all([
  writeFile(scriptPath, script),
  writeFile(stylesheetPath, stylesheet),
]);
