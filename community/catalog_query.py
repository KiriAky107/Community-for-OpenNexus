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
