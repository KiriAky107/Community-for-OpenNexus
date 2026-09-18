# Community for OpenNexus

[简体中文](README.zh-CN.md) | **English**

![Version](https://img.shields.io/badge/version-0.5.2--alpha1-5865f2)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab)
![Status](https://img.shields.io/badge/status-alpha-f59e0b)
![License](https://img.shields.io/badge/license-MIT-green)

Community for OpenNexus is an independent prototype catalog for publishing, reviewing, and discovering OpenNexus Skills, Plugins, themes, and other extension packages. It does not store user vaults and does not reuse Sync Server tokens.

> This repository contains an alpha engineering prototype, not a production marketplace.

## Development

```powershell
uv sync --frozen
uv run pytest
uv run python -m community serve
```

The development server binds to `127.0.0.1:8081`. Configure `COMMUNITY_DATABASE_PATH` to a managed location and set `COMMUNITY_ALLOWED_ORIGINS` to an explicit comma-separated allowlist before deployment.

## Administration

- `create-author --id … --namespace … --token-file …` creates a bounded publisher identity.
- `create-moderator --id … --token-file …` creates a moderation identity.
- `add-key --id … --namespace … --public-key-file …` imports a Base64 Ed25519 public key.

Tokens are written only to the requested new file and are not printed. Protect token files with operating-system ACLs. The service accepts public keys only and must never receive private signing keys.

## Security status

The current token format has no expiration lifecycle. Authentication, token rotation, revocation management, rate limiting, monitoring, backups, and a TLS reverse proxy are required before any production deployment.

Related projects:

- [OpenNexus desktop application](https://github.com/KiriAky107/OpenNexus)
- [Sync for OpenNexus](https://github.com/KiriAky107/Sync-for-OpenNexus)

## License

This project is licensed under the [MIT License](LICENSE). Third-party components remain subject to their own licenses and notices.
