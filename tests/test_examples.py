"""Public examples contain real licensed inputs and work without user data."""
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import zipfile

import pytest
import uvicorn

from community.package import Release, inspect, verify
from community.publisher import build, keygen
import base64

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'


@pytest.mark.parametrize('name', ['clear-explanations', 'measurement-summary', 'summary-mcp', 'bekko-cpu-plan'])
def test_original_sources_build_with_author_key_and_preserve_license_and_raw_changelog(tmp_path, name):
    source = EXAMPLES / name
    keys = tmp_path / 'keys'; keygen(keys)
    out = tmp_path / 'signed'
    result = build(source / 'metadata.json', source / 'payload', keys / 'private.key', out,
                   namespace='examples', author_id='author', key_id='examples-key', published_at='2026-10-08T08:00:00Z')
    release = Release.model_validate_json((out / 'release.json').read_bytes())
    metadata = json.loads((source / 'metadata.json').read_text('utf-8'))
    assert release.changelog == metadata['changelog'] and '## 中文' in release.changelog and '## English' in release.changelog
    assert inspect(release, (out / 'archive.zip').read_bytes())['files'] == 3
    verify(release, base64.b64decode((out / 'public.key').read_bytes()))
    assert result['state'] == 'signed'
    with zipfile.ZipFile(out / 'archive.zip') as archive:
        assert archive.read('LICENSE') == (source / 'payload/LICENSE').read_bytes()
        assert archive.read('LICENSE').decode('utf-8').splitlines() == (ROOT / 'LICENSE').read_text('utf-8').splitlines()
        provenance = json.loads(archive.read('provenance.json'))
        assert provenance['license'] == 'MIT' and provenance['source'].endswith('/' + name)
        assert 'private.key' not in archive.namelist() and 'server.py' not in archive.namelist()
    if name == 'bekko-cpu-plan':
        plan = json.loads((source / 'payload/model.json').read_text('utf-8'))
        assert plan['model_key'] == plan['runtime_config']['embedding_model'] == 'bekko'
        assert plan['source'] == 'hotchpotch/bekko-embedding-v1-a8m' and plan['revision'] == 'c721113d59a1d91b447450324f51c4b3332c924a'
    if name == 'summary-mcp':
        config = json.loads((source / 'payload/mcp.json').read_text('utf-8'))
        assert config['transport'] == 'streamable_http' and config['url'] == 'http://127.0.0.1:18970/mcp'
        assert not config['permissions'] and 'enabled' not in config


def test_measurement_example_produces_the_documented_real_outputs_without_modifying_inputs(tmp_path):
    template = json.loads((EXAMPLES / 'measurement-summary/payload/template.json').read_text('utf-8'))['experiment']
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir(); outputs.mkdir()
    for file in template['files']:
        path = inputs / file['path']; path.parent.mkdir(exist_ok=True)
        path.write_text(file['content'], encoding='utf-8')
    before = {p.relative_to(inputs).as_posix(): p.read_bytes() for p in inputs.rglob('*') if p.is_file()}
    result = subprocess.run([sys.executable, '-I', str(inputs / template['entry'])], cwd=outputs, capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    actual = json.loads((outputs / 'summary.json').read_text('utf-8'))
    assert actual == {'count': 4, 'mean_seconds': 14.0, 'median_seconds': 14.0, 'above_threshold': 2}
    assert 'Measurement summary' in (outputs / 'summary.md').read_text('utf-8')
    assert actual == json.loads(result.stdout)
    assert before == {p.relative_to(inputs).as_posix(): p.read_bytes() for p in inputs.rglob('*') if p.is_file()}


def test_readonly_mcp_example_negotiates_and_returns_actual_statistics_over_http():
    spec = importlib.util.spec_from_file_location('public_summary_example', EXAMPLES / 'summary-mcp/server.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(module.app, log_config=None, access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}); thread.start()
    def call(method, params, identifier=1):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            connection.request('POST', '/mcp', json.dumps({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params}), {'Content-Type': 'application/json'})
            reply = connection.getresponse(); raw = reply.read()
            return reply.status, json.loads(raw) if raw else None
        finally: connection.close()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        assert call('initialize', {'protocolVersion': '2025-11-25'})[1]['result']['protocolVersion'] == '2025-11-25'
        assert call('notifications/initialized', {}, None)[0] == 202
        assert call('tools/list', {})[1]['result']['tools'][0]['annotations']['readOnlyHint'] is True
        output = call('tools/call', {'name': 'summarize_numbers', 'arguments': {'numbers': [8, 12, 16, 20]}})[1]['result']
        assert json.loads(output['content'][0]['text']) == {'count': 4, 'total': 56.0, 'mean': 14.0, 'median': 14.0, 'minimum': 8, 'maximum': 20}
        for numbers in [[], [True], [float('nan')], [10**200], list(range(1001))]:
            assert 'error' in call('tools/call', {'name': 'summarize_numbers', 'arguments': {'numbers': numbers}})[1]
        assert call('tools/call', {'name': 'exec', 'arguments': {'numbers': [1]}})[1]['error']['code'] == -32602
        assert call('unknown', {})[1]['error']['code'] == -32601
    finally:
        server.should_exit = True; thread.join(5); listener.close()
        assert not thread.is_alive()
