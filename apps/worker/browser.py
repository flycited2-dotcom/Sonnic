"""Documented Playwright/CDP UI automation; never calls ChatGPT private HTTP APIs."""
import asyncio
import json
import re
import time
from urllib.parse import urlsplit

from apps.bridge.contracts import JobOutput

PROJECT = 'https://chatgpt.com/g/g-p-6a9c47e07510819197bd898c90d1b12b-sonnic-baza-znanii-i-razbor-snov/project'
COMPOSER = 'div#prompt-textarea[contenteditable="true"]'
USER = '[data-message-author-role="user"], [data-turn="user"], [data-author-role="user"]'
ASSISTANT = '[data-message-author-role="assistant"], [data-turn="assistant"], [data-author-role="assistant"]'
STOP = '[data-testid="stop-button"], button[aria-label="Stop streaming"], button[aria-label="Остановить потоковую передачу"]'


CONTEXT_CHECKS = {
    'temporary_url', 'empty_temporary_url', 'composer_visible', 'temporary_control_visible',
    'empty_chat', 'personalization_menu', 'private_control_visible', 'send_context',
}


def safe_composer_diagnostic(value):
    """Keep only bounded structural categories; never persist DOM text/values."""
    counts = ('canonical_count', 'strict_count', 'visible_count', 'editable_count')
    if (not isinstance(value, dict)
            or any(type(value.get(key)) is not int or not 0 <= value[key] <= 100 for key in counts)
            or not isinstance(value.get('tag'), str) or not isinstance(value.get('contenteditable'), str)
            or value.get('tag') not in {'div', 'textarea', 'other', 'missing'}
            or value.get('contenteditable') not in {'true', 'plaintext-only', 'false', 'missing', 'other'}):
        return None
    return {key: value[key] for key in (*counts, 'tag', 'contenteditable')}


async def composer_diagnostic(page):
    # Scope is the known composer ID, not arbitrary editors or page contents.
    return safe_composer_diagnostic(await page.locator('#prompt-textarea').evaluate_all('''nodes => {
        const bounded = nodes.slice(0, 100);
        const visible = el => {
            const style = getComputedStyle(el);
            return style.visibility !== 'hidden' && style.visibility !== 'collapse'
                && [...el.getClientRects()].some(rect => rect.width > 0 && rect.height > 0);
        };
        const one = nodes.length === 1 ? nodes[0] : null;
        const tag = one?.tagName.toLowerCase();
        const editable = one?.getAttribute('contenteditable');
        return {
            canonical_count: Math.min(nodes.length, 100),
            strict_count: bounded.filter(el => el.matches('div#prompt-textarea[contenteditable="true"]')).length,
            visible_count: bounded.filter(visible).length,
            editable_count: bounded.filter(el => (el.isContentEditable || el.tagName === 'TEXTAREA')
                && !el.disabled && !el.readOnly && el.getAttribute('aria-disabled') !== 'true').length,
            tag: !nodes.length ? 'missing' : ['div', 'textarea'].includes(tag) ? tag : 'other',
            contenteditable: !one || editable === null ? 'missing'
                : ['true', 'plaintext-only', 'false'].includes(editable) ? editable : 'other'
        };
    }'''))


class BrowserFailure(Exception):
    def __init__(self, code, limit_kind=None, *, context_check=None, composer=None):
        super().__init__(code)
        self.limit_kind = limit_kind if limit_kind in {'request_rate', 'usage_limit'} else None
        self.context_check = context_check if context_check in CONTEXT_CHECKS else None
        self.composer = safe_composer_diagnostic(composer)


def parse_answer(text, packet):
    raw = text.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw, count=1).removesuffix('```').strip()
    # Observed model formatting mistake: the internal source list was placed
    # inside reflection. Move this field, or discard an identical duplicate.
    # Conflicting values stay invalid; the schema and correlation remain strict.
    data = json.loads(raw)
    if (isinstance(data, dict) and isinstance(data.get('reflection'), dict)
            and 'source_use' in data['reflection']):
        nested_sources = data['reflection']['source_use']
        if 'source_use' not in data:
            data['source_use'] = data['reflection'].pop('source_use')
        elif data['source_use'] == nested_sources:
            data['reflection'].pop('source_use')
    result = JobOutput.model_validate(data)
    for key in ('job_id', 'lease_id', 'attempt', 'input_sha256', 'output_locale'):
        if str(getattr(result, key)) != str(packet[key]):
            raise ValueError('correlation_mismatch')
    if result.reflection:
        dream = packet['dream']['dream_text']
        for symbol in result.reflection.symbols:
            # Russian output sometimes adds typographic quotation marks around
            # an otherwise exact quote. Strip one pair only when the inner
            # text occurs verbatim; never change case, spacing or wording.
            evidence = symbol.evidence
            if (evidence not in dream and len(evidence) > 2 and evidence.startswith('«')
                    and evidence.endswith('»') and evidence[1:-1] in dream):
                symbol.evidence = evidence[1:-1]
            if symbol.evidence not in dream:
                raise ValueError('ungrounded_evidence')
    return result.model_dump(mode='json')


def chat_url(url):
    parts = urlsplit(url)
    project_path = urlsplit(PROJECT).path.removesuffix('/project')
    return (parts.scheme == 'https' and parts.netloc == 'chatgpt.com'
            and re.fullmatch(re.escape(project_path) + r'/c/[a-f0-9-]{36}', parts.path) is not None
            and not parts.query and not parts.fragment)


def turn_matches(text, packet):
    # Job alone is insufficient after retry: every attempt has a different lease.
    return str(packet['job_id']) in text and str(packet['lease_id']) in text


AFTER_USER = """(node, selector) => {
    const users = [...document.querySelectorAll(selector)];
    const user = users[users.length - 1];
    return !!user && !!(user.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
}"""


class ChatGPTBrowser:
    # Kept for recovering already-sent legacy jobs, not for new visitor requests.
    transport = 'project_v1'

    def __init__(self, browser):
        self.browser = browser
        self.prepared = None
        self.active = None

    async def open_project(self, page, *, via_sidebar=False):
        """Open via the observed sidebar route, not the failing direct deep link."""
        if not via_sidebar and page.url.rstrip('/') != 'https://chatgpt.com':
            await page.goto('https://chatgpt.com/', wait_until='domcontentloaded', timeout=30000)
        await self.check(page)
        menu = page.get_by_role('button', name='Открыть параметры проекта «Sonnic — база знаний и разбор снов»', exact=True)
        try:
            await menu.wait_for(state='visible', timeout=20000)
            await menu.locator('..').get_by_role('button', name='Открыть главную страницу проекта', exact=True).click()
            await page.wait_for_url(PROJECT, timeout=20000)
        except Exception:
            await self.check(page)
            raise BrowserFailure('page_changed') from None
        await self.check(page)

    async def check(self, page):
        if urlsplit(page.url).hostname != 'chatgpt.com':
            raise BrowserFailure('login_required')
        # Only UI alerts count as quota errors; dream/model text cannot trip them.
        alerts = await page.locator('[role="alert"], [role="dialog"]').all_text_contents()
        alert_text = ' '.join(alerts)
        if re.search(r'too many requests|слишком много запросов', alert_text, re.I):
            raise BrowserFailure('model_limit', limit_kind='request_rate')
        if re.search(r'usage limit|message limit|достигнут.{0,30}лимит', alert_text, re.I):
            raise BrowserFailure('model_limit', limit_kind='usage_limit')
        if await page.get_by_role('button', name=re.compile(r'^(Log in|Войти)$', re.I)).count():
            raise BrowserFailure('login_required')
        # The full-page load error is not a model quota or a failed answer.
        # Never click retry or open another project tab to work around it.
        retry = page.get_by_role('button', name=re.compile(
            r'^(Try again|Retry|Повторить|Повторить попытку|Попробовать снова|Попробовать ещ[её] раз)$', re.I))
        if (await retry.count() and not await page.locator(COMPOSER).count()
                and not await page.locator(USER).count()):
            raise BrowserFailure('browser_unavailable')

    async def prepare(self):
        if not self.browser.contexts:
            raise BrowserFailure('browser_unavailable')
        if self.prepared and not self.prepared.is_closed():
            page = self.prepared
            if page.url.rstrip('/') != PROJECT:
                await self.open_project(page)
        else:
            page = None
            for candidate in reversed(self.browser.contexts[0].pages):
                if candidate.url.rstrip('/') != PROJECT or candidate.is_closed():
                    continue
                await self.check(candidate)
                editor = candidate.locator(COMPOSER)
                if (await editor.count() == 1 and await editor.is_visible()
                        and not await candidate.locator(USER).count()
                        and not (await editor.inner_text()).strip()):
                    page = candidate
                    break
            if page is None:
                # Reuse only an empty homepage; never discard another tab's draft.
                for candidate in reversed(self.browser.contexts[0].pages):
                    if candidate.url.rstrip('/') != 'https://chatgpt.com' or candidate.is_closed():
                        continue
                    await self.check(candidate)
                    editor = candidate.locator(COMPOSER)
                    if (await editor.count() == 1 and not await candidate.locator(USER).count()
                            and not (await editor.inner_text()).strip()):
                        page = candidate
                        break
                if page is None:
                    page = await self.browser.contexts[0].new_page()
                await self.open_project(page)
            self.prepared = page
        await page.wait_for_timeout(1500)
        await self.check(page)
        if page.url.rstrip('/') != PROJECT:
            raise BrowserFailure('page_changed')
        try:
            await page.locator(COMPOSER).wait_for(state='visible', timeout=45000)
        except Exception:
            await self.check(page)
            raise BrowserFailure('page_changed') from None
        if await page.locator(USER).count():
            raise BrowserFailure('page_changed')
        return page

    async def resume_page(self, record):
        saved_url = record.get('chat_url')
        if saved_url and not chat_url(saved_url):
            raise BrowserFailure('submission_uncertain')
        candidates = []
        for context in self.browser.contexts:
            for page in context.pages:
                if urlsplit(page.url).hostname != 'chatgpt.com':
                    continue
                if saved_url and page.url == saved_url:
                    candidates.append(page)
                elif not saved_url and chat_url(page.url):
                    users = await page.locator(USER).all_text_contents()
                    if users and turn_matches(users[-1], record['packet']):
                        candidates.append(page)
        if len(candidates) > 1:
            raise BrowserFailure('submission_uncertain')
        if candidates:
            return candidates[0]
        if saved_url and chat_url(saved_url):
            page = await self.browser.contexts[0].new_page()
            await page.goto(saved_url, wait_until='domcontentloaded')
            return page
        # A crash after the send intent can never cause an automatic second send.
        raise BrowserFailure('submission_uncertain')

    async def validate_new_chat(self, page):
        if page.url.rstrip('/') != PROJECT or await page.locator(USER).count():
            raise BrowserFailure('page_changed')

    async def turn_url_matches(self, page, record):
        return chat_url(page.url) and (not record.get('chat_url') or record['chat_url'] == page.url)

    async def validate_send_context(self, page):
        pass

    async def generate(self, record, save, prompt):
        if record['stage'] == 'claimed':
            page = self.prepared if self.prepared and not self.prepared.is_closed() else await self.prepare()
            self.active = page
            await self.check(page)
            await self.validate_new_chat(page)
            editor = page.locator(COMPOSER)
            await editor.fill(prompt)
            await self.validate_send_context(page)
            record.update(stage='sending', started=time.time(), transport=self.transport)
            save(record)  # durable send intent BEFORE clicking
            await editor.press('Enter')
            self.prepared = None
        else:
            page = await self.resume_page(record)
            self.active = page
        deadline = record.get('started', time.time()) + 480
        correlation_deadline = time.monotonic() + 20
        confirmed = False
        previous, stable_since = '', time.monotonic()
        while time.time() < deadline:
            await self.check(page)
            users = await page.locator(USER).all_text_contents()
            matching = bool(users) and turn_matches(users[-1], record['packet'])
            correct_url = await self.turn_url_matches(page, record)
            if not matching or not correct_url:
                # Navigation/hydration can briefly expose stale or collapsed turns.
                # Never read any answer or bind a URL until this attempt is visible.
                if confirmed or time.monotonic() >= correlation_deadline:
                    raise BrowserFailure('submission_uncertain')
                previous, stable_since = '', time.monotonic()
                await asyncio.sleep(1)
                continue
            if matching and correct_url:
                confirmed = True
                if record['stage'] != 'sent' or not record.get('chat_url'):
                    record.update(stage='sent', chat_url=page.url)
                    save(record)
                answers = page.locator(ASSISTANT)
                finished = page.get_by_test_id('feedback-turn-action-button')
                if (await answers.count() and not await page.locator(STOP).count()
                        and await finished.count() and await finished.last.is_visible()
                        and await answers.last.evaluate(AFTER_USER, USER)
                        and await finished.last.evaluate(AFTER_USER, USER)):
                    last = answers.last
                    # Code block excludes the Copy/Edit/Read aloud UI labels.
                    blocks = last.locator('pre code')
                    # innerText collapses repeated spaces in prose-rendered JSON,
                    # corrupting literal evidence from dictated input. textContent
                    # preserves the actual model output without weakening validation.
                    text = (await blocks.last.text_content() if await blocks.count() else await last.text_content()) or ''
                    if text != previous:
                        previous, stable_since = text, time.monotonic()
                    elif text and time.monotonic() - stable_since >= 3:
                        try:
                            return parse_answer(text, record['packet'])
                        except ValueError as error:
                            # Allow a short gap between streaming chunks before failing.
                            if time.monotonic() - stable_since >= 15:
                                # Fixed error categories only, never raw output or field values.
                                errors = getattr(error, 'errors', lambda **_: [])(include_input=False, include_context=False, include_url=False)
                                record['output_error_types'] = sorted({item['type'] for item in errors})[:8] or [
                                    str(error) if str(error) in {'correlation_mismatch', 'ungrounded_evidence'} else type(error).__name__]
                                save(record)
                                raise BrowserFailure('invalid_model_output') from None
                else:
                    previous, stable_since = '', time.monotonic()
            await asyncio.sleep(1)
        raise BrowserFailure('generation_timeout')

    async def release(self, record):
        # Keep the last tab alive: closing it can terminate the dedicated Chrome.
        # After an acknowledged answer return through the visible project link.
        # The persisted receipt already exists; a navigation failure cannot resend.
        page, self.active = self.active, None
        if not page or page.is_closed():
            return
        self.prepared = None
        if not record.get('output'):
            return  # Preserve failed output for diagnosis; never click retry.
        await self.check(page)
        users = await page.locator(USER).all_text_contents()
        editor = page.locator(COMPOSER)
        if (page.url != record.get('chat_url') or not chat_url(page.url)
                or not users or not turn_matches(users[-1], record['packet'])
                or await editor.count() != 1 or (await editor.inner_text()).strip()):
            raise BrowserFailure('page_changed')
        try:
            await self.open_project(page, via_sidebar=True)
            # The URL changes before the previous conversation unmounts.
            await page.locator(USER).last.wait_for(state='hidden', timeout=20000)
            await page.locator(COMPOSER).wait_for(state='visible', timeout=20000)
        except Exception:
            await self.check(page)
            raise BrowserFailure('page_changed') from None
        await self.check(page)
        if (await page.locator(USER).count()
                or (await page.locator(COMPOSER).inner_text()).strip()):
            raise BrowserFailure('page_changed')
        self.prepared = page
