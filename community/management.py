"""Role-scoped, read-only audit and signing-key inspection."""
import base64
import hashlib
import time

from .workbench import digest


def audit_page(conn, *, after=0, before=None, limit=30, actor='', action='', subject='', q=''):
    descending = before is not None
    conditions = ['id<?' if descending else 'id>?']
    values = [before if descending else after]
    for field, value in [('actor', actor), ('action', action), ('subject', subject)]:
        if value:
            conditions.append(f'{field}=?')
            values.append(value)
    if q:
        conditions.append('(instr(actor,?) OR instr(action,?) OR instr(subject,?) OR instr(reason,?))')
        values.extend([q] * 4)
    rows = conn.execute(f"SELECT id,actor,action,subject,reason,timestamp FROM audit WHERE {' AND '.join(conditions)} ORDER BY id {'DESC' if descending else 'ASC'} LIMIT ?", [*values, limit + 1]).fetchall()
    return {'schema_version': 1, 'items': [dict(row) for row in rows[:limit]], 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}


def key_state(row):
    value = {'key_id': row['id'], 'namespace': row['namespace'], 'public_key': base64.b64encode(row['public_key']).decode(), 'revoked': bool(row['revoked'])}
    return {**value, 'fingerprint': hashlib.sha256(row['public_key']).hexdigest(), 'review_digest': digest(value)}


def key_page(conn, actor, *, q='', state=None, offset=0, limit=30):
    conditions, values = [], []
    if actor['role'] != 'moderator':
        # NULL authors must never gain access to unscoped keys.
        conditions.append('namespace=?')
        values.append(actor['namespace'])
    if q:
        conditions.append('(instr(id,?) OR instr(namespace,?))')
        values.extend([q, q])
    if state is not None:
        conditions.append('revoked=?')
        values.append(int(state == 'revoked'))
    where = ' AND '.join(conditions) or '1'
    total = conn.execute(f'SELECT COUNT(*) FROM keys WHERE {where}', values).fetchone()[0]
    rows = conn.execute(f'SELECT id,namespace,public_key,revoked FROM keys WHERE {where} ORDER BY id LIMIT ? OFFSET ?', [*values, limit, offset])
    return {'schema_version': 1, 'total': total, 'offset': offset, 'limit': limit, 'items': [key_state(row) for row in rows], 'observed_at': int(time.time())}
