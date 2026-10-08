"""Browser sessions exercise the same catalog authority without retaining tokens."""
import hashlib
import json
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from community.operations import OperationError, backup, checksum, readiness, restore, verify_backup
from community.sessions import COOKIE, LOCAL_COOKIE
from tests.test_catalog import package

ORIGIN = 'https://catalog.example'
WRITE = {'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'}
LOGIN = '/catalog/v1/web/session'


@pytest.fixture
def env(tmp_path):
    store = Registry(tmp_path / 'catalog.db')
    author = store.add_principal('author', 'author', 'examples')
    moderator = store.add_principal('moderator', 'moderator')
    other = store.add_principal('other', 'author', 'other')
    key = Ed25519PrivateKey.generate()
    store.add_key('test-key', 'examples', key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    app = create_app(store, web_origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        yield store, client, author, moderator, other, key


def login(client, token):
    response = client.post(LOGIN, headers=WRITE, json={'token': token})
    assert response.status_code == 200, response.text
    return response.json()


def writes(session):
    return {**WRITE, 'X-Community-CSRF': session['csrf']}


def test_cookie_authenticates_actual_author_writes_and_keeps_tokens_out_of_db_and_responses(env):
    store, client, author, _moderator, _other, key = env
    response = client.post(LOGIN, headers=WRITE, json={'token': author})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert all(flag in cookie for flag in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/', 'Max-Age=7200'))
    assert 'Domain=' not in cookie and cookie.startswith(COOKIE + '=')
    raw_cookie = client.cookies[COOKIE]
    session = response.json()
    assert author not in response.text and raw_cookie not in response.text
    assert session['role'] == 'author' and session['namespace'] == 'examples'
    assert client.get(LOGIN).json() == session
    with store.connect(write=False) as conn:
        row = dict(conn.execute('SELECT * FROM web_sessions').fetchone())
        assert row['token_hash'] == hashlib.sha256(raw_cookie.encode()).hexdigest()
        assert author not in json.dumps(row) and raw_cookie not in json.dumps(row)
        original = conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
    data = package(key)
    assert client.post('/catalog/v1/publish/submissions', headers=WRITE, json=data).status_code == 403
    with store.connect(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0] == original
    created = client.post('/catalog/v1/publish/submissions', headers=writes(session), json=data)
    assert created.status_code == 200
    assert client.get('/catalog/v1/publish/submissions').json()['total'] == 1
    assert client.get('/catalog/v1/moderation/reviews').status_code == 403
    for response in (created, client.get('/catalog/v1/publish/submissions'), client.get('/catalog/v1/moderation/reviews')):
        assert response.headers['cache-control'] == 'private, no-store'
        assert author not in response.text and raw_cookie not in response.text


@pytest.mark.parametrize('headers', [{}, {'Origin': 'https://elsewhere.example'}, {'Origin': 'null'}, {**WRITE, 'Sec-Fetch-Site': 'same-site'}, {**WRITE, 'Host': 'elsewhere.example'}, {**WRITE, 'Host': '[invalid'}])
def test_login_csrf_origin_and_host_fail_before_creating_sessions(env, headers):
    store, client, author, *_ = env
    response = client.post(LOGIN, headers=headers, json={'token': author})
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'private, no-store'
    with store.connect(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 0


def test_cross_site_cookie_requests_and_ambiguous_identity_do_not_access_or_mutate(env):
    store, client, author, moderator, _other, _key = env
    session = login(client, author)
    assert client.get(LOGIN, headers={'Cookie': f'{COOKIE}={client.cookies[COOKIE]}; {COOKIE}={"x" * 64}'}).json()['error']['code'] == 'AUTH_AMBIGUOUS'
    assert client.get(LOGIN, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/catalog/v1/publish/submissions', headers={'Origin': 'https://elsewhere.example'}).status_code == 403
    assert client.get('/catalog/v1/moderation/reviews', headers={'Authorization': 'Bearer ' + moderator}).json()['error']['code'] == 'AUTH_AMBIGUOUS'
    assert client.post(LOGIN + '/logout', headers={**WRITE, 'X-Community-CSRF': '0' * 64}).status_code == 403
    assert client.post(LOGIN + '/logout', headers={**writes(session), 'Sec-Fetch-Site': 'same-site'}).status_code == 403
    with store.connect(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 1
    # An explicit principal bearer remains usable without ambient browser auth.
    client.cookies.clear()
    assert client.get('/catalog/v1/moderation/reviews', headers={'Authorization': 'Bearer ' + moderator}).status_code == 200


def test_current_role_and_principal_revocation_are_read_on_every_request(env):
    store, client, author, _moderator, _other, _key = env
    session = login(client, author)
    with store.connect() as conn: conn.execute("UPDATE principals SET role='moderator' WHERE id='author'")
    assert client.get(LOGIN).json()['role'] == 'moderator'
    assert client.get('/catalog/v1/moderation/reviews').status_code == 200
    assert client.post('/catalog/v1/publish/submissions', headers=writes(session), json=package(_key)).status_code == 403
    with store.connect() as conn: conn.execute("UPDATE principals SET revoked=1 WHERE id='author'")
    response = client.get(LOGIN)
    assert response.status_code == 401 and COOKIE not in client.cookies
    assert client.get('/catalog/v1/moderation/reviews').status_code == 401


def test_absolute_expiry_cannot_be_extended_by_read_or_replayed_csrf(env, monkeypatch):
    import community.sessions as module
    store, client, author, *_ = env
    now = 1900000000
    monkeypatch.setattr(module.time, 'time', lambda: now)
    session = login(client, author)
    raw = client.cookies[COOKIE]
    assert session['expires_at'] == now + 7200
    now += 7199
    assert client.get(LOGIN).json()['expires_at'] == session['expires_at']
    now += 1
    # Send the original cookie explicitly after the client would naturally stop
    # sending it, proving that absolute server expiry also refuses replay.
    assert client.post(LOGIN + '/logout', headers={**writes(session), 'Cookie': COOKIE + '=' + raw}).status_code == 401
    assert COOKIE not in client.cookies
    with store.connect(write=False) as conn:
        assert conn.execute('SELECT expires_at FROM web_sessions').fetchone()[0] == session['expires_at']


def test_logout_revokes_cookie_and_original_cookie_cannot_be_replayed(env):
    store, client, author, *_ = env
    session = login(client, author)
    raw = client.cookies[COOKIE]
    assert client.post(LOGIN + '/logout', headers=writes(session)).json()['state'] == 'logged_out'
    assert COOKIE not in client.cookies
    client.cookies.set(COOKIE, raw)
    assert client.get(LOGIN).status_code == 401
    with store.connect(write=False) as conn: assert conn.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 0


def test_other_session_revocation_is_owned_repeatable_and_does_not_expose_foreign_sessions(env):
    store, client, author, moderator, *_ = env
    first = login(client, author); first_cookie = client.cookies[COOKIE]
    client.cookies.clear()
    second = login(client, author); second_cookie = client.cookies[COOKIE]
    client.cookies.clear()
    foreign = login(client, moderator)
    client.cookies.clear(); client.cookies.set(COOKIE, first_cookie)
    listing = client.get('/catalog/v1/web/sessions').json()['items']
    assert {item['id'] for item in listing} == {first['session_id'], second['session_id']}
    assert all('token_hash' not in item and 'csrf_nonce' not in item for item in listing)
    path = '/catalog/v1/web/sessions/' + second['session_id'] + '/revoke'
    for _ in range(2): assert client.post(path, headers=writes(first)).status_code == 200
    assert client.post('/catalog/v1/web/sessions/' + foreign['session_id'] + '/revoke', headers=writes(first)).status_code == 200
    with store.connect(write=False) as conn:
        assert {row['id'] for row in conn.execute('SELECT id FROM web_sessions')} == {first['session_id'], foreign['session_id']}
    client.cookies.clear(); client.cookies.set(COOKIE, second_cookie)
    assert client.get(LOGIN).status_code == 401


def test_active_session_and_login_attempt_limits_are_bounded(env):
    store, client, author, *_ = env
    for _ in range(8):
        client.cookies.clear(); login(client, author)
    client.cookies.clear()
    assert client.post(LOGIN, headers=WRITE, json={'token': author}).json()['error']['code'] == 'SESSION_LIMIT'
    assert client.post(LOGIN, headers=WRITE, json={'token': 'x' * 64}).status_code == 401
    assert client.post(LOGIN, headers=WRITE, json={'token': author}).json()['error']['code'] == 'LOGIN_RATE_LIMIT'
    with store.connect(write=False) as conn: assert conn.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 8


def test_browser_sessions_require_an_explicit_origin_and_loopback_exception(tmp_path):
    store = Registry(tmp_path / 'catalog.db'); token = store.add_principal('author', 'author', 'examples')
    with TestClient(create_app(store)) as client:
        assert client.post(LOGIN, json={'token': token}).json()['error']['code'] == 'WEB_ORIGIN_REQUIRED'
    with pytest.raises(ValueError): create_app(store, web_origin='http://catalog.example', allow_insecure_loopback_sessions=True)
    with pytest.raises(ValueError): create_app(store, web_origin='http://127.0.0.1:8081')
    with pytest.raises(ValueError): create_app(store, web_origin='https://catalog.example/prefix')
    with pytest.raises(ValueError): create_app(store, web_origin=ORIGIN, session_ttl=299)
    with TestClient(create_app(store, web_origin='http://127.0.0.1:8081', allow_insecure_loopback_sessions=True), base_url='http://127.0.0.1:8081') as client:
        response = client.post(LOGIN, headers={'Origin': 'http://127.0.0.1:8081'}, json={'token': token})
        assert response.status_code == 200 and 'Secure' not in response.headers['set-cookie']
        assert LOCAL_COOKIE in client.cookies and client.get(LOGIN).status_code == 200
    # TLS may terminate at the reviewed public proxy; untrusted forwarded host
    # values never change the explicit origin or cookie security policy.
    with TestClient(create_app(store, web_origin=ORIGIN), base_url='http://catalog.example') as client:
        response = client.post(LOGIN, headers={**WRITE, 'X-Forwarded-Host': 'elsewhere.example'}, json={'token': token})
        assert response.status_code == 200 and 'Secure' in response.headers['set-cookie']
        assert client.get(LOGIN).status_code == 401  # Secure cookie is not sent over plaintext.


def test_snapshot_keeps_persistent_authority_and_bytes_but_never_revives_browser_sessions(env, tmp_path):
    store, client, author, moderator, _other, key = env
    session = login(client, author); raw = client.cookies[COOKIE]
    created = client.post('/catalog/v1/publish/submissions', headers=writes(session), json=package(key)).json()
    assert readiness(store.path) is not None
    bundle = tmp_path / 'session-backup'
    manifest = backup(store.path, bundle)
    assert verify_backup(bundle, manifest['sha256']) == manifest
    assert client.get(LOGIN).status_code == 200
    with sqlite3.connect(bundle / 'catalog.sqlite3') as conn:
        assert conn.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 0
    target = tmp_path / 'restored.db'
    restore(bundle, target, manifest['sha256'])
    with TestClient(create_app(Registry(target), web_origin=ORIGIN), base_url=ORIGIN) as restored:
        restored.cookies.set(COOKIE, raw)
        assert restored.get(LOGIN).status_code == 401
        restored.cookies.clear()
        assert restored.get('/catalog/v1/publish/submissions/' + created['submission_id'], headers={'Authorization': 'Bearer ' + author}).json()['state'] == 'pending'
        assert restored.get('/catalog/v1/moderation/reviews', headers={'Authorization': 'Bearer ' + moderator}).json()['total'] == 1
        assert login(restored, author)['session_id'] != session['session_id']
    with sqlite3.connect(bundle / 'catalog.sqlite3') as conn:
        conn.execute('INSERT INTO web_sessions VALUES (?,?,?,?,?,?)', ('f' * 32, 'e' * 64, 'author', 1, 9999999999, 'a' * 32))
    manifest['sha256'] = checksum(bundle / 'catalog.sqlite3')
    manifest['bytes'] = (bundle / 'catalog.sqlite3').stat().st_size
    (bundle / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
    with pytest.raises(OperationError, match='BACKUP_CONTAINS_BROWSER_SESSIONS'): verify_backup(bundle)
    assert author not in json.dumps(manifest) and raw not in json.dumps(manifest)


def test_legacy_snapshot_without_session_table_stays_readable_and_migrates_only_on_start(env, tmp_path):
    store, _client, author, *_ = env
    with store.connect() as conn: conn.execute('DROP TABLE web_sessions')
    before = checksum(store.path)
    assert readiness(store.path) is not None and checksum(store.path) == before
    manifest = backup(store.path, tmp_path / 'legacy')
    target = tmp_path / 'legacy-restored.db'
    restore(tmp_path / 'legacy', target, manifest['sha256'])
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_sessions'").fetchone() is None
    registry = Registry(target)
    with TestClient(create_app(registry, web_origin=ORIGIN), base_url=ORIGIN) as client:
        assert login(client, author)['role'] == 'author'
