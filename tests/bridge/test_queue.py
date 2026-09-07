import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from apps.bridge.contracts import JobOutput, SourceUse, UnconnectedProjectTransport
from apps.bridge.queue import BridgeError, Principal, Queue


@pytest.fixture
def lab(tmp_path):
    now = [1000.0]
    queue = Queue(tmp_path / 'queue.sqlite3', clock=lambda: now[0])
    site = Principal('site-a', 'owner-a', frozenset({'site'}))
    worker = Principal('worker-a', 'owner-a', frozenset({'worker'}))
    reviewer = Principal('human-a', 'owner-a', frozenset({'review'}))
    return queue, site, worker, reviewer, now


def claimed(lab, case='blue-parrot-ru'):
    queue, site, worker, *_ = lab
    job = UUID(queue.enqueue(site, case, uuid4()))
    return job, queue.claim(worker, job, uuid4())


def answer(packet):
    return JobOutput(
        job_id=packet.job_id, lease_id=packet.lease_id, attempt=packet.attempt,
        input_sha256=packet.input_sha256, output_locale=packet.output_locale,
        outcome='needs_context', reflection={
            'title': 'Синий попугай',
            'symbols': [{'label': 'Синий попугай', 'evidence': 'синий попугай'}],
            'summary': 'В рассказе запомнились птица и её цвет. Их личное значение пока неизвестно.',
            'key_connection': 'Можно начать с ваших собственных ассоциаций с этой птицей.',
            'reflection_question': 'Что попугай делал во сне?'},
        safe_message=None, source_use=[])


def test_approval_gate_and_site_projection(lab):
    q, site, worker, reviewer, _ = lab
    job, packet = claimed(lab)
    result = answer(packet)
    result.source_use = [SourceUse(source_id='R02', note='Original note considered; association is not a causal explanation.')]
    assert q.submit(worker, result) == 'awaiting_review'
    with pytest.raises(BridgeError, match='result_not_ready'):
        q.result_for_site(site, job)
    with pytest.raises(BridgeError, match='forbidden'):
        q.review(worker, job, True)
    q.review(reviewer, job, True)
    public = q.result_for_site(site, job)
    assert public['mode'] == 'bridge_contract_preview'
    assert public['origin_verified'] is False
    assert public['data_class'] == 'synthetic'
    assert set(public) == {'mode', 'origin_verified', 'data_class', 'locale', 'outcome', 'reflection', 'safe_message'}
    assert public['reflection']['title'] == 'Синий попугай'
    assert q.submit(worker, result) == 'completed'


def test_synthetic_only_and_all_fixed_cases(lab):
    q, site, *_ = lab
    with pytest.raises(BridgeError, match='synthetic_case_required'):
        q.enqueue(site, 'My private dream written here', uuid4())
    assert len(q.cases) == 7
    for case in q.cases:
        assert q.status(site, UUID(q.enqueue(site, case, uuid4())))['state'] == 'queued'


def test_idempotent_enqueue_claim_and_submit(lab):
    q, site, worker, *_ = lab
    key, claim_key = uuid4(), uuid4()
    job = UUID(q.enqueue(site, 'blue-parrot-ru', key))
    assert q.enqueue(site, 'blue-parrot-ru', key) == str(job)
    with pytest.raises(BridgeError, match='idempotency_conflict'):
        q.enqueue(site, 'red-bicycle-ru', key)
    packet = q.claim(worker, job, claim_key)
    assert q.claim(worker, job, claim_key) == packet
    with pytest.raises(BridgeError, match='job_not_queued'):
        q.claim(worker, job, uuid4())
    result = answer(packet)
    assert q.submit(worker, result) == q.submit(worker, result) == 'awaiting_review'
    result.reflection.title = 'Другой ответ'
    with pytest.raises(BridgeError, match='result_conflict'):
        q.submit(worker, result)


@pytest.mark.parametrize('operation', ['status', 'result_for_site', 'cancel', 'retry', 'claim', 'review', 'submit'])
def test_foreign_owner_cannot_access(lab, operation):
    q, _, _, _, _ = lab
    job, packet = claimed(lab)
    foreign = Principal('intruder', 'owner-b', frozenset({'site', 'worker', 'review'}))
    args = {'status': (job,), 'result_for_site': (job,), 'cancel': (job,), 'retry': (job,),
            'claim': (job, uuid4()), 'review': (job, True), 'submit': (answer(packet),)}
    with pytest.raises(BridgeError, match='job_not_found'):
        getattr(q, operation)(foreign, *args[operation])


def test_scope_and_worker_identity(lab):
    q, site, worker, *_ = lab
    with pytest.raises(BridgeError, match='forbidden'):
        q.enqueue(worker, 'blue-parrot-ru', uuid4())
    job, packet = claimed(lab)
    with pytest.raises(BridgeError, match='forbidden'):
        q.submit(site, answer(packet))
    other = Principal('worker-other', site.owner, worker.scopes)
    with pytest.raises(BridgeError, match='stale_or_mismatched_result'):
        q.submit(other, answer(packet))


@pytest.mark.parametrize('field,value,error', [
    ('input_sha256', '0'*64, 'stale_or_mismatched_result'),
    ('lease_id', UUID(int=1), 'stale_or_mismatched_result'),
    ('attempt', 2, 'stale_or_mismatched_result'),
    ('output_locale', 'en-US', 'locale_mismatch'),
])
def test_reject_mismatched_output(lab, field, value, error):
    q, _, worker, *_ = lab
    _, packet = claimed(lab)
    result = answer(packet).model_copy(update={field: value})
    with pytest.raises(BridgeError, match=error):
        q.submit(worker, result)


def test_reject_ungrounded_evidence_and_extra_fields(lab):
    q, _, worker, *_ = lab
    _, packet = claimed(lab)
    result = answer(packet)
    result.reflection.symbols[0].evidence = 'летел в клетке'
    with pytest.raises(BridgeError, match='ungrounded_evidence'):
        q.submit(worker, result)
    raw = result.model_dump()
    raw['callback_url'] = 'https://example.invalid/collect'
    with pytest.raises(ValidationError):
        JobOutput.model_validate(raw)
    with pytest.raises(ValidationError):
        q.submit(worker, result.model_copy(update={'contract_version': 'unknown'}))


def test_expiry_retry_and_stale_lease(lab):
    q, site, worker, _, now = lab
    job, packet = claimed(lab)
    now[0] += 600
    assert q.status(site, job)['state'] == 'expired'
    with pytest.raises(BridgeError, match='job_not_leased'):
        q.submit(worker, answer(packet))
    q.retry(site, job)
    newer = q.claim(worker, job, uuid4())
    assert newer.attempt == 2 and newer.lease_id != packet.lease_id
    with pytest.raises(BridgeError, match='stale_or_mismatched_result'):
        q.submit(worker, answer(packet))
    q.submit(worker, answer(newer))


def test_retry_limit_and_rejected_review(lab):
    q, site, worker, reviewer, _ = lab
    job, packet = claimed(lab)
    for attempt in range(1, 4):
        assert packet.attempt == attempt
        q.submit(worker, answer(packet))
        q.review(reviewer, job, False)
        assert q.status(site, job)['error'] == 'review_rejected'
        if attempt < 3:
            q.retry(site, job)
            packet = q.claim(worker, job, uuid4())
    with pytest.raises(BridgeError, match='retry_not_allowed'):
        q.retry(site, job)


def test_cancel_scrubs_payload_and_rejects_late_result(lab):
    q, site, worker, *_ = lab
    key = uuid4()
    job = UUID(q.enqueue(site, 'blue-parrot-ru', key))
    packet = q.claim(worker, job, uuid4())
    q.submit(worker, answer(packet))
    q.cancel(site, job)
    q.cancel(site, job)
    assert q.enqueue(site, 'blue-parrot-ru', key) == str(job)
    assert q.status(site, job)['state'] == 'cancelled'
    with q.db() as db:
        row = db.execute('SELECT input,output FROM jobs WHERE id=?', (str(job),)).fetchone()
        assert row['input'] == '{}' and row['output'] is None
    with pytest.raises(BridgeError, match='stale_or_mismatched_result'):
        q.submit(worker, answer(packet))


def test_persistence_and_concurrent_claim(lab):
    q, site, worker, _, now = lab
    jobs = [UUID(q.enqueue(site, 'blue-parrot-ru', uuid4())) for _ in range(2)]
    def claim(job):
        reopened = Queue(q.path, clock=lambda: now[0])
        try:
            return reopened.claim(worker, job, uuid4())
        except BridgeError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, jobs))
    assert sum(isinstance(r, str) and r == 'worker_busy' for r in results) == 1
    packet = next(r for r in results if not isinstance(r, str))
    reopened = Queue(q.path, clock=lambda: now[0])
    assert reopened.submit(worker, answer(packet)) == 'awaiting_review'


def test_queue_capacity_and_whole_job_deadline(lab):
    q, site, worker, _, now = lab
    jobs = [UUID(q.enqueue(site, 'blue-parrot-ru', uuid4())) for _ in range(20)]
    with pytest.raises(BridgeError, match='queue_full'):
        q.enqueue(site, 'blue-parrot-ru', uuid4())
    packet = q.claim(worker, jobs[0], uuid4())
    q.submit(worker, answer(packet))
    now[0] += 3600
    assert all(q.status(site, job)['state'] == 'expired' for job in jobs)
    assert q.enqueue(site, 'blue-parrot-ru', uuid4())


def test_support_result_and_exclusive_shape(lab):
    q, site, worker, reviewer, _ = lab
    job, packet = claimed(lab)
    raw = answer(packet).model_dump()
    raw.update(outcome='support', reflection=None, safe_message='Please seek immediate help if you are in danger.')
    result = JobOutput.model_validate(raw)
    q.submit(worker, result)
    q.review(reviewer, job, True)
    assert q.result_for_site(site, job)['reflection'] is None
    raw['outcome'] = 'reflection'
    with pytest.raises(ValidationError):
        JobOutput.model_validate(raw)


def test_unconnected_transport_does_not_claim_processing():
    with pytest.raises(RuntimeError, match='chatgpt_project_transport_not_connected'):
        asyncio.run(UnconnectedProjectTransport().request_processing(uuid4()))
