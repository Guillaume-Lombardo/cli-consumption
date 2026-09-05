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
    await page.evaluate(async () => {
      await document.fonts.ready;
      return (
        document.fonts.check('16px "Inter Variable"') &&
        getComputedStyle(document.body).fontFamily.startsWith('"Inter Variable"')
      );
    }),
  ).toBe(true);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  const calendarDimensions = await activity
    .locator(".calendar-scroll")
    .evaluate((node) => ({
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
    }));
  if ((page.viewportSize()?.width ?? 0) <= 600) {
    expect(calendarDimensions.scrollWidth).toBeGreaterThan(
      calendarDimensions.clientWidth,
    );
  } else {
    expect(calendarDimensions.scrollWidth).toBeGreaterThanOrEqual(
      calendarDimensions.clientWidth,
    );
  }
  expect(
    await activity.evaluate((root) => {
      const cells = [...root.querySelectorAll<HTMLElement>(".activity-cell")];
      const weekdays = [...root.querySelectorAll<HTMLElement>(".activity-axis span")];
      const month = root.querySelector<HTMLElement>(".activity-months span");
      if (!month || cells.length !== 364 || weekdays.length !== 4) return false;
      const rowsAligned = [0, 2, 4, 6].every(
        (row, index) =>
          Math.abs(
            (weekdays[index]?.getBoundingClientRect().top ?? -99) -
              (cells[row]?.getBoundingClientRect().top ?? 99),
          ) < 8,
      );
      const week = Number.parseInt(month.style.gridColumn, 10) - 1;
      return (
        rowsAligned &&
        Math.abs(
          month.getBoundingClientRect().left -
            (cells[week * 7]?.getBoundingClientRect().left ?? 99),
        ) < 8
      );
    }),
  ).toBe(true);
  const calendarCells = activity.locator(".activity-cell");
  const calendarCell = calendarCells.first();
  for (const index of [0, 6, 357, 363]) {
    const edgeCell = calendarCells.nth(index);
    await edgeCell.hover();
    const renderedTooltip = await edgeCell.evaluate((cell) => {
      const tooltip = getComputedStyle(cell, "::after");
      const cellBox = cell.getBoundingClientRect();
      const viewport = cell.closest(".calendar-scroll")?.getBoundingClientRect();
      const width = Number.parseFloat(tooltip.width);
      const height = Number.parseFloat(tooltip.height);
      const offset = (value: string) =>
        value === "auto" ? undefined : Number.parseFloat(value);
      const leftOffset = offset(tooltip.left);
      const rightOffset = offset(tooltip.right);
      const topOffset = offset(tooltip.top);
      const bottomOffset = offset(tooltip.bottom);
      const paddingLeft = cellBox.left + cell.clientLeft;
      const paddingTop = cellBox.top + cell.clientTop;
      const untransformedLeft =
        leftOffset === undefined
          ? paddingLeft + cell.clientWidth - (rightOffset ?? 0) - width
          : paddingLeft + leftOffset;
      const untransformedTop =
        topOffset === undefined
          ? paddingTop + cell.clientHeight - (bottomOffset ?? 0) - height
          : paddingTop + topOffset;
      const transform =
        tooltip.transform === "none"
          ? new DOMMatrixReadOnly()
          : new DOMMatrixReadOnly(tooltip.transform);
      const left = untransformedLeft + transform.e;
      const top = untransformedTop + transform.f;
      return {
        contentMatches: tooltip.content.includes(
          cell.getAttribute("data-tooltip") ?? "missing",
        ),
        height,
        left,
        top,
        viewport: viewport
          ? {
              bottom: viewport.bottom,
              left: viewport.left,
              right: viewport.right,
              top: viewport.top,
            }
          : undefined,
        width,
      };
    });
    expect(renderedTooltip.contentMatches).toBe(true);
    expect(renderedTooltip.width).toBeGreaterThan(0);
    expect(renderedTooltip.height).toBeGreaterThan(0);
    expect(renderedTooltip.viewport).toBeDefined();
    expect(renderedTooltip.left).toBeGreaterThanOrEqual(
      (renderedTooltip.viewport?.left ?? Number.POSITIVE_INFINITY) - 1,
    );
    expect(renderedTooltip.left + renderedTooltip.width).toBeLessThanOrEqual(
      (renderedTooltip.viewport?.right ?? Number.NEGATIVE_INFINITY) + 1,
    );
    expect(renderedTooltip.top).toBeGreaterThanOrEqual(
      (renderedTooltip.viewport?.top ?? Number.POSITIVE_INFINITY) - 1,
    );
    expect(renderedTooltip.top + renderedTooltip.height).toBeLessThanOrEqual(
      (renderedTooltip.viewport?.bottom ?? Number.NEGATIVE_INFINITY) + 1,
    );
  }
  await calendarCell.focus();
  await page.keyboard.press("ArrowRight");
  await expect(calendarCells.nth(7)).toBeFocused();
  const keyboardFocusedCell = page.locator(".activity-cell:focus");
  expect(
    await keyboardFocusedCell.evaluate((cell) => {
      const focus = getComputedStyle(cell);
      const tooltip = getComputedStyle(cell, "::after");
      return {
        outline:
          focus.outlineStyle !== "none" && focus.outlineColor !== "rgba(0, 0, 0, 0)",
        tooltip:
          tooltip.backgroundColor !== "rgba(0, 0, 0, 0)" &&
          tooltip.color !== tooltip.backgroundColor,
      };
    }),
  ).toEqual({ outline: true, tooltip: true });
  await keyboardFocusedCell.evaluate((cell) => (cell as HTMLElement).blur());
  await page.mouse.move(0, 0);
  await expect(page.locator(".activity-cell:hover, .activity-cell:focus")).toHaveCount(
    0,
  );
  const darkTheme = page.getByRole("button", { name: "Dark theme" });
  if (await darkTheme.isVisible()) await darkTheme.click();
  await expect(activity).toHaveScreenshot("activity-dark.png", {
    animations: "disabled",
    maxDiffPixelRatio: 0.005,
  });
  await page.getByRole("button", { name: "Light theme" }).click();
  await expect(activity).toHaveScreenshot("activity-light.png", {
    animations: "disabled",
    maxDiffPixelRatio: 0.005,
  });
});
