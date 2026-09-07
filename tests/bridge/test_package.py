import hashlib
import json
from pathlib import Path

from apps.bridge.contracts import JobInput, JobOutput
from apps.bridge.queue import digest, serialized

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / 'chatgpt-project'


def read(name):
    return json.loads((PACK / name).read_text(encoding='utf-8'))


def test_allowlisted_package_matches_manifest_and_sources():
    manifest = read('UPLOAD_MANIFEST.json')
    assert len(manifest['sources']) == 12
    assert len(manifest['files']) == 11
    for entry in manifest['files']:
        target = (PACK / entry['path']).resolve()
        assert target.is_relative_to(PACK.resolve())
        content = target.read_bytes()
        assert len(content) == entry['bytes']
        assert hashlib.sha256(content).hexdigest() == entry['sha256']
        assert entry['upload_allowed'] and not entry['contains_personal_dreams']
    catalog = json.loads((ROOT / 'docs/research/sources.json').read_text(encoding='utf-8'))
    assert read('SOURCE_CATALOG.json') == catalog
    assert {s['source_id'] for s in manifest['sources']} == {s['id'] for s in catalog['sources']}
    assert all(not s['full_text_included'] and not s['full_text_upload_allowed_by_this_manifest'] for s in manifest['sources'])


def test_schemas_and_correlated_synthetic_example():
    assert read('schemas/input.schema.json') == JobInput.model_json_schema()
    assert read('schemas/output.schema.json') == JobOutput.model_json_schema()
    packet = JobInput.model_validate(read('examples/blue-parrot.input.json'))
    result = JobOutput.model_validate(read('examples/blue-parrot.output.json'))
    for field in ('job_id', 'lease_id', 'attempt', 'input_sha256', 'output_locale', 'knowledge_version', 'contract_version'):
        assert getattr(packet, field) == getattr(result, field)
    assert packet.input_sha256 == digest(serialized(packet.dream.model_dump(mode='json')))
    assert packet.data_class == 'synthetic'
    assert all(s.evidence in packet.dream.dream_text for s in result.reflection.symbols)
    assert len(read('examples/quality-cases.json')) == 7
