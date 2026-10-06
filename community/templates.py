"""Portable experiment-template data; publishing never imports or executes it."""
import re

MAX_TEMPLATE_FILES = 32
MAX_TEMPLATE_FILE_BYTES = 64 * 1024
MAX_TEMPLATE_TOTAL_BYTES = 512 * 1024


def _path(value):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 512:
        return False
    if value.rsplit('.', 1)[-1].lower() not in {'py', 'json', 'csv'}:
        return False
    for part in value.split('/'):
        stem = part.split('.', 1)[0].upper()
        device = stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or re.fullmatch(r'(?:COM|LPT)[1-9¹²³]', stem)
        if (not part or part in {'.', '..'} or part.endswith(('.', ' '))
                or len(part.encode('utf-16-le')) // 2 > 255 or device
                or part.lower() in {'.ainote', '.git', 'opennexus-records'}
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in '\\:*?"<>|' for char in part)):
            return False
    return True


def validate_template(manifest):
    if (not isinstance(manifest.get('markdown'), str)
            or (manifest.get('executable') is not None and manifest['executable'] is not False)):
        raise ValueError('模板只能作为数据导入')
    if 'experiment' not in manifest:
        return None
    value = manifest['experiment']
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'entry', 'inputs', 'files'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['entry'], str) or not value['entry'].lower().endswith('.py')
            or not isinstance(value['inputs'], list) or len(value['inputs']) >= MAX_TEMPLATE_FILES
            or not isinstance(value['files'], list) or not 1 <= len(value['files']) <= MAX_TEMPLATE_FILES):
        raise ValueError('实验模板 schema 无效')
    paths, files, total = set(), {}, 0
    for file in value['files']:
        if (not isinstance(file, dict) or set(file) != {'path', 'content'}
                or not _path(file['path']) or not isinstance(file['content'], str)):
            raise ValueError('实验模板文件无效')
        size = len(file['content'].encode('utf-8'))
        key = file['path'].upper()
        if size > MAX_TEMPLATE_FILE_BYTES or key in paths:
            raise ValueError('实验模板文件超限或重名')
        total += size
        paths.add(key)
        files[file['path']] = file
    if (total > MAX_TEMPLATE_TOTAL_BYTES or value['entry'] not in files
            or any('/'.join(path.split('/')[:index]) in paths for path in paths for index in range(1, len(path.split('/'))))):
        raise ValueError('实验模板超限或路径冲突')
    selected = {value['entry']}
    for path in value['inputs']:
        if not isinstance(path, str) or path not in files or path in selected:
            raise ValueError('实验模板输入未绑定')
        selected.add(path)
    return value
