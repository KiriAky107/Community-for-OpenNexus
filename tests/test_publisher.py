"""Offline outputs work with the existing catalog; failed builds retain inputs."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from community.publisher import AuthorError, build, keygen

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def material(tmp_path):
    content = tmp_path / 'payload'; content.mkdir()
    (content / 'persona.json').write_text(json.dumps({'system_prompt': 'Explain one concrete example. 用中文或用户的语言回答。', 'permissions': []}), encoding='utf-8')
    (content / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
    metadata = tmp_path / 'metadata.json'
    metadata.write_text(json.dumps(dict(package_id='clear-explanation', type='persona', version='1.0.0', name='清晰解释', license='MIT', description='Concrete explanations.', platforms=['windows'], architectures=['x86_64'], min_app_version='0.6.0', changelog='## 中文\n初版\n## English\nFirst version', permissions=[])), encoding='utf-8')
    keys = tmp_path / 'keys'; keygen(keys)
    args = dict(metadata_file=metadata, content_dir=content, private_key_file=keys / 'private.key',
                output_dir=tmp_path / 'signed', namespace='examples', author_id='author', key_id='author-key', published_at='2026-10-08T08:00:00Z')
    return args, keys


def invoke(*args):
    return subprocess.run([sys.executable, '-m', 'community.publisher', *map(str, args)], cwd=ROOT, capture_output=True, encoding='utf-8', timeout=15,
                          env={**os.environ, 'PYTHONUTF8': '1'}, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def test_actual_offline_cli_output_submits_and_independent_moderator_publishes(material, tmp_path):
    args, keys = material
    options = [part for name, value in args.items() for part in ('--' + name.replace('_', '-'), value)]
    result = invoke('build', *options)
    assert result.returncode == 0, result.stderr
    private = (keys / 'private.key').read_text().strip()
    assert private not in result.stdout + result.stderr
    directory = args['output_dir']
    release = json.loads((directory / 'release.json').read_text('utf-8'))
    blob = (directory / 'archive.zip').read_bytes()
    complete = json.loads((directory / 'complete.json').read_text('utf-8'))
    assert complete['sha256'] == hashlib.sha256(blob).hexdigest()
    assert complete['metadata_sha256'] == hashlib.sha256((directory / 'release.json').read_bytes()).hexdigest()
    assert sorted(item['path'] for item in complete['files']) == ['LICENSE', 'persona.json']
    with zipfile.ZipFile(directory / 'archive.zip') as archive:
        assert archive.read('LICENSE') == (ROOT / 'LICENSE').read_bytes()
        assert archive.read('persona.json') == (args['content_dir'] / 'persona.json').read_bytes()
    registry = Registry(tmp_path / 'catalog.db')
    author = registry.add_principal('author', 'author', 'examples')
    moderator = registry.add_principal('reviewer', 'moderator')
    registry.add_key('author-key', 'examples', base64.b64decode((directory / 'public.key').read_bytes()))
    with TestClient(create_app(registry)) as client:
        submitted = client.post('/catalog/v1/publish/submissions', headers={'Authorization': 'Bearer ' + author}, json={'release': release, 'archive_base64': base64.b64encode(blob).decode()})
        assert submitted.status_code == 200
        identifier = submitted.json()['submission_id']
        assert client.post('/catalog/v1/moderation/reviews', headers={'Authorization': 'Bearer ' + moderator}, json={'submission_id': identifier, 'approve': True, 'reason': 'Reviewed actual signed package.'}).json()['state'] == 'published'
        assert client.get('/catalog/v1/releases/' + identifier + '/archive').content == blob
    duplicate = invoke('build', *options)
    assert json.loads(duplicate.stdout)['error']['code'] == 'OUTPUT_ALREADY_EXISTS'
    assert (directory / 'archive.zip').read_bytes() == blob
    assert (keys / 'private.key').read_text().strip() == private


def test_keygen_cli_is_exclusive_and_does_not_print_private_material(tmp_path):
    keys = tmp_path / 'keys'
    result = invoke('keygen', '--output-dir', keys)
    assert result.returncode == 0
    private = (keys / 'private.key').read_bytes()
    assert private.strip().decode() not in result.stdout + result.stderr
    duplicate = invoke('keygen', '--output-dir', keys)
    assert json.loads(duplicate.stdout)['error']['code'] == 'OUTPUT_ALREADY_EXISTS'
    assert (keys / 'private.key').read_bytes() == private


@pytest.mark.parametrize('bad', ['bad-license', 'credential', 'computed-field', 'duplicate-field', 'case-collision', 'key-in-content', 'output-in-content', 'no-zone'])
def test_failed_inspection_never_creates_outputs_or_changes_source(material, bad):
    args, keys = material
    metadata = json.loads(args['metadata_file'].read_text('utf-8'))
    if bad == 'bad-license': metadata['license'] = 'unknown'
    if bad == 'computed-field': metadata['sha256'] = '0' * 64
    if bad in {'bad-license', 'computed-field'}:
        args['metadata_file'].write_text(json.dumps(metadata), encoding='utf-8')
    if bad == 'duplicate-field': args['metadata_file'].write_text('{"type":"persona","type":"model"}')
    if bad == 'credential': (args['content_dir'] / 'persona.json').write_text('{"system_prompt":"test","token":"not-for-publication"}')
    if bad == 'case-collision':
        (args['content_dir'] / 'straße.txt').write_text('one')
        (args['content_dir'] / 'STRASSE.txt').write_text('two')
    if bad == 'key-in-content':
        args['private_key_file'] = args['content_dir'] / 'private.key'
        args['private_key_file'].write_bytes((keys / 'private.key').read_bytes())
    if bad == 'no-zone': args['published_at'] = '2026-10-08T08:00:00'
    if bad == 'output-in-content': args['output_dir'] = args['content_dir'] / 'signed'
    before = {p.name: p.read_bytes() for p in args['content_dir'].iterdir()}
    with pytest.raises((AuthorError, ValueError)):
        build(**args)
    assert not args['output_dir'].exists()
    assert before == {p.name: p.read_bytes() for p in args['content_dir'].iterdir()}


def test_hardlinked_input_is_refused_before_signing(material, tmp_path):
    args, _ = material
    os.link(args['content_dir'] / 'persona.json', tmp_path / 'other.json')
    with pytest.raises(AuthorError, match='REGULAR_INPUT_REQUIRED'):
        build(**args)
    assert not args['output_dir'].exists()
