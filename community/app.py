"""社区只读目录与作者/审核写接口分离；发布不可变，撤回保留审计。"""

from contextlib import contextmanager
import base64
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .package import Release, inspect, verify
from .catalog_query import configure_connection, ensure_indexes, page, public
from .operations import readiness
from .web import mount_web


class CatalogError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: Release
    archive_base64: str = Field(max_length=14 * 1024 * 1024)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    approve: bool
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip(): raise ValueError('REASON_REQUIRED')
        return value


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip(): raise ValueError('REASON_REQUIRED')
        return value


class ReportResolution(Reason):
    decision: Literal['addressed', 'dismissed']


class Registry:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
            """)
            if conn.execute("SELECT version FROM schema_version").fetchone()[0] != 1:
                raise RuntimeError("社区数据库版本不兼容")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS principals (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, role TEXT NOT NULL, namespace TEXT UNIQUE, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS keys (id TEXT PRIMARY KEY, namespace TEXT NOT NULL, public_key BLOB NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, namespace TEXT NOT NULL, package_id TEXT NOT NULL, version TEXT NOT NULL, metadata TEXT NOT NULL, blob BLOB NOT NULL, author_id TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(namespace,package_id,version));
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, reason TEXT NOT NULL, timestamp INTEGER NOT NULL);
            """)
            ensure_indexes(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS moderation_pending ON submissions(id) WHERE state='pending'")
            conn.execute('CREATE INDEX IF NOT EXISTS audit_action_subject ON audit(action,subject,id)')

    @contextmanager
    def connect(self, *, write=True):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        configure_connection(conn)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_principal(self, principal_id: str, role: str, namespace: str | None = None):
        if role not in {"author", "moderator"}: raise ValueError("角色无效")
        token = secrets.token_urlsafe(48)
        with self.connect() as conn:
            conn.execute("INSERT INTO principals VALUES (?,?,?,?,0)", (principal_id, hashlib.sha256(token.encode()).hexdigest(), role, namespace))
        return token

    def add_key(self, key_id: str, namespace: str, public_key: bytes):
        if len(public_key) != 32: raise ValueError("Ed25519 公钥须为 32 字节")
        with self.connect() as conn:
            conn.execute("INSERT INTO keys VALUES (?,?,?,0)", (key_id, namespace, public_key))


def create_app(registry: Registry, source_id="self-hosted", allowed_origins=()):
    app = FastAPI(title="NotesAgent Community", version="1.0.0")
    mount_web(app)
    app.add_middleware(CORSMiddleware, allow_origins=list(allowed_origins), allow_methods=["GET"], allow_headers=["If-None-Match"], expose_headers=["ETag"])

    @app.exception_handler(CatalogError)
    async def error(_request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse({"error": {"code": "INVALID_REQUEST"}}, status_code=422)

    @app.middleware("http")
    async def limit_body(request, call_next):
        # 限制分块请求及 Content-Length，不能只在 Pydantic 解码后检查大包。
        if request.method in {"POST", "PUT", "PATCH"}:
            total = 0
            data = bytearray()
            async for chunk in request.stream():
                total += len(chunk)
                if total > 15 * 1024 * 1024:
                    return JSONResponse({"error": {"code": "PACKAGE_TOO_LARGE"}}, status_code=413)
                data.extend(chunk)
            request._body = bytes(data)
        return await call_next(request)

    def principal(conn, authorization, role=None):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        actor = conn.execute("SELECT * FROM principals WHERE token_hash=? AND revoked=0", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not actor: raise CatalogError(401, "AUTH_REQUIRED")
        if role and actor["role"] != role: raise CatalogError(403, "ROLE_REQUIRED")
        return actor

    def audit(conn, actor, action, subject, reason=""):
        return conn.execute("INSERT INTO audit(actor,action,subject,reason,timestamp) VALUES (?,?,?,?,?)", (actor, action, subject, reason, int(time.time()))).lastrowid

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.get('/ready')
    def ready():
        status = readiness(registry.path)
        return JSONResponse(status or {'status': 'unavailable', 'error': {'code': 'DATABASE_NOT_READY'}}, status_code=200 if status else 503, headers={'Cache-Control': 'no-store'})

    @app.get("/catalog/v1/sources")
    def source():
        with registry.connect(write=False) as conn:
            keys = [{"key_id": x["id"], "namespace": x["namespace"], "public_key": base64.b64encode(x["public_key"]).decode(), "revoked": bool(x["revoked"])} for x in conn.execute("SELECT * FROM keys")]
        return JSONResponse({"schema_version": 1, "source_id": source_id, "keys": keys}, headers={'Cache-Control': 'no-store'})

    @app.get("/catalog/v1/packages")
    def packages(q: str = Query(default="", max_length=120), type: Literal["theme", "skill", "plugin", "mcp", "persona", "template", "model"] | None = None,
                 offset: int = Query(default=0, ge=0, le=2147483647), limit: int = Query(default=30, ge=1, le=100),
                 if_none_match: str | None = Header(default=None)):
        with registry.connect(write=False) as conn:
            result = page(conn, query=q, kind=type, offset=offset, limit=limit)
        etag = '"' + hashlib.sha256(json.dumps([source_id,q,type,result], sort_keys=True, ensure_ascii=False).encode()).hexdigest() + '"'
        if etag == if_none_match: return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(result, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})

    @app.get("/catalog/v1/packages/{namespace}/{package_id}/releases")
    def versions(namespace: str, package_id: str, offset: int = Query(default=0, ge=0, le=2147483647), limit: int = Query(default=100, ge=1, le=100),
                 version: str | None = Query(default=None, min_length=1, max_length=120)):
        with registry.connect(write=False) as conn:
            result = page(conn, namespace=namespace, package_id=package_id, version=version, offset=offset, limit=limit)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/catalog/v1/releases/{release_id}")
    def release_metadata(release_id: str):
        with registry.connect(write=False) as conn:
            item = conn.execute(f"SELECT id,metadata,state FROM submissions WHERE id=? AND state IN ('published','withdrawn')", (release_id,)).fetchone()
            if not item: raise CatalogError(404, "RELEASE_NOT_FOUND")
            return JSONResponse(public(item), headers={"Cache-Control": "no-store"})

    @app.get("/catalog/v1/releases/{release_id}/archive")
    def archive(release_id: str):
        with registry.connect() as conn:
            item = conn.execute("SELECT * FROM submissions WHERE id=?", (release_id,)).fetchone()
            if not item or item["state"] not in {"published", "withdrawn"}: raise CatalogError(404, "RELEASE_NOT_FOUND")
            try: release = Release.model_validate_json(item["metadata"])
            except ValueError: raise CatalogError(422, "INVALID_RELEASE_METADATA") from None
            key = conn.execute("SELECT * FROM keys WHERE id=?", (release.key_id,)).fetchone()
            if item["state"] == "withdrawn" or not key or key["revoked"]: raise CatalogError(410, "RELEASE_WITHDRAWN")
            return Response(item["blob"], media_type="application/zip", headers={"Cache-Control": "no-store"})

    @app.post("/catalog/v1/publish/submissions")
    def submit(body: Submission, authorization: str = Header(default="")):
        with registry.connect() as conn:
            actor = principal(conn, authorization, "author")
            release = body.release
            if actor["namespace"] != release.namespace or actor["id"] != release.author_id: raise CatalogError(403, "NAMESPACE_OWNERSHIP")
            key = conn.execute("SELECT * FROM keys WHERE id=? AND namespace=? AND revoked=0", (release.key_id, release.namespace)).fetchone()
            if not key: raise CatalogError(403, "UNTRUSTED_SIGNER")
            try:
                verify(release, key["public_key"])
                blob = base64.b64decode(body.archive_base64, validate=True)
                inspect(release, blob)
            except Exception:
                raise CatalogError(422, "PACKAGE_VALIDATION_FAILED") from None
            if conn.execute("SELECT 1 FROM submissions WHERE namespace=? AND package_id=? AND version=?", (release.namespace, release.package_id, release.version)).fetchone():
                raise CatalogError(409, "IMMUTABLE_VERSION")
            submission_id = secrets.token_hex(16)
            conn.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,'pending')", (submission_id, release.namespace, release.package_id, release.version, release.model_dump_json(), blob, actor["id"]))
            audit(conn, actor["id"], "submit", submission_id)
            return {"submission_id": submission_id, "state": "pending"}

    @app.get("/catalog/v1/moderation/reviews")
    def pending(offset: int = Query(default=0, ge=0, le=2147483647), limit: int = Query(default=30, ge=1, le=100), authorization: str = Header(default="")):
        with registry.connect(write=False) as conn:
            principal(conn, authorization, "moderator")
            total = conn.execute("SELECT COUNT(*) FROM submissions WHERE state='pending'").fetchone()[0]
            rows = conn.execute("SELECT id,metadata FROM submissions WHERE state='pending' ORDER BY id LIMIT ? OFFSET ?", (limit, offset))
            return {'schema_version': 1, 'total': total, 'offset': offset, 'limit': limit, 'items': [{"submission_id": x["id"], "release": json.loads(x["metadata"])} for x in rows]}

    @app.get('/catalog/v1/publish/submissions/{submission_id}')
    def submission_status(submission_id: str, authorization: str = Header(default='')):
        with registry.connect(write=False) as conn:
            actor = principal(conn, authorization)
            item = conn.execute('SELECT id,metadata,author_id,state FROM submissions WHERE id=?', (submission_id,)).fetchone()
            if not item: raise CatalogError(404, 'SUBMISSION_NOT_FOUND')
            if actor['role'] != 'moderator' and actor['id'] != item['author_id']: raise CatalogError(403, 'OWNERSHIP_REQUIRED')
            return {'schema_version': 1, 'submission_id': item['id'], 'state': item['state'], 'release': json.loads(item['metadata'])}

    @app.get('/catalog/v1/publish/submissions')
    def own_submissions(package_id: str | None = Query(default=None, max_length=64), version: str | None = Query(default=None, max_length=120), offset: int = Query(default=0, ge=0, le=2147483647), limit: int = Query(default=30, ge=1, le=100), authorization: str = Header(default='')):
        with registry.connect(write=False) as conn:
            actor = principal(conn, authorization)
            conditions, values = [], []
            if actor['role'] != 'moderator': conditions.append('author_id=?'); values.append(actor['id'])
            if package_id is not None: conditions.append('package_id=?'); values.append(package_id)
            if version is not None: conditions.append('version=?'); values.append(version)
            where = ' AND '.join(conditions) or '1'
            total = conn.execute(f'SELECT COUNT(*) FROM submissions WHERE {where}', values).fetchone()[0]
            rows = conn.execute(f'SELECT id,metadata,state FROM submissions WHERE {where} ORDER BY id LIMIT ? OFFSET ?', [*values, limit, offset])
            return {'schema_version': 1, 'total': total, 'offset': offset, 'limit': limit, 'items': [{'submission_id': row['id'], 'state': row['state'], 'release': json.loads(row['metadata'])} for row in rows]}

    @app.get('/catalog/v1/moderation/audit')
    def audit_page(after: int = Query(default=0, ge=0, le=9223372036854775807), limit: int = Query(default=30, ge=1, le=100), authorization: str = Header(default='')):
        with registry.connect(write=False) as conn:
            principal(conn, authorization, 'moderator')
            rows = conn.execute('SELECT id,actor,action,subject,reason,timestamp FROM audit WHERE id>? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
            return {'schema_version': 1, 'items': [dict(row) for row in rows[:limit]], 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    @app.get('/catalog/v1/moderation/reports')
    def report_queue(after: int = Query(default=0, ge=0, le=9223372036854775807), limit: int = Query(default=30, ge=1, le=100), authorization: str = Header(default='')):
        with registry.connect(write=False) as conn:
            principal(conn, authorization, 'moderator')
            rows = conn.execute("SELECT id,actor,subject AS release_id,reason,timestamp FROM audit AS report WHERE action='report' AND id>? AND NOT EXISTS (SELECT 1 FROM audit AS resolution WHERE resolution.action='resolve-report' AND resolution.subject=CAST(report.id AS TEXT)) ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
            return {'schema_version': 1, 'items': [dict(row) for row in rows[:limit]], 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    @app.post('/catalog/v1/moderation/reports/{report_id}/resolve')
    def resolve_report(report_id: int, body: ReportResolution, authorization: str = Header(default='')):
        with registry.connect() as conn:
            actor = principal(conn, authorization, 'moderator')
            if not conn.execute("SELECT 1 FROM audit WHERE id=? AND action='report'", (report_id,)).fetchone(): raise CatalogError(404, 'REPORT_NOT_FOUND')
            if conn.execute("SELECT 1 FROM audit WHERE action='resolve-report' AND subject=?", (str(report_id),)).fetchone(): raise CatalogError(409, 'REPORT_ALREADY_RESOLVED')
            audit(conn, actor['id'], 'resolve-report', str(report_id), body.model_dump_json())
            return {'state': 'resolved', 'report_id': report_id, 'decision': body.decision}

    @app.post("/catalog/v1/moderation/reviews")
    def review(body: Review, authorization: str = Header(default="")):
        with registry.connect() as conn:
            actor = principal(conn, authorization, "moderator")
            item = conn.execute("SELECT * FROM submissions WHERE id=?", (body.submission_id,)).fetchone()
            if not item: raise CatalogError(404, "SUBMISSION_NOT_FOUND")
            if item["author_id"] == actor["id"]: raise CatalogError(403, "SELF_REVIEW_FORBIDDEN")
            if item["state"] != "pending": raise CatalogError(409, "REVIEW_ALREADY_CLOSED")
            try: release = Release.model_validate_json(item["metadata"])
            except ValueError: raise CatalogError(422, "INVALID_RELEASE_METADATA") from None
            key = conn.execute("SELECT * FROM keys WHERE id=? AND revoked=0", (release.key_id,)).fetchone()
            if body.approve and not key: raise CatalogError(403, "UNTRUSTED_SIGNER")
            state = "published" if body.approve else "rejected"
            conn.execute("UPDATE submissions SET state=? WHERE id=?", (state, body.submission_id))
            audit(conn, actor["id"], state, body.submission_id, body.reason)
            return {"state": state}

    @app.post("/catalog/v1/releases/{release_id}/withdraw")
    def withdraw(release_id: str, body: Reason, authorization: str = Header(default="")):
        with registry.connect() as conn:
            actor = principal(conn, authorization)
            item = conn.execute("SELECT * FROM submissions WHERE id=?", (release_id,)).fetchone()
            if not item: raise CatalogError(404, "RELEASE_NOT_FOUND")
            if actor["role"] != "moderator" and actor["id"] != item["author_id"]: raise CatalogError(403, "OWNERSHIP_REQUIRED")
            if item["state"] not in {"published", "withdrawn"}: raise CatalogError(409, "RELEASE_NOT_PUBLISHED")
            conn.execute("UPDATE submissions SET state='withdrawn' WHERE id=?", (release_id,))
            audit(conn, actor["id"], "withdraw", release_id, body.reason)
            return {"state": "withdrawn"}

    @app.post("/catalog/v1/releases/{release_id}/reports")
    def report(release_id: str, body: Reason, authorization: str = Header(default="")):
        with registry.connect() as conn:
            actor = principal(conn, authorization)
            if not conn.execute("SELECT 1 FROM submissions WHERE id=?", (release_id,)).fetchone(): raise CatalogError(404, "RELEASE_NOT_FOUND")
            report_id = audit(conn, actor["id"], "report", release_id, body.reason)
            return {"state": "reported", 'report_id': report_id}

    @app.post("/catalog/v1/keys/{key_id}/revoke")
    def revoke(key_id: str, body: Reason, authorization: str = Header(default="")):
        with registry.connect() as conn:
            actor = principal(conn, authorization, "moderator")
            if not conn.execute('SELECT 1 FROM keys WHERE id=?', (key_id,)).fetchone(): raise CatalogError(404, 'KEY_NOT_FOUND')
            conn.execute("UPDATE keys SET revoked=1 WHERE id=?", (key_id,))
            audit(conn, actor["id"], "revoke-key", key_id, body.reason)
            return {"state": "revoked"}

    return app
