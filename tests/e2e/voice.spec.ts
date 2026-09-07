import { test, expect, type Page } from "@playwright/test";

// Contract double only: never exercises a real microphone or remote speech service.
async function installSpeechDouble(page: Page) {
  await page.addInitScript(() => {
    class FakeSpeech {
      lang = ""; continuous = false; interimResults = false;
      onaudiostart: (() => void) | null = null; onstart: (() => void) | null = null; onend: (() => void) | null = null;
      onresult: ((event: unknown) => void) | null = null; onerror: ((event: unknown) => void) | null = null;
      constructor() { (window as unknown as {speech: FakeSpeech}).speech = this; }
      start() { this.onstart?.(); this.onaudiostart?.(); }
      stop() { this.onend?.(); }
      abort() { this.onend?.(); }
      emit(text: string, isFinal = true) { this.onresult?.({results:[{isFinal,0:{transcript:text}}]}); }
      fail(error: string) { this.onerror?.({error}); }
    }
    Object.defineProperty(window,"SpeechRecognition",{configurable:true,value:FakeSpeech});
  });
}

test("speech contract: append final text, preserve draft, edit and submit", async ({ page }) => {
  await installSpeechDouble(page);
  await page.goto("/");
  await page.locator("#dream").fill("Мне приснился знакомый дом.");
  await page.getByLabel("Язык распознавания", {exact:true}).selectOption("en-US");
  await page.getByRole("button", {name:"Наговорить сон"}).click();
  await expect(page.getByText("Слушаем… Говорите о своём сне.")).toBeVisible();
  await expect(page.getByRole("button", {name:"Посмотреть демо-разбор"})).toBeDisabled();
  expect(await page.evaluate(() => (window as unknown as {speech:{lang:string}}).speech.lang)).toBe("en-US");
  await page.evaluate(() => (window as unknown as {speech:{emit:(s:string)=>void}}).speech.emit("I saw clear water and a key."));
  await page.getByRole("button", {name:"Остановить и вставить"}).click();
  await expect(page.locator("#dream")).toHaveValue("Мне приснился знакомый дом.\nI saw clear water and a key.");
  await page.locator("#dream").fill("Мне приснился дом и прозрачная вода. Это исправленный текст.");
  await page.getByRole("button", {name:"Посмотреть демо-разбор"}).click();
  await expect(page.getByRole("heading", {name:"Сначала — ваши ощущения"})).toBeVisible();
});

test("speech contract: cancellation discards transcript and ignores late events", async ({ page }) => {
  await installSpeechDouble(page); await page.goto("/");
  await page.locator("#dream").fill("Исходный текст остаётся без изменений.");
  await page.getByRole("button", {name:"Наговорить сон"}).click();
  await page.evaluate(() => (window as unknown as {speech:{emit:(s:string)=>void}}).speech.emit("Не добавлять."));
  await page.getByRole("button", {name:"Отменить запись"}).click();
  await page.evaluate(() => (window as unknown as {speech:{emit:(s:string)=>void}}).speech.emit("Поздний результат."));
  await expect(page.locator("#dream")).toHaveValue("Исходный текст остаётся без изменений.");
});

for (const [code, message] of [["not-allowed","Доступ к микрофону запрещён"],["audio-capture","Микрофон не найден"],["network","Сервис распознавания недоступен по сети"],["no-speech","Речь не распознана"]]) {
  test(`speech contract: ${code}`, async ({ page }) => {
    await installSpeechDouble(page); await page.goto("/");
    await page.getByRole("button", {name:"Наговорить сон"}).click();
    await page.evaluate(code => (window as unknown as {speech:{fail:(s:string)=>void}}).speech.fail(code), code);
    await expect(page.getByRole("main").getByRole("alert")).toContainText(message);
    await expect(page.getByRole("button", {name:"Наговорить сон"})).toBeEnabled();
  });
}

test("speech unavailable keeps text input usable", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window,"SpeechRecognition",{value:undefined});
    Object.defineProperty(window,"webkitSpeechRecognition",{value:undefined});
  });
  await page.goto("/");
  await expect(page.getByRole("button", {name:"Наговорить сон"})).toBeDisabled();
  await expect(page.getByText("В этом браузере распознавание речи недоступно.",{exact:false})).toBeVisible();
  await expect(page.locator("#dream")).toBeEditable();
});

test("speech contract: interim text is not committed", async ({ page }) => {
  await installSpeechDouble(page); await page.goto("/");
  await page.locator("#dream").fill("Этот текст написан до начала записи.");
  await page.getByRole("button", {name:"Наговорить сон"}).click();
  await page.evaluate(() => (window as unknown as {speech:{emit:(s:string,f:boolean)=>void}}).speech.emit("Незавершённая фраза",false));
  await expect(page.locator("#dream")).toHaveValue("Этот текст написан до начала записи.");
  await page.getByRole("button", {name:"Остановить и вставить"}).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Речь не распознана");
  await expect(page.locator("#dream")).toHaveValue("Этот текст написан до начала записи.");
});

test("speech: immediate end reports disconnect, preserves draft and never restarts", async ({ page }) => {
  await installSpeechDouble(page); await page.goto("/");
  await page.locator("#dream").fill("приснился синий попугай");
  await page.getByRole("button", {name:"Наговорить сон"}).click();
  await page.evaluate(() => (window as unknown as {speech:{onend:()=>void}}).speech.onend());
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Причина не сообщена");
  await expect(page.locator("#dream")).toHaveValue("приснился синий попугай");
  await expect(page.locator("#dream")).toBeEditable();
  await page.getByText("Подробности остановки",{exact:true}).click();
  await expect(page.locator(".voice-input code")).toContainText("ended-early; start=true; audio=true; stop=false");
  await expect(page.getByRole("button",{name:"Наговорить сон"})).toBeEnabled();
});

test("speech: network failure retains transcript for explicit recovery into edited draft", async ({ page }) => {
  await installSpeechDouble(page); await page.goto("/");
  await page.locator("#dream").fill("Исходный текст.");
  await page.getByRole("button",{name:"Наговорить сон"}).click();
  await page.evaluate(() => { const s=(window as unknown as {speech:{emit:(s:string)=>void,fail:(s:string)=>void}}).speech; s.emit("Синий попугай"); s.fail("network"); });
  await expect(page.locator("#dream")).toHaveValue("Исходный текст.");
  await expect(page.getByLabel("Текст, полученный до остановки")).toHaveValue("Синий попугай");
  await page.locator("#dream").fill("Исправленный черновик.");
  await page.getByRole("button",{name:"Добавить этот текст"}).click();
  await expect(page.locator("#dream")).toHaveValue("Исправленный черновик.\nСиний попугай");
  await expect(page.getByRole("button",{name:"Добавить этот текст"})).toHaveCount(0);
});

test("speech: start constructor rejection leaves form usable", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(window,"SpeechRecognition",{value:class {constructor(){throw new DOMException("denied","NotAllowedError");}}}));
  await page.goto("/"); await page.getByRole("button",{name:"Наговорить сон"}).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Доступ к микрофону запрещён");
  await expect(page.locator("#dream")).toBeEditable();
});

test("speech: stalled startup times out without claiming to listen", async ({ page }) => {
  await installSpeechDouble(page); await page.goto("/"); await page.clock.install();
  await page.evaluate(() => { const c=(window as unknown as {SpeechRecognition:{prototype:{start:()=>void}}}).SpeechRecognition; c.prototype.start=function() {}; });
  await page.getByRole("button",{name:"Наговорить сон"}).click();
  await expect(page.getByText("Слушаем… Говорите о своём сне.")).toHaveCount(0);
  await page.clock.fastForward(21000);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Браузер не завершил");
  await expect(page.locator("#dream")).toBeEditable();
});

test("speech: stop timeout retains interim text and ignores stale callbacks", async ({ page }) => {
  await installSpeechDouble(page); await page.goto("/"); await page.clock.install();
  await page.getByRole("button",{name:"Наговорить сон"}).click();
  await page.evaluate(() => { const s=(window as unknown as {speech:{emit:(s:string,f:boolean)=>void,stop:()=>void}}).speech; s.emit("Незавершённая фраза",false); s.stop=()=>{}; });
  await page.getByRole("button",{name:"Остановить и вставить"}).click();
  await page.clock.fastForward(11000);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Браузер не завершил");
  await expect(page.getByLabel("Текст, полученный до остановки")).toHaveValue("Незавершённая фраза");
  await expect(page.locator("#dream")).toBeEditable();
});
