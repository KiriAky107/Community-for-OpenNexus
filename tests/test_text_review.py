"""Actual signed before/after text, bounded output and actor/snapshot isolation."""
import base64
import hashlib
import sqlite3
from contextlib import contextmanager

from community.package import Release, signed_payload
from tests.test_workbench import env, revised


def versions(env, files, old_files=None, metadata=None):
    store, client, author, moderator, other, key = env
    original = revised(key, '1.0.0', old_files or {'persona.json': '{"system_prompt":"old"}', 'read me.md': 'old\n'})
    prior = client.post('/catalog/v1/publish/submissions', headers=author, json=original).json()['submission_id']
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json={'submission_id': prior, 'approve': True, 'reason': 'independent first check'}).status_code == 200
    current = revised(key, '2.0.0', files)
    if metadata:
        release = Release.model_validate({**current['release'], **metadata})
        release.signature = base64.b64encode(key.sign(signed_payload(release))).decode()
        current['release'] = release.model_dump()
    identifier = client.post('/catalog/v1/publish/submissions', headers=author, json=current).json()['submission_id']
    detail = client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection', headers=moderator).json()
    params = {'expected_sha256': detail['inspection']['review_digest'], 'previous_release_id': prior}
    return identifier, detail, params, original, current


def test_text_and_key_changes_are_actual_inert_bytes_and_readonly(env, monkeypatch):
    store, client, author, moderator, other, key = env
    files = {'persona.json': '{"system_prompt":"new","name":"Changed"}', 'read me.md': '<script>alert("literal")</script>\nnew\n', 'added.py': 'raise RuntimeError("never executed")'}
    identifier, detail, params, original, current = versions(env, files, metadata={'min_app_version': '0.6.0-beta1', 'dependencies': {'examples/base': '>=1,<2'}})
    assert {entry['field'] for entry in detail['previous']['key_changes']} == {'min_app_version', 'dependencies'}
    manifest = detail['previous']['manifest_changes']
    assert manifest['status'] == 'available' and manifest['total'] == 2
    assert next(x for x in manifest['items'] if x['field'] == 'system_prompt')['before']['value'] == '"old"'
    connect = store.connect
    @contextmanager
    def readonly(*, write=True):
        assert write is False
        with connect(write=False) as conn:
            conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} else sqlite3.SQLITE_OK)
            yield conn
    path = f'/catalog/v1/publish/submissions/{identifier}/inspection/text'
    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', readonly)
        reply = client.get(path, params={**params, 'path': 'read me.md'}, headers=author)
        assert reply.status_code == 200, reply.text
        data = reply.json()
        assert data['before']['text'] == 'old\n' and data['after']['text'] == files['read me.md']
        assert data['after']['file']['sha256'] == hashlib.sha256(files['read me.md'].encode()).hexdigest()
        assert '-old\n' in data['diff']['unified'] and '+<script>' in data['diff']['unified']
        assert reply.headers['cache-control'] == 'private, no-store'
        assert client.get(path, params={**params, 'path': 'added.py'}, headers=moderator).json()['before']['status'] == 'missing'
        assert client.get(path, params={**params, 'path': 'read me.md'}, headers=other).status_code == 403
        assert client.get(path, params={**params, 'path': 'read me.md'}).status_code == 401
    with connect(write=False) as conn:
        stored = conn.execute('SELECT metadata,blob FROM submissions WHERE id=?', (identifier,)).fetchone()
        assert stored['blob'] == base64.b64decode(current['archive_base64'])
    assert client.get(path, params={**params, 'path': '../read me.md'}, headers=author).json()['error']['code'] == 'FILE_NOT_FOUND'
    assert client.get(path, params={**params, 'path': 'read me.md', 'expected_sha256': 'f' * 64}, headers=author).status_code == 409


def test_explicit_binary_encoding_size_and_line_limits_with_truncated_diff(env):
    _, client, author, moderator, *_ = env
    files = {'persona.json': '{"system_prompt":"new"}', 'binary.txt': b'a\x00b', 'bad.txt': b'\xff', 'image.png': b'\x89PNG',
             'large.md': 'x' * 32769, 'lines.txt': '\n' * 1001, 'many.txt': ''.join(f'new {i}\n' for i in range(300)),
             'bom.txt': b'\xef\xbb\xbfhello', 'empty.txt': ''}
    identifier, _, params, *_ = versions(env, files, {'persona.json': '{"system_prompt":"old"}', 'many.txt': ''.join(f'old {i}\n' for i in range(300)), 'removed.md': 'removed'})
    path = f'/catalog/v1/publish/submissions/{identifier}/inspection/text'
    expected = {'binary.txt': 'binary', 'bad.txt': 'invalid_encoding', 'image.png': 'unsupported_type', 'large.md': 'too_large', 'lines.txt': 'too_many_lines'}
    for name, status in expected.items():
        data = client.get(path, params={**params, 'path': name}, headers=moderator).json()
        assert data['after']['status'] == status and data['after']['text'] is None
        assert data['diff']['status'] == 'unavailable' and data['diff']['unified'] == ''
    data = client.get(path, params={**params, 'path': 'many.txt'}, headers=author).json()
    assert data['diff']['truncated'] and len(data['diff']['unified'].encode()) <= 32768
    assert len(data['diff']['unified'].splitlines()) <= 200
    assert len(data['before']['text'].encode()) <= 32768
    assert client.get(path, params={**params, 'path': 'removed.md'}, headers=author).json()['after']['status'] == 'missing'
    assert client.get(path, params={**params, 'path': 'bom.txt'}, headers=author).json()['after']['text'] == 'hello'
    assert client.get(path, params={**params, 'path': 'empty.txt'}, headers=author).json()['after']['status'] == 'text'


def test_changed_baseline_is_rejected_and_unverifiable_prior_is_explicit(env):
    store, client, author, moderator, _, key = env
    identifier, _, params, *_ = versions(env, {'persona.json': '{"system_prompt":"new"}', 'read me.md': 'new'})
    path = f'/catalog/v1/publish/submissions/{identifier}/inspection/text'
    with store.connect() as conn:
        conn.execute('UPDATE submissions SET blob=zeroblob(length(blob)) WHERE id=?', (params['previous_release_id'],))
    result = client.get(path, params={**params, 'path': 'read me.md'}, headers=moderator).json()
    assert result['before']['status'] == 'previous_unavailable' and result['diff']['status'] == 'unavailable'
    assert result['after']['text'] == 'new'
    middle = revised(key, '1.5.0', {'persona.json': '{"system_prompt":"middle"}'})
    mid_id = client.post('/catalog/v1/publish/submissions', headers=author, json=middle).json()['submission_id']
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator, json={'submission_id': mid_id, 'approve': True, 'reason': 'new prior independently checked'}).status_code == 200
    assert client.get(path, params={**params, 'path': 'read me.md'}, headers=author).json()['error']['code'] == 'REVIEW_CHANGED'


def test_first_release_text_and_manifest_summary_bounds(env):
    _, client, author, moderator, _, key = env
    data = revised(key, '1.0.0', {'persona.json': '{"system_prompt":"first"}', 'first.md': 'no newline'})
    identifier = client.post('/catalog/v1/publish/submissions', headers=author, json=data).json()['submission_id']
    detail = client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection', headers=author).json()
    result = client.get(f'/catalog/v1/publish/submissions/{identifier}/inspection/text', params={'path': 'first.md', 'expected_sha256': detail['inspection']['review_digest']}, headers=moderator).json()
    assert result['previous_release_id'] is None and result['before']['status'] == 'missing'
    assert '\\ No newline at end of file' in result['diff']['unified']
    from community.text_review import manifest_changes
    import json
    result = manifest_changes(b'{}', json.dumps({str(i): 'x' * 3000 if i == 0 else i for i in range(45)}).encode())
    assert result['total'] == 45 and len(result['items']) == 40
    assert result['items'][0]['after']['truncated'] and len(result['items'][0]['after']['value']) == 2048
    result = manifest_changes(b'{"system_prompt":"old"}', json.dumps({'system_prompt': 'new', **{str(i): i for i in range(45)}}).encode())
    assert result['items'][0]['field'] == 'system_prompt'
