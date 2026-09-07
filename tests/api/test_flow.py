import asyncio
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.api.sonnic.orchestrator import DemoProvider
from apps.api.sonnic.store import Store

DREAM = "Мне приснился дом, в котором была вода. Я нашёл ключ и открыл дверь."


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "test.sqlite3"), mode="demo")) as c:
        yield c


def guest(c):
    return c.post("/api/v1/sessions/guest", json={"attribution": {"source": "test", "content_key": "test-001"}})


def submit(c, text=DREAM, key=None, **extras):
    return c.post("/api/v1/dreams", json={"dream_text": text, **extras}, headers={"Idempotency-Key": key or str(uuid4())})


def completed(c, did):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        result = c.get(f"/api/v1/dreams/{did}/free")
        assert result.status_code == 200
        if result.json()["status"] in {"completed", "failed"}:
            return result.json()
        time.sleep(.02)
    pytest.fail("job did not complete")


def test_free_flow_and_private_attribution(client):
    assert client.get("/health/ready").json()["real_ai_available"] is False
    session = guest(client)
    assert "HttpOnly" in session.headers["set-cookie"]
    assert "SameSite=strict" in session.headers["set-cookie"]
    sid = session.json()["guest_session_id"]
    created = submit(client, wake_emotion="любопытство", content_language="ru", recognition_locale="ru-RU")
    assert created.status_code == 201
    did = created.json()["dream_id"]
    job = completed(client, did)
    assert job["status"] == "completed"
    result = job["result"]
    assert result["mode"] == "demo" and result["research_retrieval_used"] is False
    assert result["emotional_tone"] == "любопытство"
    assert all(s["evidence"] in DREAM for s in result["symbols"])
    assert 120 <= len(result["summary"].split()) <= 220
    assert len(result["locked_sections"]) == 5
    assert "no-store" in client.get(f"/api/v1/dreams/{did}/free").headers["cache-control"]
    for _ in range(2):
        assert client.post(f"/api/v1/dreams/{did}/viewed").status_code == 204
    with client.app.state.store.db() as db:
        meta = json.loads(db.execute("SELECT metadata FROM sessions WHERE id=?", (sid,)).fetchone()[0])
        assert meta["attribution"]["content_key"] == "test-001"
        events = [r[0] for r in db.execute("SELECT name FROM events WHERE dream_id=?", (did,))]
        assert sorted(events) == ["dream_submitted", "free_completed", "free_viewed"]
        telemetry = json.loads(db.execute("SELECT telemetry FROM dreams WHERE id=?", (did,)).fetchone()[0])
        assert telemetry["tokens"] == 0 and telemetry["provider"] == "demo"
        assert DREAM not in json.dumps(telemetry)


def test_idempotency_and_changed_payload(client):
    guest(client)
    key = str(uuid4())
    one, two = submit(client, key=key), submit(client, key=key)
    assert one.json()["dream_id"] == two.json()["dream_id"]
    assert submit(client, text=DREAM+" Другой сюжет.", key=key).status_code == 409


def test_no_access_from_another_session(client):
    guest(client)
    did = submit(client).json()["dream_id"]
    completed(client, did)
    client.cookies.clear()
    assert client.get(f"/api/v1/dreams/{did}/free").status_code == 401
    guest(client)
    for method, path in [("get",f"/dreams/{did}/free"),("post",f"/dreams/{did}/free"),("delete",f"/dreams/{did}"),("post",f"/dreams/{did}/viewed")]:
        assert getattr(client, method)(f"/api/v1{path}").status_code == 404


@pytest.mark.parametrize("text", [" ", "x"*19, " "*40, "x"*10001])
def test_input_limits_and_redacted_errors(client, text):
    guest(client)
    response = submit(client, text=text)
    assert response.status_code == 422
    assert "input" not in response.json() and "dream_text" not in response.json()
    assert len(response.text) < 200


def test_supported_boundaries_and_no_invented_symbols(client):
    guest(client)
    for text in ["а"*20, "я"*10000]:
        created = submit(client, text=text)
        assert created.status_code == 201
        result = completed(client, created.json()["dream_id"])["result"]
        assert result["symbols"] == []


def test_validation_and_csrf(client):
    guest(client)
    assert submit(client, dream_date="2999-01-01").status_code == 422
    assert submit(client, locale="not-a-locale").status_code == 422
    assert submit(client, region="United States").status_code == 422
    assert client.post("/api/v1/sessions/guest", json={}, headers={"Origin":"https://other.example"}).status_code == 403
    assert client.post("/api/v1/sessions/guest", content=b"x"*65537).status_code == 413


def test_language_and_culture_are_separate(client):
    guest(client)
    did = submit(client, locale="en-US", content_language="ja", region="FR", cultural_perspective="chosen by user", recognition_locale="ja-JP").json()["dream_id"]
    result = completed(client, did)["result"]
    assert result["locale"] == "en-US"
    assert result["title"] == "Start with how it felt"
    with client.app.state.store.db() as db:
        body = json.loads(db.execute("SELECT input FROM dreams WHERE id=?", (did,)).fetchone()[0])
    assert body["region"] == "FR" and body["content_language"] == "ja"
    assert body["recognition_locale"] == "ja-JP"


def test_no_dangerous_symbol_interpretation(client):
    guest(client)
    did = submit(client, text="Мне снилась смерть и кровь. Я проснулся и понимаю, что это был только сон.").json()["dream_id"]
    job = completed(client, did)
    assert job["status"] == "completed"  # Dream content is not diagnosed as real intent.
    assert job["result"]["evidence_basis"] == "template_not_ai"


def test_rate_limit_and_delete(client):
    guest(client)
    ids = [submit(client).json()["dream_id"] for _ in range(10)]
    assert submit(client).status_code == 429
    assert client.delete(f"/api/v1/dreams/{ids[0]}").status_code == 204
    assert client.get(f"/api/v1/dreams/{ids[0]}/free").status_code == 404
    with client.app.state.store.db() as db:
        assert db.execute("SELECT count(*) FROM events WHERE dream_id=?", (ids[0],)).fetchone()[0] == 0


def test_disabled_mode_does_not_silently_fake_ai(tmp_path):
    with TestClient(create_app(str(tmp_path / "disabled.sqlite3"), mode="disabled")) as c:
        guest(c)
        assert submit(c).status_code == 503


class FailsOnce(DemoProvider):
    calls = 0
    async def free(self, body, symbols):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("private provider error with dream text")
        return await super().free(body, symbols)


def test_failure_redaction_and_retry(tmp_path):
    with TestClient(create_app(str(tmp_path / "retry.sqlite3"), FailsOnce(), mode="demo")) as c:
        guest(c)
        did = submit(c).json()["dream_id"]
        job = completed(c, did)
        assert job["error"] == "processing_failed" and job["can_retry"]
        assert "private provider" not in json.dumps(job)
        assert c.post(f"/api/v1/dreams/{did}/free").status_code == 202
        assert completed(c, did)["status"] == "completed"


def test_restart_recovery_and_persistence(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    with TestClient(create_app(path, mode="demo")) as c:
        guest(c)
        cookie = c.cookies.get("sonnic_guest")
        did = submit(c).json()["dream_id"]
        completed(c, did)
    with TestClient(create_app(path, mode="demo")) as c:
        c.cookies.set("sonnic_guest", cookie)
        assert c.get(f"/api/v1/dreams/{did}/free").json()["status"] == "completed"
    store = Store(path)
    with store.db() as db:
        db.execute("UPDATE dreams SET status='processing', result=NULL WHERE id=?", (did,))
    store.recover()
    with TestClient(create_app(path, mode="demo")) as c:
        c.cookies.set("sonnic_guest", cookie)
        assert c.get(f"/api/v1/dreams/{did}/free").json()["error"] == "interrupted"


class AlwaysFails(DemoProvider):
    async def free(self, body, symbols):
        raise TimeoutError()


def test_timeout_and_retry_limit(tmp_path):
    with TestClient(create_app(str(tmp_path / "timeout.sqlite3"), AlwaysFails(), mode="demo")) as c:
        guest(c)
        did = submit(c).json()["dream_id"]
        for attempt in range(3):
            job = completed(c, did)
            assert job["error"] == "timeout"
            assert job["can_retry"] is (attempt < 2)
            response = c.post(f"/api/v1/dreams/{did}/free")
            assert response.status_code == (202 if attempt < 2 else 409)
