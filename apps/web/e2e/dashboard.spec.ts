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

  await page.getByRole("combobox", { name: "Project" }).selectOption("project-a");
  await expect(page).toHaveURL(/\/dashboard\?range=30$/);
  expect(page.url()).not.toContain("project-a");
  await expect(page.getByRole("status")).toBeHidden();

  await page.getByRole("button", { name: "Inspect" }).click();
  await expect(page.getByRole("region", { name: "Conversation detail" })).toBeVisible();
  const dashboardAccessibility = await new AxeBuilder({ page }).analyze();
  expect(dashboardAccessibility.violations).toEqual([]);
});
