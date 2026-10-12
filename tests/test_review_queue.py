import json
from contextlib import contextmanager
import sqlite3
import pytest
from tests.test_workbench import env
from tests.test_catalog import package


@pytest.fixture
def populated(env):
    store, client, author, moderator, other, key = env
    with store.connect() as conn:
        for index in range(27):
            release = package(key)['release']
            release.update(package_id=f'lesson-{index}', version='1.0.0', name='中文 %_ Reader' if index % 2 == 0 else 'Other Reader', type='persona' if index % 2 == 0 else 'template')
            owned = index != 26
            release.update(namespace='examples' if owned else 'other', author_id='author' if owned else 'other')
            state = ['pending', 'published', 'rejected', 'withdrawn'][index % 4]
            conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)', (f'{index:032x}', release['namespace'], release['package_id'], release['version'], json.dumps(release), b'owned synthetic pagination bytes', release['author_id'], state))
    return env


def test_literal_filters_role_scope_and_pending_only(populated):
    _store, client, author, moderator, other, _key = populated
    path = '/catalog/v1/publish/submissions'
    result = client.get(path, headers=moderator, params={'q': '%_', 'type': 'persona', 'namespace': 'examples', 'state': 'pending'}).json()
    assert result['total'] == 7
    assert all(item['state'] == 'pending' and item['release']['namespace'] == 'examples' for item in result['items'])
    assert client.get(path, headers=author, params={'namespace': 'other', 'q': '%_'}).json()['total'] == 0
    assert client.get(path, headers=other, params={'q': '%_'}).json()['total'] == 1
    queue = client.get('/catalog/v1/moderation/reviews', headers=moderator, params={'q': 'reader'}).json()
    assert queue['total'] == 7 and all('state' not in item for item in queue['items'])
    assert client.get('/catalog/v1/moderation/reviews', headers=author).status_code == 403
    assert client.get(path, headers=moderator, params={'q': "%' OR 1=1 --"}).json()['total'] == 0


def test_filtered_pagination_and_old_exact_contract(populated):
    _store, client, author, moderator, _other, _key = populated
    path = '/catalog/v1/publish/submissions'
    pages = [client.get(path, headers=moderator, params={'q': 'Reader', 'limit': 20, 'offset': offset}).json() for offset in (0, 20)]
    assert pages[0]['total'] == pages[1]['total'] == 27
    assert [len(page['items']) for page in pages] == [20, 7]
    ids = [item['submission_id'] for page in pages for item in page['items']]
    assert ids == sorted(set(ids))
    assert client.get(path, headers=author).json()['total'] == 26
    exact = client.get(path, headers=moderator, params={'package_id': 'lesson-3', 'version': '1.0.0'}).json()
    assert exact['total'] == 1 and exact['items'][0]['release']['package_id'] == 'lesson-3'


def test_queue_queries_use_readonly_connection_without_audit_writes(populated, monkeypatch):
    store, client, author, moderator, _other, _key = populated
    original = store.connect
    @contextmanager
    def only_read(*, write=True):
        assert write is False
        with original(write=False) as conn:
            conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} else sqlite3.SQLITE_OK)
            yield conn
    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', only_read)
        assert client.get('/catalog/v1/publish/submissions', headers=author, params={'q': '%_'}).status_code == 200
        assert client.get('/catalog/v1/moderation/reviews', headers=moderator, params={'type': 'persona'}).status_code == 200
    with original(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0] == 0


def test_queue_parameter_limits_and_enums(populated):
    _store, client, _author, moderator, _other, _key = populated
    for path in ['/catalog/v1/publish/submissions', '/catalog/v1/moderation/reviews']:
        for params in [{'q': 'x' * 121}, {'namespace': 'x' * 65}, {'type': 'executable'}]:
            assert client.get(path, headers=moderator, params=params).status_code == 422
    assert client.get('/catalog/v1/publish/submissions', headers=moderator, params={'state': 'deleted'}).status_code == 422
