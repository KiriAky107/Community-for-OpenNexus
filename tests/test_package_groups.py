"""Real grouped directory queries: compatibility, revocation and legacy API."""
import json
import sqlite3

from fastapi.testclient import TestClient

from community.app import Registry, create_app
from community.catalog_query import group_page


def insert(conn, identity, package, version='1.0.0', *, state='published', key='active', **fields):
    metadata = {'schema_version': 1, 'namespace': 'examples', 'package_id': package, 'version': version,
                'name': package, 'description': "Straße 100%_ ' quoted", 'type': 'persona', 'key_id': key,
                'min_app_version': '0.6.0-beta1', 'max_app_version': None,
                'platforms': ['windows'], 'architectures': ['x86_64'], **fields}
    raw = json.dumps(metadata, ensure_ascii=False)
    conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)', (identity, 'examples', package, version, raw, b'UNCHANGED-ARCHIVE', 'author', state))
    return raw


def fixture(tmp_path):
    registry = Registry(tmp_path / 'catalog.db')
    with registry.connect() as conn:
        conn.execute('INSERT INTO keys VALUES (?,?,?,?)', ('active', 'examples', b'A'*32, 0))
        conn.execute('INSERT INTO keys VALUES (?,?,?,?)', ('revoked', 'examples', b'R'*32, 1))
    return registry


def test_latest_semver_and_explicit_unavailable_and_earlier_selection(tmp_path):
    registry = fixture(tmp_path)
    with registry.connect() as conn:
        insert(conn, 'old', 'family', '1.9.0')
        original = insert(conn, 'new', 'family', '1.10.0')
        insert(conn, 'beta', 'family', '1.10.0-beta.11')
        insert(conn, 'pending', 'family', '9.0.0', state='pending')
        insert(conn, 'blocked-old', 'blocked', '1.0.0', key='revoked')
        insert(conn, 'blocked-new', 'blocked', '2.0.0', state='withdrawn')
        insert(conn, 'missing', 'missing', key='missing')
        insert(conn, 'invalid', 'invalid', '01.2.3')
    with TestClient(create_app(registry)) as client:
        route = '/catalog/v1/package-groups'
        response = client.get(route)
        groups = {item['package_id']: item for item in response.json()['items']}
        assert groups['family']['release']['release_id'] == 'new'
        assert groups['family']['version_count'] == groups['family']['matching_version_count'] == 3
        assert groups['family']['selection'] == 'latest'
        assert groups['blocked']['availability'] == 'withdrawn' and groups['blocked']['selection'] == 'unavailable'
        assert groups['missing']['availability'] == 'key_missing'
        assert groups['invalid']['availability'] == 'invalid_version'
        assert client.get(route, headers={'If-None-Match': response.headers['etag']}).status_code == 304
        with registry.connect() as conn:
            conn.execute("UPDATE submissions SET state='withdrawn' WHERE id='new'")
        changed = client.get(route, headers={'If-None-Match': response.headers['etag']})
        assert changed.status_code == 200 and changed.headers['cache-control'] == 'no-cache'
        family = next(item for item in changed.json()['items'] if item['package_id'] == 'family')
        assert family['release']['release_id'] == 'beta' and family['latest_version'] == '1.10.0'
        assert family['selection'] == 'earlier_available'
        with registry.connect() as conn:
            conn.execute("UPDATE keys SET revoked=1 WHERE id='active'")
        revoked = client.get(route, headers={'If-None-Match': changed.headers['etag']})
        assert revoked.status_code == 200
        assert next(item for item in revoked.json()['items'] if item['package_id'] == 'family')['selection'] == 'unavailable'
        versions = client.get('/catalog/v1/packages/examples/family/releases').json()
        assert versions['total'] == 3 and versions['items'][0]['withdrawn']
        assert client.get('/catalog/v1/packages').json()['total'] == 7
    with registry.connect(write=False) as conn:
        assert conn.execute("SELECT metadata,blob FROM submissions WHERE id='new'").fetchone()[:] == (original, b'UNCHANGED-ARCHIVE')


def test_compatibility_filters_bounds_platforms_and_no_match(tmp_path):
    registry = fixture(tmp_path)
    with registry.connect() as conn:
        insert(conn, 'beta-min', 'versions', '1.0.0', max_app_version='0.6.0')
        insert(conn, 'stable-min', 'versions', '2.0.0', min_app_version='0.6.0', platforms=['linux'], architectures=['aarch64'])
        insert(conn, 'inverted', 'inverted', min_app_version='0.7.0', max_app_version='0.6.0')
    with TestClient(create_app(registry)) as client:
        route = '/catalog/v1/package-groups'
        unfiltered = client.get(route).json()
        assert next(item for item in unfiltered['items'] if item['package_id'] == 'inverted')['availability'] == 'invalid_version'
        beta = client.get(route, params={'app_version':'0.6.0-beta1','platform':'windows','architecture':'x86_64'}).json()
        assert beta['total'] == 1 and beta['items'][0]['release']['release_id'] == 'beta-min'
        assert beta['items'][0]['latest_version'] == '2.0.0' and beta['items'][0]['matching_version_count'] == 1
        assert client.get(route, params={'app_version':'0.5.9'}).json()['total'] == 0
        assert client.get(route, params={'app_version':'0.6.0','platform':'linux','architecture':'aarch64'}).json()['items'][0]['release']['release_id'] == 'stable-min'
        assert client.get(route, params={'app_version':'0.6.1','platform':'windows'}).json()['total'] == 0
        assert client.get(route, params={'platform':'Windows'}).json()['total'] == 0
        for bad in ['0.6', '01.6.0', "' OR 1=1 --"]:
            assert client.get(route, params={'app_version':bad}).status_code == 422


def test_package_pages_metadata_only_literal_search_and_selected_names(tmp_path):
    registry = fixture(tmp_path)
    with registry.connect() as conn:
        for index in range(135):
            insert(conn, f'old-{index:04}', f'item-{index:04}', type='template', name='obsolete-name')
            insert(conn, f'new-{index:04}', f'item-{index:04}', '1.1.0', type='persona')
    with registry.connect(write=False) as conn:
        conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'submissions' and column == 'blob' else sqlite3.SQLITE_OK)
        first = group_page(conn, limit=100)
        second = group_page(conn, offset=100, limit=100)
        assert first['total'] == second['total'] == 135 and len(second['items']) == 35
        assert [i['release']['release_id'] for i in first['items'] + second['items']] == [f'new-{n:04}' for n in range(135)]
        assert group_page(conn, query='obsolete-name')['total'] == 0
        assert group_page(conn, query='obsolete-name', kind='template')['total'] == 135
        for query in ['STRASSE', '100%_', "' quoted"]:
            assert group_page(conn, query=query, kind='persona', offset=130, limit=7)['total'] == 135
        assert group_page(conn, query="' OR 1=1 --")['total'] == 0
    with TestClient(create_app(registry)) as client:
        one = client.get('/catalog/v1/package-groups',params={'limit':3})
        assert client.get('/catalog/v1/package-groups',params={'limit':3,'offset':3},headers={'If-None-Match':one.headers['etag']}).status_code == 200
        assert client.get('/catalog/v1/package-groups',params={'offset':2**40}).status_code == 422
        assert client.get('/catalog/v1/package-groups',params={'type':'unknown'}).status_code == 422
