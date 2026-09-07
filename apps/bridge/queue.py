"""Persistent, owner-scoped bridge queue; no HTTP, MCP or model calls.

Principal comes from the authenticated HTTP adapter or trusted local tests,
NEVER from an owner/scopes field supplied by a model.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

from apps.api.sonnic.models import DreamInput
from .contracts import JobInput, JobOutput

ROOT = Path(__file__).resolve().parents[2]


class BridgeError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    owner: str
    scopes: frozenset[str]


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Queue:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self.cases = {c["id"]: c for c in json.loads((ROOT / "tests/quality/dreams.json").read_text(encoding="utf-8"))}
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                case_id TEXT NOT NULL, input TEXT NOT NULL, input_hash TEXT NOT NULL,
                data_class TEXT NOT NULL DEFAULT 'synthetic',
                state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, deadline REAL NOT NULL, lease_until REAL,
                lease_id TEXT, claim_key TEXT, worker TEXT, output TEXT, output_hash TEXT,
                reviewer TEXT, error TEXT, UNIQUE(owner, request_key))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "data_class" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN data_class TEXT NOT NULL DEFAULT 'synthetic'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def require(self, actor, scope):
        if not actor.subject or not actor.owner or scope not in actor.scopes:
            raise BridgeError("forbidden")

    def owned(self, db, actor, job_id):
        row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (str(job_id), actor.owner)).fetchone()
        if row is None:
            raise BridgeError("job_not_found")
        return row

    def expire(self, db):
        # Run in the same transaction as the action: no stale pre-lock check.
        now = self.clock()
        db.execute("""UPDATE jobs SET state='expired', error='deadline'
            WHERE state IN ('queued','leased','awaiting_review')
            AND (deadline<=? OR (state='leased' AND lease_until<=?))""", (now, now))

    def enqueue(self, actor: Principal, case_id: str, request_key: UUID) -> str:
        self.require(actor, "site")
        if case_id not in self.cases:
            raise BridgeError("synthetic_case_required")
        case = self.cases[case_id]
        body = DreamInput(dream_text=case["dream_text"], locale=case["locale"])
        return self._enqueue(actor, case_id, body, "synthetic", request_key)

    def enqueue_dream(self, actor: Principal, body: DreamInput, request_key: UUID) -> str:
        """Queue owner-provided text for the private beta bridge."""
        self.require(actor, "site")
        return self._enqueue(actor, "personal-input", body, "personal", request_key)

    def _enqueue(self, actor: Principal, case_id: str, body: DreamInput, data_class: str,
                 request_key: UUID) -> str:
        raw = serialized(body.model_dump(mode="json"))
        with self.db() as db:
            self.expire(db)
            old = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?", (actor.owner, str(request_key))).fetchone()
            if old:
                if old["input_hash"] != digest(raw):
                    raise BridgeError("idempotency_conflict")
                return old["id"]
            count = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','leased','awaiting_review')").fetchone()[0]
            if count >= 20:
                raise BridgeError("queue_full")
            job_id = str(uuid4())
            db.execute("INSERT INTO jobs(id,owner,request_key,case_id,input,input_hash,data_class,state,created,deadline) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                       (job_id, actor.owner, str(request_key), case_id, raw, digest(raw), data_class,
                        self.clock(), self.clock()+3600))
            return job_id

    def claim(self, actor: Principal, job_id: UUID, claim_key: UUID) -> JobInput:
        self.require(actor, "worker")
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, job_id)
            repeated = row["state"] == "leased" and row["claim_key"] == str(claim_key) and row["worker"] == actor.subject
            if not repeated:
                if row["state"] != "queued":
                    raise BridgeError("job_not_queued")
                if db.execute("SELECT count(*) FROM jobs WHERE state='leased'").fetchone()[0]:
                    raise BridgeError("worker_busy")
                if row["attempt"] >= 3:
                    raise BridgeError("retry_exhausted")
                db.execute("UPDATE jobs SET state='leased',attempt=attempt+1,lease_until=?,lease_id=?,claim_key=?,worker=? WHERE id=?",
                           (min(self.clock()+600, row["deadline"]), str(uuid4()), str(claim_key), actor.subject, str(job_id)))
                row = self.owned(db, actor, job_id)
            body = DreamInput.model_validate_json(row["input"])
            return JobInput(job_id=UUID(row["id"]), lease_id=UUID(row["lease_id"]), attempt=row["attempt"],
                            input_sha256=row["input_hash"], data_class=row["data_class"],
                            output_locale=body.locale, dream=body)

    def submit(self, actor: Principal, result: JobOutput) -> str:
        self.require(actor, "worker")
        # Validate even when an internal caller constructed an unchecked model.
        result = JobOutput.model_validate_json(result.model_dump_json())
        raw = serialized(result.model_dump(mode="json"))
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, result.job_id)
            if (row["worker"] != actor.subject or row["lease_id"] != str(result.lease_id)
                    or row["attempt"] != result.attempt or row["input_hash"] != result.input_sha256):
                raise BridgeError("stale_or_mismatched_result")
            if row["state"] in {"awaiting_review", "completed"}:
                if row["output_hash"] == digest(raw):
                    return row["state"]
                raise BridgeError("result_conflict")
            if row["state"] != "leased":
                raise BridgeError("job_not_leased")
            body = DreamInput.model_validate_json(row["input"])
            if result.output_locale != body.locale:
                raise BridgeError("locale_mismatch")
            if result.reflection and any(s.evidence not in body.dream_text for s in result.reflection.symbols):
                raise BridgeError("ungrounded_evidence")
            db.execute("UPDATE jobs SET state='awaiting_review',output=?,output_hash=? WHERE id=?", (raw, digest(raw), str(result.job_id)))
            return "awaiting_review"

    def review(self, actor: Principal, job_id: UUID, approve: bool, expected_hash: str | None = None):
        self.require(actor, "review")
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, job_id)
            if row["state"] != "awaiting_review":
                raise BridgeError("not_awaiting_review")
            if expected_hash is not None and row['output_hash'] != expected_hash:
                raise BridgeError('review_changed')
            # A real human review happens outside the model's MCP tool surface.
            db.execute("UPDATE jobs SET state=?,reviewer=?,error=? WHERE id=?",
                       ("completed" if approve else "failed", actor.subject, None if approve else "review_rejected", str(job_id)))

    def retry(self, actor: Principal, job_id: UUID):
        self.require(actor, "site")
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, job_id)
            if row["state"] not in {"expired", "failed"} or row["attempt"] >= 3:
                raise BridgeError("retry_not_allowed")
            db.execute("UPDATE jobs SET state='queued',deadline=?,lease_id=NULL,claim_key=NULL,worker=NULL,output=NULL,output_hash=NULL,reviewer=NULL,error=NULL WHERE id=?",
                       (self.clock()+3600, str(job_id)))

    def cancel(self, actor: Principal, job_id: UUID):
        self.require(actor, "site")
        with self.db() as db:
            self.owned(db, actor, job_id)
            db.execute("UPDATE jobs SET state='cancelled',input='{}',output=NULL,output_hash=NULL,lease_id=NULL WHERE id=?", (str(job_id),))

    def status(self, actor: Principal, job_id: UUID) -> dict:
        self.require(actor, "site")
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, job_id)
            return {"job_id": row["id"], "state": row["state"], "attempt": row["attempt"], "error": row["error"]}

    def result_for_site(self, actor: Principal, job_id: UUID) -> dict:
        self.require(actor, "site")
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, job_id)
            if row["state"] != "completed":
                raise BridgeError("result_not_ready")
            result = JobOutput.model_validate_json(row["output"])
            # Do not leak internal source notes, owner, lease or correlation hashes.
            return {"mode": "bridge_contract_preview", "origin_verified": False, "data_class": row["data_class"],
                    "locale": result.output_locale, "outcome": result.outcome,
                    "reflection": result.reflection.model_dump() if result.reflection else None,
                    "safe_message": result.safe_message}

    def snapshot_for_site(self, actor: Principal, job_id: UUID) -> dict:
        """Owner's review screen; unapproved candidate is explicitly separated."""
        self.require(actor, 'site')
        with self.db() as db:
            self.expire(db)
            row = self.owned(db, actor, job_id)
            output = JobOutput.model_validate_json(row['output']) if row['output'] else None
            public = None
            if output:
                public = {'mode': 'bridge_contract_preview', 'origin_verified': False, 'data_class': row['data_class'],
                          'locale': output.output_locale, 'outcome': output.outcome,
                          'reflection': output.reflection.model_dump() if output.reflection else None,
                          'safe_message': output.safe_message}
            return {'job_id': row['id'], 'state': row['state'], 'attempt': row['attempt'], 'error': row['error'],
                    'data_class': row['data_class'],
                    'case_id': row['case_id'], 'dream_text': json.loads(row['input']).get('dream_text'),
                    'can_retry': row['state'] in {'expired', 'failed'} and row['attempt'] < 3,
                    'candidate': public if row['state'] == 'awaiting_review' else None,
                    'output_sha256': row['output_hash'] if row['state'] == 'awaiting_review' else None,
                    'result': public if row['state'] == 'completed' else None,
                    'automatic_dispatch': False}
