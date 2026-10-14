from uuid import uuid4
import pytest
from apps.bridge.queue import BridgeError
from scripts.recover_worker_result import recover
from tests.bridge.test_dispatch import SERVICE, SITE, answer, queue


def failed_result(queue):
    q, _ = queue
    q.enqueue(SITE, 'blue-parrot-ru', uuid4())
    packet = q.claim_next(SERVICE, uuid4())
    q.fail_dispatch(SERVICE, packet.job_id, packet.lease_id, packet.attempt, 'invalid_model_output')
    return q, packet, answer(packet).model_dump(mode='json')


def test_operator_recovers_same_validated_result(queue):
    q, packet, output = failed_result(queue)
    assert recover(q, output)['state'] == 'completed'
    snapshot = q.snapshot_for_site(SITE, packet.job_id)
    assert snapshot['state'] == 'completed' and snapshot['attempt'] == 1


@pytest.mark.parametrize('change', ['cancel', 'retry', 'delete', 'evidence', 'lease'])
def test_recovery_rejects_changed_job_and_ungrounded_output(queue, change):
    q, packet, output = failed_result(queue)
    if change in {'cancel', 'retry', 'delete'}:
        getattr(q, change)(SITE, packet.job_id)
    elif change == 'evidence':
        output['reflection']['symbols'][0]['evidence'] = 'not present in the dream'
    else:
        output['lease_id'] = str(uuid4())
    with pytest.raises(BridgeError):
        recover(q, output)
