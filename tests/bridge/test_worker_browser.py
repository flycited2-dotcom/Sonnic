"""Exercise the production browser state machine with deterministic UI frames.

No Chrome, HTTP, CDP, or model calls. Stale DOM and streaming are simulated,
but JSON parsing, correlation, journal transitions and send logic are real.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from apps.worker import browser as module
from apps.worker.browser import ASSISTANT, COMPOSER, STOP, USER, BrowserFailure, ChatGPTBrowser, PROJECT, chat_url
from apps.worker.main import make_prompt

ROOT = Path(__file__).resolve().parents[2]
PACKET = json.loads((ROOT / 'chatgpt-project/examples/blue-parrot.input.json').read_text(encoding='utf-8'))
ANSWER = (ROOT / 'chatgpt-project/examples/blue-parrot.output.json').read_text(encoding='utf-8')
URL = PROJECT.removesuffix('/project') + '/c/' + str(uuid4())
OLD_URL = PROJECT.removesuffix('/project') + '/c/' + str(uuid4())
TURN = f"SONNIC JOB {PACKET['job_id']} LEASE {PACKET['lease_id']} ATTEMPT 1"


def test_misplaced_source_list_is_relocated_without_changing_content():
    expected = json.loads(ANSWER)
    misplaced = deepcopy(expected)
    misplaced['reflection']['source_use'] = misplaced.pop('source_use')
    assert module.parse_answer(json.dumps(misplaced), PACKET) == expected


def test_identical_duplicate_sources_are_removed_without_changing_content():
    expected = json.loads(ANSWER)
    duplicated = deepcopy(expected)
    duplicated['reflection']['source_use'] = deepcopy(duplicated['source_use'])
    assert module.parse_answer(json.dumps(duplicated), PACKET) == expected
    duplicated['reflection']['source_use'] = [{'source_id': 'R01', 'note': 'A conflicting account of source usage'}]
    with pytest.raises(ValueError):
        module.parse_answer(json.dumps(duplicated), PACKET)


def test_typographic_quotes_are_removed_only_around_exact_source_text():
    output = json.loads(ANSWER)
    expected = deepcopy(output)
    for symbol in output['reflection']['symbols']:
        symbol['evidence'] = '«' + symbol['evidence'] + '»'
    assert module.parse_answer(json.dumps(output), PACKET) == expected
    packet = deepcopy(PACKET)
    packet['dream']['dream_text'] = ' '.join(symbol['evidence'] for symbol in output['reflection']['symbols'])
    assert module.parse_answer(json.dumps(output), packet) == output  # Real source quotes stay.
    output['reflection']['symbols'][0]['evidence'] = '«A different dream»'
    with pytest.raises(ValueError, match='ungrounded_evidence'):
        module.parse_answer(json.dumps(output), PACKET)


@pytest.mark.parametrize('defect', ['conflicting_sources', 'unknown_field', 'bad_source', 'wrong_job', 'foreign_evidence'])
def test_source_relocation_keeps_schema_correlation_and_evidence_strict(defect):
    misplaced = json.loads(ANSWER)
    misplaced['reflection']['source_use'] = misplaced.pop('source_use')
    if defect == 'conflicting_sources':
        misplaced['source_use'] = [{'source_id': 'R01', 'note': 'Different source usage from the nested field'}]
    elif defect == 'unknown_field':
        misplaced['reflection']['extra'] = 'no'
    elif defect == 'bad_source':
        misplaced['reflection']['source_use'] = [{'source_id': 'R99', 'note': 'Unknown reference'}]
    elif defect == 'wrong_job':
        misplaced['job_id'] = str(uuid4())
    else:
        misplaced['reflection']['symbols'][0]['evidence'] = 'foreign dream fragment'
    with pytest.raises(ValueError):
        module.parse_answer(json.dumps(misplaced), PACKET)


class Locator:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    @property
    def last(self):
        return self

    async def count(self):
        frame = self.page.frame
        if self.kind == USER:
            return len(frame.get('users', []))
        if self.kind == STOP:
            return int(frame.get('streaming', False))
        if self.kind == 'feedback':
            return int(frame.get('finished', True))
        if self.kind == 'code':
            return int('answer' in frame and frame.get('code_block', True))
        if self.kind == ASSISTANT:
            return int('answer' in frame)
        return 1

    async def all_text_contents(self):
        return self.page.frame.get('users', [])

    async def is_visible(self):
        return True

    def locator(self, selector):
        assert selector == 'pre code'
        return Locator(self.page, 'code')

    async def inner_text(self):
        self.page.answer_reads += 1
        return self.page.frame['answer']

    async def text_content(self):
        self.page.answer_reads += 1
        return self.page.frame['answer']

    async def evaluate(self, script, selector):
        assert selector == USER
        return self.page.frame.get('after_user', True)

    async def fill(self, prompt):
        self.page.prompt = prompt

    async def press(self, key):
        assert key == 'Enter'
        self.page.sends += 1
        self.page.frame_index = 0


class Page:
    def __init__(self, frames, fresh):
        self.frames, self.frame_index = frames, -1 if fresh else 0
        self.sends = self.answer_reads = 0
        self.prompt = None
        self.closed = False

    @property
    def frame(self):
        return {'url': PROJECT, 'users': []} if self.frame_index < 0 else self.frames[min(self.frame_index, len(self.frames)-1)]

    @property
    def url(self):
        return self.frame.get('url', URL)

    def is_closed(self):
        return self.closed

    def locator(self, kind):
        return Locator(self, kind)

    def get_by_test_id(self, name):
        assert name == 'feedback-turn-action-button'
        return Locator(self, 'feedback')


def exercise(monkeypatch, frames, stage='claimed', saved_url=None):
    page = Page(frames, fresh=stage == 'claimed')
    record = {'packet': deepcopy(PACKET), 'stage': stage, 'started': 1000}
    if saved_url:
        record['chat_url'] = saved_url
    browser = ChatGPTBrowser(SimpleNamespace(contexts=[SimpleNamespace(pages=[page])]))
    browser.prepared = page if stage == 'claimed' else None
    browser.check = AsyncMock()
    clock = [1000.0]
    async def sleep(seconds):
        clock[0] += seconds
        page.frame_index += 1
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=sleep))
    saves = []
    return page, record, saves, browser.generate(record, lambda r: saves.append(deepcopy(r)), make_prompt(PACKET))


def frame(**updates):
    return {'users': [TURN], 'answer': ANSWER, 'url': URL, **updates}


def test_transient_old_chat_never_read_or_bound(monkeypatch):
    page, record, saves, task = exercise(monkeypatch, [
        frame(users=['previous job'], url=OLD_URL, answer='old answer'),
        frame(users=['truncated loading message'], url=OLD_URL, answer='old answer'),
        frame(),
    ])
    result = asyncio.run(task)
    assert result['job_id'] == PACKET['job_id']
    assert page.sends == 1 and page.answer_reads == 4
    assert saves[0]['stage'] == 'sending' and 'chat_url' not in saves[0]
    assert {r['chat_url'] for r in saves if 'chat_url' in r} == {URL}
    assert page.prompt.splitlines()[0] == TURN


def test_different_user_after_confirmation_stops_before_answer(monkeypatch):
    page, _, _, task = exercise(monkeypatch, [frame(streaming=True), frame(users=['someone else'])])
    with pytest.raises(BrowserFailure, match='submission_uncertain'):
        asyncio.run(task)
    assert page.sends == 1 and page.answer_reads == 0


def test_wrong_attempt_times_out_without_reading_or_resending(monkeypatch):
    wrong = f"SONNIC JOB {PACKET['job_id']} LEASE {uuid4()} ATTEMPT 1"
    page, record, saves, task = exercise(monkeypatch, [frame(users=[wrong])])
    with pytest.raises(BrowserFailure, match='submission_uncertain'):
        asyncio.run(task)
    assert page.frame_index == 20 and page.sends == 1 and page.answer_reads == 0
    assert 'chat_url' not in record and len(saves) == 1


def test_resume_hydration_keeps_original_url_and_never_sends(monkeypatch):
    page, _, saves, task = exercise(monkeypatch, [frame(users=[]), frame(users=['loading']), frame()],
                                     stage='sent', saved_url=URL)
    assert asyncio.run(task)['job_id'] == PACKET['job_id']
    assert page.sends == 0 and saves == []


def test_resume_saved_url_cannot_be_rebound(monkeypatch):
    page, record, saves, task = exercise(monkeypatch, [frame(streaming=True), frame(url=OLD_URL)],
                                     stage='sent', saved_url=URL)
    with pytest.raises(BrowserFailure, match='submission_uncertain'):
        asyncio.run(task)
    assert page.sends == 0 and page.answer_reads == 0 and record['chat_url'] == URL and saves == []


def test_sending_recovery_finds_exact_lease_without_resend(monkeypatch):
    page, _, saves, task = exercise(monkeypatch, [frame()], stage='sending')
    assert asyncio.run(task)['job_id'] == PACKET['job_id']
    assert page.sends == 0 and saves[0]['chat_url'] == URL


def test_sending_without_matching_chat_fails_without_resend(monkeypatch):
    page, _, _, task = exercise(monkeypatch, [frame(users=['unrelated'])], stage='sending')
    with pytest.raises(BrowserFailure, match='submission_uncertain'):
        asyncio.run(task)
    assert page.sends == 0 and page.answer_reads == 0


def test_streaming_and_old_answer_controls_are_not_completion(monkeypatch):
    frames = [frame(streaming=True), frame(after_user=False), frame(finished=False), frame()]
    page, _, _, task = exercise(monkeypatch, frames)
    assert asyncio.run(task)['job_id'] == PACKET['job_id']
    assert page.answer_reads == 4


@pytest.mark.parametrize('code_block', [True, False])
def test_dictated_double_spaces_are_preserved_from_dom(monkeypatch, code_block):
    packet = deepcopy(PACKET)
    packet['dream']['dream_text'] = packet['dream']['dream_text'].replace('синий попугай', 'синий  попугай')
    output = json.loads(ANSWER)
    for symbol in output['reflection']['symbols']:
        symbol['evidence'] = symbol['evidence'].replace('синий попугай', 'синий  попугай')
    monkeypatch.setattr(__import__(__name__, fromlist=['PACKET']), 'PACKET', packet)
    async def rendered_text(_):
        raise AssertionError('innerText would corrupt whitespace; use textContent')
    monkeypatch.setattr(Locator, 'inner_text', rendered_text)
    page, _, _, task = exercise(monkeypatch, [frame(answer=json.dumps(output, ensure_ascii=False), code_block=code_block)])
    result = asyncio.run(task)
    assert result['reflection']['symbols'] == output['reflection']['symbols']
    assert page.sends == 1


@pytest.mark.parametrize('url', [
    'https://chatgpt.com/c/' + str(uuid4()),
    URL + '?other=1', URL + '#x', URL.replace('https:', 'http:'),
    URL.replace('chatgpt.com', 'chatgpt.com.attacker.invalid'),
    URL.replace('g-p-6a9c47e07510819197bd898c90d1b12b', 'g-p-other-project'),
])
def test_saved_chat_must_belong_to_sonnic(monkeypatch, url):
    assert not chat_url(url)
    page, _, _, task = exercise(monkeypatch, [frame(url=url)], stage='sent', saved_url=url)
    with pytest.raises(BrowserFailure, match='submission_uncertain'):
        asyncio.run(task)
    assert page.sends == 0


@pytest.mark.parametrize('problem', [None, 'draft', 'other_turn', 'navigation', 'failed_output'])
def test_release_keeps_browser_alive_and_returns_only_from_our_answer(problem):
    from unittest.mock import Mock
    editor = SimpleNamespace(count=AsyncMock(return_value=1),
                             inner_text=AsyncMock(return_value='draft' if problem == 'draft' else ''),
                             wait_for=AsyncMock())
    users = SimpleNamespace(all_text_contents=AsyncMock(return_value=['other' if problem == 'other_turn' else TURN]),
                            count=AsyncMock(return_value=0))
    users.last = SimpleNamespace(wait_for=AsyncMock())
    link = SimpleNamespace(click=AsyncMock())
    page = SimpleNamespace(url=URL, is_closed=lambda: False, close=AsyncMock(),
                           locator=lambda selector: users if selector == USER else editor,
                           get_by_role=Mock(return_value=link), wait_for_url=AsyncMock())
    if problem == 'navigation':
        link.click.side_effect = RuntimeError('navigation failed')
    adapter = ChatGPTBrowser(SimpleNamespace(contexts=[]))
    adapter.active = page
    adapter.check = AsyncMock()
    adapter.open_project = AsyncMock(side_effect=link.click)
    record = {'packet': PACKET, 'chat_url': URL, 'output': json.loads(ANSWER)}
    if problem == 'failed_output':
        record.pop('output')
    if problem in {'draft', 'other_turn', 'navigation'}:
        with pytest.raises(BrowserFailure, match='page_changed'):
            asyncio.run(adapter.release(record))
        assert adapter.prepared is None
    else:
        asyncio.run(adapter.release(record))
        assert adapter.prepared is (None if problem else page)
    if problem in {'draft', 'other_turn', 'failed_output'}:
        link.click.assert_not_awaited()
    else:
        link.click.assert_awaited_once()
    if problem is None:
        users.last.wait_for.assert_awaited_once_with(state='hidden', timeout=20000)
    page.close.assert_not_awaited()
    assert adapter.active is None
