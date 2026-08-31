import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@cli-consumption/analytics": fileURLToPath(
        new URL("./packages/analytics/src/index.ts", import.meta.url),
      ),
      "@cli-consumption/contracts": fileURLToPath(
        new URL("./packages/contracts/src/index.ts", import.meta.url),
      ),
      "@cli-consumption/ui": fileURLToPath(
        new URL("./packages/ui/src/index.ts", import.meta.url),
      ),
    },
  },
  test: {
    coverage: { enabled: false },
    include: ["packages/**/*.test.ts"],
  },
});
