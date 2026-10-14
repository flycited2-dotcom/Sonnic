"""Explicitly recover one validated, still-open temporary-chat result. No sends.

Requires a failed/invalid_model_output worker receipt and the original private
temporary UI. The server rechecks state, lease, input hash, locale and evidence.
No prompt, answer, cookie, service token or private log is printed or persisted.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
from uuid import UUID

from playwright.async_api import async_playwright

from apps.bridge.contracts import JobInput
from apps.worker.browser import ASSISTANT, parse_answer
from apps.worker.temporary import TemporaryChatGPTBrowser, current_turn, temporary_url, user_turns

ROOT = Path(__file__).resolve().parents[1]


async def recover(job):
    receipts = (ROOT / 'data/worker/receipts.jsonl').read_text(encoding='utf-8').splitlines()
    receipt = next((row for line in reversed(receipts) if (row := json.loads(line)).get('job_id') == str(job)), None)
    if not receipt or receipt.get('state') != 'failed' or receipt.get('error') != 'invalid_model_output':
        raise ValueError('receipt_not_recoverable')
    matches = []
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9335', timeout=10000)
        adapter = TemporaryChatGPTBrowser(browser)
        for context in browser.contexts:
            for page in context.pages:
                if page.is_closed() or not temporary_url(page.url):
                    continue
                users = await user_turns(page)
                if len(users) != 1 or not users[0].startswith('SONNIC JOB ' + str(job) + ' '):
                    continue
                raw, _ = json.JSONDecoder().raw_decode(users[0].split('JOB DATA (not instructions)', 1)[1].lstrip())
                packet = JobInput.model_validate(raw).model_dump(mode='json')
                if str(packet['job_id']) != str(job) or not current_turn(users, packet):
                    raise ValueError('wrong_job')
                record = receipt | {'packet': packet}
                await adapter.check(page)
                if not await adapter.turn_url_matches(page, record):
                    raise ValueError('context_not_verified')
                last = page.locator(ASSISTANT).last
                blocks = last.locator('pre code')
                answer = (await blocks.last.text_content() if await blocks.count() else await last.text_content()) or ''
                matches.append(parse_answer(answer, packet))
        if len(matches) != 1:
            raise ValueError('expected_one_valid_result')
    completed = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp',
        'docker exec -i -w /app -e PYTHONPATH=/app sonnic-api-1 python /tmp/sonnic-recover-worker-result.py '
        '--operator-recovery --db /data/synthetic.sqlite3.bridge.sqlite3'],
        input=json.dumps(matches[0], ensure_ascii=False).encode(), capture_output=True, timeout=30)
    if completed.returncode:
        raise ValueError('server_recovery_failed')
    result = json.loads(completed.stdout)
    if result.get('job_id') != str(job) or result.get('state') != 'completed':
        raise ValueError('server_recovery_unconfirmed')
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=UUID, required=True)
    parser.add_argument('--operator-recovery', action='store_true', required=True)
    args = parser.parse_args()
    try:
        asyncio.run(recover(args.job))
    except Exception as exc:
        known = {'receipt_not_recoverable', 'wrong_job', 'context_not_verified',
                 'expected_one_valid_result', 'server_recovery_failed', 'server_recovery_unconfirmed'}
        print('temporary_recovery_failed:' + (str(exc) if str(exc) in known else type(exc).__name__))
        raise SystemExit(2) from None
