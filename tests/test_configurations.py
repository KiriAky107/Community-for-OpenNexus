import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from community.package import Release, inspect, signed_payload
from community.configurations import validate_configuration
from tests.test_catalog import env, package

SHARED = json.loads((Path(__file__).parent / 'fixtures/community-v1-configurations.json').read_text('utf-8'))


@pytest.mark.parametrize('case', SHARED['cases'], ids=lambda case: case['name'])
def test_shared_configuration_is_checked_in_the_real_signed_package_path(case):
    signer = Ed25519PrivateKey.generate()
    payload = package(signer, case['kind'], {case['kind']+'.json':json.dumps(case['manifest'], ensure_ascii=False)})
    release, blob = Release(**payload['release']), base64.b64decode(payload['archive_base64'])
    if case['accepted']:
        validate_configuration(case['kind'], case['manifest'])
        inspect(release, blob)
    else:
        with pytest.raises(ValueError):
            validate_configuration(case['kind'], case['manifest'])
        with pytest.raises(ValueError):
            inspect(release, blob)


@pytest.mark.parametrize('name', ['mcp-stdio-secret-declarations', 'model-granite-runtime', 'model-resources-not-object', 'mcp-numeric-argument'])
def test_publication_retains_independent_review_and_refuses_bad_configurations(env, name):
    client, signer, author, moderator = env
    case = next(case for case in SHARED['cases'] if case['name'] == name)
    payload = package(signer, case['kind'], {case['kind']+'.json':json.dumps(case['manifest'], ensure_ascii=False)})
    response = client.post('/catalog/v1/publish/submissions', headers=author, json=payload)
    assert client.get('/catalog/v1/packages').json()['total'] == 0
    if not case['accepted']:
        assert response.status_code == 422
        return
    assert response.status_code == 200, response.text
    submitted = response.json()['submission_id']
    assert client.post('/catalog/v1/moderation/reviews', headers=moderator,
                       json={'submission_id':submitted,'approve':True,'reason':'Reviewed actual target proposal'}).status_code == 200
    published = client.get('/catalog/v1/packages').json()['items'][0]
    assert client.get(published['download_path']).content == base64.b64decode(payload['archive_base64'])


def test_json_permissions_schema_and_nonfinite_values_match_native_refusal():
    signer = Ed25519PrivateKey.generate()
    for body in ('{"system_prompt":"text","schema_version":true}',
                 '{"system_prompt":"text","permissions":["notes.read"]}',
                 '{"system_prompt":"text","nested":NaN}'):
        payload = package(signer, 'persona', {'persona.json':body})
        with pytest.raises(ValueError):
            inspect(Release(**payload['release']), base64.b64decode(payload['archive_base64']))
    payload = package(signer, 'persona', {'persona.json':'{"system_prompt":"text","permissions":["notes.read"]}'})
    release = Release(**payload['release'])
    release.permissions = ['notes.read']
    release.signature = base64.b64encode(signer.sign(signed_payload(release))).decode()
    inspect(release, base64.b64decode(payload['archive_base64']))
