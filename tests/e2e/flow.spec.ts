import { test, expect } from "@playwright/test";

test("typed dream → actual API → demo result → reload → delete", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", err => errors.push(err.message));
  await page.goto("/?utm_source=e2e&content_key=e2e-001");
  await expect(page.getByRole("button", {name:"Посмотреть демо-разбор"})).toBeEnabled();
  await page.getByRole("button", { name: "Вставить пример" }).click();
  await page.getByLabel("Чувство после пробуждения").fill("спокойствие");
  await page.screenshot({ path: `test-results/entry-${test.info().project.name}.png`, fullPage: true });
  await page.getByRole("button", { name: "Посмотреть демо-разбор" }).click();
  await expect(page.getByRole("heading", { name:"Сначала — ваши ощущения" })).toBeVisible();
  await expect(page.getByText("Бесплатный результат · демо")).toBeVisible();
  await expect(page.getByText("Оплата и подписка недоступны.", {exact:false})).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name:"Сначала — ваши ощущения" })).toBeVisible();
  await page.screenshot({ path: `test-results/result-${test.info().project.name}.png`, fullPage: true });
  await page.getByRole("button", { name:"Удалить этот сон" }).click();
  await expect(page.getByRole("status").filter({hasText:"удалены"})).toBeVisible();
  expect(errors).toEqual([]);
});

test("English UI, validation and result locale", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", {name:"EN",exact:true}).click();
  await expect(page.locator("html")).toHaveAttribute("lang","en");
  await page.getByRole("textbox", {name:"Your dream",exact:true}).fill("short");
  await page.getByRole("button", {name:"See a demo reflection"}).click();
  await expect(page.getByRole("heading", {name:"What did you dream?"})).toBeVisible();
  await page.getByRole("button", {name:"Use an example"}).click();
  await page.getByRole("button", {name:"See a demo reflection"}).click();
  await expect(page.getByRole("heading", {name:"Start with how it felt"})).toBeVisible();
  await expect(page.locator("body")).not.toContainText("Сначала — ваши ощущения");
});

test("API unavailable: actionable error, draft kept", async ({ page }) => {
  await page.route("**/api/v1/dreams", route => route.abort());
  await page.goto("/");
  await page.getByRole("button", {name:"Вставить пример"}).click();
  const before = await page.locator("#dream").inputValue();
  await page.getByRole("button", {name:"Посмотреть демо-разбор"}).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Не удалось связаться");
  await expect(page.locator("#dream")).toHaveValue(before);
});

test("no horizontal overflow at viewport width", async ({ page }) => {
  await page.goto("/");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
