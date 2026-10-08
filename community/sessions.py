"""Short-lived, same-origin browser credentials; legacy bearer clients stay separate."""
from collections import deque
import hashlib
import hmac
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

COOKIE = '__Host-opennexus-community-session'
LOCAL_COOKIE = 'opennexus-community-session'
SESSION_COLUMNS = ('id', 'token_hash', 'principal_id', 'issued_at', 'expires_at', 'csrf_nonce')
SESSION_SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_sessions (
    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, principal_id TEXT NOT NULL,
    issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, csrf_nonce TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS web_sessions_actor_expiry ON web_sessions(principal_id, expires_at);
CREATE INDEX IF NOT EXISTS web_sessions_expiry ON web_sessions(expires_at);
'''


class SessionError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=32, max_length=256, pattern=r'^[A-Za-z0-9_-]+$')


class BrowserSessions:
    def __init__(self, registry, *, origin=None, ttl=7200, allow_insecure_loopback=False):
        if type(ttl) is not int or not 300 <= ttl <= 86400:
            raise ValueError('WEB_SESSION_TTL_INVALID')
        self.registry, self.ttl, self.origin = registry, ttl, None
        self.cookie, self.secure = COOKIE, True
        if origin is not None:
            parts = urlsplit(origin)
            try:
                port = parts.port
            except ValueError:
                raise ValueError('WEB_ORIGIN_INVALID') from None
            if not parts.hostname or parts.username or parts.password or parts.path not in {'', '/'} or parts.query or parts.fragment:
                raise ValueError('WEB_ORIGIN_INVALID')
            if parts.scheme != 'https':
                if not (allow_insecure_loopback and parts.scheme == 'http' and parts.hostname in {'localhost', '127.0.0.1', '::1'}):
                    raise ValueError('WEB_ORIGIN_HTTPS_REQUIRED')
                self.cookie, self.secure = LOCAL_COOKIE, False
            hostname = parts.hostname.lower()
            hostname = f'[{hostname}]' if ':' in hostname else hostname
            default_port = 443 if parts.scheme == 'https' else 80
            self.authority = hostname + (f':{port}' if port is not None and port != default_port else '')
            self.origin = f'{parts.scheme}://{self.authority}'
        self.attempts, self.lock = {}, threading.Lock()

    def origin_guard(self, request: Request, *, write=False):
        if not self.origin:
            raise SessionError(503, 'WEB_ORIGIN_REQUIRED')
        # The configured public origin is authoritative, including behind TLS
        # termination. Do not derive trust from arbitrary forwarding headers.
        try:
            authority = urlsplit(self.origin.split('://')[0] + '://' + request.headers.get('host', '')).netloc.lower()
        except ValueError:
            raise SessionError(403, 'WEB_ORIGIN_MISMATCH') from None
        default_suffix = ':443' if self.secure else ':80'
        if authority.endswith(default_suffix): authority = authority[:-len(default_suffix)]
        if authority != self.authority:
            raise SessionError(403, 'WEB_ORIGIN_MISMATCH')
        origins = request.headers.getlist('origin')
        if (write and origins != [self.origin]) or (origins and origins != [self.origin]):
            raise SessionError(403, 'WEB_ORIGIN_MISMATCH')
        site = request.headers.get('sec-fetch-site', '')
        if site and site not in ({'same-origin'} if write else {'same-origin', 'none'}):
            raise SessionError(403, 'WEB_ORIGIN_MISMATCH')

    def throttle(self, request):
        now = time.monotonic()
        address = request.client.host if request.client else 'unknown'
        with self.lock:
            for key in list(self.attempts):
                queue = self.attempts[key]
                while queue and queue[0] <= now - 60: queue.popleft()
                if not queue: del self.attempts[key]
            if address not in self.attempts and len(self.attempts) >= 1024:
                raise SessionError(429, 'LOGIN_RATE_LIMIT')
            queue = self.attempts.setdefault(address, deque())
            if len(queue) >= 10: raise SessionError(429, 'LOGIN_RATE_LIMIT')
            queue.append(now)

    def credential(self, request):
        names = [part.split('=', 1)[0].strip() for part in request.headers.get('cookie', '').split(';') if '=' in part]
        if names.count(self.cookie) > 1:
            raise SessionError(401, 'AUTH_AMBIGUOUS')
        return request.cookies.get(self.cookie, '')

    def session(self, conn, request):
        self.origin_guard(request, write=request.method not in {'GET', 'HEAD', 'OPTIONS'})
        token = self.credential(request)
        if not re.fullmatch(r'[A-Za-z0-9_-]{64}', token): raise SessionError(401, 'AUTH_REQUIRED')
        item = conn.execute('SELECT * FROM web_sessions WHERE token_hash=? AND expires_at>?', (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        if not item: raise SessionError(401, 'AUTH_REQUIRED')
        actor = conn.execute('SELECT * FROM principals WHERE id=? AND revoked=0', (item['principal_id'],)).fetchone()
        if not actor or actor['role'] not in {'author', 'moderator'}: raise SessionError(401, 'AUTH_REQUIRED')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            proof = request.headers.get('x-community-csrf', '')
            if not hmac.compare_digest(proof.encode(), self.csrf(token, item).encode()):
                raise SessionError(403, 'CSRF_REQUIRED')
        return item, actor

    @staticmethod
    def csrf(token, item):
        return hmac.new(token.encode(), ('community-csrf:' + item['csrf_nonce']).encode(), hashlib.sha256).hexdigest()

    def actor(self, conn, request, authorization):
        if len(request.headers.getlist('authorization')) > 1: raise SessionError(401, 'AUTH_AMBIGUOUS')
        has_cookie = self.cookie in request.cookies
        if authorization and has_cookie: raise SessionError(401, 'AUTH_AMBIGUOUS')
        if has_cookie: return self.session(conn, request)[1]
        token = authorization.removeprefix('Bearer ') if authorization.startswith('Bearer ') else ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token): raise SessionError(401, 'AUTH_REQUIRED')
        actor = conn.execute('SELECT * FROM principals WHERE token_hash=? AND revoked=0', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not actor or actor['role'] not in {'author', 'moderator'}: raise SessionError(401, 'AUTH_REQUIRED')
        return actor

    def describe(self, token, item, actor):
        return {'schema_version': 1, 'session_id': item['id'], 'principal_id': actor['id'], 'role': actor['role'], 'namespace': actor['namespace'], 'expires_at': item['expires_at'], 'csrf': self.csrf(token, item)}

    def mount(self, app):
        @app.post('/catalog/v1/web/session')
        def login(request: Request, body: Login):
            self.origin_guard(request, write=True)
            if request.headers.get('authorization'): raise SessionError(401, 'AUTH_AMBIGUOUS')
            self.throttle(request)
            now = int(time.time())
            with self.registry.connect() as conn:
                actor = conn.execute('SELECT * FROM principals WHERE token_hash=? AND revoked=0', (hashlib.sha256(body.token.encode()).hexdigest(),)).fetchone()
                if not actor or actor['role'] not in {'author', 'moderator'}: raise SessionError(401, 'AUTH_REQUIRED')
                existing = self.credential(request)
                if existing and conn.execute('SELECT 1 FROM web_sessions WHERE token_hash=? AND expires_at>?', (hashlib.sha256(existing.encode()).hexdigest(), now)).fetchone():
                    raise SessionError(409, 'SESSION_ALREADY_ACTIVE')
                conn.execute('DELETE FROM web_sessions WHERE expires_at<=?', (now,))
                if conn.execute('SELECT COUNT(*) FROM web_sessions WHERE principal_id=?', (actor['id'],)).fetchone()[0] >= 8:
                    raise SessionError(409, 'SESSION_LIMIT')
                token = secrets.token_urlsafe(48)
                item = {'id': secrets.token_hex(16), 'principal_id': actor['id'], 'issued_at': now, 'expires_at': now + self.ttl, 'csrf_nonce': secrets.token_hex(16)}
                conn.execute('INSERT INTO web_sessions VALUES (?,?,?,?,?,?)', (item['id'], hashlib.sha256(token.encode()).hexdigest(), item['principal_id'], now, item['expires_at'], item['csrf_nonce']))
                response = JSONResponse(self.describe(token, item, actor))
                response.set_cookie(self.cookie, token, max_age=self.ttl, secure=self.secure, httponly=True, samesite='strict', path='/')
                return response

        @app.get('/catalog/v1/web/session')
        def current(request: Request):
            if request.headers.get('authorization'): raise SessionError(401, 'AUTH_AMBIGUOUS')
            with self.registry.connect(write=False) as conn:
                item, actor = self.session(conn, request)
                return self.describe(self.credential(request), item, actor)

        @app.get('/catalog/v1/web/sessions')
        def sessions(request: Request):
            if request.headers.get('authorization'): raise SessionError(401, 'AUTH_AMBIGUOUS')
            with self.registry.connect(write=False) as conn:
                current, actor = self.session(conn, request)
                rows = conn.execute('SELECT id,issued_at,expires_at FROM web_sessions WHERE principal_id=? AND expires_at>? ORDER BY issued_at,id', (actor['id'], int(time.time())))
                return {'schema_version': 1, 'items': [{**dict(row), 'current': row['id'] == current['id']} for row in rows]}

        @app.post('/catalog/v1/web/sessions/{session_id}/revoke')
        def revoke(session_id: str, request: Request):
            if request.headers.get('authorization'): raise SessionError(401, 'AUTH_AMBIGUOUS')
            if not re.fullmatch('[a-f0-9]{32}', session_id): raise SessionError(404, 'SESSION_NOT_FOUND')
            with self.registry.connect() as conn:
                item, actor = self.session(conn, request)
                # Repeating one's original revocation is safe. Missing or foreign
                # identifiers disclose nothing and never remove another actor.
                conn.execute('DELETE FROM web_sessions WHERE id=? AND principal_id=?', (session_id, actor['id']))
                response = JSONResponse({'schema_version': 1, 'state': 'revoked', 'session_id': session_id})
                if item['id'] == session_id:
                    response.delete_cookie(self.cookie, path='/', secure=self.secure, httponly=True, samesite='strict')
                return response

        @app.post('/catalog/v1/web/session/logout')
        def logout(request: Request):
            if request.headers.get('authorization'): raise SessionError(401, 'AUTH_AMBIGUOUS')
            with self.registry.connect() as conn:
                item, _actor = self.session(conn, request)
                conn.execute('DELETE FROM web_sessions WHERE id=?', (item['id'],))
            response = JSONResponse({'schema_version': 1, 'state': 'logged_out'})
            response.delete_cookie(self.cookie, path='/', secure=self.secure, httponly=True, samesite='strict')
            return response
