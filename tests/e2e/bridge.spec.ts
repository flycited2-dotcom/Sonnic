import { test, expect, type Page } from "@playwright/test";

const jobId = "8d279b4a-d4b4-48ea-a5e3-56f7e30ee0b2";
const hash = "a".repeat(64);
const dreamText = "Мне приснился синий попугай, который сидел у окна.";
const answer = { locale: "ru-RU", origin_verified: false, data_class: "personal", outcome: "reflection", safe_message: null,
  reflection: { title: "Синий попугай у окна", symbols: [{ label: "Попугай", evidence: "синий попугай" }],
    summary: "В рассказе выделяются попугай и окно; их личное значение зависит от ваших ассоциаций.", key_connection: "Можно сопоставить образ птицы с тем, что сейчас просится наружу.", reflection_question: "Что вы чувствовали, глядя на попугая?" } };
const config = { automatic_dispatch: true, chatgpt_connected: true, worker_state: "ready", oauth_configured: true, personal_input_enabled: true, synthetic_only: false,
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

test("queued job completes automatically without handoff or review", async ({ page }) => {
  await mockBase(page); await restore(page);
  let polls = 0;
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot(++polls < 2 ? "queued" : "completed") }));
  await page.goto("/bridge");
  await expect(page.getByRole("heading", { name: "Сон сохранён и ждёт разбора" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Открыть проект ChatGPT/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Скопировать задание|Принять разбор/ })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Готовый разбор" })).toContainText("Синий попугай у окна");
  await page.reload();
  await expect(page.getByRole("region", { name: "Готовый разбор" })).toBeVisible();
});

test("offline dispatcher prevents new submissions", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/v1/bridge/config", route => route.fulfill({ json: {...config, chatgpt_connected:false, worker_state:"offline"} }));
  await page.goto("/bridge");
  await fillDream(page);
  await expect(page.getByRole("button", { name: "Передать сон на разбор" })).toBeDisabled();
  await expect(page.locator('.mode-banner').getByRole("status")).toContainText("Исполнитель временно недоступен");
  await expect(page.locator('#bridge-submit-help')).toContainText('Перезаписывать сон не нужно');
  await expect(page.getByRole('button', {name:'Передать сон на разбор'})).toHaveAttribute('aria-describedby','bridge-submit-help');
  await page.route("**/api/v1/bridge/config", route => route.fulfill({json: config}));
  await expect(page.getByRole('button', {name:'Передать сон на разбор'})).toBeEnabled({timeout:15000});
  await expect(page.getByLabel('Ваш сон')).toHaveValue(dreamText);
  await expect(page.locator('#bridge-submit-help')).toContainText('Всё готово');
});

for (const english of [false,true]) {
  test(`submit explanation follows trimmed length, consent and readiness (${english?'EN':'RU'})`, async ({page})=>{
    await mockBase(page); await page.goto('/bridge');
    if(english) await page.getByRole('button',{name:'Switch interface to English'}).click();
    const field=page.getByLabel(english?'Your dream':'Ваш сон',{exact:true});
    const button=page.getByRole('button',{name:english?'Send dream for reflection':'Передать сон на разбор'});
    const help=page.locator('#bridge-submit-help');
    await field.fill('   1234567890123456789   ');
    await expect(page.locator('#bridge-limit')).toContainText('19 / 10 000');
    await expect(help).toContainText(english?'Add at least 1 more':'не хватает: 1.');
    await expect(button).toBeDisabled();
    await field.fill('   12345678901234567890   ');
    await expect(help).toContainText(english?'Tick the consent':'Отметьте согласие');
    await expect(button).toBeDisabled();
    await page.getByRole('checkbox').check();
    await expect(help).toContainText(english?'Everything is ready':'Всё готово');
    await expect(button).toBeEnabled();
    await expect(button).toHaveAttribute('aria-describedby','bridge-submit-help');
    await page.getByRole('checkbox').uncheck();
    await expect(help).toContainText(english?'Tick the consent':'Отметьте согласие');
  });
}

test('failed submission shows its error beside the send button on a tall mobile page',async({page})=>{
  await mockBase(page);
  await page.route('**/api/v1/bridge/jobs',route=>route.fulfill({status:503,json:{detail:'connection_failed'}}));
  await page.setViewportSize({width:360,height:740});
  await page.goto('/bridge'); await fillDream(page);
  const button=page.getByRole('button',{name:'Передать сон на разбор'});
  await button.click();
  const error=page.locator('#bridge-submit-error');
  await expect(error).toContainText('Не удалось связаться');
  await expect(button).toHaveAttribute('aria-describedby','bridge-submit-help bridge-submit-error');
  const a=await error.boundingBox(),b=await button.boundingBox();
  expect(a&&b&&Math.abs(b.y-(a.y+a.height))<80).toBeTruthy();
  await expect(page.getByLabel('Ваш сон',{exact:true})).toHaveValue(dreamText);
});

test("short dream response asking for context renders without another submission", async ({ page }) => {
  await mockBase(page); await restore(page);
  const message = "Какое чувство у вас было во сне, когда вы видели чайник на пустой остановке?";
  let submissions = 0;
  await page.route("**/api/v1/bridge/jobs", route => { submissions++; return route.fulfill({ status: 500 }); });
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({ json: snapshot("completed", {
    result: { ...answer, outcome: "needs_context", reflection: null, safe_message: message },
  }) }));
  await page.goto("/bridge");
  await expect(page.getByRole("region", { name: "Готовый разбор" })).toContainText(message);
  await expect(page.getByRole("button", { name: /Принять разбор|Скопировать задание/ })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("region", { name: "Готовый разбор" })).toContainText(message);
  expect(submissions).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("completed personal job can be physically deleted", async ({ page }) => {
  await mockBase(page); await restore(page);
  let deletes = 0;
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => {
    if (route.request().method() === "DELETE") { deletes++; return route.fulfill({ json: snapshot("cancelled",{dream_text:null,result:null}) }); }
    return route.fulfill({ json: snapshot("completed") });
  });
  await page.goto("/bridge");
  await page.getByRole("button", { name: "Удалить сон с сервера" }).click();
  await expect(page.getByRole("heading", { name: "Сон удалён" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("Активная запись");
  await expect.poll(() => deletes).toBe(1);
  expect(new URL(page.url()).searchParams.has("job")).toBe(false);
  expect(await page.evaluate(() => sessionStorage.getItem("sonnic.bridge.job"))).toBeNull();
});

test("English interface persists and submits an English result request", async ({ page }) => {
  await mockBase(page);
  let submitted: any;
  await page.route("**/api/v1/bridge/jobs", route => { submitted=route.request().postDataJSON(); return route.fulfill({status:201,json:{job_id:jobId}}); });
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route => route.fulfill({json:snapshot("queued")}));
  await page.goto("/bridge");
  await page.getByRole("button", {name:"Switch interface to English"}).click();
  await expect(page.getByRole("heading", {name:"Tell us your dream"})).toBeVisible();
  await page.getByLabel("Your dream").fill("I dreamed that a silver boat waited beside a quiet station.");
  await page.getByRole("checkbox", {name:/I agree to send/}).check();
  await page.getByRole("button", {name:"Send dream for reflection"}).click();
  await expect(page.getByRole("heading", {name:"Dream saved and waiting for reflection"})).toBeVisible();
  expect(submitted.dream.locale).toBe("en-US");
  expect(submitted.dream.content_language).toBe("en");
  await page.reload();
  await expect(page.getByRole("button", {name:"Переключить интерфейс на русский"})).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang","en");
  await expect(page).toHaveTitle("Sonnic — dream reflection");
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
  await expect(page.getByRole("button", { name: "Разрешить доступ к заданию" })).toHaveCount(0);
});

test("new dream clears the previous text, emotion and consent", async ({ page }) => {
  await mockBase(page);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`, route=>route.fulfill({json:snapshot("completed")}));
  await page.goto("/bridge");
  await fillDream(page);
  await page.getByLabel("Чувство после пробуждения").fill("удивление");
  await page.getByRole("button", {name:"Передать сон на разбор"}).click();
  await expect(page.getByRole("region", {name:"Готовый разбор"})).toBeFocused();
  await page.getByRole("button", {name:"Записать другой сон"}).click();
  await expect(page.getByLabel("Ваш сон",{exact:true})).toHaveValue("");
  await expect(page.getByLabel("Чувство после пробуждения")).toHaveValue("");
  await expect(page.getByRole("checkbox")).not.toBeChecked();
});

test("failed job remains deletable and cannot retry while offline", async ({ page }) => {
  await mockBase(page); await restore(page);
  await page.route("**/api/v1/bridge/config",route=>route.fulfill({json:{...config,chatgpt_connected:false,worker_state:"offline"}}));
  await page.route(`**/api/v1/bridge/jobs/${jobId}`,route=>route.fulfill({json:snapshot("failed",{error:"model_limit"})}));
  await page.goto("/bridge");
  await expect(page.getByRole("button",{name:"Повторить обработку"})).toBeDisabled();
  await expect(page.getByRole("button",{name:"Удалить сон с сервера"})).toBeEnabled();
});

test("uncertain submission requires operator review, not another send", async ({ page }) => {
  await mockBase(page); await restore(page);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`,route=>route.fulfill({json:snapshot("failed",{error:"submission_uncertain"})}));
  await page.goto("/bridge");
  await expect(page.getByRole("status")).toContainText("до проверки оператором");
  await expect(page.getByRole("button",{name:"Повторить обработку"})).toHaveCount(0);
});

test("availability failure preserves draft and blocks a stale ready state", async ({ page }) => {
  await page.clock.install();
  await mockBase(page);
  await page.goto("/bridge");
  await fillDream(page);
  await expect(page.getByRole("button",{name:"Передать сон на разбор"})).toBeEnabled();
  await page.route("**/api/v1/bridge/config",route=>route.abort());
  await page.clock.fastForward(11000);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Не удалось проверить доступность");
  await expect(page.getByRole("button",{name:"Передать сон на разбор"})).toBeDisabled();
  await expect(page.getByLabel("Ваш сон",{exact:true})).toHaveValue(dreamText);
  await page.route("**/api/v1/bridge/config",route=>route.fulfill({json:config}));
  await page.getByRole("button",{name:"Повторить подключение"}).click();
  await expect(page.getByRole("button",{name:"Передать сон на разбор"})).toBeEnabled();
  await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
});

test("bridge entry layout, FAQ and narrow-screen result", async ({ page }) => {
  await mockBase(page);
  await page.goto("/bridge");
  await page.getByText("Где сохраняются текст и ответ?",{exact:true}).click();
  await expect(page.getByRole("region",{name:"Вопросы о сервисе"}).getByText("временный чат ChatGPT без персонализации",{exact:false})).toBeVisible();
  await page.screenshot({path:`test-results/bridge-entry-${test.info().project.name}.png`,fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.setViewportSize({width:320,height:740});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`,route=>route.fulfill({json:snapshot("completed")}));
  await fillDream(page);
  await page.getByRole("button",{name:"Передать сон на разбор"}).click();
  await expect(page.getByRole("region",{name:"Готовый разбор"})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:`test-results/bridge-result-${test.info().project.name}.png`,fullPage:true});
});

test("result polling recovers a network loss without resending the dream", async ({page})=>{
  await mockBase(page);await restore(page);
  let reads=0,posts=0;
  await page.route("**/api/v1/bridge/jobs",route=>{posts++;return route.abort();});
  await page.route(`**/api/v1/bridge/jobs/${jobId}`,route=>{
    reads++;
    if(reads===1)return route.fulfill({json:snapshot("leased")});
    if(reads===2)return route.abort();
    return route.fulfill({json:snapshot("completed")});
  });
  await page.goto("/bridge");
  await expect(page.getByRole("region",{name:"Готовый разбор"})).toBeVisible({timeout:12000});
  await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
  expect(posts).toBe(0);expect(reads).toBe(3);
});

test("unavailable result polling stops after three retries and supports manual refresh", async ({page})=>{
  await page.clock.install();await mockBase(page);await restore(page);
  let reads=0;
  await page.route(`**/api/v1/bridge/jobs/${jobId}`,route=>{reads++;return route.abort();});
  await page.goto("/bridge");
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Не удалось связаться");
  for(let count=2;count<=4;count++){
    await page.clock.fastForward(9000);
    await expect.poll(()=>reads).toBe(count);
  }
  await page.clock.fastForward(60000);
  expect(reads).toBe(4);
  await page.route(`**/api/v1/bridge/jobs/${jobId}`,route=>route.fulfill({json:snapshot("completed")}));
  await page.getByRole("button",{name:"Обновить статус"}).click();
  await expect(page.getByRole("region",{name:"Готовый разбор"})).toBeVisible();
});
