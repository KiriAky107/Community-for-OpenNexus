"""Real SQLite/WAL snapshots preserve identities, signatures, archives and audit."""
import base64
import hashlib
import json
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from community.operations import OperationError, backup, checksum, readiness, restore, verify_backup
from tests.test_catalog import package


@pytest.fixture
def registry(tmp_path):
    registry = Registry(tmp_path / 'catalog.db')
    author = registry.add_principal('author', 'author', 'examples')
    moderator = registry.add_principal('moderator', 'moderator')
    key = Ed25519PrivateKey.generate()
    registry.add_key('test-key', 'examples', key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return registry, key, {'Authorization': 'Bearer ' + author}, {'Authorization': 'Bearer ' + moderator}


def insert(registry, key, identifier, state='published', version='1.0.0'):
    data = package(key)
    metadata = data['release']
    metadata['version'] = version
    payload = json.dumps({key: value for key, value in metadata.items() if key != 'signature'}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    metadata['signature'] = base64.b64encode(key.sign(payload)).decode()
    with registry.connect() as conn:
        conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)', (identifier, metadata['namespace'], metadata['package_id'], version, json.dumps(metadata, ensure_ascii=False), base64.b64decode(data['archive_base64']), metadata['author_id'], state))
    return metadata


def test_readiness_never_initializes_missing_database_and_does_not_change_rows(registry, tmp_path):
    store, _, _, _ = registry
    missing = tmp_path / 'not-created.db'
    assert readiness(missing) is None and not missing.exists()
    original = checksum(store.path)
    with TestClient(create_app(store)) as client:
        assert client.get('/ready').json()['database_schema'] == 1
        assert client.get('/ready').headers['Cache-Control'] == 'no-store'
    assert checksum(store.path) == original
    with store.connect() as conn:
        conn.execute('UPDATE schema_version SET version=99')
    with TestClient(create_app(store)) as client:
        assert client.get('/health').status_code == 200
        result = client.get('/ready')
        assert result.status_code == 503 and result.json()['error']['code'] == 'DATABASE_NOT_READY'
        assert str(store.path) not in result.text


def test_live_wal_snapshot_restores_revoked_keys_old_signed_versions_and_archives(registry, tmp_path):
    store, key, author, moderator = registry
    insert(store, key, 'old', version='01.0.0')
    insert(store, key, 'pending', state='pending', version='1.0.1')
    insert(store, key, 'rejected', state='rejected', version='1.0.2')
    insert(store, key, 'withdrawn', state='withdrawn', version='1.0.3')
    with TestClient(create_app(store)) as client:
        report = client.post('/catalog/v1/releases/withdrawn/reports', headers=author, json={'reason': '中文审计 #%'})
        assert report.status_code == 200
        assert client.post('/catalog/v1/keys/test-key/revoke', headers=moderator, json={'reason': 'retain revocation'}).status_code == 200
    active = sqlite3.connect(store.path)
    try:
        assert active.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        active.execute("INSERT INTO audit(actor,action,subject,reason,timestamp) VALUES ('moderator','wal-proof','old','committed only in WAL',1)")
        active.commit()
        assert (tmp_path / 'catalog.db-wal').stat().st_size > 0
        bundle = tmp_path / '备份 #%'
        manifest = backup(store.path, bundle)
        assert manifest['signed_archives_verified'] == 4
        assert manifest['counts']['audit'] == 3
        assert verify_backup(bundle, manifest['sha256']) == manifest
        target = tmp_path / 'restored.db'
        assert restore(bundle, target, manifest['sha256'])['state'] == 'restored'
        restored = Registry(target)
        with TestClient(create_app(restored)) as client:
            assert client.get('/ready').status_code == 200
            # Its original non-SemVer release remains stored/readable, while
            # the existing archive route continues to reject that metadata.
            assert client.get('/catalog/v1/releases/old/archive').status_code == 422
            assert client.get('/catalog/v1/releases/withdrawn/archive').status_code == 410
            status = client.get('/catalog/v1/publish/submissions/pending', headers=author)
            assert status.json()['state'] == 'pending'
            assert client.get('/catalog/v1/moderation/audit', headers=moderator).json()['items'][-1]['action'] == 'wal-proof'
        with restored.connect(write=False) as conn:
            assert json.loads(conn.execute("SELECT metadata FROM submissions WHERE id='old'").fetchone()[0])['version'] == '01.0.0'
            assert conn.execute('SELECT revoked FROM keys').fetchone()[0] == 1
    finally:
        active.close()


@pytest.mark.parametrize('kind', ['archive', 'signature', 'schema', 'checksum', 'manifest-count'])
def test_corruption_and_forged_checksums_never_create_a_restore_target(registry, tmp_path, kind):
    store, key, _, _ = registry
    insert(store, key, 'one')
    bundle = tmp_path / 'bundle'
    manifest = backup(store.path, bundle)
    if kind in {'archive', 'signature', 'schema'}:
        with sqlite3.connect(bundle / 'catalog.sqlite3') as conn:
            if kind == 'archive': conn.execute("UPDATE submissions SET blob=zeroblob(length(blob))")
            elif kind == 'schema': conn.execute('UPDATE schema_version SET version=99')
            else:
                metadata = json.loads(conn.execute('SELECT metadata FROM submissions').fetchone()[0])
                metadata['description'] = 'tampered'
                conn.execute('UPDATE submissions SET metadata=?', (json.dumps(metadata),))
        manifest['sha256'] = checksum(bundle / 'catalog.sqlite3')
        manifest['bytes'] = (bundle / 'catalog.sqlite3').stat().st_size
    elif kind == 'checksum': manifest['sha256'] = '0' * 64
    else: manifest['counts']['audit'] += 1
    (bundle / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    target = tmp_path / 'must-not-appear.db'
    with pytest.raises((OperationError, sqlite3.Error)):
        restore(bundle, target, manifest['sha256'])
    assert not target.exists()


def test_existing_backup_restore_and_wrong_external_digest_are_not_overwritten(registry, tmp_path):
    store, key, _, _ = registry
    insert(store, key, 'one')
    bundle = tmp_path / 'bundle'
    manifest = backup(store.path, bundle)
    with pytest.raises(FileExistsError): backup(store.path, bundle)
    existing = tmp_path / 'existing.db'
    existing.write_bytes(b'User data')
    with pytest.raises(FileExistsError): restore(bundle, existing, manifest['sha256'])
    assert existing.read_bytes() == b'User data'
    with pytest.raises(OperationError): restore(bundle, tmp_path / 'wrong.db', '0' * 64)
    assert not (tmp_path / 'wrong.db').exists()
    sidecar = tmp_path / 'sidecar.db-wal'
    sidecar.write_bytes(b'Old owned sidecar')
    with pytest.raises(OperationError): restore(bundle, tmp_path / 'sidecar.db', manifest['sha256'])
    assert sidecar.read_bytes() == b'Old owned sidecar'


def test_incomplete_and_linked_backup_objects_are_not_accepted(registry, tmp_path, monkeypatch):
    import os
    import community.operations as operations
    store, key, _, _ = registry
    insert(store, key, 'one')
    bundle = tmp_path / 'interrupted'
    def interrupted(_path): raise OperationError('injected post-snapshot failure')
    with monkeypatch.context() as patch:
        patch.setattr(operations, 'inspect_database', interrupted)
        with pytest.raises(OperationError): backup(store.path, bundle)
    assert not (bundle / 'manifest.json').exists()
    with pytest.raises(OperationError): verify_backup(bundle)
    complete = tmp_path / 'complete'
    backup(store.path, complete)
    os.link(complete / 'catalog.sqlite3', tmp_path / 'linked.db')
    with pytest.raises(OperationError): verify_backup(complete)


def test_moderation_lists_are_bounded_do_not_read_blobs_and_keep_reports_auditable(registry, monkeypatch):
    from contextlib import contextmanager
    store, key, author, moderator = registry
    for index in range(105): insert(store, key, f'item-{index:03}', 'pending', f'1.0.{index}')
    original = store.connect
    @contextmanager
    def protected(*, write=True):
        with original(write=write) as conn:
            conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'submissions' and column == 'blob' else sqlite3.SQLITE_OK)
            yield conn
    monkeypatch.setattr(store, 'connect', protected)
    with TestClient(create_app(store)) as client:
        page = client.get('/catalog/v1/moderation/reviews?offset=100&limit=5', headers=moderator).json()
        assert page['total'] == 105 and len(page['items']) == 5
        own = client.get('/catalog/v1/publish/submissions?package_id=test-package&version=1.0.104', headers=author).json()
        assert len(own['items']) == 1 and own['items'][0]['submission_id'] == 'item-104'
        assert client.get('/catalog/v1/publish/submissions/item-104', headers=author).status_code == 200
        assert client.get('/catalog/v1/moderation/reviews?limit=101', headers=moderator).status_code == 422
        assert client.get('/catalog/v1/moderation/audit', headers=author).status_code == 403
        first = client.post('/catalog/v1/releases/item-104/reports', headers=author, json={'reason': 'retain original report'}).json()['report_id']
        second = client.post('/catalog/v1/releases/item-103/reports', headers=author, json={'reason': 'second report'}).json()['report_id']
        reports = client.get('/catalog/v1/moderation/reports?limit=1', headers=moderator).json()
        assert reports['items'][0]['id'] == first and reports['next_cursor'] == first
        assert client.get(f'/catalog/v1/moderation/reports?after={first}', headers=moderator).json()['items'][0]['id'] == second
        body = {'decision': 'addressed', 'reason': 'reviewed with evidence'}
        assert client.post(f'/catalog/v1/moderation/reports/{first}/resolve', headers=author, json=body).status_code == 403
        assert client.post(f'/catalog/v1/moderation/reports/{first}/resolve', headers=moderator, json=body).status_code == 200
        assert client.post(f'/catalog/v1/moderation/reports/{first}/resolve', headers=moderator, json=body).status_code == 409
        assert [row['id'] for row in client.get('/catalog/v1/moderation/reports', headers=moderator).json()['items']] == [second]
        audit = client.get('/catalog/v1/moderation/audit?limit=1', headers=moderator).json()
        assert audit['items'][0]['action'] == 'report' and audit['next_cursor'] == first
        assert client.get('/catalog/v1/keys/missing/revoke', headers=moderator).status_code == 405
        assert client.post('/catalog/v1/keys/missing/revoke', headers=moderator, json={'reason': 'known missing'}).status_code == 404


@pytest.mark.parametrize('action', ['review', 'report', 'resolve-report'])
def test_blank_reasons_do_not_change_publication_or_append_audit(registry, action):
    store, key, author, moderator = registry
    insert(store, key, 'pending', 'pending')
    with TestClient(create_app(store)) as client:
        report_id = client.post('/catalog/v1/releases/pending/reports', headers=author, json={'reason': 'actual report'}).json()['report_id']
        if action == 'review': path, body = '/catalog/v1/moderation/reviews', {'submission_id': 'pending', 'approve': True, 'reason': ' \r\n\t'}
        elif action == 'report': path, body = '/catalog/v1/releases/pending/reports', {'reason': ' \r\n\t'}
        else: path, body = f'/catalog/v1/moderation/reports/{report_id}/resolve', {'decision': 'addressed', 'reason': ' \r\n\t'}
        assert client.post(path, headers=moderator, json=body).status_code == 422
        with store.connect(write=False) as conn:
            assert conn.execute("SELECT state FROM submissions WHERE id='pending'").fetchone()[0] == 'pending'
            assert conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0] == 1
