"""Rebuild only the allowlisted public/synthetic upload package, without networking."""
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api.sonnic.models import DreamInput
from apps.bridge.contracts import JobInput, JobOutput
from apps.bridge.queue import digest, serialized


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build():
    pack = ROOT / 'chatgpt-project'
    (pack / 'schemas').mkdir(parents=True, exist_ok=True)
    (pack / 'examples').mkdir(exist_ok=True)
    for name, shape in [('input', JobInput), ('output', JobOutput)]:
        write_json(pack / 'schemas' / f'{name}.schema.json', shape.model_json_schema())
    catalog = json.loads((ROOT / 'docs/research/sources.json').read_text(encoding='utf-8'))
    cases = json.loads((ROOT / 'tests/quality/dreams.json').read_text(encoding='utf-8'))
    write_json(pack / 'SOURCE_CATALOG.json', catalog)
    write_json(pack / 'examples/quality-cases.json', cases)
    notes = ['# Sonnic — собственные исследовательские заметки\n',
             'Версия knowledge-v1. Подготовлено 2026-09-06 из каталога, проверенного 2026-09-05. '
             'Это библиография и собственное краткое изложение; полные тексты, таблицы, иллюстрации, словари '
             'и личные корпуса не включены. Загрузка заметок не означает загрузку/прочтение исходной статьи '
             'и не является обучением модели.\n']
    sources = []
    for source in catalog['sources']:
        notes.extend([
            f'<a id="{source["id"]}"></a>\n\n## {source["id"]} — {source["title"]}\n',
            f'{source["authors"]}. {source["year"]}. [Исходная публикация]({source["url"]}); '
            f'[проверенная страница]({source["verified_url"]}).\n',
            f'Тип: {source["type"]}. Язык: {source["language"]}. Выборка/контекст: {source["region"]}.\n',
            f'Собственное краткое изложение: {source["finding"]}\n',
            f'Ограничения: {source["limits"]}\n',
            f'Применение в Sonnic: {source["use"]}\n',
            f'Права оригинала: {source["rights"]} В пакет включены только собственные заметки и библиография. '
            f'Проверка источника: {source["checked_at"]}; {source["verification"]}\n',
        ])
        sources.append(dict(source_id=source['id'], title=source['title'], url=source['url'],
                            checked_at=source['checked_at'], included_material='original_note_and_bibliography',
                            full_text_included=False, full_text_upload_allowed_by_this_manifest=False,
                            rights_of_original=source['rights'], location_in_upload=f'RESEARCH_NOTES.md#{source["id"]}'))
    (pack / 'RESEARCH_NOTES.md').write_text('\n'.join(notes), encoding='utf-8')
    case = next(c for c in cases if c['id'] == 'blue-parrot-ru')
    dream = DreamInput(dream_text=case['dream_text'], locale=case['locale'])
    packet = JobInput(job_id=UUID(int=101), lease_id=UUID(int=102), attempt=1,
                      input_sha256=digest(serialized(dream.model_dump(mode='json'))),
                      output_locale=dream.locale, dream=dream)
    result = JobOutput(job_id=packet.job_id, lease_id=packet.lease_id, attempt=packet.attempt,
                       input_sha256=packet.input_sha256, output_locale=packet.output_locale,
                       outcome='needs_context', reflection={
                           'title': 'Синий попугай',
                           'symbols': [{'label': 'Попугай', 'evidence': 'попугай'},
                                       {'label': 'Синий цвет', 'evidence': 'синий'}],
                           'summary': 'В коротком рассказе выделяются попугай и его синий цвет. Возможно, '
                           'образ связан с недавним впечатлением или личной ассоциацией, но по одной фразе '
                           'этого не определить. Пока неизвестно, что птица делала и какие чувства вы '
                           'испытывали. Здесь можно начать с того, чем именно она вам запомнилась.',
                           'key_connection': 'Значение цвета и птицы лучше искать в ваших ассоциациях, '
                           'а не в универсальном словаре символов.',
                           'reflection_question': 'Что попугай делал во сне?'},
                       safe_message=None, source_use=[])
    write_json(pack / 'examples/blue-parrot.input.json', packet.model_dump(mode='json'))
    write_json(pack / 'examples/blue-parrot.output.json', result.model_dump(mode='json'))
    files = ['PROJECT_INSTRUCTIONS.md', 'STYLE_GUIDE.md', 'CULTURAL_GAPS.md', 'RESEARCH_NOTES.md',
             'SOURCE_CATALOG.json', 'BRIDGE_CONTRACT.md', 'schemas/input.schema.json',
             'schemas/output.schema.json', 'examples/quality-cases.json',
             'examples/blue-parrot.input.json', 'examples/blue-parrot.output.json']
    manifest = dict(version='knowledge-v1', prepared_at='2026-09-06',
                    target_project_name='Sonnic — база знаний и разбор снов',
                    included_scope='bibliography_and_original_notes_only',
                    source_catalog='docs/research/sources.json', sources=sources, files=[])
    for name in files:
        data = (pack / name).read_bytes()
        manifest['files'].append(dict(path=name, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                                     upload_allowed=True, contains_personal_dreams=False))
    # Remote state is recorded separately and never reset/guessed by rebuilding.
    manifest['remote_state_file'] = 'PROJECT_STATUS.json'
    write_json(pack / 'UPLOAD_MANIFEST.json', manifest)
    print(f'Prepared {len(files)} allowlisted files and {len(sources)} source notes; no uploads or model calls.')


if __name__ == '__main__':
    build()
