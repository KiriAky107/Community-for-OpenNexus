"""Offline author tools. No catalog connection, upload or package execution."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .package import Release, inspect, signed_payload, verify


class AuthorError(Exception):
    pass


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthorError('DUPLICATE_METADATA_FIELD')
        result[key] = value
    return result


def plain(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise AuthorError('LINK_INPUT_REFUSED')
    return path


def read(path, limit):
    path = plain(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise AuthorError('REGULAR_INPUT_REQUIRED')
    with path.open('rb') as stream:
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_nlink != 1:
            raise AuthorError('INPUT_CHANGED')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise AuthorError('INPUT_TOO_LARGE')
    return data


def write(path, data, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def new_directory(path):
    path = Path(path).absolute()
    plain(path.parent)
    path.mkdir(mode=0o700)
    return path


def keygen(output):
    key = Ed25519PrivateKey.generate()
    directory = new_directory(output)
    write(directory / 'private.key', base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())) + b'\n')
    write(directory / 'public.key', base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)) + b'\n')
    return {'state': 'key-created', 'output_dir': str(directory)}


def archive(directory):
    directory = plain(directory)
    if not directory.is_dir():
        raise AuthorError('CONTENT_DIRECTORY_REQUIRED')
    pending, files, visited, total = [directory], {}, 0, 0
    while pending:
        current = pending.pop()
        for path in sorted(current.iterdir()):
            visited += 1
            if visited > 2048:
                raise AuthorError('CONTENT_ENTRY_LIMIT')
            plain(path)
            if path.is_dir():
                if len(path.relative_to(directory).parts) > 32:
                    raise AuthorError('CONTENT_DEPTH_LIMIT')
                pending.append(path)
            else:
                data = read(path, 10 * 1024 * 1024)
                total += len(data)
                if total > 10 * 1024 * 1024:
                    raise AuthorError('CONTENT_SIZE_LIMIT')
                files[path.relative_to(directory).as_posix()] = data
    if not files:
        raise AuthorError('CONTENT_EMPTY')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as output:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            output.writestr(entry, data)
    return buffer.getvalue()


def build(metadata_file, content_dir, private_key_file, output_dir, *, namespace, author_id, key_id, published_at):
    content = plain(content_dir)
    if Path(private_key_file).resolve().is_relative_to(content.resolve()):
        raise AuthorError('SIGNING_KEY_IN_CONTENT')
    if Path(output_dir).resolve().is_relative_to(content.resolve()):
        raise AuthorError('OUTPUT_IN_CONTENT')
    metadata = json.loads(read(metadata_file, 1024 * 1024), object_pairs_hook=unique_fields)
    computed = {'namespace', 'author_id', 'key_id', 'published_at', 'sha256', 'size', 'signature'}
    if not isinstance(metadata, dict) or computed.intersection(metadata):
        raise AuthorError('AUTHOR_METADATA_FIELDS_INVALID')
    timestamp = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise AuthorError('UTC_TIMESTAMP_REQUIRED')
    timestamp = timestamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(read(private_key_file, 256).strip(), validate=True))
    blob = archive(content)
    release = Release(**metadata, namespace=namespace, author_id=author_id, key_id=key_id,
                      published_at=timestamp, sha256=hashlib.sha256(blob).hexdigest(), size=len(blob), signature='')
    checked = inspect(release, blob, details=True)
    release.signature = base64.b64encode(key.sign(signed_payload(release))).decode('ascii')
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    verify(release, public)
    # All inputs are validated before an exclusive output directory is created.
    # Interrupted outputs remain distinguishable by the missing completion file.
    directory = new_directory(output_dir)
    metadata_bytes = (json.dumps(release.model_dump(), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    write(directory / 'archive.zip', blob)
    write(directory / 'release.json', metadata_bytes)
    write(directory / 'public.key', base64.b64encode(public) + b'\n')
    completion = {'schema_version': 1, 'state': 'signed', 'sha256': release.sha256, 'size': release.size,
                  'metadata_sha256': hashlib.sha256(metadata_bytes).hexdigest(), 'files': checked['file_list']}
    write(directory / 'complete.json', (json.dumps(completion, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return {**completion, 'output_dir': str(directory)}


def parser():
    result = argparse.ArgumentParser(description='Prepare signed Community packages locally; never upload or run them.')
    commands = result.add_subparsers(dest='command', required=True)
    key = commands.add_parser('keygen', help='Write a new private/public Ed25519 pair to a new directory.')
    key.add_argument('--output-dir', type=Path, required=True)
    package = commands.add_parser('build', help='Inspect a payload directory and sign its release metadata.')
    for field in ('metadata-file', 'content-dir', 'private-key-file', 'output-dir'):
        package.add_argument('--' + field, type=Path, required=True)
    for field in ('namespace', 'author-id', 'key-id', 'published-at'):
        package.add_argument('--' + field, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        value = keygen(args.output_dir) if args.command == 'keygen' else build(**{name: value for name, value in vars(args).items() if name != 'command'})
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except AuthorError as error:
        code = str(error)
    except FileExistsError:
        code = 'OUTPUT_ALREADY_EXISTS'
    except FileNotFoundError:
        code = 'INPUT_NOT_FOUND'
    except (OSError, ValueError, TypeError, RecursionError):
        code = 'AUTHOR_INPUT_INVALID'
    print(json.dumps({'error': {'code': code}}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
