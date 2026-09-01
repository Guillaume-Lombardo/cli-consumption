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

  await page.getByRole("button", { name: "Inspect" }).click();
  await expect(page.getByRole("region", { name: "Conversation detail" })).toBeVisible();
  const dashboardAccessibility = await new AxeBuilder({ page }).analyze();
  expect(dashboardAccessibility.violations).toEqual([]);
});
