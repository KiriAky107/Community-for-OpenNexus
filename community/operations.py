"""Readiness and verified SQLite snapshots, independent of account mutation."""
from contextlib import closing
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .package import Release
from .sessions import SESSION_COLUMNS

TABLES = {
    'schema_version': ('version',),
    'principals': ('id', 'token_hash', 'role', 'namespace', 'revoked'),
    'keys': ('id', 'namespace', 'public_key', 'revoked'),
    'submissions': ('id', 'namespace', 'package_id', 'version', 'metadata', 'blob', 'author_id', 'state'),
    'audit': ('id', 'actor', 'action', 'subject', 'reason', 'timestamp'),
}
OPTIONAL_TABLES = {'web_sessions': SESSION_COLUMNS}


class OperationError(Exception):
    pass


def regular(path):
    path = Path(path).absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OperationError('REGULAR_FILE_REQUIRED')
    return path


def connect_existing(path, *, writable=False):
    # mode=ro/rw never initializes a missing database or migrates its schema.
    path = regular(path)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute('PRAGMA query_only=ON')
    return conn


def schema(conn):
    if [row[0] for row in conn.execute('SELECT version FROM schema_version')] != [1]:
        raise OperationError('DATABASE_SCHEMA_UNSUPPORTED')
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if names - set(TABLES) - set(OPTIONAL_TABLES) - {'sqlite_sequence'} or set(TABLES) - names:
        raise OperationError('DATABASE_SCHEMA_UNSUPPORTED')
    for name, columns in {**TABLES, **OPTIONAL_TABLES}.items():
        if name not in names: continue
        if tuple(row[1] for row in conn.execute(f'PRAGMA table_info({name})')) != columns:
            raise OperationError('DATABASE_SCHEMA_UNSUPPORTED')


def readiness(path):
    try:
        with closing(connect_existing(path, writable=True)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            schema(conn)
            conn.execute('UPDATE schema_version SET version=version WHERE 0')
            conn.execute('SELECT id FROM submissions LIMIT 1').fetchall()
            conn.rollback()
        return {'status': 'ready', 'database_schema': 1, 'archive_storage': 'sqlite'}
    except (OSError, sqlite3.Error, OperationError):
        # Paths, SQL, account hashes and environment values stay private.
        return None


def checksum(path):
    value = hashlib.sha256()
    with regular(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OperationError('DATABASE_METADATA_INVALID')
        result[key] = value
    return result


def inspect_database(path):
    with closing(connect_existing(path)) as conn:
        conn.execute('BEGIN')
        schema(conn)
        if [row[0] for row in conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise OperationError('DATABASE_INTEGRITY_FAILED')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_sessions'").fetchone() and conn.execute('SELECT 1 FROM web_sessions LIMIT 1').fetchone():
            raise OperationError('BACKUP_CONTAINS_BROWSER_SESSIONS')
        counts = {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in TABLES if name != 'schema_version'}
        for key in conn.execute('SELECT id,namespace,public_key,revoked FROM keys'):
            if not isinstance(key['public_key'], bytes) or len(key['public_key']) != 32 or key['revoked'] not in (0, 1):
                raise OperationError('DATABASE_SIGNER_INVALID')
        archive_bytes = 0
        for row in conn.execute('SELECT * FROM submissions ORDER BY id'):
            metadata = json.loads(row['metadata'], object_pairs_hook=unique_fields)
            if not isinstance(metadata, dict) or set(metadata) - set(Release.model_fields) or metadata.get('schema_version') != 1:
                raise OperationError('DATABASE_METADATA_INVALID')
            if any(row[field] != metadata.get(field) for field in ('namespace', 'package_id', 'version', 'author_id')) or row['state'] not in {'pending', 'published', 'rejected', 'withdrawn'}:
                raise OperationError('DATABASE_METADATA_INVALID')
            blob = row['blob']
            if not isinstance(blob, bytes) or not 0 < len(blob) <= 10 * 1024 * 1024 or metadata.get('size') != len(blob) or metadata.get('sha256') != hashlib.sha256(blob).hexdigest():
                raise OperationError('DATABASE_ARCHIVE_INVALID')
            key = conn.execute('SELECT namespace,public_key FROM keys WHERE id=?', (metadata.get('key_id'),)).fetchone()
            if key is None or key['namespace'] != row['namespace']:
                raise OperationError('DATABASE_SIGNER_INVALID')
            # Preserve old immutable signed versions verbatim, including versions
            # that predate stricter SemVer validation. Revoked keys and withdrawn
            # releases remain verifiable evidence, never become active here.
            payload = {name: value for name, value in metadata.items() if name != 'signature'}
            signed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            try:
                Ed25519PublicKey.from_public_bytes(key['public_key']).verify(base64.b64decode(metadata['signature'], validate=True), signed)
            except Exception:
                raise OperationError('DATABASE_SIGNATURE_INVALID') from None
            archive_bytes += len(blob)
        return {'database_schema': 1, 'counts': counts, 'archive_bytes': archive_bytes, 'signed_archives_verified': counts['submissions']}


def backup(database, output_dir):
    database = regular(database)
    output_dir = Path(output_dir).absolute()
    os.mkdir(output_dir, 0o700)  # A backup never replaces an existing directory.
    target = output_dir / 'catalog.sqlite3'
    deadline = time.monotonic() + 60

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise OperationError('BACKUP_TIMEOUT')

    with closing(connect_existing(database)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination, pages=256, progress=progress, sleep=0.05)
        # Only the owned snapshot loses short-lived browser credentials. Live
        # sessions remain valid at the source and cannot revive after recovery.
        if destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_sessions'").fetchone():
            destination.execute('DELETE FROM web_sessions')
            destination.commit()
        destination.execute('PRAGMA journal_mode=DELETE').fetchone()
    os.chmod(target, 0o600)
    summary = inspect_database(target)
    digest = checksum(target)
    manifest = {'schema_version': 1, 'file': 'catalog.sqlite3', 'bytes': target.stat().st_size, 'sha256': digest, 'created_at': datetime.now(timezone.utc).isoformat(), **summary}
    with target.open('r+b') as stream:
        os.fsync(stream.fileno())
    # The manifest is the completion marker. Interrupted bundles do not verify.
    descriptor = os.open(output_dir / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def verify_backup(input_dir, expected_sha256=None):
    input_dir = Path(input_dir).absolute()
    if input_dir.is_symlink() or {path.name for path in input_dir.iterdir()} != {'catalog.sqlite3', 'manifest.json'}:
        raise OperationError('BACKUP_INCOMPLETE')
    manifest_path = regular(input_dir / 'manifest.json')
    if manifest_path.stat().st_size > 16 * 1024:
        raise OperationError('BACKUP_MANIFEST_INVALID')
    manifest = json.loads(manifest_path.read_text('utf-8'), object_pairs_hook=unique_fields)
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('file') != 'catalog.sqlite3':
        raise OperationError('BACKUP_MANIFEST_INVALID')
    database = regular(input_dir / 'catalog.sqlite3')
    digest = checksum(database)
    if digest != manifest.get('sha256') or database.stat().st_size != manifest.get('bytes') or (expected_sha256 is not None and digest != expected_sha256):
        raise OperationError('BACKUP_CHECKSUM_MISMATCH')
    summary = inspect_database(database)
    if any(manifest.get(key) != value for key, value in summary.items()):
        raise OperationError('BACKUP_CONTENT_MISMATCH')
    return manifest


def restore(input_dir, output, expected_sha256):
    manifest = verify_backup(input_dir, expected_sha256)
    output = Path(output).absolute()
    if any(Path(str(output) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise OperationError('RESTORE_TARGET_SIDECAR_EXISTS')
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    source_path = regular(Path(input_dir) / 'catalog.sqlite3')
    with os.fdopen(descriptor, 'wb') as destination, source_path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            destination.write(block)
            digest.update(block)
        destination.flush()
        os.fsync(destination.fileno())
    if digest.hexdigest() != manifest['sha256']:
        raise OperationError('BACKUP_CHECKSUM_MISMATCH')
    summary = inspect_database(output)
    if any(manifest.get(key) != value for key, value in summary.items()):
        raise OperationError('BACKUP_CONTENT_MISMATCH')
    return {'state': 'restored', 'sha256': manifest['sha256'], **summary}
