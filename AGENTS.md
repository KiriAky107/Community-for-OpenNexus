# Repository writing guide

The canonical README style is defined in [OpenNexus's AGENTS.md](https://github.com/KiriAky107/OpenNexus/blob/main/AGENTS.md). Apply it to `README.md` and `README.zh-CN.md` here.

- Use the existing OpenNexus logo and a centered header with a short Community description, language switch, navigation and relevant `flat-square` badges. Link version and CI badges to this repository.
- Present the published release, updates, highlights, quick start and user, author and moderator workflows before architecture and development details. Adapt installation instructions to a self-hosted catalog service.
- Explain finding packages, checking permissions, signing, submitting, reviewing, updating and withdrawing through concrete actions. Distinguish available API, CLI and web features; do not advertise an unfinished workbench.
- Keep English and Simplified Chinese READMEs equivalent in section order, facts, commands, examples and requirements. Keep API routes, package types, variable names and commands exact.
- Use short paragraphs, task-oriented headings, numbered steps for workflows, tables for package types and Mermaid for useful architecture or lifecycle diagrams. Use screenshots only from the real application.
- Make capability descriptions concrete. Avoid slogans, filler, exaggerated readiness claims and repetitive disclaimers. Keep product versions distinct from catalog protocol versions.
- Describe signatures, independent moderation and desktop installation permissions separately. Signing private keys stay with authors; a published package still requires the user's installation review.
- Verify commands, manifests and links against the implementation. Preserve documented session, revocation, unknown-result and recovery behavior without implying planned features already exist.

Private plans, internal `docs`, local receipts, tokens, private keys, databases and user vaults stay local. Public README files and this user-requested writing guide may be committed. Do not repeat unchanged documentation tracking checks. Group changes by purpose, validate affected behavior, and protect production data and old releases.
