import { readFile } from "node:fs/promises";

const fontDirectory = new URL(
  "../node_modules/@fontsource-variable/inter/files/",
  import.meta.url,
);

/** Remove the only remote URL embedded by React's production error helper. */
export function sanitizeOfflineScript(script) {
  return script.replaceAll("https://react.dev/errors/", "about:blank#react-error-");
}

/** Inline package-owned fonts and reject every remaining external stylesheet asset. */
export async function sanitizeOfflineStylesheet(source) {
  let stylesheet = source
    .replaceAll("https://tailwindcss.com", "tailwindcss.com")
    .replace(/\n?$/, "\n");
  const fontFiles = new Set(
    [...stylesheet.matchAll(/url\(\.\/files\/([a-z0-9-]+\.woff2)\)/g)].map(
      (match) => match[1],
    ),
  );
  for (const fontFile of fontFiles) {
    if (!fontFile) continue;
    const encoded = (await readFile(new URL(fontFile, fontDirectory))).toString(
      "base64",
    );
    stylesheet = stylesheet.replaceAll(
      `url(./files/${fontFile})`,
      `url(data:font/woff2;base64,${encoded})`,
    );
  }
  if (/url\((?!data:)/.test(stylesheet)) {
    throw new Error("offline_stylesheet_external_asset");
  }
  return stylesheet;
}
