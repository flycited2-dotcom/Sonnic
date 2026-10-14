"""Explicit operator recovery via the ChatGPT home page; never sends a message."""
import argparse
import asyncio
import json

from playwright.async_api import async_playwright

from apps.worker.browser import PROJECT, COMPOSER, USER, BrowserFailure, ChatGPTBrowser
from apps.worker.main import ROOT


async def recover(browser, completed=None):
    completed_url = completed.get('chat_url') if completed and completed.get('state') == 'completed' else None
    pages = [page for context in browser.contexts for page in context.pages
             if (page.url.rstrip('/') in {PROJECT, 'https://chatgpt.com'} or page.url == completed_url)
             and not page.is_closed()]
    if len(pages) != 1:
        raise RuntimeError('expected_one_project_tab')
    page = pages[0]
    users = await page.locator(USER).all_text_contents()
    own_completed = bool(completed_url and page.url == completed_url and users
                         and str(completed['job_id']) in users[-1])
    if users and not own_completed:
        raise RuntimeError('existing_user_turn')
    editor = page.locator(COMPOSER)
    if await editor.count() and (await editor.inner_text()).strip():
        raise RuntimeError('existing_draft')
    adapter = ChatGPTBrowser(browser)
    # One operator-requested navigation, never an idle recovery/reload loop.
    await adapter.open_project(page, via_sidebar=own_completed)
    await page.locator(USER).last.wait_for(state='hidden', timeout=20000)
    await page.locator(COMPOSER).wait_for(state='visible', timeout=20000)
    await page.wait_for_timeout(1500)
    await adapter.check(page)
    if await page.locator(USER).count() or (await page.locator(COMPOSER).inner_text()).strip():
        raise RuntimeError('project_not_empty')
    return 'browser_state:ready'


async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9335', timeout=10000)
            receipts = ROOT / 'data/worker/receipts.jsonl'
            lines = receipts.read_text(encoding='utf-8').splitlines() if receipts.exists() else []
            completed = json.loads(lines[-1]) if lines else None
            print(await recover(browser, completed))
        except BrowserFailure as exc:
            print('recovery_stopped:' + str(exc))
            raise SystemExit(2) from None
        except Exception as exc:
            # Playwright exception text can contain private page data.
            print('recovery_stopped:' + type(exc).__name__)
            raise SystemExit(2) from None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--operator-recovery', action='store_true', required=True)
    parser.parse_args()
    asyncio.run(main())
