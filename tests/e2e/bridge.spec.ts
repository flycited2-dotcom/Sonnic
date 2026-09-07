import { test, expect, type Page } from "@playwright/test";

const jobId = "8d279b4a-d4b4-48ea-a5e3-56f7e30ee0b2";
const hash = "a".repeat(64);
const dreamText = "Мне приснился синий попугай, который сидел у окна.";
const answer = { locale: "ru-RU", origin_verified: false, data_class: "personal", outcome: "reflection", safe_message: null,
  reflection: { title: "Синий попугай у окна", symbols: [{ label: "Попугай", evidence: "синий попугай" }],
    summary: "В рассказе выделяются попугай и окно; их личное значение зависит от ваших ассоциаций.", key_connection: "Можно сопоставить образ птицы с тем, что сейчас просится наружу.", reflection_question: "Что вы чувствовали, глядя на попугая?" } };
const config = { oauth_configured: true, personal_input_enabled: true, synthetic_only: false,
  mcp_url: "https://sonnic.example/mcp", cases: [
    { id: "blue-parrot-ru", dream_text: "приснился синий попугай", locale: "ru-RU" },
  ] };
function snapshot(state: string, extra: Record<string, unknown> = {}) {
  return { job_id: jobId, state, attempt: state === "queued" ? 0 : 1, data_class: "personal", dream_text: dreamText,
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
  await page.addInitScript(({id,key}) => {
    sessionStorage.setItem("sonnic.bridge.job", id);
    sessionStorage.setItem(`sonnic.bridge.claim.${id}`, key);
  }, {id: jobId, key: "09aac5c3-7ef7-4538-9be2-06026020830e"});
}

async function fillDream(page: Page) {
  await page.getByLabel("Ваш сон").fill(dreamText);
  await page.getByRole("checkbox", { name: /Я согласен отправить/ }).check();
}

test("bridge accepts owner dream only after explicit consent", async ({ page }) => {
  await mockBase(page);
  let submitted: unknown;
  await page.route("**/api/v1/bridge/jobs", route => { submitted = route.request().postDataJSON(); return route.fulfill({ status: 201, json: { job_id: jobId } }); });
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("queued") }));
  await page.goto("/bridge");
  await expect(page.getByRole("heading", { name: "Расскажите свой сон" })).toBeVisible();
  await page.getByLabel("Ваш сон").fill(dreamText);
  await expect(page.getByRole("button", { name: "Передать сон на разбор" })).toBeDisabled();
  await page.getByRole("checkbox", { name: /Я согласен отправить/ }).check();
  await page.getByRole("button", { name: "Передать сон на разбор" }).click();
  await expect(page.getByRole("heading", { name: "Сон сохранён и ждёт разбора" })).toBeVisible();
  expect(submitted).toMatchObject({ dream: { dream_text: dreamText, locale: "ru-RU", content_language: "ru" } });
});

test("lost create response reuses the idempotency key", async ({ page }) => {
  await mockBase(page);
  const keys: string[] = [];
  await page.route("**/api/v1/bridge/jobs", async route => {
    keys.push(route.request().headers()["idempotency-key"]);
    if (keys.length === 1) return route.fulfill({ status: 503, json: { detail: "connection_failed" } });
    return route.fulfill({ status: 201, json: { job_id: jobId } });
  });
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("queued") }));
  await page.goto("/bridge");
  await fillDream(page);
  await page.getByRole("button", { name: "Передать сон на разбор" }).click();
  await expect(page.locator('p[role="alert"]')).toContainText("Не удалось связаться");
  await page.getByRole("button", { name: "Передать сон на разбор" }).click();
  await expect(page.getByRole("heading", { name: "Сон сохранён и ждёт разбора" })).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
});

test("queued job shows exact ChatGPT handoff", async ({ page }) => {
  await mockBase(page); await restore(page);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("queued") }));
  await page.goto("/bridge");
  await expect(page.getByRole("link", { name: "2. Открыть проект ChatGPT" })).toHaveAttribute("href", /chatgpt\.com/);
  await page.getByText("Показать текст задания").click();
  const task = page.getByRole("textbox", { name: "Текст задания для ChatGPT" });
  await expect(task).toHaveValue(new RegExp(jobId));
  await expect(task).toHaveValue(/output\.schema\.json/);
});

test("candidate requires owner review and survives reload", async ({ page }) => {
  await mockBase(page); await restore(page);
  let state = "awaiting_review";
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot(state) }));
  await page.route(`**/api/v1/bridge/jobs/${jobId}/review`, route => {
    expect(route.request().postDataJSON()).toEqual({ approve: true, output_sha256: hash });
    state = "completed"; return route.fulfill({ json: snapshot(state) });
  });
  await page.goto("/bridge");
  const review = page.getByRole("region", { name: "Проверка разбора" });
  await expect(review).toBeVisible();
  await expect(page.getByRole("button", { name: "Принять разбор" })).toBeDisabled();
  await review.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Принять разбор" }).click();
  await expect(page.getByRole("region", { name: "Готовый разбор" })).toContainText("Синий попугай у окна");
  await page.reload();
  await expect(page.getByRole("region", { name: "Готовый разбор" })).toBeVisible();
});

test("completed personal job can be scrubbed", async ({ page }) => {
  await mockBase(page); await restore(page);
  let state = "completed";
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot(state, state === "cancelled" ? {dream_text:null,result:null} : {}) }));
  await page.route(`**/api/v1/bridge/jobs/${jobId}/cancel`, route => { state = "cancelled"; return route.fulfill({ json: snapshot(state,{dream_text:null,result:null}) }); });
  await page.goto("/bridge");
  await page.getByRole("button", { name: "Удалить сон с сервера" }).click();
  await expect(page.getByRole("heading", { name: "Сон удалён" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Текст сна и результат удалены");
});

test("expired session offers a fresh start", async ({ page }) => {
  await mockBase(page); await restore(page);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ status: 401, json: { detail: "session_required" } }));
  await page.goto("/bridge");
  await expect(page.getByRole("heading", { name: "Задание недоступно" })).toBeVisible();
  await page.getByRole("button", { name: "Записать другой сон" }).click();
  await expect(page.getByRole("heading", { name: "Расскажите свой сон" })).toBeVisible();
});

test("invalid OAuth request never presents an approval action", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/v1/bridge/oauth/prepare", route => route.fulfill({ status: 400, json: { detail: "invalid_oauth_request" } }));
  await page.goto("/bridge/connect?client_id=unknown");
  await expect(page.locator('p[role="alert"]')).toContainText("Запрос подключения недействителен");
  await expect(page.getByRole("button", { name: "Разрешить тестовый доступ" })).toHaveCount(0);
});
