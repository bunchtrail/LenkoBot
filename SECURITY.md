# Security Policy

## Supported versions

Before the first tagged release, security fixes target the latest commit on
`main`. After `0.1.0`, the latest `0.1.x` release and `main` receive fixes on a
best-effort basis. Older development snapshots are not supported.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/bunchtrail/LenkoBot/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Include:

- the affected commit or release;
- the trust boundary and expected invariant;
- minimal reproduction steps or a test case;
- realistic impact for a single-owner local deployment;
- whether credentials or personal data may have been exposed.

Do not send live OAuth tokens, Telegram bot tokens, private messages, production
databases, or other people's data. Redact secrets from logs and screenshots.

The maintainer aims to acknowledge reports within 7 days and provide an initial
assessment within 14 days. Complex fixes may take longer; timelines will be
coordinated through the private advisory.

## Security scope

High-priority reports include:

- bypassing the configured owner or private-chat authorization gate;
- reading memory or transcript data across owner or persona scope;
- exposing OAuth, Telegram, or search-provider credentials;
- replaying or transferring a destructive confirmation;
- persisting locally denied sensitive memory;
- escaping an allowlisted provider or search endpoint;
- corrupting or bypassing schema migration and lifecycle fences.

The current limitations and trust assumptions are documented in
[docs/architecture/security-model.md](docs/architecture/security-model.md).
LenkoBot has not received an independent security audit and should not be used
as the sole control for high-risk or regulated data.
