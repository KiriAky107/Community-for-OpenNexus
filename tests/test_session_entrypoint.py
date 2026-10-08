"""The production CLI serves real cookie sessions and restores durable receipts."""
import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx

from community.sessions import LOCAL_COOKIE
from tests.test_catalog import package
from tests.test_cli import invoke

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def served(database, private_values=()):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    env = os.environ.copy()
    env.update(COMMUNITY_DATABASE_PATH=str(database), COMMUNITY_WEB_ORIGIN=origin, COMMUNITY_WEB_SESSION_TTL='7200', PYTHONUTF8='1')
    process = subprocess.Popen([sys.executable, '-m', 'community', 'serve', '--port', str(port), '--allow-insecure-loopback-sessions'], cwd=ROOT, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        with httpx.Client(base_url=origin, trust_env=False, timeout=2) as probe:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None and time.monotonic() < deadline, 'Owned CLI server did not become ready'
                try:
                    if probe.get('/ready').status_code == 200: break
                except httpx.RequestError: pass
                time.sleep(.05)
        yield origin
    finally:
        # Only this exact child is owned; no port or global process-name cleanup.
        if process.poll() is None: process.terminate()
        stdout, stderr = process.communicate(timeout=8)
        assert all(value.encode() not in stdout + stderr for value in private_values), 'Owned server logs must not contain credentials'


def test_actual_cli_two_roles_csrf_expiry_snapshot_and_restored_receipt(tmp_path):
    database = tmp_path / 'catalog.db'
    environment = {'COMMUNITY_DATABASE_PATH': str(database)}
    author_file, moderator_file = tmp_path / 'author.token', tmp_path / 'moderator.token'
    assert invoke(['create-author', '--id', 'author', '--namespace', 'examples', '--token-file', author_file], environment=environment)[0].returncode == 0
    assert invoke(['create-moderator', '--id', 'reviewer', '--token-file', moderator_file], environment=environment)[0].returncode == 0
    author_token, moderator_token = author_file.read_text('ascii'), moderator_file.read_text('ascii')
    key = Ed25519PrivateKey.generate()
    public_file = tmp_path / 'public.txt'
    public_file.write_bytes(base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)))
    assert invoke(['add-key', '--id', 'test-key', '--namespace', 'examples', '--public-key-file', public_file], environment=environment)[0].returncode == 0
    data = package(key)
    private_values = [author_token, moderator_token]
    with served(database, private_values) as origin, httpx.Client(base_url=origin, trust_env=False) as author, httpx.Client(base_url=origin, trust_env=False) as moderator:
        def login(client, token):
            response = client.post('/catalog/v1/web/session', headers={'Origin': origin}, json={'token': token})
            assert response.status_code == 200
            assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
            assert token not in response.text
            private_values.append(response.json()['csrf'])
            return {'Origin': origin, 'X-Community-CSRF': response.json()['csrf']}
        write = login(author, author_token); review_write = login(moderator, moderator_token)
        cookie = author.cookies[LOCAL_COOKIE]
        private_values.append(cookie)
        assert author.get('/catalog/v1/moderation/reviews').status_code == 403
        assert author.post('/catalog/v1/publish/submissions', headers={'Origin': origin}, json=data).status_code == 403
        preview = author.post('/catalog/v1/publish/preflight', headers=write, json=data).json()
        intent = {**data, 'operation_id': 'a' * 32, 'expected_sha256': preview['inspection']['review_digest']}
        result = author.post('/catalog/v1/publish/submissions', headers=write, json=intent).json()
        assert result['state'] == 'pending'
        identifier = result['submission_id']
        status = '/catalog/v1/web/operations/' + intent['operation_id']
        assert author.get(status).json() == result
        assert moderator.get(status).json()['state'] == 'not_found'
        inspection = moderator.get(f'/catalog/v1/publish/submissions/{identifier}/inspection').json()
        decision = {'submission_id': identifier, 'approve': True, 'reason': 'actual independent HTTP review', 'operation_id': 'b' * 32, 'expected_sha256': inspection['inspection']['review_digest']}
        assert moderator.post('/catalog/v1/moderation/reviews', headers=review_write, json=decision).json()['state'] == 'published'
        archive = author.get(f'/catalog/v1/releases/{identifier}/archive').content
        assert hashlib.sha256(archive).hexdigest() == data['release']['sha256']
        backup_dir = tmp_path / 'backup'
        created, manifest = invoke(['backup', '--output-dir', backup_dir], environment=environment, secrets=(author_token, moderator_token, cookie))
        assert created.returncode == 0 and manifest['counts']['submissions'] == 1
        assert author.get('/catalog/v1/web/session').status_code == 200
        with sqlite3.connect(database) as conn: conn.execute("UPDATE web_sessions SET expires_at=0 WHERE principal_id='author'")
        assert author.get('/catalog/v1/web/session').status_code == 401
        assert LOCAL_COOKIE not in author.cookies
    target = tmp_path / 'restored.db'
    assert invoke(['restore', '--input-dir', backup_dir, '--output', target, '--expected-sha256', manifest['sha256']], secrets=(author_token, moderator_token, cookie))[0].returncode == 0
    with served(target, private_values) as origin, httpx.Client(base_url=origin, trust_env=False) as restored:
        restored.cookies.set(LOCAL_COOKIE, cookie)
        assert restored.get('/catalog/v1/web/session').status_code == 401
        restored.cookies.clear()
        token = {'Authorization': 'Bearer ' + author_token}
        assert restored.get(status, headers=token).json() == result
        assert restored.post('/catalog/v1/publish/submissions', headers=token, json=intent).json() == result
        assert restored.get('/catalog/v1/packages').json()['total'] == 1
        assert restored.get(f'/catalog/v1/releases/{identifier}/archive').content == archive
        session = restored.post('/catalog/v1/web/session', headers={'Origin': origin}, json={'token': author_token}).json()
        response = restored.post('/catalog/v1/web/session/logout', headers={'Origin': origin, 'X-Community-CSRF': session['csrf']})
        assert response.status_code == 200 and LOCAL_COOKIE not in restored.cookies
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 0
