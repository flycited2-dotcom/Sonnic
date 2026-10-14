"""Operator-only repair of a falsely rejected result; no browser or generation calls."""
import argparse
import json
from pathlib import Path
import sys

from apps.api.sonnic.models import DreamInput
from apps.bridge.contracts import JobOutput
from apps.bridge.queue import Queue, BridgeError, serialized, digest


def recover(queue, output):
    result = JobOutput.model_validate(output)
    raw = serialized(result.model_dump(mode='json'))
    with queue.db() as db:
        # Queue.db already holds an immediate transaction.
        row = db.execute('SELECT * FROM jobs WHERE id=?', (str(result.job_id),)).fetchone()
        if (row is None or row['dispatch'] != 'automatic' or row['worker'] != 'sonnic-dispatcher'
                or row['state'] != 'failed' or row['error'] != 'invalid_model_output'):
            raise BridgeError('recovery_not_allowed')
        if (row['lease_id'] != str(result.lease_id) or row['attempt'] != result.attempt
                or row['input_hash'] != result.input_sha256):
            raise BridgeError('stale_or_mismatched_result')
        body = DreamInput.model_validate_json(row['input'])
        if result.output_locale != body.locale:
            raise BridgeError('locale_mismatch')
        if result.reflection and any(s.evidence not in body.dream_text for s in result.reflection.symbols):
            raise BridgeError('ungrounded_evidence')
        db.execute("UPDATE jobs SET state='completed',output=?,output_hash=?,error=NULL,reviewer='operator:validated-recovery' WHERE id=?",
                   (raw, digest(raw), str(result.job_id)))
    return {'job_id': str(result.job_id), 'state': 'completed', 'operator_recovery': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--operator-recovery', action='store_true', required=True)
    args = parser.parse_args()
    print(json.dumps(recover(Queue(Path(args.db)), json.load(sys.stdin))))
