"""Bounded, inert UTF-8 review of already validated archive members."""
import difflib
import hashlib
import json
from pathlib import PurePosixPath

TEXT_BYTES = 32768
TEXT_LINES = 1000
DIFF_LINES = 200
DIFF_BYTES = 32768
EXTENSIONS = {'.txt', '.md', '.json', '.yaml', '.yml', '.toml', '.csv', '.py', '.js', '.ts',
              '.css', '.html', '.xml', '.ini', '.conf', '.sh', '.ps1'}
KEY_FIELDS = ('type', 'license', 'min_app_version', 'max_app_version', 'platforms', 'architectures', 'dependencies')


def key_changes(before, after, fields=KEY_FIELDS):
    def value(source, field):
        present = field in source
        rendered = json.dumps(source[field], ensure_ascii=False, sort_keys=True) if present else ''
        return {'present': present, 'value': rendered[:2048], 'truncated': len(rendered) > 2048}
    return [{'field': field, 'before': value(before, field), 'after': value(after, field)}
            for field in fields if (field in before) != (field in after) or before.get(field) != after.get(field)]


def manifest_changes(before, after):
    # JSON manifests have already passed the package's type and structural checks.
    # Other formats stay available through the bounded plain-text view.
    if before is None or after is None:
        return {'status': 'unsupported_format' if before is None and after is None else 'different_type', 'total': 0, 'items': []}
    if len(before) > TEXT_BYTES or len(after) > TEXT_BYTES:
        return {'status': 'too_large', 'total': 0, 'items': []}
    try:
        a, b = json.loads(before), json.loads(after)
        if not isinstance(a, dict) or not isinstance(b, dict): raise ValueError()
    except (ValueError, UnicodeError):
        return {'status': 'unsupported_format', 'total': 0, 'items': []}
    fields = sorted(a.keys() | b.keys())
    changes = key_changes(a, b, fields)
    return {'status': 'available', 'total': len(changes), 'items': changes[:40]}


def side(path, files, *, unavailable=False):
    if unavailable: return {'status': 'previous_unavailable', 'file': None, 'text': None}
    raw = files.get(path)
    if raw is None: return {'status': 'missing', 'file': None, 'text': None}
    entry = {'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    result = {'file': entry, 'text': None}
    if PurePosixPath(path).suffix.lower() not in EXTENSIONS: status = 'unsupported_type'
    elif len(raw) > TEXT_BYTES: status = 'too_large'
    else:
        try: text = raw.decode('utf-8-sig', errors='strict')
        except UnicodeError: status = 'invalid_encoding'
        else:
            if any((ord(char) < 32 and char not in '\t\r\n') or ord(char) == 127 for char in text): status = 'binary'
            elif len(text.splitlines()) > TEXT_LINES: status = 'too_many_lines'
            else: status = 'text'; result['text'] = text
    return {**result, 'status': status}


def comparison(before, after):
    if before['status'] not in {'text', 'missing'} or after['status'] not in {'text', 'missing'}:
        return {'status': 'unavailable', 'unified': '', 'truncated': False}
    lines = difflib.unified_diff((before['text'] or '').splitlines(keepends=True),
                                 (after['text'] or '').splitlines(keepends=True), fromfile='previous', tofile='current', n=3)
    output, size, count, truncated = [], 0, 0, False
    for line in lines:
        if not line.endswith('\n'): line += '\n\\ No newline at end of file\n'
        size += len(line.encode('utf-8'))
        count += len(line.splitlines())
        if count > DIFF_LINES or size > DIFF_BYTES:
            truncated = True
            break
        output.append(line)
    return {'status': 'available', 'unified': ''.join(output), 'truncated': truncated}
