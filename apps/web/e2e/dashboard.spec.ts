import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("authenticates and renders the bounded persistent dashboard", async ({ page }) => {
  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/login\?reason=session$/);
  await expect(page.getByRole("heading", { name: "CLI Consumption" })).toBeVisible();
  const loginAccessibility = await new AxeBuilder({ page }).analyze();
  expect(loginAccessibility.violations).toEqual([]);

  await page.getByLabel("Dashboard password").fill("e2e dashboard password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard\?range=30$/);
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Conversation explorer" }),
  ).toBeVisible();
  const layoutWidgets = page.locator("[data-widget-type]");
  await expect(layoutWidgets).toHaveCount(12);
  expect(
    await layoutWidgets.evaluateAll((widgets) =>
      widgets.map((widget) => ({
        height: widget.getAttribute("data-size-height"),
        type: widget.getAttribute("data-widget-type"),
        width: widget.getAttribute("data-size-width"),
        x: widget.getAttribute("data-position-x"),
        y: widget.getAttribute("data-position-y"),
      })),
    ),
  ).toEqual([
    { height: "1", type: "headline-metrics", width: "12", x: "0", y: "0" },
    { height: "1", type: "activity", width: "6", x: "0", y: "1" },
    { height: "1", type: "tools", width: "6", x: "6", y: "1" },
    { height: "1", type: "models", width: "6", x: "0", y: "2" },
    { height: "1", type: "turn-performance", width: "6", x: "6", y: "2" },
    { height: "1", type: "workflow-complexity", width: "6", x: "0", y: "3" },
    { height: "1", type: "turn-outcomes", width: "6", x: "6", y: "3" },
    { height: "1", type: "technical-work-items", width: "6", x: "0", y: "4" },
    { height: "1", type: "context-pressure", width: "6", x: "6", y: "4" },
    { height: "1", type: "cohorts", width: "6", x: "0", y: "5" },
    { height: "1", type: "data-quality", width: "6", x: "6", y: "5" },
    { height: "2", type: "conversation-explorer", width: "12", x: "0", y: "6" },
  ]);

  const cardIsInvisible = await page
    .locator(".metric-card")
    .first()
    .evaluate((card) => {
      const body = getComputedStyle(document.body);
      const style = getComputedStyle(card);
      return (
        style.backgroundColor === body.backgroundColor && style.borderTopWidth === "0px"
      );
    });
  expect(cardIsInvisible).toBe(true);

  const viewport = page.viewportSize();
  const titleBox = await page.locator(".hero h1").boundingBox();
  const eyebrowBox = await page.locator(".hero .eyebrow").boundingBox();
  expect(titleBox).not.toBeNull();
  expect(eyebrowBox).not.toBeNull();
  if (viewport && viewport.width > 832 && titleBox && eyebrowBox) {
    expect(eyebrowBox.x).toBeGreaterThan(titleBox.x + titleBox.width);
  }

  await page.getByRole("combobox", { name: "Project" }).selectOption("project-a");
  await expect(page).toHaveURL(/\/dashboard\?range=30$/);
  expect(page.url()).not.toContain("project-a");
  await expect(page.getByRole("status")).toBeHidden();

  await page
    .getByRole("combobox", { name: "Offline export profile" })
    .selectOption("share-safe");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export offline" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("cli-consumption-dashboard.html");
  const stream = await download.createReadStream();
  let exported = "";
  for await (const chunk of stream) exported += chunk.toString();
  expect(exported).toContain('data-profile="share-safe"');
  expect(exported).toContain("project-a offline export");
  expect(exported).not.toContain("e2e-export-token");
  expect(exported).not.toMatch(/https?:\/\//);

  await page
    .locator(".conversation-card button:visible, .conversation-table button:visible")
    .click();
  await expect(page.getByRole("region", { name: "Conversation detail" })).toBeVisible();
  const dashboardAccessibility = await new AxeBuilder({ page }).analyze();
  expect(dashboardAccessibility.violations).toEqual([]);
});

test("keeps the chart catalog stable in light and dark responsive layouts", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Dashboard password").fill("e2e dashboard password");
  await page.getByRole("button", { name: "Sign in" }).click();
  const activity = page.locator('[data-widget-type="activity"]');
  await expect(
    activity.getByRole("button", { name: /tokens|turns/ }).first(),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  expect(
    await activity
      .locator(".calendar-scroll")
      .evaluate((node) => node.scrollWidth >= node.clientWidth),
  ).toBe(true);
  const darkTheme = page.getByRole("button", { name: "Dark theme" });
  if (await darkTheme.isVisible()) await darkTheme.click();
  await expect(activity).toHaveScreenshot("activity-dark.png", {
    animations: "disabled",
  });
  await page.getByRole("button", { name: "Light theme" }).click();
  await expect(activity).toHaveScreenshot("activity-light.png", {
    animations: "disabled",
  });
});
