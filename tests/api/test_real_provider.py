"""All provider traffic uses MockTransport; no key or microphone is used."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.api.sonnic.models import DreamInput, FreeResult
from apps.api.sonnic.real_provider import AISettings, OpenAIProvider, ProviderError, Extraction, Reflection, Review, response_schema
from test_flow import guest, submit, completed

BODY = DreamInput(dream_text='приснился синий попугай')
SYMBOLS = [{'label':'Синий попугай','evidence':'синий попугай'}]
DRAFT = {'title':'Синий попугай', 'summary':'В вашем рассказе выделяется синий попугай. Его цвет можно рассмотреть через собственные ассоциации, а не как знак будущего. Пока неизвестно, что птица делала и какие чувства вы испытывали. Поэтому точнее начать с этих деталей и проверить, чем этот образ отличается для вас от обычной птицы.', 'key_connection':'Связь цвета и птицы зависит от ваших собственных ассоциаций.', 'reflection_question':'Что делал синий попугай и что вы чувствовали рядом с ним?'}
SETTINGS = AISettings('test-only-key','extract-test','free-test')


def envelope(data):
    return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(data,ensure_ascii=False)}]}], 'usage':{'input_tokens':100,'output_tokens':50}}


def provider_with(responses, requests=None):
    responses = iter(responses)
    async def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        assert str(request.url) == 'https://api.openai.com/v1/responses'
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value,httpx.Response) else httpx.Response(200,json=envelope(value))
    return OpenAIProvider(SETTINGS, httpx.MockTransport(handler))


def test_real_flow_with_mock_and_private_telemetry(tmp_path):
    requests=[]
    provider=provider_with([{'symbols':SYMBOLS,'safety_action':'allow'},DRAFT,{'approved':True}],requests)
    with TestClient(create_app(str(tmp_path/'real.sqlite3'),provider=provider,mode='real')) as c:
        assert c.get('/api/v1/config').json()['ai_mode']=='real'
        guest(c); created=submit(c,text=BODY.dream_text)
        assert created.json()['mode']=='real'
        did=created.json()['dream_id']; job=completed(c,did)
        assert job['status']=='completed',job
        assert job['result']['summary']==DRAFT['summary']
        assert job['result']['evidence_basis']=='model_from_user_text'
        assert job['result']['research_retrieval_used'] is False
        assert job['result']['emotional_tone']=='Не указано'
        with c.app.state.store.db() as db:
            stats=json.loads(db.execute('SELECT telemetry FROM dreams WHERE id=?',(did,)).fetchone()[0])
        assert len(stats['calls'])==3
        assert stats['prompt_version']=='real-v1'
        assert stats['cost_estimate'] is None
        assert BODY.dream_text not in json.dumps(stats,ensure_ascii=False)
        assert SETTINGS.api_key not in json.dumps(stats)
        # Job-local measurements do not accumulate in the shared factory.
        assert provider.calls==[]
        c.post(f'/api/v1/dreams/{did}/free')
        assert len(requests)==3
    for r in requests:
        assert r['store'] is False and r['text']['format']['strict'] is True
        assert 'tools' not in r
        assert BODY.dream_text in r['input'][0]['content']
    assert requests[1]['model']=='free-test'


@pytest.mark.parametrize('status,code',[(401,'provider_auth'),(403,'provider_auth'),(429,'provider_rate_limited'),(503,'provider_unavailable')])
def test_redacted_http_errors_no_paid_retry(status,code):
    seen=[]; p=provider_with([httpx.Response(status,json={'private':'SECRET '+BODY.dream_text})],seen)
    with pytest.raises(ProviderError) as error:
        asyncio.run(p.extract(BODY))
    assert str(error.value)==code
    assert len(seen)==1


@pytest.mark.parametrize('response',[
    {'status':'incomplete','output':[]},
    {'status':'completed','output':[]},
    envelope({'symbols':[{'label':'Клетка','evidence':'клетка'}],'safety_action':'allow'}),
    envelope({'symbols':SYMBOLS,'safety_action':'allow','extra':'bad'}),
    envelope({'symbols':[{'label':'','evidence':''}],'safety_action':'allow'}),
])
def test_invalid_or_ungrounded_output_is_rejected(response):
    p=provider_with([httpx.Response(200,json=response)])
    with pytest.raises(ProviderError,match='invalid_model_output'):
        asyncio.run(p.extract(BODY))


def test_refusal():
    p=provider_with([httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'private text'}]}]})])
    with pytest.raises(ProviderError,match='provider_refused'):
        asyncio.run(p.extract(BODY))


def test_timeout_and_network_errors():
    for error,code in [(httpx.ReadTimeout('private'),'timeout'),(httpx.ConnectError('private'),'provider_unavailable')]:
        with pytest.raises(ProviderError,match=code):
            asyncio.run(provider_with([error]).extract(BODY))


def test_failed_review_never_exposes_draft(tmp_path):
    p=provider_with([{'symbols':SYMBOLS,'safety_action':'allow'},DRAFT,{'approved':False}])
    with TestClient(create_app(str(tmp_path/'rejected.sqlite3'),provider=p,mode='real')) as c:
        guest(c); did=submit(c,text=BODY.dream_text).json()['dream_id']; job=completed(c,did)
        assert job['status']=='failed' and job['result'] is None
        assert job['error']=='invalid_model_output'
        with c.app.state.store.db() as db:
            stats=json.loads(db.execute('SELECT telemetry FROM dreams WHERE id=?',(did,)).fetchone()[0])
        assert len(stats['calls'])==3


@pytest.mark.parametrize('action,code',[('support','safety_support'),('refuse','safety_refused')])
def test_safety_action_skips_interpretation_and_retry(tmp_path,action,code):
    seen=[]; p=provider_with([{'symbols':[],'safety_action':action}],seen)
    with TestClient(create_app(str(tmp_path/f'{action}.sqlite3'),provider=p,mode='real')) as c:
        guest(c); did=submit(c,text=BODY.dream_text).json()['dream_id']; job=completed(c,did)
        assert job['error']==code and job['result'] is None and not job['can_retry']
        assert c.post(f'/api/v1/dreams/{did}/free').status_code==409
        assert len(seen)==1


def test_real_requires_explicit_access_demo_does_not(monkeypatch,tmp_path):
    for name in ('OPENAI_API_KEY','OPENAI_EXTRACT_MODEL','OPENAI_FREE_MODEL','AI_ALLOW_PAID_REQUESTS'):
        monkeypatch.delenv(name,raising=False)
    with pytest.raises(RuntimeError,match='AI_ALLOW_PAID_REQUESTS'):
        create_app(mode='real')
    monkeypatch.setenv('AI_ALLOW_PAID_REQUESTS','true')
    with pytest.raises(RuntimeError,match='OPENAI_API_KEY'):
        create_app(mode='real')
    with TestClient(create_app(str(tmp_path/'demo.sqlite3'),mode='demo')) as c:
        guest(c); did=submit(c).json()['dream_id']
        assert completed(c,did)['result']['mode']=='demo'


def test_strict_schemas_and_provenance():
    def visit(node):
        if isinstance(node,dict):
            if node.get('type')=='object':
                assert node['additionalProperties'] is False
                assert set(node['required'])==set(node['properties'])
            for value in node.values(): visit(value)
        elif isinstance(node,list):
            for value in node: visit(value)
    for shape in (Extraction,Reflection,Review): visit(response_schema(shape))
    assert 'test-only-key' not in repr(SETTINGS)
    with pytest.raises(ValueError):
        FreeResult(**DRAFT,symbols=[],emotional_tone='',locked_sections=['a']*5,locale='ru-RU',mode='real')
