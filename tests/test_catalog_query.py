"""Real SQLite/HTTP pages, semantic ordering, read snapshots and legacy writes."""
import json
import sqlite3
from functools import cmp_to_key
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from community.package import Release
from community.catalog_query import compare_versions, page

def seed(registry, count=135):
    with registry.connect() as conn:
        for index in range(count):
            metadata = {"schema_version":1,"namespace":"examples","package_id":f"item-{index:04}","version":"1.0.0","name":f"课程 {index}","description":"Straße 100%_ ' quoted", "type":"persona" if index%2 == 0 else "template", "min_app_version":"0.5.9-beta2","max_app_version":"0.6.0","platforms":["windows"],"architectures":["x86_64"]}
            conn.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)", (f"id-{index:04}","examples",metadata['package_id'],metadata['version'],json.dumps(metadata), b"ARCHIVE-DO-NOT-READ"*100,"author","published"))

def test_more_than_100_items_exact_pages_etags_and_literal_unicode_query(tmp_path):
    registry = Registry(tmp_path/'catalog.db')
    seed(registry)
    with TestClient(create_app(registry)) as client:
        first = client.get('/catalog/v1/packages',params={'limit':100})
        second = client.get('/catalog/v1/packages',params={'limit':100,'offset':100})
        assert first.json()['total'] == second.json()['total'] == 135
        assert len(first.json()['items']) == 100 and len(second.json()['items']) == 35
        ids = [item['release_id'] for item in first.json()['items']+second.json()['items']]
        assert len(set(ids)) == 135 and ids == [f'id-{n:04}' for n in range(135)]
        assert client.get('/catalog/v1/packages',params={'limit':100},headers={'If-None-Match':first.headers['etag']}).status_code == 304
        assert client.get('/catalog/v1/packages',params={'limit':100,'offset':100},headers={'If-None-Match':first.headers['etag']}).status_code == 200
        for query in ['STRASSE','100%_',"' quoted"]:
            filtered = client.get('/catalog/v1/packages',params={'q':query,'type':'persona','limit':7})
            assert filtered.json()['total'] == 68 and len(filtered.json()['items']) == 7
        assert client.get('/catalog/v1/packages',params={'q':"' OR 1=1 --"}).json()['total'] == 0
        assert client.get('/catalog/v1/packages',params={'type':'unknown'}).status_code == 422
        assert client.get('/catalog/v1/packages',params={'offset':2**40}).status_code == 422
        release = client.get('/catalog/v1/releases/id-0129')
        assert release.status_code == 200 and release.headers['cache-control'] == 'no-store'
        assert release.json()['min_app_version'] == '0.5.9-beta2'
        assert client.get('/catalog/v1/releases/missing').status_code == 404

def test_sql_limits_and_counts_without_loading_archive_and_reuses_index(tmp_path):
    registry = Registry(tmp_path/'catalog.db')
    seed(registry,1001)
    traced = []
    with registry.connect(write=False) as conn:
        conn.set_trace_callback(traced.append)
        # Deny all catalog access to the archive column, including SELECT *.
        conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'submissions' and column == 'blob' else sqlite3.SQLITE_OK)
        result = page(conn,kind='persona',offset=490,limit=3)
        assert result['total'] == 501 and len(result['items']) == 3
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT id,metadata,state FROM submissions WHERE state IN ('published','withdrawn') AND json_extract(metadata,'$.type')='persona'").fetchall()
        assert any('catalog_visible_type' in row['detail'] for row in plan)
    assert any('COUNT(*)' in sql for sql in traced)
    assert any('LIMIT 3 OFFSET 490' in sql for sql in traced)
    assert not any('SELECT *' in sql for sql in traced)

def test_semantic_versions_build_ties_withdrawn_legacy_and_immutable_metadata(tmp_path):
    registry = Registry(tmp_path/'catalog.db')
    versions = ['1.9.0','1.10.0','1.10.0-beta.2','1.10.0-beta.11','1.10.0+build.2','1.10.0+build.1','01.99.0']
    originals = {}
    with registry.connect() as conn:
        for index, version in enumerate(versions):
            raw = json.dumps({'namespace':'examples','package_id':'versions','version':version,'name':'versions','description':'','type':'persona'})
            originals[str(index)] = raw
            conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)',(str(index),'examples','versions',version,raw,b'archive','author','withdrawn' if index==1 else 'published'))
        conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)',('pending','examples','versions','9.0.0',raw,b'archive','author','pending'))
    with TestClient(create_app(registry)) as client:
        response = client.get('/catalog/v1/packages/examples/versions/releases')
        assert [r['version'] for r in response.json()['items']] == ['1.10.0','1.10.0+build.1','1.10.0+build.2','1.10.0-beta.11','1.10.0-beta.2','1.9.0','01.99.0']
        assert response.json()['items'][0]['withdrawn']
        first = client.get('/catalog/v1/packages/examples/versions/releases',params={'limit':2})
        following = client.get('/catalog/v1/packages/examples/versions/releases',params={'limit':2,'offset':2})
        assert first.json()['total'] == 7 and first.json()['items'] != following.json()['items']
    # An old connection can still insert its original eight-column schema.
    with sqlite3.connect(registry.path) as legacy:
        legacy.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)',('legacy','examples','legacy','1.0.0',raw,b'archive','author','pending'))
    reopened = Registry(registry.path)
    with reopened.connect(write=False) as conn:
        assert dict(conn.execute("SELECT id,metadata FROM submissions WHERE package_id='versions' AND id!='pending'")) == originals

@pytest.mark.parametrize('version',['01.2.3','1.0.0-01','1.0.0-alpha..1','1.0','1.0.0+'])
def test_new_release_semver_validation_rejects_noncanonical(version):
    from tests.test_catalog import package
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    payload = package(Ed25519PrivateKey.generate())['release']
    with pytest.raises(ValueError): Release(**{**payload,'version':version})

def test_rejects_inverted_compatibility_and_accepts_build_version():
    from tests.test_catalog import package
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    payload = package(Ed25519PrivateKey.generate())['release']
    assert Release(**{**payload,'version':'1.0.0+build.1'}).version == '1.0.0+build.1'
    with pytest.raises(ValueError): Release(**{**payload,'min_app_version':'0.6.0','max_app_version':'0.5.9'})

def test_shared_desktop_contract_orders_versions_and_exposes_last_page(tmp_path):
    fixture = json.loads((Path(__file__).parent/'fixtures/community-v1-catalog.json').read_text('utf-8'))
    def descending(a, b):
        semantic = compare_versions(a, b)
        return -semantic if semantic else (a > b) - (a < b)
    assert sorted(fixture['versions'],key=cmp_to_key(descending)) == fixture['descending']
    from tests.test_catalog import package
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    payload = package(Ed25519PrivateKey.generate())['release']
    for version in fixture['invalid']:
        with pytest.raises(ValueError): Release(**{**payload,'version':version})
    registry = Registry(tmp_path/'catalog.db')
    seed(registry,fixture['catalog_total'])
    with TestClient(create_app(registry)) as client:
        result = client.get('/catalog/v1/packages',params={'limit':fixture['page_limit'],'offset':fixture['last_offset']}).json()
        assert result['total'] == fixture['catalog_total']
        assert len(result['items']) == fixture['last_count']

def test_exact_old_version_after_first_hundred_preserves_plus_and_withdrawal(tmp_path):
    registry = Registry(tmp_path/'catalog.db')
    with registry.connect() as conn:
        for index in range(135):
            version = '1.0.0+build.1' if index == 0 else f'2.0.{index}'
            metadata = {'namespace':'examples','package_id':'family','version':version,'name':'family','description':'','type':'persona'}
            conn.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)',(f'family-{index}','examples','family',version,json.dumps(metadata),b'archive','author','withdrawn' if index==0 else 'published'))
    with TestClient(create_app(registry)) as client:
        path = '/catalog/v1/packages/examples/family/releases'
        first = client.get(path).json()
        assert first['total'] == 135 and len(first['items']) == 100
        assert '1.0.0+build.1' not in [item['version'] for item in first['items']]
        exact = client.get(path,params={'version':'1.0.0+build.1','limit':100})
        assert exact.headers['cache-control'] == 'no-store'
        assert exact.json()['total'] == 1 and exact.json()['offset'] == 0
        assert exact.json()['items'][0]['withdrawn']
        assert exact.json()['items'][0]['version'] == '1.0.0+build.1'
        assert client.get(path,params={'version':"' OR 1=1 --"}).json()['total'] == 0
        assert client.get(path,params={'version':'a'*121}).status_code == 422
