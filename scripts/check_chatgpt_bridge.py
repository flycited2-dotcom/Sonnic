"""Local synthetic round trip; the candidate is authored, never a live model reply."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.bridge.contracts import JobOutput
from apps.bridge.queue import Principal, Queue


def main():
    with TemporaryDirectory(prefix='sonnic-bridge-') as temporary:
        queue = Queue(Path(temporary) / 'queue.sqlite3')
        site = Principal('local-site', 'synthetic-lab', frozenset({'site'}))
        worker = Principal('local-fixture', 'synthetic-lab', frozenset({'worker'}))
        reviewer = Principal('local-test-review', 'synthetic-lab', frozenset({'review'}))
        job = UUID(queue.enqueue(site, 'blue-parrot-ru', uuid4()))
        states = [queue.status(site, job)['state']]
        packet = queue.claim(worker, job, uuid4())
        states.append(queue.status(site, job)['state'])
        raw = json.loads((ROOT / 'chatgpt-project/examples/blue-parrot.output.json').read_text(encoding='utf-8'))
        raw.update(job_id=str(job), lease_id=str(packet.lease_id), attempt=packet.attempt,
                   input_sha256=packet.input_sha256)
        states.append(queue.submit(worker, JobOutput.model_validate(raw)))
        # Fixture-only approval checks delivery mechanics, not model quality.
        queue.review(reviewer, job, True)
        states.append(queue.status(site, job)['state'])
        preview = queue.result_for_site(site, job)
        output = ROOT / '.cache/bridge-preview.json'
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(preview, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(' -> '.join(states))
        print('Synthetic, hand-authored fixture; no ChatGPT connection, no website publication.')
        print(f'Preview: {output}')


if __name__ == '__main__':
    main()
