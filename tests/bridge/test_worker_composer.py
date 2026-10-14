"""Composer readiness and tab selection against intercepted local HTML.

These tests use a separate headless Chrome. Every request is fulfilled locally;
they do not attach to the owner's browser or contact ChatGPT or a model.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.worker import browser as browser_module
from apps.worker.browser import BrowserFailure, COMPOSER
from apps.worker.temporary import PRIVATE, TEMPORARY, TemporaryChatGPTBrowser


EDITOR = '<div id="prompt-textarea" role="textbox" contenteditable="true"></div>'
PRIVATE_DRAFT = 'A private draft that must remain untouched'
ATTRIBUTE_SECRET = 'arbitrary-attribute-content-must-not-be-reported'
HTML = '''<!doctype html><meta charset="utf-8">
<style>#prompt-textarea { min-height:24px }</style>
<div id="conversation-header-actions">
  <button id="privacy" aria-label="С персонализацией">С персонализацией</button>
  <button aria-label="Выключить временный чат">Временный чат</button>
</div>
<div id="editor-slot">EDITOR_HTML</div>
<output id="metrics" data-privacy-clicks="0"></output>
<script>
const privacy=document.getElementById('privacy'), metrics=document.getElementById('metrics');
privacy.onclick=()=>{
 metrics.dataset.privacyClicks++;
 const menu=document.createElement('div'); menu.setAttribute('role','menu');
 const choice=document.createElement('button');choice.textContent='Без персонализации';
 choice.onclick=()=>{privacy.textContent=privacy.ariaLabel='Без персонализации';menu.remove()};
 menu.append(choice);document.body.append(menu);
};
</script>'''


class BoundedComposerLocator:
    """Keep absent/hidden-editor failures short without changing real DOM checks."""

    def __init__(self, locator, timeout):
        self.locator, self.timeout = locator, timeout

    def __getattr__(self, name):
        return getattr(self.locator, name)

    async def wait_for(self, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout', self.timeout), self.timeout)
        return await self.locator.wait_for(**kwargs)


class BoundedPage:
    def __init__(self, page, timeout=250):
        self.page, self.timeout = page, timeout

    def __getattr__(self, name):
        return getattr(self.page, name)

    def locator(self, selector, **kwargs):
        locator = self.page.locator(selector, **kwargs)
        return BoundedComposerLocator(locator, self.timeout) if selector == COMPOSER else locator


@pytest.fixture(scope='module')
def local_browser():
    from playwright.async_api import async_playwright

    runner = asyncio.Runner()
    manager = async_playwright()
    playwright = runner.run(manager.__aenter__())
    browser = runner.run(playwright.chromium.launch(channel='chrome', headless=True))
    try:
        yield browser, runner.run
    finally:
        runner.run(browser.close())
        runner.run(manager.__aexit__(None, None, None))
        runner.close()


async def sandbox(browser, editor_htmls):
    context = await browser.new_context()
    requests = []
    pages = []
    def interceptor(body):
        async def intercept(route):
            requests.append(route.request.url)
            await route.fulfill(status=200, content_type='text/html', body=body)
        return intercept
    for editor_html in editor_htmls:
        page = await context.new_page()
        await page.route('**/*', interceptor(HTML.replace('EDITOR_HTML', editor_html)))
        await page.goto(TEMPORARY)
        pages.append(page)
    return context, pages, requests


def adapter_for(pages, *, timeout=250):
    wrapped = [BoundedPage(page, timeout) for page in pages]
    context = SimpleNamespace(pages=wrapped, new_page=AsyncMock(
        side_effect=AssertionError('Existing temporary pages must not trigger another tab')))
    return TemporaryChatGPTBrowser(SimpleNamespace(contexts=[context])), context, wrapped


async def assert_untouched(pages, requests, context):
    assert requests == [TEMPORARY] * len(pages)
    context.new_page.assert_not_awaited()
    for page in pages:
        assert await page.locator('#metrics').get_attribute('data-privacy-clicks') == '0'


def assert_safe_diagnostic(diagnostic):
    assert set(diagnostic) == {'canonical_count', 'strict_count', 'visible_count',
                               'editable_count', 'tag', 'contenteditable'}
    for name in ('canonical_count', 'strict_count', 'visible_count', 'editable_count'):
        assert type(diagnostic[name]) is int and 0 <= diagnostic[name] <= 100
    assert diagnostic['tag'] in {'div', 'textarea', 'other', 'missing'}
    assert diagnostic['contenteditable'] in {'true', 'plaintext-only', 'false', 'missing', 'other'}
    encoded = json.dumps(diagnostic)
    assert PRIVATE_DRAFT not in encoded and ATTRIBUTE_SECRET not in encoded


def test_delayed_composer_reuses_existing_temporary_page_without_navigation(local_browser):
    browser, run = local_browser
    async def exercise():
        context, pages, requests = await sandbox(browser, [''])
        try:
            page = pages[0]
            assert await page.locator(COMPOSER).count() == 0
            await page.evaluate('''html => setTimeout(() => {
                document.getElementById('editor-slot').innerHTML=html;
            }, 300)''', EDITOR)
            adapter, selected_context, wrapped = adapter_for(pages, timeout=1500)
            assert await adapter.prepare() is wrapped[0]
            assert await page.get_by_role('button', name=PRIVATE, exact=True).is_visible()
            assert requests == [TEMPORARY] and context.pages == pages
            selected_context.new_page.assert_not_awaited()
            assert await page.locator('#metrics').get_attribute('data-privacy-clicks') == '1'
        finally:
            await context.close()
    run(exercise())


@pytest.mark.parametrize('markup,expected', [
    (EDITOR + EDITOR, {'canonical_count': 2, 'strict_count': 2,
                      'visible_count': 2, 'editable_count': 2}),
    (EDITOR.replace('role="textbox"', 'role="textbox" style="display:none"'),
     {'canonical_count': 1, 'strict_count': 1, 'visible_count': 0,
      'tag': 'div', 'contenteditable': 'true'}),
    (EDITOR.replace('role="textbox"', 'role="textbox" aria-disabled="true"'),
     {'canonical_count': 1, 'strict_count': 1, 'visible_count': 1,
      'editable_count': 0, 'tag': 'div', 'contenteditable': 'true'}),
    (EDITOR.replace('contenteditable="true"', 'contenteditable="plaintext-only"'),
     {'canonical_count': 1, 'strict_count': 0, 'visible_count': 1,
      'tag': 'div', 'contenteditable': 'plaintext-only'}),
    (EDITOR.replace('contenteditable="true"', 'contenteditable="false"'),
     {'canonical_count': 1, 'strict_count': 0, 'visible_count': 1,
      'editable_count': 0, 'tag': 'div', 'contenteditable': 'false'}),
    ('<textarea id="prompt-textarea" placeholder="Спросить ChatGPT"></textarea>',
     {'canonical_count': 1, 'strict_count': 0, 'visible_count': 1,
      'tag': 'textarea', 'contenteditable': 'missing'}),
    (f'<textarea id="prompt-textarea" contenteditable="true">{PRIVATE_DRAFT}</textarea>',
     {'canonical_count': 1, 'strict_count': 0, 'visible_count': 1,
      'editable_count': 1, 'tag': 'textarea', 'contenteditable': 'true'}),
    (EDITOR.replace('contenteditable="true"', f'contenteditable="{ATTRIBUTE_SECRET}"'),
     {'canonical_count': 1, 'strict_count': 0, 'visible_count': 1,
      'tag': 'div', 'contenteditable': 'other'}),
    ('', {'canonical_count': 0, 'strict_count': 0, 'visible_count': 0,
          'editable_count': 0, 'tag': 'missing', 'contenteditable': 'missing'}),
], ids=['duplicate', 'hidden', 'disabled', 'plaintext-only', 'false', 'textarea',
        'textarea-with-editable-attribute', 'unknown-attribute', 'absent'])
def test_unready_or_ambiguous_composer_fails_before_privacy_changes(local_browser, markup, expected):
    browser, run = local_browser
    async def exercise():
        context, pages, requests = await sandbox(browser, [markup])
        try:
            adapter, selected_context, _ = adapter_for(pages)
            with pytest.raises(BrowserFailure, match='page_changed') as caught:
                await adapter.prepare()
            assert caught.value.context_check == 'composer_visible'
            diagnostic = caught.value.composer
            assert_safe_diagnostic(diagnostic)
            assert {key: diagnostic[key] for key in expected} == expected
            if markup.startswith('<textarea') and PRIVATE_DRAFT in markup:
                assert await pages[0].locator('#prompt-textarea').input_value() == PRIVATE_DRAFT
            await assert_untouched(pages, requests, selected_context)
            assert context.pages == pages
        finally:
            await context.close()
    run(exercise())


def test_private_draft_is_preserved_and_diagnostic_contains_no_text_or_arbitrary_attributes(local_browser):
    browser, run = local_browser
    markup = EDITOR.replace('></div>', f' data-private="{ATTRIBUTE_SECRET}">{PRIVATE_DRAFT}</div>')
    async def exercise():
        context, pages, requests = await sandbox(browser, [markup])
        try:
            diagnostic = await browser_module.composer_diagnostic(pages[0])
            assert_safe_diagnostic(diagnostic)
            adapter, selected_context, _ = adapter_for(pages)
            with pytest.raises(BrowserFailure, match='page_changed') as caught:
                await adapter.prepare()
            assert caught.value.context_check == 'empty_chat'
            assert await pages[0].locator(COMPOSER).inner_text() == PRIVATE_DRAFT
            await assert_untouched(pages, requests, selected_context)
        finally:
            await context.close()
    run(exercise())


@pytest.mark.parametrize('editors', [[EDITOR, EDITOR], ['', '']], ids=['two-ready', 'two-waiting'])
def test_multiple_blank_candidates_fail_without_selecting_one_or_opening_another(local_browser, editors):
    browser, run = local_browser
    async def exercise():
        context, pages, requests = await sandbox(browser, editors)
        try:
            adapter, selected_context, _ = adapter_for(pages)
            with pytest.raises(BrowserFailure, match='page_changed') as caught:
                await adapter.prepare()
            assert caught.value.context_check == 'empty_chat'
            await assert_untouched(pages, requests, selected_context)
            assert context.pages == pages
        finally:
            await context.close()
    run(exercise())


def test_one_ready_page_is_selected_ahead_of_a_later_unready_page(local_browser):
    browser, run = local_browser
    async def exercise():
        context, pages, requests = await sandbox(browser, [EDITOR, ''])
        try:
            adapter, selected_context, wrapped = adapter_for(pages)
            assert await adapter.prepare() is wrapped[0]
            assert await pages[0].get_by_role('button', name=PRIVATE, exact=True).is_visible()
            assert await pages[0].locator('#metrics').get_attribute('data-privacy-clicks') == '1'
            assert await pages[1].locator('#metrics').get_attribute('data-privacy-clicks') == '0'
            assert requests == [TEMPORARY, TEMPORARY] and context.pages == pages
            selected_context.new_page.assert_not_awaited()
        finally:
            await context.close()
    run(exercise())


@pytest.mark.parametrize('author', ['user', 'assistant'])
def test_previous_turn_is_preserved_and_a_fresh_temporary_page_is_used(local_browser, author):
    browser, run = local_browser
    async def exercise():
        context, pages, requests = await sandbox(browser, [EDITOR])
        try:
            await pages[0].evaluate('''({author,text}) => {
                const turn=document.createElement('article');
                turn.dataset.messageAuthorRole=author;turn.textContent=text;
                document.body.append(turn);
            }''', {'author': author, 'text': PRIVATE_DRAFT})
            adapter, selected_context, _ = adapter_for(pages)
            fresh = []
            async def new_page():
                page = await context.new_page()
                async def intercept(route):
                    requests.append(route.request.url)
                    await route.fulfill(status=200, content_type='text/html',
                                        body=HTML.replace('EDITOR_HTML', EDITOR))
                await page.route('**/*', intercept)
                wrapped = BoundedPage(page)
                fresh.append(wrapped)
                return wrapped
            selected_context.new_page = AsyncMock(side_effect=new_page)
            assert await adapter.prepare() is fresh[0]
            assert await pages[0].locator(f'[data-message-author-role="{author}"]').inner_text() == PRIVATE_DRAFT
            assert await pages[0].locator('#metrics').get_attribute('data-privacy-clicks') == '0'
            assert await fresh[0].get_by_role('button', name=PRIVATE, exact=True).is_visible()
            selected_context.new_page.assert_awaited_once()
            assert requests == [TEMPORARY, TEMPORARY] and len(context.pages) == 2
        finally:
            await context.close()
    run(exercise())


def test_diagnostic_counts_are_bounded(local_browser):
    browser, run = local_browser
    async def exercise():
        context, pages, _ = await sandbox(browser, [EDITOR * 105])
        try:
            diagnostic = await browser_module.composer_diagnostic(pages[0])
            assert_safe_diagnostic(diagnostic)
            assert diagnostic['canonical_count'] == diagnostic['strict_count'] == 100
        finally:
            await context.close()
    run(exercise())
