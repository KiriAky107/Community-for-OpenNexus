# Security Policy

Security fixes target the latest release and current `main`; older alpha versions do not receive guaranteed backports.

Do not create a public Issue for an unpatched vulnerability, leaked token, private key, malicious unpublished package, moderation bypass, or report containing private data. Use GitHub Private Vulnerability Reporting when enabled, or contact the repository owner privately through information published on the owner's GitHub profile.

Include the affected version, route or package type, prerequisites, minimum reproduction, impact, sanitized proof, and possible mitigation. Never send real tokens, private signing keys, personal data, user Vaults, or third-party packages without authorization.

Important boundaries include namespace ownership, bearer-token hashing, moderator independence, Ed25519 verification, canonical signed metadata, immutable versions, ZIP path and expansion checks, manifest/permission consistency, key revocation, withdrawal behavior, audit integrity, CORS, request limits, rate limiting, TLS, and backups.

The current token design has no expiration lifecycle and is not production-ready. Maintainers will attempt to reproduce, assess, fix, test, and coordinate disclosure, but alpha development has no guaranteed response SLA.
