"""Role-scoped key inspection and durable frozen revocation."""
import base64
from contextlib import contextmanager
import hashlib
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from tests.test_catalog import package


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / 'catalog.db')
    tokens = {name: registry.add_principal(name, role, namespace) for name, role, namespace in [('author', 'author', 'examples'), ('other', 'author', 'other'), ('reviewer', 'moderator', None)]}
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    registry.add_key('test-key', 'examples', raw)
    registry.add_key('other-key', 'other', b'x' * 32)
    registry.add_key('百分%_公钥', 'examples', b'y' * 32)
    with TestClient(create_app(registry)) as client:
        yield registry, client, {name: {'Authorization': 'Bearer ' + token} for name, token in tokens.items()}, private, raw


def test_audit_old_cursor_recent_snapshot_and_literal_filters(env):
    registry, client, auth, _, _ = env
    with registry.connect() as conn:
        for i in range(7):
            conn.execute('INSERT INTO audit(actor,action,subject,reason,timestamp) VALUES (?,?,?,?,?)', ('author' if i % 2 else 'reviewer', 'review', '包%_x' if i == 3 else 'plain', '<script>仅原文</script>' if i == 3 else '', 100 + i))
    path = '/catalog/v1/moderation/audit'
    old = client.get(path, headers=auth['reviewer'], params={'limit': 2}).json()
    assert [x['id'] for x in old['items']] == [1, 2] and old['next_cursor'] == 2
    assert [x['id'] for x in client.get(path, headers=auth['reviewer'], params={'after': 2, 'limit': 2}).json()['items']] == [3, 4]
    recent = client.get(path, headers=auth['reviewer'], params={'before': 9007199254740991, 'limit': 2}).json()
    assert [x['id'] for x in recent['items']] == [7, 6] and recent['next_cursor'] == 6
    with registry.connect() as conn:
        conn.execute("INSERT INTO audit(actor,action,subject,reason,timestamp) VALUES ('new','submit','new','',200)")
    assert [x['id'] for x in client.get(path, headers=auth['reviewer'], params={'before': 6, 'limit': 2}).json()['items']] == [5, 4]
    literal = client.get(path, headers=auth['reviewer'], params={'q': '%_', 'actor': 'author', 'action': 'review', 'subject': '包%_x'}).json()
    assert len(literal['items']) == 1 and literal['items'][0]['reason'] == '<script>仅原文</script>'
    assert len(client.get(path, headers=auth['reviewer'], params={'q': '仅原文'}).json()['items']) == 1
    for invalid in [{'before': 2, 'after': 1}, {'limit': 101}, {'q': 'x' * 121}, {'before': 0}]:
        assert client.get(path, headers=auth['reviewer'], params=invalid).status_code == 422
    assert client.get(path, headers=auth['author']).status_code == 403
    assert client.get(path).status_code == 401


def test_key_scope_fingerprint_pagination_and_readonly(env, monkeypatch):
    registry, client, auth, _, raw = env
    original = registry.connect
    @contextmanager
    def readonly(*, write=True):
        assert not write
        with original(write=False) as conn:
            conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} else sqlite3.SQLITE_OK)
            yield conn
    with monkeypatch.context() as patch:
        patch.setattr(registry, 'connect', readonly)
        response = client.get('/catalog/v1/publish/keys', headers=auth['author'], params={'limit': 1})
        page = response.json()
        assert response.status_code == 200 and page['total'] == 2 and page['items'][0]['key_id'] == 'test-key'
        key = page['items'][0]
        assert base64.b64decode(key['public_key']) == raw and key['fingerprint'] == hashlib.sha256(raw).hexdigest()
        assert len(key['review_digest']) == 64 and key['revoked'] is False
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert 'Authorization' in response.headers['Vary']
        second = client.get('/catalog/v1/publish/keys', headers=auth['author'], params={'offset': 1, 'limit': 1}).json()
        assert second['items'][0]['key_id'] == '百分%_公钥'
        assert client.get('/catalog/v1/publish/keys', headers=auth['other'], params={'q': 'test-key'}).json()['total'] == 0
        assert client.get('/catalog/v1/publish/keys', headers=auth['reviewer']).json()['total'] == 3
        assert client.get('/catalog/v1/publish/keys', headers=auth['reviewer'], params={'q': '%_'}).json()['total'] == 1
        assert client.get('/catalog/v1/publish/keys').status_code == 401
    with registry.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0] == 0


def test_frozen_revocation_receipt_conflict_and_legacy_compatibility(env):
    registry, client, auth, _, raw = env
    path = '/catalog/v1/keys/test-key/revoke'
    key = client.get('/catalog/v1/publish/keys', headers=auth['reviewer'], params={'q': 'test-key'}).json()['items'][0]
    body = {'reason': '核对后撤销', 'operation_id': 'a' * 32, 'expected_sha256': key['review_digest']}
    assert client.post(path, headers=auth['author'], json=body).status_code == 403
    assert client.post(path, headers=auth['reviewer'], json={**body, 'expected_sha256': '0' * 64}).status_code == 409
    accepted = client.post(path, headers=auth['reviewer'], json=body)
    assert accepted.status_code == 200 and accepted.json()['key_id'] == 'test-key'
    assert client.get('/catalog/v1/web/operations/' + body['operation_id'], headers=auth['reviewer']).json() == accepted.json()
    assert client.post(path, headers=auth['reviewer'], json=body).json() == accepted.json()
    assert client.post(path, headers=auth['reviewer'], json={**body, 'reason': 'changed'}).status_code == 409
    assert client.post(path, headers=auth['reviewer'], json={**body, 'operation_id': 'b' * 32}).status_code == 409
    revoked = client.get('/catalog/v1/publish/keys', headers=auth['reviewer'], params={'state': 'revoked'}).json()['items'][0]
    assert revoked['review_digest'] != key['review_digest'] and base64.b64decode(revoked['public_key']) == raw
    assert client.post(path, headers=auth['reviewer'], json={**body, 'operation_id': 'c' * 32, 'expected_sha256': revoked['review_digest']}).json()['error']['code'] == 'KEY_ALREADY_REVOKED'
    # Existing integrations without frozen state remain valid.
    assert client.post('/catalog/v1/keys/other-key/revoke', headers=auth['reviewer'], json={'reason': 'legacy'}).status_code == 200
    with registry.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='revoke-key' AND subject='test-key'").fetchone()[0] == 1


def test_key_revocation_blocks_real_signed_submission_without_changing_bytes(env):
    registry, client, auth, private, raw = env
    data = package(private)
    assert client.post('/catalog/v1/publish/preflight', headers=auth['author'], json=data).status_code == 200
    key = client.get('/catalog/v1/publish/keys', headers=auth['reviewer'], params={'q': 'test-key'}).json()['items'][0]
    response = client.post('/catalog/v1/keys/test-key/revoke', headers=auth['reviewer'], json={'reason': 'new fixture only', 'operation_id': 'd' * 32, 'expected_sha256': key['review_digest']})
    assert response.status_code == 200
    assert client.post('/catalog/v1/publish/preflight', headers=auth['author'], json=data).json()['error']['code'] == 'UNTRUSTED_SIGNER'
    assert base64.b64decode(client.get('/catalog/v1/sources').json()['keys'][0]['public_key']) == raw


def test_browser_revocation_csrf_and_original_cookie_receipt(env):
    registry, _, _, _, _ = env
    token = registry.add_principal('web-reviewer', 'moderator')
    origin = 'https://catalog.example'
    with TestClient(create_app(registry, web_origin=origin), base_url=origin) as client:
        session = client.post('/catalog/v1/web/session', headers={'Origin': origin}, json={'token': token}).json()
        key = client.get('/catalog/v1/publish/keys', params={'q': 'test-key'}).json()['items'][0]
        body = {'reason': 'own fixture', 'operation_id': 'e' * 32, 'expected_sha256': key['review_digest']}
        path = '/catalog/v1/keys/test-key/revoke'
        assert client.post(path, json=body).status_code == 403
        headers = {'Origin': origin, 'X-Community-CSRF': session['csrf']}
        result = client.post(path, headers=headers, json=body)
        assert result.status_code == 200
        assert client.get('/catalog/v1/web/operations/' + body['operation_id']).json() == result.json()
        assert client.post(path, headers=headers, json=body).json() == result.json()
