import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from community.package import Release, inspect, signed_payload
from community.templates import validate_template, MAX_TEMPLATE_FILE_BYTES
from tests.test_catalog import env, package


FIXTURE = json.loads((Path(__file__).parent/'fixtures/community-v1-templates.json').read_text('utf-8'))


def template_package(signer, manifest):
    payload = package(signer, 'template', {'template.json':json.dumps(manifest,ensure_ascii=False)})
    release = Release(**payload['release'])
    release.min_app_version = '0.6.0'
    release.signature = base64.b64encode(signer.sign(signed_payload(release))).decode()
    payload['release'] = release.model_dump()
    return payload


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['name'])
def test_shared_templates_are_data_and_inputs_bind_exact_files(case):
    if case['accepted']:
        value = validate_template(case['manifest'])
        assert value == case['manifest'].get('experiment')
    else:
        with pytest.raises(ValueError):
            validate_template(case['manifest'])


def test_templates_count_utf8_bytes_file_count_and_total():
    value = {'markdown':'# Example','experiment':{'schema_version':1,'entry':'0.py','inputs':[], 'files':[{'path':'0.py','content':'x'*MAX_TEMPLATE_FILE_BYTES}]}}
    validate_template(value)
    value['experiment']['files'][0]['content'] = '中'*(MAX_TEMPLATE_FILE_BYTES//3+1)
    with pytest.raises(ValueError): validate_template(value)
    value['experiment']['files'] = [{'path':f'{index}.py','content':''} for index in range(32)]
    validate_template(value)
    value['experiment']['files'].append({'path':'extra.py','content':''})
    with pytest.raises(ValueError): validate_template(value)
    value['experiment']['files'] = [{'path':f'{index}.py','content':'x'*MAX_TEMPLATE_FILE_BYTES} for index in range(9)]
    with pytest.raises(ValueError): validate_template(value)


def test_published_archive_uses_the_same_experiment_checker_and_rejects_duplicate_keys():
    signer = Ed25519PrivateKey.generate()
    for case in FIXTURE['cases']:
        value = template_package(signer, case['manifest'])
        release, blob = Release(**value['release']), base64.b64decode(value['archive_base64'])
        if case['accepted']:
            inspect(release, blob)
        else:
            with pytest.raises(ValueError): inspect(release, blob)
    for body in ['{"markdown":"one","markdown":"two"}', '{"markdown":"x","experiment":{"entry":"one.py","entry":"two.py"}}']:
        value = package(signer, 'template', {'template.json':body})
        with pytest.raises(ValueError): inspect(Release(**value['release']), base64.b64decode(value['archive_base64']))


@pytest.mark.parametrize('name', ['unicode-literal-paths-and-crlf', 'auto-run-flag', 'case-collision', 'missing-entry-file'])
def test_http_publication_keeps_review_and_experiment_data_bound(env, name):
    client, signer, author, moderator = env
    case = next(case for case in FIXTURE['cases'] if case['name'] == name)
    payload = template_package(signer, case['manifest'])
    response = client.post('/catalog/v1/publish/submissions', headers=author, json=payload)
    if not case['accepted']:
        assert response.status_code == 422
        assert client.get('/catalog/v1/packages').json()['total'] == 0
        return
    assert response.status_code == 200, response.text
    assert client.get('/catalog/v1/packages').json()['total'] == 0
    submission = response.json()['submission_id']
    assert client.post('/catalog/v1/moderation/reviews',headers=moderator,json={'submission_id':submission,'approve':True,'reason':'Review source and inputs'}).status_code == 200
    release = client.get('/catalog/v1/packages').json()['items'][0]
    assert release['type'] == 'template'
    assert client.get(release['download_path']).content == base64.b64decode(payload['archive_base64'])


def test_experiment_templates_cannot_advertise_legacy_client_support():
    signer = Ed25519PrivateKey.generate()
    value = template_package(signer, FIXTURE['cases'][2]['manifest'])
    release = Release(**value['release'])
    release.min_app_version = '0.5.9'
    with pytest.raises(ValueError): inspect(release, base64.b64decode(value['archive_base64']))


def test_json_manifest_size_depth_nodes_and_invalid_utf8_are_rejected():
    signer = Ed25519PrivateKey.generate()
    deep = 0
    for _ in range(40): deep = [deep]
    for manifest in [{'markdown':'x'*1024*1024}, {'markdown':'x','extra':deep},
                     {'markdown':'x','extra':[0]*10001}, {'markdown':'\ud800'}]:
        # ASCII JSON escapes let the package parser itself check surrogate text.
        value = package(signer, 'template', {'template.json':json.dumps(manifest)})
        with pytest.raises(ValueError): inspect(Release(**value['release']), base64.b64decode(value['archive_base64']))
