"""Literal, role-scoped read-only queue filters shared by both list routes."""
import json


def queue_page(conn, *, author_id=None, pending=False, q='', kind=None,
               namespace='', state=None, package_id=None, version=None,
               offset=0, limit=30):
    conditions, values = [], []
    if author_id is not None:
        conditions.append('author_id=?')
        values.append(author_id)
    if pending or state is not None:
        conditions.append('state=?')
        values.append('pending' if pending else state)
    for field, value in [('namespace', namespace), ('package_id', package_id), ('version', version)]:
        if value is not None and (value or field != 'namespace'):
            conditions.append(field + '=?')
            values.append(value)
    if kind:
        conditions.append("json_extract(metadata,'$.type')=?")
        values.append(kind)
    if q:
        conditions.append("(instr(lower(package_id),lower(?))>0 OR instr(lower(json_extract(metadata,'$.name')),lower(?))>0 OR instr(lower(json_extract(metadata,'$.description')),lower(?))>0)")
        values.extend([q, q, q])
    where = ' AND '.join(conditions) or '1'
    total = conn.execute(f'SELECT COUNT(*) FROM submissions WHERE {where}', values).fetchone()[0]
    entries = conn.execute(f'SELECT id,metadata,state FROM submissions WHERE {where} ORDER BY id LIMIT ? OFFSET ?', [*values, limit, offset])
    items = [{'submission_id': item['id'], 'release': json.loads(item['metadata']), **({} if pending else {'state': item['state']})} for item in entries]
    return {'schema_version': 1, 'total': total, 'offset': offset, 'limit': limit, 'items': items}
