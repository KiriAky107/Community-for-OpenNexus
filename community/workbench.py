"""Bounded archive inspection and immutable action receipts; no package execution."""
import hashlib
import json
import re
import sqlite3

from .package import Release, inspect, verify

KINDS = {'submit', 'review', 'withdraw', 'report', 'resolve-report', 'revoke-key'}
STATES = {'submit': {'pending'}, 'review': {'published', 'rejected'}, 'withdraw': {'withdrawn'}, 'report': {'reported'}, 'resolve-report': {'resolved'}, 'revoke-key': {'revoked'}}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def release_digest(release):
    return digest(release.model_dump())


def receipt(row):
    value = json.loads(row['reason'])
    result = value['result']
    if (value.get('schema_version') != 1 or value.get('kind') not in KINDS or not re.fullmatch('[a-f0-9]{64}', value.get('fingerprint', ''))
            or not isinstance(result, dict) or result.get('schema_version') != 1 or result.get('operation_id') != row['subject']
            or not re.fullmatch('[a-f0-9]{32}', row['subject']) or result.get('kind') != value['kind']
            or not isinstance(result.get('target'), str) or not 1 <= len(result['target']) <= 256
            or result.get('state') not in STATES[value['kind']]
            or type(result.get('confirmed_at')) is not int or result['confirmed_at'] < 0):
        raise ValueError('INVALID_OPERATION_RECEIPT')
    return value


def inspection(conn, release, blob):
    key = conn.execute('SELECT * FROM keys WHERE id=? AND namespace=?', (release.key_id, release.namespace)).fetchone()
    if not key: raise ValueError('UNKNOWN_SIGNER')
    verify(release, key['public_key'])
    return {**inspect(release, blob, details=True), 'archive_sha256': release.sha256, 'archive_bytes': len(blob),
            'review_digest': release_digest(release), 'signer_revoked': bool(key['revoked']), 'signature_verified': True}


def details(conn, item, *, offset=0, limit=100):
    release = Release.model_validate_json(item['metadata'])
    checked = inspection(conn, release, item['blob'])
    current_files = checked.pop('file_list')
    checked['file_page'] = {'total': len(current_files), 'offset': offset, 'limit': limit, 'items': current_files[offset:offset + limit]}
    prior = conn.execute("SELECT id,metadata,blob FROM submissions WHERE namespace=? AND package_id=? AND state='published' AND id<>? AND version COLLATE COMMUNITY_SEMVER < ? COLLATE COMMUNITY_SEMVER ORDER BY version COLLATE COMMUNITY_SEMVER DESC,version,id LIMIT 1", (release.namespace, release.package_id, item['id'], release.version)).fetchone()
    change = None
    if prior:
        try:
            previous = Release.model_validate_json(prior['metadata'])
            old = inspection(conn, previous, prior['blob'])
            before = {entry['path']: entry for entry in old['file_list']}
            after = {entry['path']: entry for entry in current_files}
            changes = [{'path': path, 'change': 'added' if path not in before else 'removed' if path not in after else 'changed', 'before': before.get(path), 'after': after.get(path)}
                       for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)]
            a, b = previous.model_dump(), release.model_dump()
            change = {'release_id': prior['id'], 'version': previous.version, 'available': True,
                      'metadata_fields': sorted(field for field in b if a.get(field) != b[field]),
                      'permissions_added': sorted(set(release.permissions) - set(previous.permissions)),
                      'permissions_removed': sorted(set(previous.permissions) - set(release.permissions)),
                      'file_page': {'total': len(changes), 'offset': offset, 'limit': limit, 'items': changes[offset:offset + limit]}}
        except Exception as exc:
            if isinstance(exc, sqlite3.Error): raise
            change = {'release_id': prior['id'], 'available': False}
    history = [dict(row) for row in conn.execute("SELECT id,actor,action,reason,timestamp FROM audit WHERE subject=? AND action IN ('published','rejected','withdraw') ORDER BY id DESC LIMIT 20", (item['id'],))]
    return {'schema_version': 1, 'submission_id': item['id'], 'state': item['state'], 'release': release.model_dump(),
            'inspection': checked, 'previous': change, 'decisions': history}
