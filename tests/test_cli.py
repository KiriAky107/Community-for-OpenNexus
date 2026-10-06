"""The actual CLI process talks to an owned loopback HTTP catalog."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest
import uvicorn

from community.app import Registry, create_app
from tests.test_catalog import package


ROOT = Path(__file__).resolve().parents[1]


def invoke(arguments, *, environment=None, secrets=()):
    env = os.environ.copy()
    env.pop('COMMUNITY_DATABASE_PATH', None)
    env.pop('COMMUNITY_URL', None)
    env['PYTHONUTF8'] = '1'
    if environment: env.update(environment)
    result = subprocess.run([sys.executable, '-m', 'community', *map(str, arguments)], cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=25, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    assert all(token not in result.stdout and token not in result.stderr for token in secrets), 'CLI leaked an owned test credential'
    return result, json.loads(result.stdout) if result.stdout else None


@pytest.fixture
def live(tmp_path):
    registry = Registry(tmp_path / 'catalog.db')
    author = registry.add_principal('author', 'author', 'examples')
    moderator = registry.add_principal('moderator', 'moderator')
    other = registry.add_principal('other', 'author', 'another')
    paths = {}
    for name, token in [('author', author), ('moderator', moderator), ('other', other)]:
        path = tmp_path / (name + '.token'); path.write_text(token, encoding='ascii'); paths[name] = path
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    registry.add_key('test-key', 'examples', public)
    data = package(key, 'template')
    release = tmp_path / '发行 #%.json'; release.write_text(json.dumps(data['release'], ensure_ascii=False), encoding='utf-8')
    archive = tmp_path / 'template.zip'; archive.write_bytes(base64.b64decode(data['archive_base64']))
    public_path = tmp_path / 'public.txt'; public_path.write_bytes(base64.b64encode(public))
    app = create_app(registry, 'owned-cli-fixture')
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]})
    thread.start()
    until = time.monotonic() + 5
    while not server.started:
        assert thread.is_alive() and time.monotonic() < until, 'Owned HTTP fixture did not start'
        time.sleep(0.01)
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    def call(command, *arguments, role='author', auth=True):
        args = [command, '--url', url, '--allow-loopback-http', *arguments]
        if auth: args += ['--token-file', paths[role]]
        return invoke(args, secrets=(author, moderator, other))
    try:
        yield registry, call, paths, [release, archive, public_path], data
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive(), 'Owned catalog thread must stop before fixture cleanup'


def publication_args(files):
    return ['--release-file', files[0], '--archive-file', files[1], '--public-key-file', files[2]]


def test_author_moderator_publication_reports_withdrawal_and_audit_are_real_http(live):
    registry, call, _paths, files, _data = live
    validated, content = invoke(['check-package', *publication_args(files)])
    assert validated.returncode == 0 and content['state'] == 'validated'
    ready, value = call('ready', auth=False)
    assert ready.returncode == 0 and value['status'] == 'ready'
    result, submission = call('submit', *publication_args(files))
    assert result.returncode == 0 and submission['state'] == 'pending'
    identifier = submission['submission_id']
    assert call('status', '--submission-id', identifier, role='other')[1]['error']['code'] == 'OWNERSHIP_REQUIRED'
    assert call('submissions', role='other')[1]['total'] == 0
    own = call('submissions', '--package-id', 'test-package', '--version', '1.0.0')[1]
    assert own['total'] == 1 and own['items'][0]['submission_id'] == identifier
    assert call('reviews', role='author')[1]['error']['code'] == 'ROLE_REQUIRED'
    assert call('reviews', role='moderator')[1]['items'][0]['submission_id'] == identifier
    assert call('review', '--submission-id', identifier, '--approve', '--reason', 'independent review', role='author')[0].returncode == 1
    assert call('review', '--submission-id', identifier, '--approve', '--reason', '中文审核', role='moderator')[1]['state'] == 'published'
    assert call('submit', *publication_args(files))[1]['error']['code'] == 'IMMUTABLE_VERSION'
    reported = call('report', '--release-id', identifier, '--reason', 'please inspect')[1]
    report_id = reported['report_id']
    assert call('reports', role='moderator')[1]['items'][0]['id'] == report_id
    resolved = call('resolve-report', '--report-id', report_id, '--decision', 'dismissed', '--reason', 'reviewed evidence', role='moderator')[1]
    assert resolved['state'] == 'resolved'
    assert call('reports', role='moderator')[1]['items'] == []
    assert call('withdraw', '--release-id', identifier, '--reason', 'retire release')[1]['state'] == 'withdrawn'
    assert call('revoke-key', '--key-id', 'test-key', '--reason', 'replace signer', role='moderator')[1]['state'] == 'revoked'
    assert call('status', '--submission-id', identifier)[1]['state'] == 'withdrawn'
    actions = [row['action'] for row in call('audit', '--limit', 100, role='moderator')[1]['items']]
    assert actions == ['submit', 'published', 'report', 'resolve-report', 'withdraw', 'revoke-key']
    with registry.connect(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1


def test_package_tampering_missing_reason_and_rejection_never_publish(live):
    registry, call, _paths, files, _data = live
    original = files[1].read_bytes(); files[1].write_bytes(original + b'tampered')
    result, failed = call('submit', *publication_args(files))
    assert result.returncode == 1 and failed['error']['code'] == 'PACKAGE_VALIDATION_FAILED'
    with registry.connect(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
    files[1].write_bytes(original)
    identifier = call('submit', *publication_args(files))[1]['submission_id']
    assert call('review', '--submission-id', identifier, '--approve', role='moderator')[1]['error']['code'] == 'REASON_REQUIRED'
    assert call('review', '--submission-id', identifier, '--reject', '--reason', 'not ready', role='moderator')[1]['state'] == 'rejected'
    assert call('status', '--submission-id', identifier)[1]['state'] == 'rejected'


def test_credential_bootstrap_and_backup_commands_use_exclusive_new_targets(tmp_path):
    database = tmp_path / 'operator.db'; token_file = tmp_path / 'author.token'
    env = {'COMMUNITY_DATABASE_PATH': str(database)}
    arguments = ['create-author', '--id', 'author', '--namespace', 'examples', '--token-file', token_file]
    assert invoke(arguments, environment=env)[0].returncode == 0
    token = token_file.read_text('ascii')
    assert invoke(arguments, environment=env, secrets=(token,))[1]['error']['code'] == 'OUTPUT_ALREADY_EXISTS'
    assert token_file.read_text('ascii') == token
    bundle = tmp_path / 'bundle'
    result, manifest = invoke(['backup', '--output-dir', bundle], environment=env, secrets=(token,))
    assert result.returncode == 0 and manifest['counts']['principals'] == 1
    assert invoke(['verify-backup', '--input-dir', bundle, '--expected-sha256', manifest['sha256']])[0].returncode == 0
    restored = tmp_path / 'new.db'
    assert invoke(['restore', '--input-dir', bundle, '--output', restored, '--expected-sha256', manifest['sha256']])[1]['state'] == 'restored'
    assert invoke(['restore', '--input-dir', bundle, '--output', database, '--expected-sha256', manifest['sha256']])[1]['error']['code'] == 'OUTPUT_ALREADY_EXISTS'
    with Registry(restored).connect(write=False) as conn:
        assert conn.execute('SELECT token_hash FROM principals').fetchone()[0] == hashlib.sha256(token.encode()).hexdigest()


def test_deployment_entrypoint_reports_actual_readiness_and_source_identity(tmp_path):
    import http.client
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]; listener.close()
    env = os.environ.copy()
    env.update(COMMUNITY_DATABASE_PATH=str(tmp_path / 'served.db'), COMMUNITY_SOURCE_ID='owned-deployment-smoke', PYTHONUTF8='1')
    process = subprocess.Popen([sys.executable, '-m', 'community', 'serve', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        until = time.monotonic() + 8
        while True:
            assert process.poll() is None, 'Owned deployment CLI exited before readiness'
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                connection.request('GET', '/ready'); response = connection.getresponse(); body = json.loads(response.read())
                if response.status == 200: break
            except OSError:
                pass
            finally:
                connection.close()
            assert time.monotonic() < until, 'Owned CLI did not become ready'
            time.sleep(0.03)
        assert body['archive_storage'] == 'sqlite'
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
        try:
            connection.request('GET', '/catalog/v1/sources'); response = connection.getresponse()
            assert json.loads(response.read())['source_id'] == 'owned-deployment-smoke'
        finally:
            connection.close()
    finally:
        process.terminate()
        try: process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate(timeout=5)
    assert process.poll() is not None and (tmp_path / 'served.db').exists()


@pytest.mark.parametrize('url', ['http://example.org', 'https://user:password@example.org', 'https://example.org/path?credential=x'])
def test_invalid_or_non_tls_targets_are_refused_before_network(url):
    result, value = invoke(['ready', '--url', url, '--allow-loopback-http'])
    assert result.returncode == 1 and value['error']['code'] in {'HTTPS_REQUIRED', 'CATALOG_URL_INVALID'}


@pytest.mark.parametrize('mode', ['lost', 'redirect', 'echo', 'error-echo', 'malformed'])
def test_unknown_mutations_are_not_retried_redirects_not_followed_and_tokens_not_logged(tmp_path, mode):
    token = 'E' * 48; token_file = tmp_path / 'synthetic.token'; token_file.write_text(token)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if mode == 'lost':
                self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
            status = 302 if mode == 'redirect' else 400 if mode == 'error-echo' else 200
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            if mode == 'redirect': self.send_header('Location', 'http://127.0.0.1:1/must-not-follow')
            self.end_headers()
            body = {'state': 'reported', 'report_id': 1, 'echo': token} if mode == 'echo' else {'echo': token} if mode == 'malformed' else {'error': {'code': token}}
            self.wfile.write(json.dumps(body).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        result, value = invoke(['report', '--url', f'http://127.0.0.1:{server.server_port}', '--allow-loopback-http', '--token-file', token_file, '--release-id', 'lost', '--reason', 'explicit operation'], secrets=(token,))
        assert len(requests) == 1
        if mode == 'lost': assert result.returncode == 3 and value['error']['code'] == 'OUTCOME_UNKNOWN'
        elif mode == 'redirect': assert value['error']['code'] == 'REDIRECT_REFUSED'
        elif mode == 'echo': assert result.returncode == 0 and value['echo'] == '[REDACTED]'
        elif mode == 'malformed': assert result.returncode == 3 and value['error']['code'] == 'OUTCOME_UNKNOWN'
        else: assert value['error']['code'] == 'REMOTE_REQUEST_FAILED'
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
        assert not thread.is_alive()
