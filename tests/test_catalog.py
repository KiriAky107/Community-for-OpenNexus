"""审核、签名、撤回和七类受控包的真实 HTTP 测试。"""

import base64
import hashlib
import io
import json
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
import pytest

from community.app import Registry, create_app
from community.package import Release, signed_payload


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / "catalog.db")
    author = registry.add_principal("author", "author", "examples")
    moderator = registry.add_principal("reviewer", "moderator")
    private = Ed25519PrivateKey.generate()
    registry.add_key("test-key", "examples", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    with TestClient(create_app(registry)) as client:
        yield client, private, {"Authorization": "Bearer " + author}, {"Authorization": "Bearer " + moderator}


def package(private, kind="persona", files=None):
    names = {"theme": ("theme.yaml", "theme_id: test-package\nversion: 1.0.0"), "plugin": ("plugin.yaml", "plugin_id: test-package\nversion: 1.0.0"),
             "skill": ("skill.yaml", "skill_id: test-package\nversion: 1.0.0"), "mcp": ("mcp.json", json.dumps({"transport": "stdio", "args": []})),
             "persona": ("persona.json", json.dumps({"system_prompt": "受控样例"})),
             "template": ("template.json", json.dumps({"markdown": "# {{title}}"})),
             "model": ("model.json", json.dumps({"source": "https://example.org/model", "revision": "fixed", "license": "MIT", "resources": {"ram_gb": 8}, "verified_platforms": ["test-only"]}))}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in (files or dict([names[kind]])).items(): archive.writestr(name, value)
    blob = buffer.getvalue()
    release = Release(namespace="examples", package_id="test-package", type=kind, version="1.0.0", name="受控示例", author_id="author", license="MIT",
                      description="用于隔离验收", sha256=hashlib.sha256(blob).hexdigest(), size=len(blob), platforms=["windows"], architectures=["x86_64"],
                      min_app_version="0.2.0", changelog="初始版本", published_at="2026-09-07T00:00:00Z", key_id="test-key", signature="")
    release.signature = base64.b64encode(private.sign(signed_payload(release))).decode()
    return {"release": release.model_dump(), "archive_base64": base64.b64encode(blob).decode()}


@pytest.mark.parametrize("kind", ["theme", "skill", "plugin", "mcp", "persona", "template", "model"])
def test_publish_review_read_withdraw(env, kind):
    client, private, author, moderator = env
    payload = package(private, kind)
    submit = client.post("/catalog/v1/publish/submissions", headers=author, json=payload)
    assert submit.status_code == 200, submit.text
    submission = submit.json()["submission_id"]
    assert client.get("/catalog/v1/packages").json()["total"] == 0
    assert client.post("/catalog/v1/moderation/reviews", headers=author, json={"submission_id": submission, "approve": True, "reason": "自审"}).status_code == 403
    assert client.post("/catalog/v1/moderation/reviews", headers=moderator, json={"submission_id": submission, "approve": True, "reason": "受控验收"}).status_code == 200
    listing = client.get("/catalog/v1/packages", params={"type": kind, "q": "示例"})
    assert listing.json()["total"] == 1
    assert client.get("/catalog/v1/packages", params={"type": kind, "q": "示例"}, headers={"If-None-Match": listing.headers["ETag"]}).status_code == 304
    path = listing.json()["items"][0]["download_path"]
    assert hashlib.sha256(client.get(path).content).hexdigest() == payload["release"]["sha256"]
    assert client.post("/catalog/v1/publish/submissions", headers=author, json=payload).status_code == 409
    assert client.post(f"/catalog/v1/releases/{submission}/withdraw", headers=author, json={"reason": "撤回验收"}).status_code == 200
    assert client.get(path).status_code == 410
    assert client.get("/catalog/v1/packages").json()["items"][0]["withdrawn"]


def test_tampered_permissions_and_signer_revocation(env):
    client, private, author, moderator = env
    payload = package(private)
    payload["release"]["permissions"] = ["network.request"]
    assert client.post("/catalog/v1/publish/submissions", headers=author, json=payload).status_code == 422
    payload = package(private)
    submission = client.post("/catalog/v1/publish/submissions", headers=author, json=payload).json()["submission_id"]
    assert client.post("/catalog/v1/keys/test-key/revoke", headers=moderator, json={"reason": "密钥轮换测试"}).status_code == 200
    assert client.post("/catalog/v1/moderation/reviews", headers=moderator, json={"submission_id": submission, "approve": True, "reason": "签名已撤回"}).status_code == 403


@pytest.mark.parametrize("files", [{"../persona.json": "{}"}, {"CON": ""}, {"a": "", "A": ""}, {"persona.json": '{"system_prompt":"x","api_key":"fixture"}'}])
def test_bad_archives_and_embedded_secret_rejected(env, files):
    client, private, author, _ = env
    assert client.post("/catalog/v1/publish/submissions", headers=author, json=package(private, files=files)).status_code == 422


def test_cross_namespace_and_missing_auth(env):
    client, private, author, _ = env
    payload = package(private)
    assert client.post("/catalog/v1/publish/submissions", json=payload).status_code == 401
    payload["release"]["namespace"] = "other"
    assert client.post("/catalog/v1/publish/submissions", headers=author, json=payload).status_code == 403
