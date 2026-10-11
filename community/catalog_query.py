"""Bounded public metadata pages. Archive BLOBs never enter catalog queries."""
from functools import lru_cache
import json

from semver import Version

KINDS = {"theme", "skill", "plugin", "mcp", "persona", "template", "model"}
VISIBLE = "state IN ('published','withdrawn')"
PROJECTION = "id,metadata,state"

@lru_cache(maxsize=2048)
def _version(value):
    try:
        return Version.parse(value)
    except ValueError:
        return None

def compare_versions(first, second):
    # Previous regex validation permitted some non-SemVer values. Keep those
    # immutable records readable and order them after valid releases, without
    # making them eligible for installation or modifying their signed bytes.
    a = _version(first) if len(first) <= 120 else None
    b = _version(second) if len(second) <= 120 else None
    if a is not None and b is not None:
        return a.compare(b)
    if a is not None: return 1
    if b is not None: return -1
    return (first > second) - (first < second)

def configure_connection(conn):
    conn.create_collation("COMMUNITY_SEMVER", compare_versions)
    conn.create_function("community_casefold", 1, lambda value: (value or "").casefold(), deterministic=True)
    conn.create_function('community_valid_version', 1, valid_version, deterministic=True)
    conn.create_function('community_app_matches', 2, app_matches, deterministic=True)


def valid_version(value):
    return int(isinstance(value, str) and len(value) <= 120 and _version(value) is not None)


def app_matches(metadata, requested):
    try:
        value = json.loads(metadata)
        minimum = _version(value.get('min_app_version', ''))
        maximum = _version(value['max_app_version']) if value.get('max_app_version') is not None else None
        current = _version(requested)
        return int(current is not None and minimum is not None and current >= minimum
                   and (value.get('max_app_version') is None or maximum is not None and minimum <= maximum and current <= maximum))
    except (ValueError, TypeError):
        return 0

def ensure_indexes(conn):
    # Additive builtin-only expression indexes preserve the original eight-column
    # submissions table and remain writable by older service connections.
    conn.execute(f"CREATE INDEX IF NOT EXISTS catalog_visible_order ON submissions(namespace,package_id,version,id) WHERE {VISIBLE}")
    conn.execute(f"CREATE INDEX IF NOT EXISTS catalog_visible_type ON submissions(json_extract(metadata,'$.type'),namespace,package_id) WHERE {VISIBLE}")

def public(item):
    metadata = json.loads(item["metadata"])
    return {**metadata, "release_id": item["id"], "withdrawn": item["state"] == "withdrawn",
            "download_path": "/catalog/v1/releases/" + item["id"] + "/archive"}

def page(conn, *, query="", kind=None, namespace=None, package_id=None, version=None, offset=0, limit=30):
    conditions, parameters = [VISIBLE], []
    if kind is not None:
        conditions.append("json_extract(metadata,'$.type')=?")
        parameters.append(kind)
    if query:
        # Literal substring matching, including Unicode casefold and %/_/quotes.
        # Never interpolate user input into SQL or treat it as LIKE syntax.
        conditions.append("instr(community_casefold(json_extract(metadata,'$.name') || ' ' || json_extract(metadata,'$.description')),?)>0")
        parameters.append(query.casefold())
    if namespace is not None:
        conditions.extend(["namespace=?", "package_id=?"])
        parameters.extend([namespace, package_id])
    if version is not None:
        conditions.append("version=?")
        parameters.append(version)
    where = " AND ".join(conditions)
    total = conn.execute(f"SELECT COUNT(*) FROM submissions WHERE {where}", parameters).fetchone()[0]
    rows = conn.execute(f"SELECT {PROJECTION} FROM submissions WHERE {where} ORDER BY namespace,package_id,version COLLATE COMMUNITY_SEMVER DESC,version,id LIMIT ? OFFSET ?", [*parameters, limit, offset])
    return {"schema_version": 1, "items": [public(item) for item in rows], "total": total, "offset": offset, "limit": limit}


def group_page(conn, *, query='', kind=None, app_version=None, platform=None, architecture=None, offset=0, limit=30):
    """Rank metadata in SQLite; only deserialize the requested package page.

    Search matches the selected card's text. Compatibility filters restrict the
    candidates, without changing the original release API or signed metadata.
    """
    conditions, parameters = [], []
    if kind is not None:
        conditions.append("json_extract(metadata,'$.type')=?")
        parameters.append(kind)
    if app_version is not None:
        conditions.append('community_app_matches(metadata,?)=1')
        parameters.append(app_version)
    for field, value in [('platforms', platform), ('architectures', architecture)]:
        if value is not None:
            conditions.append(f"EXISTS (SELECT 1 FROM json_each(metadata,'$.{field}') WHERE value=?)")
            parameters.append(value)
    matches = ' AND '.join(conditions) or '1'
    cte = f"""WITH visible AS (
        SELECT id,namespace,package_id,version,metadata,state,
          COUNT(*) OVER (PARTITION BY namespace,package_id) AS version_count,
          FIRST_VALUE(version) OVER (PARTITION BY namespace,package_id ORDER BY
            version COLLATE COMMUNITY_SEMVER DESC,version,id) AS latest_version,
          CASE WHEN community_valid_version(version)=0 OR
                    community_app_matches(metadata,json_extract(metadata,'$.min_app_version'))=0 THEN 'invalid_version'
               WHEN state='withdrawn' THEN 'withdrawn'
               WHEN NOT EXISTS (SELECT 1 FROM keys WHERE keys.id=json_extract(metadata,'$.key_id')
                 AND keys.namespace=submissions.namespace) THEN 'key_missing'
               WHEN EXISTS (SELECT 1 FROM keys WHERE keys.id=json_extract(metadata,'$.key_id')
                 AND keys.namespace=submissions.namespace AND keys.revoked!=0) THEN 'key_revoked'
               ELSE 'available' END AS availability
        FROM submissions WHERE {VISIBLE}
      ), matching AS (
        SELECT id,namespace,package_id,version,metadata,state,version_count,latest_version,availability
        FROM visible WHERE {matches}
      ), ranked AS (
        SELECT id,namespace,package_id,version,metadata,state,version_count,latest_version,availability,
          COUNT(*) OVER (PARTITION BY namespace,package_id) AS matching_version_count,
          ROW_NUMBER() OVER (PARTITION BY namespace,package_id ORDER BY
            (availability='available') DESC,version COLLATE COMMUNITY_SEMVER DESC,version,id) AS position
        FROM matching
      )"""
    where = 'position=1'
    if query:
        where += " AND instr(community_casefold(json_extract(metadata,'$.name') || ' ' || json_extract(metadata,'$.description')),?)>0"
        parameters.append(query.casefold())
    total = conn.execute(f'{cte} SELECT COUNT(*) FROM ranked WHERE {where}', parameters).fetchone()[0]
    selected = conn.execute(f"{cte} SELECT id,namespace,package_id,version,metadata,state,version_count,latest_version,availability,matching_version_count FROM ranked WHERE {where} ORDER BY namespace,package_id LIMIT ? OFFSET ?", [*parameters, limit, offset])
    items = []
    for item in selected:
        selection = 'unavailable' if item['availability'] != 'available' else ('latest' if item['version'] == item['latest_version'] else 'earlier_available')
        items.append({'namespace': item['namespace'], 'package_id': item['package_id'],
                      'release': public(item), 'version_count': item['version_count'],
                      'matching_version_count': item['matching_version_count'], 'latest_version': item['latest_version'],
                      'availability': item['availability'], 'selection': selection})
    return {'schema_version': 1, 'items': items, 'total': total, 'offset': offset, 'limit': limit}
