import { test, expect, type Page } from "@playwright/test";

const jobId = "8d279b4a-d4b4-48ea-a5e3-56f7e30ee0b2";
const hash = "a".repeat(64);
const answer = { locale: "ru-RU", origin_verified: false, outcome: "reflection", safe_message: null,
  reflection: { title: "Синий попугай", symbols: [{ label: "Попугай", evidence: "синий попугай" }],
    summary: "В коротком рассказе выделяется синий попугай.", key_connection: "Значение цвета зависит от ваших ассоциаций.", reflection_question: "Что делала птица?" } };
const config = { oauth_configured: false, mcp_url: "http://127.0.0.1:8000/mcp", cases: [
  { id: "blue-parrot-ru", dream_text: "приснился синий попугай", locale: "ru-RU" },
  { id: "flying-en", dream_text: "I dreamed that I was flying above my school.", locale: "en-US" },
] };
function snapshot(state: string, extra: Record<string, unknown> = {}) {
  return { job_id: jobId, state, attempt: state === "queued" ? 0 : 1, dream_text: config.cases[0].dream_text,
    error: null, candidate: state === "awaiting_review" ? answer : null, result: state === "completed" ? answer : null,
    output_sha256: state === "awaiting_review" ? hash : null, can_retry: ["failed", "expired"].includes(state), ...extra };
}
async function mockBase(page: Page) {
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/bridge/config") return route.fulfill({ json: config });
    if (path === "/api/v1/config") return route.fulfill({ json: { ai_mode: "demo", supported_locales: ["ru-RU", "en-US"] } });
    if (path === "/api/v1/sessions/guest") return route.fulfill({ json: { guest_session_id: "test-owner" } });
    if (path === "/api/v1/bridge/jobs") return route.fulfill({ status: 201, json: { job_id: jobId, automatic_dispatch: false } });
    return route.fulfill({ status: 404, json: { detail: "job_not_found" } });
  });
}
async function restore(page: Page) {
  await page.addInitScript(id => {
    sessionStorage.setItem("sonnic.bridge.job", id);
    sessionStorage.setItem("sonnic.bridge.claim", "09aac5c3-7ef7-4538-9be2-06026020830e");
  }, jobId);
}

test("home exposes the bridge flow and explains the separate ChatGPT step", async ({ page }) => {
  await mockBase(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Открыть тест связи с ChatGPT" }).click();
  await expect(page).toHaveURL(/\/bridge$/);
  await expect(page.getByRole("heading", { name: "От сайта до проверенного ответа" })).toBeVisible();
  await expect(page.getByText("Обработка запускается отдельным запросом в ChatGPT.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Создать тестовое задание" })).toBeEnabled();
  await expect(page.locator("textarea:not([readonly])")).toHaveCount(0);
});

test("saved job link restores its result in a fresh tab without session storage", async ({ page }) => {
  await mockBase(page);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("completed") }));
  await page.goto(`/bridge?job=${jobId}`);
  await expect(page.getByRole("heading", { name: "Проверенный результат", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Синий попугай" })).toBeVisible();
});

test("lost create response can be retried with the same idempotency key", async ({ page }) => {
  await mockBase(page);
  const keys: string[] = [];
  await page.route("**/api/v1/bridge/jobs", async route => {
    keys.push(route.request().headers()["idempotency-key"]);
    expect(route.request().postDataJSON()).toEqual({ case_id: "blue-parrot-ru" });
    if (keys.length === 1) return route.fulfill({ status: 503, json: { detail: "connection_failed" } });
    return route.fulfill({ json: { job_id: jobId } });
  });
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("queued") }));
  await page.goto("/bridge");
  await page.getByRole("button", { name: "Создать тестовое задание" }).click();
  await expect(page.locator('p[role="alert"]')).toContainText("Не удалось связаться");
  await page.getByRole("button", { name: "Создать тестовое задание" }).click();
  await expect(page.getByRole("heading", { name: "Ожидает запроса из ChatGPT" })).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  await page.getByText("Текст задания", { exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Текст задания для ChatGPT" })).toHaveValue(new RegExp(jobId));
  await expect(page.getByText("Оно не запускается автоматически.", { exact: false })).toBeVisible();
});

test("candidate requires review and the accepted result survives reload", async ({ page }) => {
  await mockBase(page);
  await restore(page);
  let state = "awaiting_review";
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot(state) }));
  await page.route(`**/api/v1/bridge/jobs/${jobId}/review`, route => {
    expect(route.request().postDataJSON()).toEqual({ approve: true, output_sha256: hash });
    state = "completed";
    return route.fulfill({ json: snapshot(state) });
  });
  await page.goto("/bridge");
  await expect(page.getByRole("region", { name: "Проверка кандидата" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Проверенный результат" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Принять и показать результат" })).toBeDisabled();
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Принять и показать результат" }).click();
  await expect(page.getByRole("region", { name: "Проверенный результат" })).toContainText("Синий попугай");
  await expect(page.getByRole("region", { name: "Проверка кандидата" })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("region", { name: "Проверенный результат" })).toBeVisible();
});

test("changed candidate invalidates the previous review checkbox", async ({ page }) => {
  await mockBase(page);
  await restore(page);
  let changed = false;
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("awaiting_review", changed ? {
    output_sha256: "b".repeat(64), candidate: { ...answer, reflection: { ...answer.reflection, title: "Другой вариант ответа" } },
  } : {}) }));
  await page.goto("/bridge");
  await page.getByRole("checkbox").check();
  changed = true;
  await page.getByRole("button", { name: "Обновить статус" }).click();
  await expect(page.getByRole("heading", { name: "Другой вариант ответа" })).toBeVisible();
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(page.getByRole("button", { name: "Принять и показать результат" })).toBeDisabled();
});

test("a late status request cannot undo a cancellation", async ({ page }) => {
  await mockBase(page);
  await restore(page);
  let state = "queued", getCount = 0;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let arrived!: () => void;
  const pending = new Promise<void>(resolve => { arrived = resolve; });
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, async route => {
    getCount++;
    if (getCount === 2) {
      arrived();
      await held;
      return route.fulfill({ json: snapshot("queued") });
    }
    return route.fulfill({ json: snapshot(state) });
  });
  await page.route(`**/api/v1/bridge/jobs/${jobId}/cancel`, route => {
    state = "cancelled";
    return route.fulfill({ json: snapshot(state, { dream_text: null }) });
  });
  await page.goto("/bridge");
  await expect(page.getByRole("heading", { name: "Ожидает запроса из ChatGPT" })).toBeVisible();
  await page.getByRole("button", { name: "Обновить статус" }).click();
  await pending;
  await page.getByRole("button", { name: "Отменить задание" }).click();
  await expect(page.getByRole("heading", { name: "Задание отменено" })).toBeVisible();
  const lateResponse = page.waitForResponse(response => response.url().endsWith(`/bridge/jobs/${jobId}`));
  release();
  await lateResponse;
  await expect(page.getByRole("heading", { name: "Задание отменено" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Отменить задание" })).toHaveCount(0);
});

test("expired session offers a fresh test instead of an endless loading screen", async ({ page }) => {
  await mockBase(page);
  await restore(page);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ status: 401, json: { detail: "session_required" } }));
  await page.goto("/bridge");
  await expect(page.getByRole("heading", { name: "Задание недоступно" })).toBeVisible();
  await page.getByRole("button", { name: "Начать новый тест" }).click();
  await expect(page.getByRole("button", { name: "Создать тестовое задание" })).toBeEnabled();
  expect(await page.evaluate(() => sessionStorage.getItem("sonnic.bridge.job"))).toBeNull();
});

test("rejected answer remains an error until a retry creates a queued attempt", async ({ page }) => {
  await mockBase(page);
  await restore(page);
  let state = "failed";
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot(state, { error: state === "failed" ? "review_rejected" : null }) }));
  await page.route(`**/api/v1/bridge/jobs/${jobId}/retry`, route => {
    state = "queued";
    return route.fulfill({ json: snapshot(state) });
  });
  await page.goto("/bridge");
  await expect(page.getByRole("status")).toContainText("Вы отклонили ответ");
  await page.getByRole("button", { name: "Повторить обработку" }).click();
  await expect(page.getByRole("heading", { name: "Ожидает запроса из ChatGPT" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Проверенный результат" })).toHaveCount(0);
});

test("configuration can recover after the server reconnects", async ({ page }) => {
  await mockBase(page);
  let online = false;
  await page.route("**/api/v1/bridge/config", route => online ? route.fulfill({ json: config }) : route.fulfill({ status: 503, json: { detail: "connection_failed" } }));
  await page.goto("/bridge");
  await expect(page.getByRole("button", { name: "Создать тестовое задание" })).toBeDisabled();
  online = true;
  await page.getByRole("button", { name: "Повторить загрузку примеров" }).click();
  await expect(page.getByRole("button", { name: "Создать тестовое задание" })).toBeEnabled();
  await expect(page.locator('p[role="alert"]')).toHaveCount(0);
});

test("invalid OAuth request never presents an approval action", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/v1/bridge/oauth/prepare", route => route.fulfill({ status: 400, json: { detail: "invalid_oauth_request" } }));
  await page.goto("/bridge/connect?client_id=unknown");
  await expect(page.locator('p[role="alert"]')).toContainText("Запрос подключения недействителен");
  await expect(page.getByRole("button", { name: "Разрешить тестовый доступ" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "К тестовым заданиям" })).toHaveAttribute("href", "/bridge");
});

test("OAuth sends a decision only after a user action and clears consumed consent on failure", async ({ page }) => {
  await mockBase(page);
  const decisions: unknown[] = [];
  await page.route("**/api/v1/bridge/oauth/prepare", route => route.fulfill({ json: {
    handle: "handle".repeat(10), client_id: "sonnic-test", scope: "sonnic:synthetic", redirect_uri: "https://chatgpt.com/connector/callback", synthetic_only: true,
  } }));
  await page.route("**/api/v1/bridge/oauth/approve", route => {
    decisions.push(route.request().postDataJSON());
    return route.fulfill({ status: 409, json: { detail: "consent_expired" } });
  });
  await page.goto("/bridge/connect?client_id=sonnic-test");
  await expect(page.getByRole("button", { name: "Разрешить тестовый доступ" })).toBeEnabled();
  expect(decisions).toEqual([]);
  await page.getByRole("button", { name: "Отказать", exact: true }).click();
  expect(decisions).toEqual([{ handle: "handle".repeat(10), allow: false }]);
  await expect(page.locator('p[role="alert"]')).toContainText("Начните авторизацию заново");
  await expect(page.getByRole("button", { name: "Разрешить тестовый доступ" })).toHaveCount(0);
});
