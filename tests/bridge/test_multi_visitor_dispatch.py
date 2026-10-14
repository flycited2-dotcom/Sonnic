"""Concurrent visitor requests and serial dispatch; real HTTP/SQLite, no model calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.bridge.contracts import JobInput, JobOutput
from apps.bridge.dispatch import DispatchSettings
from apps.worker.browser import parse_answer


def test_four_concurrent_visitors_receive_only_their_correlated_result(tmp_path):
    token = 'synthetic-dispatch-test-token-' + 'x' * 32
    auth = {'Authorization': 'Bearer ' + token}
    app = create_app(str(tmp_path / 'site.db'), mode='disabled',
                     dispatch_settings=DispatchSettings(token, True))
    symbols = ('синий фонарь', 'красный корабль', 'зелёный поезд', 'белый воздушный шар')
    dreams = [f'Мне приснился {symbol}; это вымышленный сон посетителя {index}.'
              for index, symbol in enumerate(symbols)]

    # One app lifespan and four independent cookie jars, sharing the real database.
    with TestClient(app) as worker, ExitStack() as cleanup:
        visitors = [TestClient(app) for _ in range(4)]
        for visitor in visitors:
            cleanup.callback(visitor.close)
            assert visitor.post('/api/v1/sessions/guest', json={}).status_code == 200
        assert len({tuple(client.cookies.items()) for client in visitors}) == 4
        assert worker.post('/api/v1/bridge/worker/heartbeat', headers=auth,
                           json={'state': 'ready'}).status_code == 200

        # Deliberately reuse one request key across visitors: it is owner-scoped.
        request_key = str(uuid4())
        start = Barrier(4)

        def enqueue(index):
            start.wait(timeout=10)
            response = visitors[index].post(
                '/api/v1/bridge/jobs', headers={'Idempotency-Key': request_key},
                json={'synthetic': True, 'dream': {'dream_text': dreams[index]}})
            assert response.status_code == 201, response.text
            return response.json()['job_id']

        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(enqueue, range(4)))
        assert len(set(jobs)) == 4

        def stored_jobs():
            with app.state.bridge_queue.db() as db:
                return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created,id')]

        initial_rows = stored_jobs()
        assert {row['state'] for row in initial_rows} == {'queued'}
        assert len({row['owner'] for row in initial_rows}) == 4
        hashes = {row['id']: row['input_hash'] for row in initial_rows}
        assert len(set(hashes.values())) == 4
        dispatch_order = [row['id'] for row in initial_rows]
        completed = set()
        previous_result = None

        def check_visitor_views(active_job=None):
            for index, visitor in enumerate(visitors):
                for job in jobs:
                    response = visitor.get('/api/v1/bridge/jobs/' + job)
                    assert response.headers['cache-control'] == 'no-store'
                    if job != jobs[index]:
                        assert response.status_code == 404
                        assert response.json() == {'detail': 'job_not_found'}
                        continue
                    assert response.status_code == 200
                    snapshot = response.json()
                    assert snapshot['dream_text'] == dreams[index]
                    expected = 'completed' if job in completed else 'leased' if job == active_job else 'queued'
                    assert snapshot['state'] == expected
                    if job in completed:
                        assert snapshot['result']['reflection']['title'] == symbols[index]
                        assert snapshot['result']['reflection']['symbols'][0]['evidence'] == symbols[index]
                    else:
                        assert snapshot['result'] is None

        check_visitor_views()
        for expected_job in dispatch_order:
            claim_key = str(uuid4())
            claim = worker.post('/api/v1/bridge/worker/claim', headers=auth,
                                json={'claim_key': claim_key})
            assert claim.status_code == 200
            packet = JobInput.model_validate(claim.json()['job'])
            job = str(packet.job_id)
            index = jobs.index(job)
            assert job == expected_job
            assert packet.dream.dream_text == dreams[index]
            assert packet.input_sha256 == hashes[job]
            assert packet.attempt == 1
            assert sum(row['state'] == 'leased' for row in stored_jobs()) == 1

            # A lost claim response is repeatable, but a different claim cannot
            # start a second generation while this one is leased.
            repeat = worker.post('/api/v1/bridge/worker/claim', headers=auth,
                                 json={'claim_key': claim_key})
            assert repeat.status_code == 200 and repeat.json() == claim.json()
            busy = worker.post('/api/v1/bridge/worker/claim', headers=auth,
                               json={'claim_key': str(uuid4())})
            assert busy.status_code == 409 and busy.json() == {'detail': 'worker_busy'}
            check_visitor_views(active_job=job)

            result = JobOutput(
                job_id=packet.job_id, lease_id=packet.lease_id, attempt=packet.attempt,
                input_sha256=packet.input_sha256, output_locale=packet.output_locale,
                outcome='needs_context', reflection={
                    'title': symbols[index],
                    'symbols': [{'label': symbols[index], 'evidence': symbols[index]}],
                    'summary': f'В вымышленном сне запомнился {symbols[index]}; личное значение неизвестно.',
                    'key_connection': 'Можно начать с собственных ассоциаций с этим образом.',
                    'reflection_question': 'Какие чувства вызвал этот образ во сне?'},
                safe_message=None, source_use=[]).model_dump(mode='json')
            expected_packet = packet.model_dump(mode='json')
            assert parse_answer(json.dumps(result), expected_packet) == result
            if previous_result is not None:
                # Even a valid, internally consistent answer for the preceding
                # visitor cannot choose its recipient in the current worker turn.
                with pytest.raises(ValueError, match='^correlation_mismatch$'):
                    parse_answer(json.dumps(previous_result), expected_packet)
            other_job = jobs[(index + 1) % len(jobs)]
            before_invalid = stored_jobs()
            for field, incorrect_value in (
                ('job_id', other_job),
                ('lease_id', str(uuid4())),
                ('attempt', packet.attempt + 1),
                ('input_sha256', hashes[other_job]),
            ):
                rejected = worker.post('/api/v1/bridge/worker/submit', headers=auth,
                                       json=result | {field: incorrect_value})
                assert rejected.status_code == 409, (field, rejected.text)
                assert rejected.json() == {'detail': 'stale_or_mismatched_result'}
                # Not just the target: every stored job must remain unchanged.
                assert stored_jobs() == before_invalid

            submitted = worker.post('/api/v1/bridge/worker/submit', headers=auth, json=result)
            assert submitted.status_code == 200 and submitted.json() == {'state': 'completed'}
            completed.add(job)
            after_submit = stored_jobs()
            # Retrying delivery does not overwrite another visitor or consume a job.
            duplicate = worker.post('/api/v1/bridge/worker/submit', headers=auth, json=result)
            assert duplicate.status_code == 200 and duplicate.json() == {'state': 'completed'}
            assert stored_jobs() == after_submit
            check_visitor_views()
            previous_result = result

        assert completed == set(jobs)
        empty = worker.post('/api/v1/bridge/worker/claim', headers=auth,
                            json={'claim_key': str(uuid4())})
        assert empty.status_code == 200 and empty.json() == {'job': None}
