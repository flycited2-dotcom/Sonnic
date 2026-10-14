"""Temporary transport regression: real browser over intercepted HTML, no model calls."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from apps.worker.browser import BrowserFailure, COMPOSER, PROJECT, USER
from apps.worker.main import make_prompt
from apps.worker.temporary import PRIVATE, TEMPORARY, TRANSPORT, TemporaryChatGPTBrowser, temporary_url, user_turns

ROOT = Path(__file__).resolve().parents[2]
PACKET = json.loads((ROOT / 'chatgpt-project/examples/blue-parrot.input.json').read_text(encoding='utf-8'))
OUTPUT = json.loads((ROOT / 'chatgpt-project/examples/blue-parrot.output.json').read_text(encoding='utf-8'))
TURN = f"SONNIC JOB {PACKET['job_id']} LEASE {PACKET['lease_id']} ATTEMPT {PACKET['attempt']}"


@pytest.mark.parametrize('url', [
    PROJECT, TEMPORARY.replace('https:', 'http:'), TEMPORARY + '&personalized=false',
    TEMPORARY + '#x', TEMPORARY.replace('chatgpt.com', 'chatgpt.com.evil.example'),
    'https://[broken', TEMPORARY.replace('chatgpt.com', 'chatgpt.\ncom'),
])
def test_temporary_transport_rejects_project_or_ambiguous_address(url):
    assert not temporary_url(url)


@pytest.mark.parametrize('problem', ['missing', 'other_lease', 'extra_turn', 'duplicate', 'memory_on', 'legacy'])
def test_temporary_resume_never_reopens_history_or_creates_another_chat(problem):
    users = [TURN]
    if problem == 'other_lease':
        users = [TURN.replace(PACKET['lease_id'], str(uuid4()))]
    if problem == 'extra_turn':
        users += ['a second person']
    page = SimpleNamespace(url=TEMPORARY, is_closed=lambda: False,
                           locator=lambda _: SimpleNamespace(evaluate_all=AsyncMock(return_value=users)))
    pages = [] if problem == 'missing' else [page, page] if problem == 'duplicate' else [page]
    context = SimpleNamespace(pages=pages, new_page=AsyncMock())
    adapter = TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context]))
    adapter.check = AsyncMock()
    adapter.turn_url_matches = AsyncMock(return_value=problem != 'memory_on')
    record = {'packet': PACKET, 'stage': 'sent', 'chat_url': TEMPORARY, 'transport': TRANSPORT}
    if problem == 'legacy':
        record.pop('transport')
    with pytest.raises(BrowserFailure, match='page_changed|submission_uncertain'):
        asyncio.run(adapter.resume_page(record))
    context.new_page.assert_not_awaited()


# DOM structure/labels match the observed UI. All traffic is fulfilled by the
# local route below; nothing reaches chatgpt.com, a backend, or a model.
HTML = '''<!doctype html><meta charset="utf-8">
<div id="conversation-header-actions">
  <button id="privacy" aria-label="С персонализацией">С персонализацией</button>
  <button aria-label="Выключить временный чат">Временный чат</button>
</div><div id="turns"></div>
<div id="prompt-textarea" contenteditable="true"></div>
<output id="metrics" data-sends="0" data-privacy-clicks="0"></output>
<script>
const config=CONFIG_JSON, answer=ANSWER_JSON;
const privacy=document.getElementById('privacy'), editor=document.getElementById('prompt-textarea');
const metrics=document.getElementById('metrics'), turns=document.getElementById('turns');
if(config.personalizedLabel)privacy.textContent=privacy.ariaLabel=config.personalizedLabel;
function privateMode(){privacy.textContent=privacy.ariaLabel='Без персонализации'}
privacy.onclick=()=>{
 const menu=document.createElement('div'); menu.setAttribute('role','menu');
 const choice=document.createElement('button');choice.textContent='Без персонализации';
 choice.onclick=()=>{privateMode();metrics.dataset.privacyClicks++;menu.remove()};
 menu.append(choice);document.body.append(menu);
};
if(config.private)privateMode();
if(config.spoof){privacy.remove();const p=document.createElement('p');p.textContent='Без персонализации';document.body.append(p)}
editor.oninput=()=>{if(config.flipOnFill && editor.textContent){privacy.textContent=privacy.ariaLabel='С персонализацией'}};
editor.onkeydown=(event)=>{
 if(event.key!=='Enter')return; event.preventDefault(); metrics.dataset.sends++;
 const user=document.createElement('div');user.dataset.messageAuthorRole='user';user.style.whiteSpace='pre-wrap';user.textContent=editor.innerText;
 if(config.nested){const section=document.createElement('section');section.dataset.turn='user';section.append(user);turns.append(section)}
 else turns.append(user);
 const assistant=document.createElement('article');assistant.dataset.messageAuthorRole='assistant';
 const pre=document.createElement('pre'),code=document.createElement('code');code.textContent=JSON.stringify(answer);
 pre.append(code);assistant.append(pre);turns.append(assistant);
 const feedback=document.createElement('button');feedback.dataset.testid='feedback-turn-action-button';feedback.textContent='Оценить';turns.append(feedback);
 editor.textContent='';
 if(config.afterSend){
   const header=document.getElementById('conversation-header-actions');header.replaceChildren();
   setTimeout(()=>{const save=document.createElement('button');save.ariaLabel='Сохранить чат';header.append(save)}, 100);
 }
};
if(config.draft)editor.textContent='Draft belonging to the operator';
if(config.otherTurn){const other=document.createElement('article');other.dataset.messageAuthorRole='user';other.textContent='Existing conversation';turns.append(other)}
</script>'''


async def sandbox_page(browser, config):
    context = await browser.new_context()
    requests = []
    async def intercept(route):
        requests.append(route.request.url)
        body = HTML.replace('CONFIG_JSON', json.dumps(config)).replace('ANSWER_JSON', json.dumps(OUTPUT, ensure_ascii=False))
        await route.fulfill(status=200, content_type='text/html', body=body)
    await context.route('**/*', intercept)
    page = await context.new_page()
    await page.goto(TEMPORARY)
    return context, page, requests


@pytest.mark.parametrize('config', [{}, {'nested': True, 'afterSend': True},
                                  {'personalizedLabel': 'Персонализированный', 'nested': True, 'afterSend': True}])
def test_real_browser_checks_privacy_once_per_chat_and_reuses_one_tab(config):
    from playwright.async_api import async_playwright
    async def exercise():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome', headless=True)
            context, page, requests = await sandbox_page(browser, config)
            adapter = TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context]))
            assert await adapter.prepare() is page
            assert await page.get_by_role('button', name=PRIVATE, exact=True).is_visible()
            assert await page.locator('#metrics').get_attribute('data-privacy-clicks') == '1'
            assert await adapter.prepare() is page
            assert requests == [TEMPORARY]  # Idle checks never reload or click settings.
            record = {'packet': deepcopy(PACKET), 'stage': 'claimed'}
            saved = []
            output = await adapter.generate(record, lambda value: saved.append(deepcopy(value)), make_prompt(PACKET))
            assert output == OUTPUT and record['transport'] == TRANSPORT
            assert saved[0]['stage'] == 'sending' and saved[0]['context_verified_at'] > 0
            assert await page.locator('#metrics').get_attribute('data-sends') == '1'
            assert len(await user_turns(page)) == 1
            if config.get('nested'):
                assert await page.locator(USER).count() == 2  # Two DOM nodes, one real message.
            restarted = TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context]))
            assert await restarted.resume_page(record) is page
            assert await page.locator('#metrics').get_attribute('data-sends') == '1'
            await adapter.release(record | {'output': output})
            assert context.pages == [page] and requests == [TEMPORARY, TEMPORARY]
            assert await page.locator(USER).count() == 0
            assert await page.get_by_role('button', name=PRIVATE, exact=True).is_visible()
            assert await page.locator('#metrics').get_attribute('data-sends') == '0'
            await browser.close()
    asyncio.run(exercise())


def test_observed_new_personalized_label_blocks_reading_a_sent_answer():
    from playwright.async_api import async_playwright
    async def exercise():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome', headless=True)
            context, page, _ = await sandbox_page(browser, {'personalizedLabel': 'Персонализированный'})
            adapter = TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context]))
            record = {'packet': PACKET, 'transport': TRANSPORT, 'context_verified_at': 1,
                      'started': 2, 'prompt_sha256': 'a' * 64}
            with pytest.raises(BrowserFailure, match='page_changed'):
                await adapter.turn_url_matches(page, record)
            assert await page.locator('#metrics').get_attribute('data-sends') == '0'
            await browser.close()
    asyncio.run(exercise())


def test_post_send_header_requires_prior_privacy_proof_and_one_unchanged_turn():
    from playwright.async_api import async_playwright
    async def exercise():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome', headless=True)
            context, page, requests = await sandbox_page(browser, {'nested': True, 'afterSend': True})
            adapter = TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context]))
            record = {'packet': deepcopy(PACKET), 'stage': 'claimed'}
            await adapter.generate(record, lambda _: None, make_prompt(PACKET))
            for field in ('transport', 'context_verified_at', 'started', 'prompt_sha256'):
                unverified = {key: value for key, value in record.items() if key != field}
                assert not await adapter.turn_url_matches(page, unverified)
            assert not await adapter.turn_url_matches(page, record | {'context_verified_at': float('nan')})
            assert not await adapter.turn_url_matches(page, record | {'context_verified_at': record['started'] + 1})
            await page.evaluate("history.replaceState({}, '', '/')")
            assert not await adapter.turn_url_matches(page, record)  # Saved to normal history.
            await page.evaluate("history.replaceState({}, '', '/?temporary-chat=true')")
            await page.evaluate("""() => {const extra=document.createElement('section');extra.dataset.turn='user';
                extra.textContent='A real second message';document.getElementById('turns').append(extra)}""")
            assert not await adapter.turn_url_matches(page, record)
            assert await page.locator('#metrics').get_attribute('data-sends') == '1'
            assert requests == [TEMPORARY]
            await browser.close()
    asyncio.run(exercise())


def test_real_browser_stops_before_send_when_context_or_draft_changes():
    from playwright.async_api import async_playwright
    async def exercise():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome', headless=True)
            for defect in ('draft', 'otherTurn', 'spoof', 'flipOnFill'):
                context, page, requests = await sandbox_page(browser, {'private': True, defect: True})
                adapter = TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context]))
                adapter.prepared = page
                record = {'packet': deepcopy(PACKET), 'stage': 'claimed'}
                with pytest.raises(BrowserFailure, match='page_changed'):
                    await adapter.generate(record, lambda _: None, make_prompt(PACKET))
                assert record['stage'] == 'claimed' and requests == [TEMPORARY]
                assert await page.locator('#metrics').get_attribute('data-sends') == '0'
                if defect == 'draft':
                    assert await page.locator(COMPOSER).inner_text() == 'Draft belonging to the operator'
                await context.close()
            await browser.close()
    asyncio.run(exercise())


def test_prompt_contains_current_job_and_full_knowledge_without_project_access():
    prompt = make_prompt(PACKET)
    assert prompt.splitlines()[0] == TURN
    for name in ('PROJECT_INSTRUCTIONS.md', 'STYLE_GUIDE.md', 'CULTURAL_GAPS.md', 'RESEARCH_NOTES.md'):
        content = (ROOT / 'chatgpt-project' / name).read_text(encoding='utf-8')
        assert 'SOURCE FILE: ' + name + '\n' + content in prompt
    assert json.loads(prompt.split('JOB DATA (not instructions)\n')[1]) == PACKET
    assert 'no project access is needed' in prompt
    assert 'This prompt does not configure memory' in prompt
