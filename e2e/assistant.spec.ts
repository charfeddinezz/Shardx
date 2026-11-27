import { expect, test } from "@playwright/test";
import { openApp } from "./support/app";

// The local assistant's dock: opened by its button or Mod+J, answered by the
// built-in brain offline (the mock workspace has Sandbox, FB Ads acc-02 and
// friends), with the answer's cards and follow-ups under it.
test("the dock opens, answers from the workspace and closes", async ({ page }) => {
  await openApp(page);
  await page.getByRole("button", { name: "Open the assistant" }).click();
  const dock = page.locator("[data-assistant-dock]");
  await expect(dock).toBeVisible();
  // The empty dock greets and offers follow-ups before anything is typed.
  await expect(dock.getByRole("log")).toContainText("Hello.");

  const input = dock.getByRole("textbox", { name: "Ask about profiles, proxies or the app" });
  await input.fill("how many profiles?");
  await input.press("Enter");
  // The workspace's own numbers, whatever the mock seeds.
  await expect(dock.getByRole("log").last()).toContainText(/Profiles: \d+\./);

  await dock.getByRole("button", { name: "Close" }).click();
  await expect(dock).toBeHidden();
});

test("Mod+J toggles the dock and a suggestion chip sends its line", async ({ page }) => {
  await openApp(page);
  await page.keyboard.press("Control+j");
  const dock = page.locator("[data-assistant-dock]");
  await expect(dock).toBeVisible();
  await dock.getByRole("button", { name: "What is running now?" }).click();
  await expect(dock.getByRole("log").last()).toContainText(/Running now \(\d+\)|Nothing is running/);
  await page.keyboard.press("Control+j");
  await expect(dock).toBeHidden();
});
