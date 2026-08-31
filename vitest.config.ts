import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@cli-consumption/analytics": new URL(
        "./packages/analytics/src/index.ts",
        import.meta.url,
      ).pathname,
      "@cli-consumption/contracts": new URL(
        "./packages/contracts/src/index.ts",
        import.meta.url,
      ).pathname,
      "@cli-consumption/ui": new URL("./packages/ui/src/index.ts", import.meta.url)
        .pathname,
    },
  },
  test: {
    coverage: { enabled: false },
    include: ["packages/**/*.test.ts"],
  },
});
