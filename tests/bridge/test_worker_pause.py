"""No network/browser launch: regression for repeated ChatGPT requests after a block."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.worker.browser import BrowserFailure, ChatGPTBrowser, PROJECT
from apps.worker.main import Journal, Worker, pause_browser


def test_idle_prepare_never_reloads_chatgpt():
    locator = SimpleNamespace(wait_for=AsyncMock(), count=AsyncMock(return_value=0))
    page = SimpleNamespace(url=PROJECT, is_closed=lambda: False,
                           goto=AsyncMock(), reload=AsyncMock(),
                           wait_for_timeout=AsyncMock(), locator=lambda _: locator)
    adapter = ChatGPTBrowser(SimpleNamespace(contexts=[object()]))
    adapter.prepared = page
    adapter.check = AsyncMock()
    async def exercise():
        for _ in range(10):
            assert await adapter.prepare() is page
    asyncio.run(exercise())
    page.goto.assert_not_awaited()
    page.reload.assert_not_awaited()


@pytest.mark.parametrize('reason', ['model_limit', 'login_required', 'page_changed', 'submission_uncertain', 'browser_unavailable'])
def test_browser_block_survives_restart(tmp_path, reason):
    assert pause_browser(tmp_path, reason)
    assert (Journal(tmp_path).folder / 'pause.requested').read_text().strip() == reason


@pytest.mark.parametrize('check', ['temporary_control_visible', 'private_control_visible', 'personalization_menu',
                                  'private dream text https://example.com/token'])
def test_context_diagnostic_retains_only_known_check_names(tmp_path, check):
    error = BrowserFailure('page_changed', context_check=check)
    pause_browser(tmp_path, str(error), context_check=error.context_check)
    diagnostic = json.loads((tmp_path / 'pause-diagnostic.json').read_text())
    if check.startswith('private dream'):
        assert error.context_check is None and 'context_check' not in diagnostic
    else:
        assert diagnostic['context_check'] == check


@pytest.mark.parametrize('defect', [None, 'tag', 'contenteditable', 'negative_count', 'bool_count', 'non_string_tag'])
def test_composer_diagnostic_never_persists_arbitrary_dom_attributes_or_text(tmp_path, defect):
    shape = {'canonical_count': 1, 'strict_count': 0, 'visible_count': 1, 'editable_count': 1,
             'tag': 'textarea', 'contenteditable': 'missing', 'value': 'private-text-not-to-log'}
    if defect in {'tag', 'contenteditable'}:
        shape[defect] = 'private-text-not-to-log'
    elif defect == 'negative_count':
        shape['canonical_count'] = -1
    elif defect == 'bool_count':
        shape['canonical_count'] = True
    elif defect == 'non_string_tag':
        shape['tag'] = ['private-text-not-to-log']
    error = BrowserFailure('page_changed', context_check='composer_visible', composer=shape)
    pause_browser(tmp_path, str(error), context_check=error.context_check, composer=error.composer)
    diagnostic = json.loads((tmp_path / 'pause-diagnostic.json').read_text())
    assert 'private-text-not-to-log' not in json.dumps(diagnostic)
    if defect is None:
        assert diagnostic['composer'] == {key: value for key, value in shape.items() if key != 'value'}
    else:
        assert error.composer is None and 'composer' not in diagnostic


@pytest.mark.parametrize('kind', ['request_rate', 'usage_limit', None])
def test_limit_during_generation_latches_pause_before_delivery(tmp_path, kind):
    journal = Journal(tmp_path)
    journal.save({'stage': 'claimed', 'packet': {'job_id': 'job', 'lease_id': 'lease', 'attempt': 1}})
    browser = SimpleNamespace(generate=AsyncMock(side_effect=BrowserFailure('model_limit', kind)))
    worker = Worker(None, browser, journal)
    worker.post = AsyncMock(return_value={})
    async def delivery(record):
        assert (tmp_path / 'pause.requested').read_text().strip() == 'model_limit'
        assert record['error'] == 'model_limit'
        assert record['limit_kind'] == kind
        diagnostic = json.loads((tmp_path / 'pause-diagnostic.json').read_text())
        assert diagnostic['limit_kind'] == kind
        journal.finish(record, 'failed')
        receipt = json.loads((tmp_path / 'receipts.jsonl').read_text())
        assert receipt['error'] == 'model_limit' and receipt['limit_kind'] == kind
    worker.deliver = delivery
    # Avoid building a real model prompt; generation is deliberately blocked.
    from unittest.mock import patch
    with patch('apps.worker.main.make_prompt', return_value='synthetic'):
        asyncio.run(worker.tick())
    browser.generate.assert_awaited_once()


@pytest.mark.parametrize('alert,kind', [
    ('Too many requests. private-text-not-to-log', 'request_rate'),
    ('Слишком много запросов. Доступ временно ограничен.', 'request_rate'),
    ('You have reached your usage limit', 'usage_limit'),
    ('Достигнут лимит сообщений', 'usage_limit'),
])
def test_limit_classification_uses_alerts_without_retaining_text(alert, kind):
    def locator(selector):
        assert selector == '[role="alert"], [role="dialog"]'
        return SimpleNamespace(all_text_contents=AsyncMock(return_value=[alert]))
    page = SimpleNamespace(url=PROJECT, locator=locator)
    adapter = ChatGPTBrowser(None)
    with pytest.raises(BrowserFailure) as caught:
        asyncio.run(adapter.check(page))
    assert str(caught.value) == 'model_limit'
    assert caught.value.limit_kind == kind
    assert alert not in str(caught.value)


def test_dream_text_cannot_trigger_a_limit():
    def locator(selector):
        assert selector == '[role="alert"], [role="dialog"]'
        return SimpleNamespace(all_text_contents=AsyncMock(return_value=[]))
    page = SimpleNamespace(url=PROJECT, locator=locator,
                           get_by_role=lambda *a, **kw: SimpleNamespace(count=AsyncMock(return_value=0)))
    asyncio.run(ChatGPTBrowser(None).check(page))


@pytest.mark.parametrize('label', [
    'Try again', 'Retry', 'Повторить', 'Повторить попытку', 'Попробовать снова',
    'Попробовать еще раз', 'Попробовать ещё раз',
])
def test_full_page_retry_stops_without_retrying(label):
    from apps.worker.browser import COMPOSER, USER
    def locator(selector):
        if selector == '[role="alert"], [role="dialog"]':
            return SimpleNamespace(all_text_contents=AsyncMock(return_value=[]))
        return SimpleNamespace(count=AsyncMock(return_value=0))
    def role(role, name):
        return SimpleNamespace(count=AsyncMock(return_value=int(bool(name.fullmatch(label)))))
    page = SimpleNamespace(url=PROJECT, locator=locator, get_by_role=role)
    with pytest.raises(BrowserFailure, match='browser_unavailable'):
        asyncio.run(ChatGPTBrowser(None).check(page))


@pytest.mark.parametrize('composer,users', [(1, 0), (0, 1)])
def test_retry_label_inside_loaded_chat_is_not_full_page_failure(composer, users):
    from apps.worker.browser import COMPOSER
    def locator(selector):
        if selector == '[role="alert"], [role="dialog"]':
            return SimpleNamespace(all_text_contents=AsyncMock(return_value=[]))
        return SimpleNamespace(count=AsyncMock(return_value=composer if selector == COMPOSER else users))
    def role(role, name):
        return SimpleNamespace(count=AsyncMock(return_value=int(not name.fullmatch('Log in'))))
    page = SimpleNamespace(url=PROJECT, locator=locator, get_by_role=role)
    asyncio.run(ChatGPTBrowser(None).check(page))


def test_existing_broken_project_does_not_create_another_tab():
    page = SimpleNamespace(url=PROJECT, is_closed=lambda: False)
    context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    adapter = ChatGPTBrowser(SimpleNamespace(contexts=[context]))
    adapter.check = AsyncMock(side_effect=BrowserFailure('browser_unavailable'))
    with pytest.raises(BrowserFailure, match='browser_unavailable'):
        asyncio.run(adapter.prepare())
    context.new_page.assert_not_awaited()


@pytest.mark.parametrize('draft', ['', 'Unsent private draft'])
def test_prepare_reuses_only_empty_project_tabs(draft):
    from apps.worker.browser import COMPOSER
    editor=SimpleNamespace(count=AsyncMock(return_value=1),is_visible=AsyncMock(return_value=True),
                           inner_text=AsyncMock(return_value=draft),wait_for=AsyncMock())
    users=SimpleNamespace(count=AsyncMock(return_value=0))
    def make_page():
        return SimpleNamespace(url=PROJECT,is_closed=lambda:False,goto=AsyncMock(),
                               wait_for_timeout=AsyncMock(),locator=lambda selector:editor if selector==COMPOSER else users)
    existing,new=make_page(),make_page()
    context=SimpleNamespace(pages=[existing],new_page=AsyncMock(return_value=new))
    adapter=ChatGPTBrowser(SimpleNamespace(contexts=[context]));adapter.check=AsyncMock()
    adapter.open_project=AsyncMock()
    assert asyncio.run(adapter.prepare()) is (new if draft else existing)
    existing.goto.assert_not_awaited()
    assert context.new_page.await_count == (1 if draft else 0)


def test_project_navigation_uses_sidebar_once_then_checks_target():
    homepage_button = SimpleNamespace(click=AsyncMock())
    parent = SimpleNamespace(get_by_role=lambda *args, **kw: homepage_button)
    menu = SimpleNamespace(wait_for=AsyncMock(), locator=lambda _: parent)
    page = SimpleNamespace(url='about:blank', goto=AsyncMock(),
                           get_by_role=lambda *args, **kw: menu, wait_for_url=AsyncMock())
    adapter = ChatGPTBrowser(None)
    adapter.check = AsyncMock()
    asyncio.run(adapter.open_project(page))
    page.goto.assert_awaited_once_with('https://chatgpt.com/', wait_until='domcontentloaded', timeout=30000)
    homepage_button.click.assert_awaited_once()
    page.wait_for_url.assert_awaited_once_with(PROJECT, timeout=20000)


def test_homepage_error_stops_without_clicking_or_retrying():
    page = SimpleNamespace(url='https://chatgpt.com/', goto=AsyncMock(), get_by_role=AsyncMock())
    adapter = ChatGPTBrowser(None)
    adapter.check = AsyncMock(side_effect=BrowserFailure('model_limit'))
    with pytest.raises(BrowserFailure, match='model_limit'):
        asyncio.run(adapter.open_project(page))
    page.goto.assert_not_awaited()
    page.get_by_role.assert_not_called()


def test_changed_sidebar_stops_without_direct_link_fallback():
    menu = SimpleNamespace(wait_for=AsyncMock(side_effect=TimeoutError()))
    page = SimpleNamespace(url='https://chatgpt.com/', goto=AsyncMock(),
                           get_by_role=lambda *args, **kw: menu)
    adapter = ChatGPTBrowser(None)
    adapter.check = AsyncMock()
    with pytest.raises(BrowserFailure, match='page_changed'):
        asyncio.run(adapter.open_project(page))
    page.goto.assert_not_awaited()
