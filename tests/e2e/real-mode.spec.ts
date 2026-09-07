import { test, expect } from "@playwright/test";

test("real-mode UI labels, consent notice and saved result provenance", async ({ page }) => {
  await page.route("**/api/v1/**", async route => {
    const path=new URL(route.request().url()).pathname;
    let json: unknown = {};
    if(path.endsWith('/config')) json={ai_mode:'real',supported_locales:['ru-RU','en-US']};
    else if(path.endsWith('/sessions/guest')) json={guest_session_id:'test'};
    else if(path.endsWith('/dreams')) json={dream_id:'test-real'};
    else if(path.endsWith('/free')) json={status:'completed',stage:'completed',result:{title:'Синий попугай',symbols:[{label:'Синий попугай',evidence:'синий попугай'}],emotional_tone:'Не указано',summary:'В вашем рассказе выделяется синий попугай. Что для вас значит его цвет?',key_connection:'Связь цвета и птицы зависит от ваших ассоциаций.',reflection_question:'Что делала птица и что вы чувствовали?',locked_sections:['1','2','3','4','5'],mode:'real',locale:'ru-RU',evidence_basis:'model_from_user_text',research_retrieval_used:false}};
    await route.fulfill({json});
  });
  await page.goto('/');
  await expect(page.getByText('AI-разбор · локальная версия',{exact:true})).toBeVisible();
  await expect(page.getByText('При отправке текст сна и указанный контекст передаются OpenAI',{exact:false})).toBeVisible();
  await page.locator('#dream').fill('приснился синий попугай');
  await page.getByRole('button',{name:'Разобрать мой сон'}).click();
  await expect(page.getByRole('heading',{name:'Синий попугай',exact:true})).toBeVisible();
  await expect(page.getByText('Бесплатный результат · AI',{exact:true})).toBeVisible();
  await expect(page.getByText('Демонстрационный режим',{exact:true})).toHaveCount(0);
  await page.screenshot({path:`test-results/real-${test.info().project.name}.png`,fullPage:true});
});

test("real failure is shown without substituting demo output", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('sonnic.currentDream','failed-real'));
  await page.route('**/api/v1/config',route=>route.fulfill({json:{ai_mode:'real',supported_locales:['ru-RU','en-US']}}));
  await page.route('**/api/v1/dreams/failed-real/free',route=>route.fulfill({json:{status:'failed',stage:'failed',result:null,error:'provider_auth',can_retry:false}}));
  await page.goto('/');
  await expect(page.getByRole('main').getByRole('alert')).toContainText('OpenAI не принял настройки доступа');
  await expect(page.getByRole('button',{name:'Повторить обработку'})).toHaveCount(0);
  await expect(page.getByText('Сначала — ваши ощущения',{exact:true})).toHaveCount(0);
});
