# Contributing

## Scope

This repository contains the catalog prototype, package validation, signing metadata, moderation lifecycle, and related tests. Desktop installation/runtime changes belong in [OpenNexus](https://github.com/KiriAky107/OpenNexus); synchronization changes belong in [Sync for OpenNexus](https://github.com/KiriAky107/Sync-for-OpenNexus).

## Workflow

1. Search existing Issues and open one for schema, policy, package format, security, or compatibility changes.
2. Create a focused feature branch from `main` and use Conventional Commits.
3. Add tests for valid input, malformed input, authorization boundaries, archive limits, and audit behavior.
4. Update both README languages and package-policy documentation.
5. Open a Pull Request with exact verification commands and sanitized evidence.

## Engineering requirements

- Keep versions immutable and preserve audit history for approvals, rejections, reports, withdrawals, and key revocation.
- Never execute submitted package code during server-side inspection.
- Validate signatures, canonical metadata, SHA-256, size, paths, archive expansion, manifest identity, permissions, and license declarations.
- Keep namespace ownership and moderator independence explicit; authors cannot approve their own submissions.
- Add persistent schema changes through a versioned migration with recovery guidance.
- Explain dependency licenses, service exposure, token lifecycle, rate limiting, and backup impact.
- Never commit bearer tokens, private signing keys, real package secrets, personal information, private reports, or user Vault content.

## Verification

```powershell
uv sync --frozen
uv run pytest
```

List only checks actually run. Security-sensitive changes require negative tests. Package-format changes must cover all affected package types and state compatible OpenNexus versions.

Participation follows the [Code of Conduct](CODE_OF_CONDUCT.md). Vulnerabilities follow [SECURITY.md](SECURITY.md).
