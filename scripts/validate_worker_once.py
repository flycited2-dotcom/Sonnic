"""Explicit one-job live check; always restores the stop marker, never runs a watcher."""
import argparse
import asyncio
from datetime import datetime, timezone
import os

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from apps.worker.browser import BrowserFailure
from apps.worker.temporary import TemporaryChatGPTBrowser
from apps.worker.main import ROOT, Journal, Worker, pause_browser, single_instance
from scripts.validate_worker_live import CASES, main as validate


async def main(start):
    load_dotenv(ROOT / '.env.worker')
    endpoint = os.environ['SONNIC_WORKER_CDP']
    server = os.environ['SONNIC_WORKER_SERVER'].rstrip('/')
    token = os.environ['SONNIC_WORKER_TOKEN']
    if endpoint != 'http://127.0.0.1:9335' or server != 'https://sonnic.212.116.115.150.sslip.io' or len(token) < 32:
        raise ValueError('Expected dedicated Sonnic browser and validation server configuration')
    folder = ROOT / 'data/worker'
    report = folder / 'validation' / datetime.now(timezone.utc).strftime('control-%Y%m%dT%H%M%SZ')
    with single_instance(folder):
        journal = Journal(folder)
        existing = journal.read()
        if existing and existing['stage'] != 'claiming':
            raise RuntimeError('Pending job requires recovery before a new live check')
        async with httpx.AsyncClient(base_url=server, headers={'Authorization': 'Bearer '+token},
                                     trust_env=False, timeout=20, follow_redirects=False) as client:
            validation = None
            adapter = None
            try:
                (folder / 'stop.requested').unlink(missing_ok=True)
                (folder / 'pause.requested').unlink(missing_ok=True)
                async with async_playwright() as p:
                    browser = await p.chromium.connect_over_cdp(endpoint, timeout=10000)
                    adapter = TemporaryChatGPTBrowser(browser)
                    await adapter.prepare()
                    worker = Worker(client, adapter, journal)
                    await worker.post('heartbeat', {'state': 'ready'})
                    validation = asyncio.create_task(validate(1, start, report))
                    async with asyncio.timeout(120):
                        while not await worker.tick():
                            if validation.done():
                                await validation
                                raise RuntimeError('Validation ended without processing a job')
                            await asyncio.sleep(2)
                    results = await asyncio.wait_for(validation, timeout=75)
                    if len(results) != 1 or results[0]['state'] != 'completed':
                        raise RuntimeError('Control case did not complete; no automatic retry')
                    print('control_report:' + str(report), flush=True)
            except BrowserFailure as exc:
                pause_browser(folder, str(exc), exc.limit_kind)
                print('control_stopped:' + str(exc), flush=True)
                raise SystemExit(2) from None
            except Exception as exc:
                # Do not print Playwright call logs: they may contain visitor cookies.
                print('control_stopped:' + type(exc).__name__, flush=True)
                raise SystemExit(2) from None
            finally:
                (folder / 'stop.requested').write_text('single live check finished\n', encoding='utf-8')
                if validation and not validation.done():
                    validation.cancel()
                    await asyncio.gather(validation, return_exceptions=True)
                try:
                    response = await client.post('/api/v1/bridge/worker/heartbeat', json={'state': 'offline'})
                    print('offline_status:' + str(response.status_code), flush=True)
                except httpx.HTTPError:
                    print('offline_heartbeat_unavailable; stop marker retained', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--resume', action='store_true', required=True)
    parser.add_argument('--start', type=int, default=6, choices=range(len(CASES)))
    args = parser.parse_args()
    asyncio.run(main(args.start))
