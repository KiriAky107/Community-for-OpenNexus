"""Explicit API operations. Tokens come from files; mutations are never retried."""
import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote, urlencode, urlsplit

from .app import Registry, create_app
from .operations import OperationError, backup, regular, restore, unique_fields, verify_backup
from .package import Release, inspect, verify


class ClientError(Exception):
    def __init__(self, code, exit_code=1):
        self.code, self.exit_code = code, exit_code


def bounded_file(path, limit):
    with regular(path).open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ClientError('INPUT_TOO_LARGE')
    return value


def package(args):
    if not args.release_file or not args.archive_file or not args.public_key_file:
        raise ClientError('RELEASE_ARCHIVE_AND_PUBLIC_KEY_REQUIRED')
    try:
        release = Release.model_validate(json.loads(bounded_file(args.release_file, 1024 * 1024), object_pairs_hook=unique_fields))
        archive = bounded_file(args.archive_file, 10 * 1024 * 1024)
        public_key = base64.b64decode(bounded_file(args.public_key_file, 256).strip(), validate=True)
        inspect(release, archive)
        verify(release, public_key)
        return {'release': release.model_dump(), 'archive_base64': base64.b64encode(archive).decode()}
    except ClientError:
        raise
    except Exception:
        raise ClientError('PACKAGE_VALIDATION_FAILED') from None


class Client:
    def __init__(self, url, token_file=None, *, allow_loopback_http=False, timeout=15):
        if not url:
            raise ClientError('CATALOG_URL_REQUIRED')
        parts = urlsplit(url)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or any(part in {'.', '..'} for part in parts.path.split('/')):
            raise ClientError('CATALOG_URL_INVALID')
        if parts.scheme != 'https' and not (allow_loopback_http and parts.scheme == 'http' and parts.hostname in {'127.0.0.1', 'localhost', '::1'}):
            raise ClientError('HTTPS_REQUIRED')
        self.parts, self.timeout = parts, timeout
        self.token = None
        if token_file:
            self.token = bounded_file(token_file, 512).decode('ascii').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', self.token):
                raise ClientError('TOKEN_FILE_INVALID')

    def request(self, method, path, body=None):
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(data) > 15 * 1024 * 1024:
                raise ClientError('PACKAGE_TOO_LARGE')
            headers['Content-Type'] = 'application/json'
        connection_type = http.client.HTTPSConnection if self.parts.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(self.parts.hostname, self.parts.port, timeout=self.timeout)
        try:
            connection.request(method, self.parts.path.rstrip('/') + path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(16 * 1024 * 1024 + 1)
            if 300 <= response.status < 400:
                raise ClientError('REDIRECT_REFUSED')
            if len(raw) > 16 * 1024 * 1024:
                raise ClientError('RESPONSE_TOO_LARGE')
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ClientError('OUTCOME_UNKNOWN' if method == 'POST' else 'INVALID_RESPONSE', 3) from None
            if not 200 <= response.status < 300:
                code = value.get('error', {}).get('code') if isinstance(value, dict) else None
                if self.token and isinstance(code, str) and self.token in code:
                    code = None
                raise ClientError(code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) else 'REMOTE_REQUEST_FAILED')
            return value
        except (OSError, http.client.HTTPException):
            raise ClientError('OUTCOME_UNKNOWN' if method == 'POST' else 'SERVER_UNAVAILABLE', 3) from None
        finally:
            connection.close()


def parser():
    result = argparse.ArgumentParser(description='Community operator, author and moderator commands. Output is JSON.')
    result.add_argument('command', choices=['serve', 'create-author', 'create-moderator', 'add-key', 'check-package', 'submit', 'submissions', 'status', 'reviews', 'review', 'withdraw', 'report', 'reports', 'resolve-report', 'revoke-key', 'audit', 'ready', 'backup', 'verify-backup', 'restore'])
    for option in ['id', 'namespace', 'url', 'submission-id', 'release-id', 'key-id', 'package-id', 'version', 'reason', 'expected-sha256', 'host']:
        result.add_argument('--' + option)
    for option in ['public-key-file', 'token-file', 'release-file', 'archive-file', 'output-dir', 'input-dir', 'output']:
        result.add_argument('--' + option, type=Path)
    result.add_argument('--report-id', type=int)
    result.add_argument('--decision', choices=['addressed', 'dismissed'])
    result.add_argument('--limit', type=int, default=30)
    result.add_argument('--offset', type=int, default=0)
    result.add_argument('--after', type=int, default=0)
    result.add_argument('--port', type=int, default=8081)
    result.add_argument('--timeout', type=float, default=15)
    result.add_argument('--allow-loopback-http', action='store_true')
    result.add_argument('--allow-insecure-loopback-sessions', action='store_true')
    decision = result.add_mutually_exclusive_group()
    decision.add_argument('--approve', action='store_true')
    decision.add_argument('--reject', action='store_true')
    return result


def emit(value, token=None):
    output = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if token:
        output = output.replace(token, '[REDACTED]')
    print(output)


def mutation(client, path, body, state, **fields):
    value = client.request('POST', path, body)
    if not isinstance(value, dict) or value.get('state') != state or any(value.get(key) != expected for key, expected in fields.items()):
        raise ClientError('OUTCOME_UNKNOWN', 3)
    if state == 'pending' and (not isinstance(value.get('submission_id'), str) or not re.fullmatch('[a-f0-9]{32}', value['submission_id'])):
        raise ClientError('OUTCOME_UNKNOWN', 3)
    if state == 'reported' and (type(value.get('report_id')) is not int or value['report_id'] <= 0):
        raise ClientError('OUTCOME_UNKNOWN', 3)
    return value, client.token


def execute(args):
    if not 0 < args.timeout <= 120 or not 1 <= args.limit <= 100 or not 0 <= args.offset <= 2147483647 or not 0 <= args.after <= 9223372036854775807:
        raise ClientError('ARGUMENTS_INVALID')
    if args.command in {'backup', 'verify-backup', 'restore'}:
        if args.command == 'backup':
            if not args.output_dir or not os.getenv('COMMUNITY_DATABASE_PATH'):
                raise ClientError('DATABASE_AND_OUTPUT_DIR_REQUIRED')
            return backup(Path(os.environ['COMMUNITY_DATABASE_PATH']), args.output_dir), None
        if not args.input_dir:
            raise ClientError('INPUT_DIR_REQUIRED')
        if args.command == 'verify-backup':
            return verify_backup(args.input_dir, args.expected_sha256), None
        if not args.output or not args.expected_sha256 or not re.fullmatch('[a-f0-9]{64}', args.expected_sha256):
            raise ClientError('NEW_OUTPUT_AND_EXPECTED_SHA256_REQUIRED')
        return restore(args.input_dir, args.output, args.expected_sha256), None
    if args.command == 'check-package':
        checked = package(args)
        return {'state': 'validated', 'sha256': checked['release']['sha256'], 'size': checked['release']['size']}, None
    if args.command in {'serve', 'create-author', 'create-moderator', 'add-key'}:
        if not os.getenv('COMMUNITY_DATABASE_PATH'):
            raise ClientError('DATABASE_REQUIRED')
        if args.command == 'create-author' and not args.namespace:
            raise ClientError('AUTHOR_NAMESPACE_REQUIRED')
        registry = Registry(Path(os.environ['COMMUNITY_DATABASE_PATH']))
        if args.command == 'serve':
            import uvicorn
            if not 1 <= args.port <= 65535:
                raise ClientError('PORT_INVALID')
            origins = tuple(value.strip() for value in os.getenv('COMMUNITY_ALLOWED_ORIGINS', '').split(',') if value.strip())
            host = args.host or '127.0.0.1'
            if args.allow_insecure_loopback_sessions and host not in {'127.0.0.1', 'localhost', '::1'}:
                raise ClientError('LOOPBACK_SESSION_BIND_REQUIRED')
            try:
                app = create_app(registry, os.getenv('COMMUNITY_SOURCE_ID', 'self-hosted'), origins, web_origin=os.getenv('COMMUNITY_WEB_ORIGIN') or None,
                                 session_ttl=int(os.getenv('COMMUNITY_WEB_SESSION_TTL', '7200')), allow_insecure_loopback_sessions=args.allow_insecure_loopback_sessions)
            except ValueError:
                raise ClientError('WEB_SESSION_CONFIGURATION_INVALID') from None
            uvicorn.run(app, host=host, port=args.port, access_log=False, proxy_headers=False)
            return None, None
        if not args.id:
            raise ClientError('ID_REQUIRED')
        if args.command == 'add-key':
            if not args.namespace or not args.public_key_file:
                raise ClientError('NAMESPACE_AND_PUBLIC_KEY_REQUIRED')
            registry.add_key(args.id, args.namespace, base64.b64decode(bounded_file(args.public_key_file, 256).strip(), validate=True))
            return {'state': 'key-added'}, None
        if not args.token_file:
            raise ClientError('TOKEN_FILE_REQUIRED')
        descriptor = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            token = registry.add_principal(args.id, 'author' if args.command == 'create-author' else 'moderator', args.namespace)
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        return {'state': 'principal-created'}, None
    client = Client(args.url or os.getenv('COMMUNITY_URL'), args.token_file, allow_loopback_http=args.allow_loopback_http, timeout=args.timeout)
    if args.command != 'ready' and not client.token:
        raise ClientError('TOKEN_FILE_REQUIRED')
    paths = {'reviews': '/catalog/v1/moderation/reviews', 'submissions': '/catalog/v1/publish/submissions', 'audit': '/catalog/v1/moderation/audit', 'reports': '/catalog/v1/moderation/reports'}
    if args.command in paths:
        query = {'limit': args.limit}
        if args.command in {'reviews', 'submissions'}: query['offset'] = args.offset
        else: query['after'] = args.after
        if args.command == 'submissions':
            if args.package_id: query['package_id'] = args.package_id
            if args.version: query['version'] = args.version
        return client.request('GET', paths[args.command] + '?' + urlencode(query)), client.token
    if args.command == 'ready':
        return client.request('GET', '/ready'), client.token
    if args.command == 'submit':
        return mutation(client, '/catalog/v1/publish/submissions', package(args), 'pending')
    if args.command == 'status':
        if not args.submission_id: raise ClientError('SUBMISSION_ID_REQUIRED')
        return client.request('GET', '/catalog/v1/publish/submissions/' + quote(args.submission_id, safe='')), client.token
    if not args.reason or not args.reason.strip() or len(args.reason) > 2000:
        raise ClientError('REASON_REQUIRED')
    body = {'reason': args.reason}
    if args.command == 'review':
        if not args.submission_id or args.approve == args.reject: raise ClientError('SUBMISSION_AND_DECISION_REQUIRED')
        body.update(submission_id=args.submission_id, approve=args.approve)
        path = '/catalog/v1/moderation/reviews'
    elif args.command in {'withdraw', 'report'}:
        if not args.release_id: raise ClientError('RELEASE_ID_REQUIRED')
        path = '/catalog/v1/releases/' + quote(args.release_id, safe='') + ('/withdraw' if args.command == 'withdraw' else '/reports')
    elif args.command == 'resolve-report':
        if not args.report_id or args.report_id < 0 or not args.decision: raise ClientError('REPORT_AND_DECISION_REQUIRED')
        body['decision'] = args.decision
        path = f'/catalog/v1/moderation/reports/{args.report_id}/resolve'
    else:
        if not args.key_id: raise ClientError('KEY_ID_REQUIRED')
        path = '/catalog/v1/keys/' + quote(args.key_id, safe='') + '/revoke'
    if args.command == 'resolve-report':
        return mutation(client, path, body, 'resolved', report_id=args.report_id, decision=args.decision)
    state = {'review': 'published' if args.approve else 'rejected', 'withdraw': 'withdrawn', 'report': 'reported', 'revoke-key': 'revoked'}[args.command]
    return mutation(client, path, body, state)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        value, token = execute(args)
        if value is not None: emit(value, token)
        return 0
    except ClientError as error:
        emit({'error': {'code': error.code}}, None)
        return error.exit_code
    except OperationError as error:
        emit({'error': {'code': str(error)}})
        return 1
    except FileExistsError:
        emit({'error': {'code': 'OUTPUT_ALREADY_EXISTS'}})
        return 1
    except FileNotFoundError:
        emit({'error': {'code': 'INPUT_NOT_FOUND'}})
        return 1
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error):
        emit({'error': {'code': 'LOCAL_OPERATION_FAILED'}})
        return 1
