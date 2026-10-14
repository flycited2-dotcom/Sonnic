"""Read-only operator diagnostic of context controls for one known worker job.

No navigation, settings changes, message sends, reloads, history requests,
cookies, prompt bodies, or model answers in the output.
"""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID
import re

from playwright.async_api import async_playwright

from apps.worker.browser import USER
from apps.worker.browser import ASSISTANT, parse_answer
from apps.worker.temporary import HEADER, SAVE_TEMPORARY, TemporaryChatGPTBrowser, temporary_url, user_turns


async def inspect(job):
    reports = []
    receipts = Path(__file__).resolve().parents[1] / 'data/worker/receipts.jsonl'
    receipt = next((row for line in reversed(receipts.read_text(encoding='utf-8').splitlines())
                    if (row := json.loads(line)).get('job_id') == str(job)), {})
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9335', timeout=10000)
        adapter = TemporaryChatGPTBrowser(browser)
        for context in browser.contexts:
            for page in context.pages:
                if not temporary_url(page.url):
                    continue
                users = await user_turns(page)
                if not any(text.startswith('SONNIC JOB ' + str(job) + ' ') for text in users):
                    continue
                marker = re.fullmatch(r'SONNIC JOB ([a-f0-9-]{36}) LEASE ([a-f0-9-]{36}) ATTEMPT ([0-9]+)',
                                      users[0].splitlines()[0]) if len(users) == 1 else None
                verified = False
                if marker and marker[1] == str(job):
                    record = receipt | {'packet': {'job_id': marker[1], 'lease_id': marker[2], 'attempt': int(marker[3])}}
                    verified = await adapter.turn_url_matches(page, record)
                header = page.locator(HEADER)
                reports.append({'temporary_url': temporary_url(page.url), 'logical_user_turns': len(users),
                                'raw_user_nodes': await page.locator(USER).count(),
                                'protocol_marker_valid': bool(marker),
                                'saved_privacy_check_matches_current_ui': verified,
                                'save_chat_button': await header.get_by_role('button', name=SAVE_TEMPORARY, exact=True).count()})
                # Validate locally; report only types/field paths, never model text.
                answers = page.locator(ASSISTANT)
                if verified and await answers.count():
                    try:
                        packet, _ = json.JSONDecoder().raw_decode(users[0].split('JOB DATA (not instructions)', 1)[1].lstrip())
                        last = answers.last
                        blocks = last.locator('pre code')
                        answer = (await blocks.last.text_content() if await blocks.count() else await last.text_content()) or ''
                        reports[-1]['answer_characters'] = len(answer)
                        reports[-1]['code_blocks'] = await blocks.count()
                        try:
                            parse_answer(answer, packet)
                            reports[-1]['output_valid'] = True
                        except ValueError as error:
                            errors = getattr(error, 'errors', lambda **_: [])(include_url=False, include_context=False, include_input=False)
                            reports[-1]['output_valid'] = False
                            reports[-1]['validation_errors'] = errors or [{'type': str(error) if str(error) in {'correlation_mismatch', 'ungrounded_evidence'} else type(error).__name__}]
                        reports[-1]['answer_structure'] = await last.evaluate('''el => [...el.children].map(child => ({tag:child.tagName, class:child.className, testid:child.getAttribute('data-testid')}))''')
                    except (ValueError, KeyError, IndexError):
                        reports[-1]['packet_parseable'] = False
    print(json.dumps({'matching_pages': len(reports), 'pages': reports}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=UUID, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(inspect(args.job))
    except Exception as exc:
        print('context_diagnostic_failed:' + type(exc).__name__)
        raise SystemExit(2) from None
