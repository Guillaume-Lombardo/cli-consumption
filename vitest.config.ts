import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
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
      "server-only": fileURLToPath(
        new URL("./tools/server-only-test-stub.ts", import.meta.url),
      ),
    },
  },
  test: {
    coverage: { enabled: false },
    include: ["apps/**/*.test.ts", "apps/**/*.test.tsx", "packages/**/*.test.ts"],
  },
});
