"""签名发行元数据与七类包验证；不执行包内代码。"""

import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from semver import Version


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    package_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    type: Literal["theme", "skill", "plugin", "mcp", "persona", "template", "model"]
    version: str = Field(max_length=120)
    name: str = Field(min_length=1, max_length=120)
    author_id: str = Field(min_length=1, max_length=80)
    license: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=10000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0, le=10 * 1024 * 1024)
    platforms: list[str] = Field(max_length=12)
    architectures: list[str] = Field(max_length=12)
    min_app_version: str = Field(max_length=120)
    max_app_version: str | None = Field(default=None, max_length=120)
    dependencies: dict[str, str] = Field(default_factory=dict, max_length=64)
    permissions: list[str] = Field(default_factory=list, max_length=64)
    changelog: str = Field(max_length=10000)
    published_at: str = Field(max_length=40)
    key_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")
    signature: str = Field(max_length=128)

    @field_validator("version", "min_app_version", "max_app_version")
    @classmethod
    def semantic_version(cls, value):
        if value is not None:
            Version.parse(value)
        return value

    @model_validator(mode="after")
    def compatibility_range(self):
        if self.max_app_version is not None and Version.parse(self.max_app_version) < Version.parse(self.min_app_version):
            raise ValueError("应用版本范围倒置")
        return self

    @field_validator("license")
    @classmethod
    def declared_license(cls, value):
        if value.lower() in {"unknown", "none", "unlicensed", "tbd"}:
            raise ValueError("公开目录要求明确许可证")
        return value


def signed_payload(release: Release) -> bytes:
    # 固定 canonical JSON，签名覆盖类型、权限、兼容版本与对象摘要，而非只签 ZIP。
    return json.dumps(release.model_dump(exclude={"signature"}), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def verify(release: Release, public_key: bytes):
    Ed25519PublicKey.from_public_bytes(public_key).verify(base64.b64decode(release.signature, validate=True), signed_payload(release))


def inspect(release: Release, blob: bytes):
    if len(blob) != release.size or hashlib.sha256(blob).hexdigest() != release.sha256:
        raise ValueError("摘要或长度不匹配")
    max_size, max_entries = ((10 * 1024 * 1024, 100) if release.type == "theme" else (50 * 1024 * 1024, 2048))
    if release.type == "theme" and len(blob) > 5 * 1024 * 1024:
        raise ValueError("主题包超过 5 MiB")
    names, files, total = set(), {}, 0
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if len(archive.infolist()) > max_entries:
            raise ValueError("条目过多")
        for item in archive.infolist():
            name = item.filename.rstrip("/")
            parts = name.split("/")
            mode = item.external_attr >> 16
            if (not name or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
                    or re.search(r'[\\:\x00-\x1f<>|?*]', name)
                    or any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in parts)
                    or name.casefold() in names or item.flag_bits & 1
                    or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                raise ValueError("不安全 ZIP")
            names.add(name.casefold())
            total += item.file_size
            if total > max_size:
                raise ValueError("解压限制")
            if not item.is_dir(): files[name] = archive.read(item)
    required = {"theme": "theme.yaml", "skill": "skill.yaml", "plugin": "plugin.yaml",
                "mcp": "mcp.json", "persona": "persona.json", "template": "template.json", "model": "model.json"}[release.type]
    matches = [path for path in files if path == required or path.endswith("/" + required)]
    if len(matches) != 1:
        raise ValueError("类型清单缺失或不唯一")
    if release.type in {"theme", "skill", "plugin"}:
        import yaml
        value = yaml.safe_load(files[matches[0]])
        identity = {"theme": "theme_id", "skill": "skill_id", "plugin": "plugin_id"}[release.type]
        if (not isinstance(value, dict) or value.get(identity, value.get("id") if release.type in {"plugin", "skill"} else None) != release.package_id
                or (identity in value and "id" in value and value[identity] != value["id"])
                or value.get("version") != release.version):
            raise ValueError("发行身份与类型清单不一致")
        if set(value.get("permissions", [])) != set(release.permissions):
            raise ValueError("发行权限与类型清单不一致")
    if release.type in {"mcp", "persona", "template", "model"}:
        def unique_object(pairs):
            value = {}
            for key, child in pairs:
                if key in value:
                    raise ValueError("清单字段重复")
                value[key] = child
            return value
        if len(files[matches[0]]) > 1024 * 1024:
            raise ValueError("清单超过 1 MiB")
        value = json.loads(files[matches[0]], object_pairs_hook=unique_object)
        if not isinstance(value, dict): raise ValueError("清单必须为对象")
        forbidden = {"api_key", "password", "token", "secret", "chat_history", "messages"}
        nodes, scalar_bytes = 0, 0
        def check(node, depth=0):
            nonlocal nodes, scalar_bytes
            nodes += 1
            if depth > 32 or nodes > 10000:
                raise ValueError("清单层级或节点超过限制")
            if isinstance(node, dict):
                if forbidden.intersection(str(k).lower() for k in node): raise ValueError("清单混入秘密或历史")
                for key, child in node.items():
                    scalar_bytes += len(key.encode('utf-8'))
                    check(child, depth + 1)
            elif isinstance(node, list):
                for child in node: check(child, depth + 1)
            elif isinstance(node, str):
                scalar_bytes += len(node.encode('utf-8'))
            if scalar_bytes > 1024 * 1024:
                raise ValueError("清单文本超过限制")
        check(value)
        if release.type == "persona" and not isinstance(value.get("system_prompt"), str): raise ValueError("缺少人设提示")
        if release.type == "template":
            from community.templates import validate_template
            if validate_template(value) is not None and Version.parse(release.min_app_version) < Version(0, 6, 0):
                raise ValueError("实验模板要求支持多文件导入的应用版本")
        if release.type == "model" and not all(value.get(k) for k in ["source", "revision", "license", "resources", "verified_platforms"]): raise ValueError("模型方案不完整")
        if release.type == "mcp":
            if value.get("transport") not in {"stdio", "streamable_http", "sse"}: raise ValueError("不支持 transport")
            if value["transport"] == "stdio" and not isinstance(value.get("args"), list): raise ValueError("参数必须为数组")
    return {"files": len(files), "expanded_size": total, "manifest": matches[0]}
