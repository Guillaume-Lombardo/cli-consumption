import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      {
        find: "@cli-consumption/ui/react",
        replacement: fileURLToPath(
          new URL("./packages/ui/src/react.tsx", import.meta.url),
        ),
      },
      {
        find: "@cli-consumption/analytics",
        replacement: fileURLToPath(
          new URL("./packages/analytics/src/index.ts", import.meta.url),
        ),
      },
      {
        find: "@cli-consumption/contracts",
        replacement: fileURLToPath(
          new URL("./packages/contracts/src/index.ts", import.meta.url),
        ),
      },
      {
        find: "@cli-consumption/ui",
        replacement: fileURLToPath(
          new URL("./packages/ui/src/index.ts", import.meta.url),
        ),
      },
      {
        find: "server-only",
        replacement: fileURLToPath(
          new URL("./tools/server-only-test-stub.ts", import.meta.url),
        ),
      },
    ],
  },
  test: {
    coverage: { enabled: false },
    include: [
      "apps/**/*.test.ts",
      "apps/**/*.test.tsx",
      "packages/**/*.test.ts",
      "packages/**/*.test.tsx",
    ],
  },
});
