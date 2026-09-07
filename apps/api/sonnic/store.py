"""Local repository. A connection per operation; no raw dream data in log output."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import DreamInput, GuestInput


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dreams (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    input TEXT NOT NULL, request_key TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
                    result TEXT, error TEXT, telemetry TEXT, attempt INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(session_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, dream_id TEXT,
                    name TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(dream_id, name)
                );
                CREATE INDEX IF NOT EXISTS dream_owner ON dreams(session_id, created);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def guest(self, body: GuestInput):
        token, sid, now = secrets.token_urlsafe(32), str(uuid4()), time.time()
        with self.db() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (sid, hashlib.sha256(token.encode()).hexdigest(), now, now + 86400, body.model_dump_json()))
            db.execute("INSERT INTO events(session_id,name,created) VALUES (?, 'guest_created', ?)", (sid, now))
        return sid, token

    def session(self, token: str | None):
        if not token:
            return None
        with self.db() as db:
            row = db.execute("SELECT id FROM sessions WHERE token_hash=? AND expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return row["id"] if row else None

    def create(self, sid: str, body: DreamInput, key: str):
        serialized = body.model_dump_json()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,input,status FROM dreams WHERE session_id=? AND request_key=?", (sid, key)).fetchone()
            if existing:
                if existing["input"] != serialized:
                    raise ValueError("idempotency_conflict")
                return existing["id"], False
            count = db.execute("SELECT count(*) FROM dreams WHERE session_id=? AND created>?", (sid, time.time()-3600)).fetchone()[0]
            if count >= 10:
                raise ValueError("rate_limited")
            did = str(uuid4())
            db.execute("INSERT INTO dreams(id,session_id,input,request_key,created) VALUES (?,?,?,?,?)",
                       (did, sid, serialized, key, time.time()))
            db.execute("INSERT INTO events(session_id,dream_id,name,created) VALUES (?,?,'dream_submitted',?)", (sid,did,time.time()))
        return did, True

    def get(self, did: str, sid: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM dreams WHERE id=? AND session_id=?", (did,sid)).fetchone()
        return dict(row) if row else None

    def stage(self, did: str, stage: str):
        with self.db() as db:
            db.execute("UPDATE dreams SET status='processing',stage=? WHERE id=?", (stage,did))

    def finish(self, did: str, result: str, telemetry: dict):
        with self.db() as db:
            db.execute("UPDATE dreams SET status='completed',stage='completed',result=?,error=NULL,telemetry=? WHERE id=?", (result,json.dumps(telemetry),did))
            db.execute("INSERT OR IGNORE INTO events(session_id,dream_id,name,created) SELECT session_id,id,'free_completed',? FROM dreams WHERE id=?", (time.time(),did))

    def fail(self, did: str, code: str):
        with self.db() as db:
            db.execute("UPDATE dreams SET status='failed',stage='failed',error=? WHERE id=?", (code,did))

    def telemetry(self, did: str, data: dict):
        with self.db() as db:
            db.execute("UPDATE dreams SET telemetry=? WHERE id=?", (json.dumps(data), did))

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE dreams SET status='failed',stage='failed',error='interrupted' WHERE status IN ('queued','processing')")

    def retry(self, did: str, sid: str):
        with self.db() as db:
            cursor = db.execute("UPDATE dreams SET status='queued',stage='queued',error=NULL,attempt=attempt+1 WHERE id=? AND session_id=? AND status='failed' AND attempt<3", (did,sid))
            return cursor.rowcount == 1

    def viewed(self, did: str, sid: str):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO events(session_id,dream_id,name,created) VALUES (?,?,'free_viewed',?)", (sid,did,time.time()))

    def delete(self, did: str, sid: str):
        with self.db() as db:
            db.execute("DELETE FROM events WHERE dream_id=? AND session_id=?", (did,sid))
            db.execute("DELETE FROM dreams WHERE id=? AND session_id=?", (did,sid))

    def ready(self):
        with self.db() as db:
            db.execute("SELECT 1").fetchone()
