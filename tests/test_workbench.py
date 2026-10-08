"""Real signed archives and durable, actor-scoped catalog actions."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from community.operations import OperationError, backup, checksum, restore, verify_backup
from community.package import signed_payload, Release
from tests.test_catalog import package


@pytest.fixture
def env(tmp_path):
    store = Registry(tmp_path / 'catalog.db')
    author = {'Authorization': 'Bearer ' + store.add_principal('author', 'author', 'examples')}
    moderator = {'Authorization': 'Bearer ' + store.add_principal('reviewer', 'moderator')}
    other = {'Authorization': 'Bearer ' + store.add_principal('other', 'author', 'other')}
    key = Ed25519PrivateKey.generate()
    store.add_key('test-key', 'examples', key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    with TestClient(create_app(store)) as client:
        yield store, client, author, moderator, other, key


def revised(key, version, files):
    data = package(key, files=files)
    release = Release.model_validate(data['release'])
    release.version = version
    release.signature = base64.b64encode(key.sign(signed_payload(release))).decode()
    data['release'] = release.model_dump()
    return data


def prepare(client, author, data, operation_id='a' * 32):
    preview = client.post('/catalog/v1/publish/preflight', headers=author, json=data)
    assert preview.status_code == 200, preview.text
    return {**data, 'operation_id': operation_id, 'expected_sha256': preview.json()['inspection']['review_digest']}


def test_preflight_is_readonly_and_enforces_signature_namespace_key_and_immutable_version(env, monkeypatch):
    store, client, author, moderator, _other, key = env
    original = store.connect
    @contextmanager
    def readonly(*, write=True):
        assert write is False, 'Preflight must not start a write transaction'
        with original(write=False) as conn:
            conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} else sqlite3.SQLITE_OK)
            yield conn
    data = package(key)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', readonly)
        response = client.post('/catalog/v1/publish/preflight', headers=author, json=data)
        assert response.status_code == 200 and response.json()['state'] == 'validated'
        assert response.json()['inspection']['signature_verified']
        assert client.post('/catalog/v1/publish/preflight', headers=moderator, json=data).status_code == 403
    with original(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0] == 0
    tampered = {**data, 'release': {**data['release'], 'permissions': ['network.request']}}
    assert client.post('/catalog/v1/publish/preflight', headers=author, json=tampered).status_code == 422
    wrong_namespace = {**data, 'release': {**data['release'], 'namespace': 'other'}}
    assert client.post('/catalog/v1/publish/preflight', headers=author, json=wrong_namespace).status_code == 403
    submitted = client.post('/catalog/v1/publish/submissions', headers=author, json=prepare(client, author, data))
    assert submitted.status_code == 200
    assert client.post('/catalog/v1/publish/preflight', headers=author, json=data).json()['error']['code'] == 'IMMUTABLE_VERSION'
    with original() as conn: conn.execute("UPDATE keys SET revoked=1 WHERE id='test-key'")
    assert client.post('/catalog/v1/publish/preflight', headers=author, json=revised(key, '2.0.0', {'persona.json': '{"system_prompt":"new"}'})).json()['error']['code'] == 'UNTRUSTED_SIGNER'


def test_authorized_inspection_pages_hashes_manifest_diff_reasons_and_archive_are_actual_bytes(env):
    store, client, author, moderator, other, key = env
    initial = revised(key, '1.0.0', {'persona.json': '{"system_prompt":"old"}', 'remove.txt': 'removed'})
    prior = client.post('/catalog/v1/publish/submissions', headers=author, json=initial).json()['submission_id']
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json={'submission_id': prior, 'approve': True, 'reason': 'first version independently checked'}).status_code == 200
    files = {**{f'file-{index:03}.txt': f'actual content {index}' for index in range(105)}, 'persona.json': '{"system_prompt":"<script>plain manifest text</script>"}'}
    data = revised(key, '2.0.0', files)
    identifier = client.post('/catalog/v1/publish/submissions', headers=author, json=data).json()['submission_id']
    path = f'/catalog/v1/publish/submissions/{identifier}/inspection'
    assert client.get(path, headers=other).status_code == 403
    assert client.get(path).status_code == 401
    inspected = client.get(path + '?limit=100', headers=moderator).json()
    more = client.get(path + '?offset=100&limit=100', headers=author).json()
    page = inspected['inspection']['file_page']
    assert page['total'] == 106 and len(page['items']) == 100 and len(more['inspection']['file_page']['items']) == 6
    actual = page['items'][0]
    assert actual['bytes'] == len(files[actual['path']].encode()) and actual['sha256'] == hashlib.sha256(files[actual['path']].encode()).hexdigest()
    assert '<script>' in inspected['inspection']['manifest_text'] and not inspected['inspection']['manifest_truncated']
    assert inspected['previous']['version'] == '1.0.0' and inspected['previous']['file_page']['total'] == 107
    last_changes = more['previous']['file_page']['items']
    assert any(entry['path'] == 'remove.txt' and entry['change'] == 'removed' for entry in last_changes)
    assert any(entry['path'] == 'persona.json' and entry['change'] == 'changed' for entry in last_changes)
    archive = client.get(f'/catalog/v1/publish/submissions/{identifier}/archive', headers=moderator)
    assert archive.content == base64.b64decode(data['archive_base64']) and 'attachment' in archive.headers['content-disposition']
    assert client.get(f'/catalog/v1/publish/submissions/{identifier}/archive', headers=other).status_code == 403
    body = {'submission_id': identifier, 'approve': False, 'reason': 'Needs attribution before publication', 'operation_id': 'b' * 32, 'expected_sha256': inspected['inspection']['review_digest']}
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json=body).json()['state'] == 'rejected'
    status = client.get(f'/catalog/v1/publish/submissions/{identifier}', headers=author).json()
    assert status['decisions'][0]['reason'] == body['reason'] and status['state'] == 'rejected'


@pytest.mark.parametrize('kind', ['submit', 'review', 'withdraw', 'report', 'resolve-report', 'revoke-key'])
def test_lost_reply_readonly_receipt_and_original_retry_preserve_one_action(env, kind):
    store, client, author, moderator, other, key = env
    data = package(key)
    body = prepare(client, author, data)
    identifier = client.post('/catalog/v1/publish/submissions', headers=author, json=data).json()['submission_id'] if kind != 'submit' else None
    inspected = client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection', headers=moderator).json() if identifier else None
    op = 'c' * 32
    actor = author
    if kind == 'submit': path = '/catalog/v1/publish/submissions'; body['operation_id'] = op
    elif kind == 'review':
        path = '/catalog/v1/moderation/reviews'; actor = moderator
        body = {'submission_id': identifier, 'approve': True, 'reason': 'reviewed original', 'operation_id': op, 'expected_sha256': inspected['inspection']['review_digest']}
    elif kind == 'withdraw':
        client.post('/catalog/v1/moderation/reviews', headers=moderator, json={'submission_id': identifier, 'approve': True, 'reason': 'published before withdrawal'})
        path = f'/catalog/v1/releases/{identifier}/withdraw'; body = {'reason': 'retire original', 'operation_id': op, 'expected_sha256': inspected['inspection']['review_digest']}
    elif kind == 'report': path = f'/catalog/v1/releases/{identifier}/reports'; body = {'reason': 'inspect original', 'operation_id': op}
    elif kind == 'resolve-report':
        report_id = client.post(f'/catalog/v1/releases/{identifier}/reports', headers=author, json={'reason': 'original report'}).json()['report_id']
        report = client.get('/catalog/v1/moderation/reports', headers=moderator).json()['items'][0]
        path = f'/catalog/v1/moderation/reports/{report_id}/resolve'; actor = moderator
        body = {'reason': 'addressed original evidence', 'decision': 'addressed', 'operation_id': op, 'expected_sha256': report['review_digest']}
    else: path = '/catalog/v1/keys/test-key/revoke'; actor = moderator; body = {'reason': 'original revocation', 'operation_id': op}
    requests = []
    app = create_app(store)
    @app.middleware('http')
    async def drop_once(request, call_next):
        response = await call_next(request)
        requests.append((request.method, request.url.path))
        if request.method == 'POST' and len([item for item in requests if item[0] == 'POST']) == 1:
            assert response.status_code == 200
            return JSONResponse({'error': {'code': 'OWNED_LOST_REPLY'}}, status_code=503)
        return response
    with TestClient(app) as interrupted:
        assert interrupted.post(path, headers=actor, json=body).status_code == 503
        status = f'/catalog/v1/web/operations/{op}'
        before = interrupted.get(status, headers=actor).json()
        assert before['operation_id'] == op and before['kind'] == kind and before['state'] != 'not_found'
        assert len([item for item in requests if item[0] == 'POST']) == 1
        assert interrupted.get(status, headers=other).json()['state'] == 'not_found'
        assert interrupted.post(path, headers=actor, json=body).json() == before
        changed = {**body, 'reason': 'different intent'} if kind != 'submit' else {**body, 'expected_sha256': 'f' * 64}
        assert interrupted.post(path, headers=actor, json=changed).json()['error']['code'] == 'OPERATION_ID_REUSED'
    with store.connect(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='web-operation' AND subject=?", (op,)).fetchone()[0] == 1
        matching = 'published' if kind == 'review' else kind
        assert conn.execute('SELECT COUNT(*) FROM audit WHERE action=?', (matching,)).fetchone()[0] == 1


def test_original_receipt_survives_backup_and_concurrent_request_never_creates_duplicate(env, tmp_path):
    store, client, author, moderator, _other, key = env
    body = prepare(client, author, package(key))
    def send(_):
        with TestClient(create_app(store)) as caller:
            return caller.post('/catalog/v1/publish/submissions', headers=author, json=body).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(send, range(2)))
    assert first == second and first['state'] == 'pending'
    manifest = backup(store.path, tmp_path / 'backup')
    assert verify_backup(tmp_path / 'backup', manifest['sha256']) == manifest
    target = tmp_path / 'restored.db'; restore(tmp_path / 'backup', target, manifest['sha256'])
    with TestClient(create_app(Registry(target))) as recovered:
        assert recovered.get('/catalog/v1/web/operations/' + body['operation_id'], headers=author).json() == first
        assert recovered.post('/catalog/v1/publish/submissions', headers=author, json=body).json() == first
        assert recovered.get('/catalog/v1/moderation/reviews', headers=moderator).json()['total'] == 1
    with store.connect(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1


def test_stale_digest_self_review_revoked_key_and_corrupt_archive_never_publish(env):
    store, client, author, moderator, _other, key = env
    identifier = client.post('/catalog/v1/publish/submissions', headers=author, json=package(key)).json()['submission_id']
    preview = client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection', headers=moderator).json()
    body = {'submission_id': identifier, 'approve': True, 'reason': 'explicit review', 'operation_id': 'd' * 32, 'expected_sha256': 'f' * 64}
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json=body).json()['error']['code'] == 'REVIEW_CHANGED'
    body['expected_sha256'] = preview['inspection']['review_digest']
    with store.connect() as conn: conn.execute("UPDATE principals SET role='moderator' WHERE id='author'")
    assert client.post('/catalog/v1/moderation/reviews', headers=author, json=body).json()['error']['code'] == 'SELF_REVIEW_FORBIDDEN'
    with store.connect() as conn: conn.execute('UPDATE keys SET revoked=1')
    assert client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection', headers=moderator).json()['inspection']['signer_revoked']
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json=body).json()['error']['code'] == 'UNTRUSTED_SIGNER'
    with store.connect() as conn:
        conn.execute('UPDATE keys SET revoked=0')
        conn.execute('UPDATE submissions SET blob=zeroblob(length(blob))')
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json=body).json()['error']['code'] == 'PACKAGE_VALIDATION_FAILED'
    with store.connect(write=False) as conn:
        assert conn.execute('SELECT state FROM submissions').fetchone()[0] == 'pending'
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='web-operation'").fetchone()[0] == 0


def test_legacy_signed_non_semver_release_can_still_be_withdrawn(env):
    store, client, author, _moderator, *_ = env
    with store.connect() as conn:
        conn.execute("INSERT INTO submissions VALUES ('old','examples','old','01.0.0',?,?,'author','published')", (json.dumps({'namespace': 'examples', 'package_id': 'old', 'version': '01.0.0'}), b'legacy'))
    assert client.post('/catalog/v1/releases/old/withdraw', headers=author, json={'reason': 'retire legacy'}).status_code == 200


def test_request_lost_before_execution_is_absent_then_same_intent_can_be_explicitly_sent(env):
    store, client, author, _moderator, _other, key = env
    body = prepare(client, author, package(key))
    calls = 0
    app = create_app(store)
    @app.middleware('http')
    async def lose_before(request, call_next):
        nonlocal calls
        if request.method == 'POST':
            calls += 1
            if calls == 1: return JSONResponse({'error': {'code': 'OWNED_NOT_EXECUTED'}}, status_code=503)
        return await call_next(request)
    with TestClient(app) as caller:
        assert caller.post('/catalog/v1/publish/submissions', headers=author, json=body).status_code == 503
        status = '/catalog/v1/web/operations/' + body['operation_id']
        assert caller.get(status, headers=author).json()['state'] == 'not_found'
        assert calls == 1
        result = caller.post('/catalog/v1/publish/submissions', headers=author, json=body).json()
        assert result['state'] == 'pending' and calls == 2
        assert caller.get(status, headers=author).json() == result
    with store.connect(write=False) as conn: assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1


def test_corrupt_action_receipt_is_classified_and_backup_refuses_it(env, tmp_path):
    store, client, author, _moderator, _other, key = env
    body = prepare(client, author, package(key))
    client.post('/catalog/v1/publish/submissions', headers=author, json=body)
    bundle = tmp_path / 'receipt-backup'
    manifest = backup(store.path, bundle)
    with store.connect() as conn: conn.execute("UPDATE audit SET reason='{}' WHERE action='web-operation'")
    response = client.get('/catalog/v1/web/operations/' + body['operation_id'], headers=author)
    assert response.status_code == 503 and response.json()['error']['code'] == 'OPERATION_RECEIPT_INVALID'
    with sqlite3.connect(bundle / 'catalog.sqlite3') as conn: conn.execute("UPDATE audit SET reason='{}' WHERE action='web-operation'")
    manifest.update(sha256=checksum(bundle / 'catalog.sqlite3'), bytes=(bundle / 'catalog.sqlite3').stat().st_size)
    (bundle / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
    with pytest.raises(OperationError, match='DATABASE_OPERATION_RECEIPT_INVALID'): verify_backup(bundle)


def test_long_manifest_is_explicitly_truncated_and_invalid_old_version_does_not_hide_new_inspection(env):
    store, client, author, moderator, _other, key = env
    data = revised(key, '2.0.0', {'persona.json': json.dumps({'system_prompt': 'x' * 70000})})
    identifier = client.post('/catalog/v1/publish/submissions', headers=author, json=data).json()['submission_id']
    with store.connect() as conn:
        conn.execute("INSERT INTO submissions VALUES ('old','examples','test-package','01.0.0',?,?,'author','published')", (json.dumps({'version': '01.0.0'}), b'old'))
    value = client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection', headers=moderator).json()
    assert value['inspection']['manifest_truncated'] and len(value['inspection']['manifest_text']) == 65536
    assert value['previous'] == {'release_id': 'old', 'available': False}
