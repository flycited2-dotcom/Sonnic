"""One temporary chat per job, with the observed personalization control off.

Only supported visible UI operations. Never changes account-wide memory,
opens a project, calls private APIs, or re-sends a lost temporary conversation.
Russian UI labels were verified in the owner's account on 2026-09-14.
"""
import hashlib
import math
import re
import time
from urllib.parse import urlsplit

from .browser import ASSISTANT, COMPOSER, USER, BrowserFailure, ChatGPTBrowser, composer_diagnostic

TEMPORARY = 'https://chatgpt.com/?temporary-chat=true'
TRANSPORT = 'temporary_no_personalization_v1'
HEADER = '#conversation-header-actions'
PRIVATE = 'Без персонализации'
PERSONALIZED = 'С персонализацией'
PERSONALIZED_LABEL = re.compile(r'^(?:С персонализацией|Персонализированный)$')
EXIT_TEMPORARY = 'Выключить временный чат'
SAVE_TEMPORARY = 'Сохранить чат'


def temporary_url(url, *, blank=False):
    if any(ord(char) < 33 or ord(char) == 127 for char in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != 'https' or parts.netloc != 'chatgpt.com' or parts.fragment:
        return False
    if blank:
        return parts.path in {'', '/'} and parts.query == 'temporary-chat=true'
    # Some UI versions keep the blank URL; others assign a conversation URL.
    # This URL check alone is never evidence that memory is off.
    return (parts.query in {'', 'temporary-chat=true'} and
            (parts.path in {'', '/'} or re.fullmatch(r'/c/[a-f0-9-]{36}', parts.path) is not None))


def current_turn(users, packet):
    marker = f"SONNIC JOB {packet['job_id']} LEASE {packet['lease_id']} ATTEMPT {packet['attempt']}"
    if len(users) != 1 or not users[0]:
        return False
    return users[0].splitlines()[0].strip() == marker


async def user_turns(page):
    # The real UI nests data-message-author-role inside data-turn. Count each
    # message once, using its innermost author element, not both wrappers.
    # innerText preserves the first-line marker when the UI renders Markdown.
    return await page.locator(USER).evaluate_all('''nodes => nodes
        .filter(el => !nodes.some(other => other !== el && el.contains(other)))
        .map(el => el.innerText || '')''')


def verified_before_send(record):
    verified, started = record.get('context_verified_at'), record.get('started')
    return (record.get('transport') == TRANSPORT
            and all(type(value) in (int, float) and math.isfinite(value) for value in (verified, started))
            and 0 < verified <= started
            and re.fullmatch(r'[a-f0-9]{64}', record.get('prompt_sha256', '')) is not None)


class TemporaryChatGPTBrowser(ChatGPTBrowser):
    transport = TRANSPORT

    async def require_composer(self, page):
        editor = page.locator(COMPOSER)
        if (await page.locator('#prompt-textarea').count() != 1 or await editor.count() != 1
                or not await editor.is_visible() or not await editor.is_enabled() or not await editor.is_editable()):
            raise BrowserFailure('page_changed', context_check='composer_visible',
                                 composer=await composer_diagnostic(page))
        return editor

    async def assert_private(self, page):
        if not temporary_url(page.url):
            raise BrowserFailure('page_changed', context_check='temporary_url')
        header = page.locator(HEADER)
        for name in (PRIVATE, EXIT_TEMPORARY):
            control = header.get_by_role('button', name=name, exact=True)
            if await control.count() != 1 or not await control.is_visible():
                raise BrowserFailure('page_changed', context_check=(
                    'private_control_visible' if name == PRIVATE else 'temporary_control_visible'))

    async def configure_empty(self, page):
        await self.check(page)
        if not temporary_url(page.url, blank=True):
            raise BrowserFailure('page_changed', context_check='empty_temporary_url')
        editor = page.locator(COMPOSER)
        try:
            await editor.wait_for(state='visible', timeout=45000)
        except Exception:
            await self.check(page)
            raise BrowserFailure('page_changed', context_check='composer_visible',
                                 composer=await composer_diagnostic(page)) from None
        editor = await self.require_composer(page)
        header = page.locator(HEADER)
        try:
            await header.get_by_role('button', name=EXIT_TEMPORARY, exact=True).wait_for(state='visible', timeout=15000)
        except Exception:
            await self.check(page)
            raise BrowserFailure('page_changed', context_check='temporary_control_visible') from None
        # Inspect before any setting change or fill: another person's draft is untouched.
        if (await page.locator(USER).count() or await page.locator(ASSISTANT).count()
                or (await editor.inner_text()).strip()):
            raise BrowserFailure('page_changed', context_check='empty_chat')
        personalized = header.get_by_role('button', name=PERSONALIZED_LABEL)
        if await personalized.count() == 1 and await personalized.is_visible():
            try:
                await personalized.click()
                await page.get_by_role('menu').get_by_text(PRIVATE, exact=True).click(timeout=10000)
                await header.get_by_role('button', name=PRIVATE, exact=True).wait_for(state='visible', timeout=10000)
            except Exception:
                await self.check(page)
                raise BrowserFailure('page_changed', context_check='personalization_menu') from None
        await self.validate_new_chat(page)
        self.prepared = page
        return page

    async def validate_new_chat(self, page):
        await self.assert_private(page)
        editor = await self.require_composer(page)
        if (not temporary_url(page.url, blank=True) or await page.locator(USER).count()
                or await page.locator(ASSISTANT).count()
                or (await editor.inner_text()).strip()):
            raise BrowserFailure('page_changed', context_check='empty_chat')

    async def validate_send_context(self, page):
        await self.assert_private(page)
        if not temporary_url(page.url, blank=True) or await page.locator(USER).count():
            raise BrowserFailure('page_changed', context_check='send_context')

    async def prepare(self):
        if not self.browser.contexts:
            raise BrowserFailure('browser_unavailable')
        if self.prepared and not self.prepared.is_closed():
            # No reloads or setting changes while idle. A changed mode pauses the worker.
            await self.check(self.prepared)
            await self.validate_new_chat(self.prepared)
            return self.prepared
        context = self.browser.contexts[0]
        ready, waiting = [], []
        for candidate in reversed(context.pages):
            if candidate.is_closed() or not temporary_url(candidate.url, blank=True):
                continue
            await self.check(candidate)
            if await candidate.locator(USER).count() or await candidate.locator(ASSISTANT).count():
                # Leave old completed/failed conversations intact. The next job
                # may use one fresh chat; it must never resend this conversation.
                continue
            waiting.append(candidate)
            editor = candidate.locator(COMPOSER)
            if (await candidate.locator('#prompt-textarea').count() == 1
                    and await editor.count() == 1 and await editor.is_visible()
                    and await editor.is_enabled() and await editor.is_editable()
                    and not (await editor.inner_text()).strip()):
                ready.append(candidate)
        if len(ready) > 1 or (not ready and len(waiting) > 1):
            # The operator's intended empty page is ambiguous. Do not open yet
            # another tab or choose an unknown editor to work around the failure.
            raise BrowserFailure('page_changed', context_check='empty_chat')
        page = ready[0] if ready else waiting[0] if waiting else None
        if page is None:
            # One navigation per new job, never a loop of retries or idle reloads.
            page = await context.new_page()
            self.prepared = page
            await page.goto(TEMPORARY, wait_until='domcontentloaded', timeout=30000)
        # Even a missing/hidden editor on an existing empty page is awaited here
        # once. Its absence is never a reason to create a second temporary tab.
        return await self.configure_empty(page)

    async def turn_url_matches(self, page, record):
        if not temporary_url(page.url) or not verified_before_send(record):
            return False
        header = page.locator(HEADER)
        personalized = header.get_by_role('button', name=PERSONALIZED_LABEL)
        if await personalized.count() and await personalized.first.is_visible():
            raise BrowserFailure('page_changed')
        # After sending, the observed UI replaces both mode controls with
        # "Save chat". It offers to add this temporary chat to history; never
        # click it. The URL and button prove the chat is still temporary, while
        # our durable pre-send check proves personalization was switched off.
        # A missing header during hydration returns False for the bounded
        # correlation wait in the base adapter, rather than failing instantly.
        private = header.get_by_role('button', name=PRIVATE, exact=True)
        temporary = header.get_by_role('button', name=EXIT_TEMPORARY, exact=True)
        save = header.get_by_role('button', name=SAVE_TEMPORARY, exact=True)
        controls = (await private.count() == 1 and await private.is_visible()
                    and await temporary.count() == 1 and await temporary.is_visible())
        unsaved = (temporary_url(page.url, blank=True)
                   and await save.count() == 1 and await save.is_visible())
        return (controls or unsaved) and current_turn(await user_turns(page), record['packet'])

    async def resume_page(self, record):
        if record.get('transport') != TRANSPORT:
            raise BrowserFailure('submission_uncertain')
        matches = []
        for context in self.browser.contexts:
            for page in context.pages:
                if page.is_closed() or not temporary_url(page.url):
                    continue
                users = await user_turns(page)
                if current_turn(users, record['packet']):
                    await self.check(page)
                    if not await self.turn_url_matches(page, record):
                        raise BrowserFailure('page_changed')
                    matches.append(page)
        # URLs are not durable handles for temporary chats. Only reuse an open,
        # private page containing this exact job, lease and attempt, once.
        if len(matches) != 1:
            raise BrowserFailure('submission_uncertain')
        return matches[0]

    async def generate(self, record, save, prompt):
        if record['stage'] == 'claimed':
            page = await self.prepare()
            await self.assert_private(page)
            record['context_verified_at'] = time.time()
            record['prompt_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
        return await super().generate(record, save, prompt)

    async def release(self, record):
        page, self.active = self.active, None
        self.prepared = None
        if not page or page.is_closed() or not record.get('output'):
            return
        await self.check(page)
        if not await self.turn_url_matches(page, record):
            raise BrowserFailure('page_changed')
        users = await user_turns(page)
        editor = page.locator(COMPOSER)
        if (not current_turn(users, record['packet']) or await editor.count() != 1
                or (await editor.inner_text()).strip()):
            raise BrowserFailure('page_changed')
        # Only after durable server acknowledgement. A single fresh navigation
        # clears the completed temporary chat and reuses the same Chrome tab.
        # The dedicated browser stays alive; no growing pile of projects or tabs.
        await page.goto(TEMPORARY, wait_until='domcontentloaded', timeout=30000)
        await self.configure_empty(page)
