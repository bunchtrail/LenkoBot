# Security Model

## Status

This document summarizes the implemented `0.1.x` security boundary in English.
The normative contracts remain [mvp-spec.md](mvp-spec.md) and the verified
evidence in [product-roadmap-todo.md](product-roadmap-todo.md).

LenkoBot is a local, single-owner Telegram companion. It reduces risk by keeping
the authority surface narrow; it does not claim to make model output trusted.

## Protected assets

- Telegram bot token supplied through the process environment.
- xAI OAuth access and refresh state held in Windows Credential Manager.
- Local SQLite conversations, transcripts, summaries, memory, and provenance.
- Persona identity and owner allowlist stored in local configuration.
- One-time action-confirmation receipts and lifecycle epochs.

## Trust boundaries

| Boundary | Implemented control |
|---|---|
| Telegram ingress | Exact configured user ID and private chat are checked before routing, persistence, or provider calls. |
| Callback actions | Owner binding, immutable payload hash, expiry, and atomic one-time consumption. |
| Memory reads | Owner and persona scope are filtered in SQL, not delegated to prompts. |
| Automatic memory | Typed candidates pass local category and sensitive-content validation before activation. |
| Model context | Memory, summaries, transcripts, and search results are bounded and marked as untrusted data. |
| OAuth state | Versioned Credential Manager record, exclusive refresh mutex, allowlisted HTTPS endpoints, redacted errors. |
| Provider calls | OAuth-only composition; generic failures never trigger a paid API-key fallback. |
| SQLite lifecycle | Additive migrations, transactional version changes, future-version refusal, and reset epoch fences. |
| Web search | Config allowlist, bounded tool calls and results, controlled failures, and source URLs rendered separately. |

## Data lifecycle

Authorized user turns are stored before provider work. Successful assistant turns
are stored before Telegram delivery. Session finalization saves bounded summary
and memory outcomes before deleting raw turns; a failure leaves the transcript
available for retry. Reset increments a lifecycle epoch before purging owner data
so stale workers cannot reactivate old state.

OAuth state and the Telegram token are outside SQLite. The local SQLite database
is not encrypted at rest. The encrypted-export component is not yet exposed as a
complete end-user recovery workflow.

## Test evidence

The automated suite covers:

- unauthorized and group ingress without persistence or provider effects;
- cross-owner and cross-persona memory isolation;
- malformed and oversized OAuth state, endpoint validation, and redaction;
- replayed, expired, foreign-owner, and tampered confirmations;
- denied sensitive memory and transactional extraction rollback;
- failed and future-version SQLite migrations;
- reset races and stale lifecycle epochs;
- unknown or repeated web-search tool calls.

These are regression controls, not an independent security audit.

## Known limitations

- The runtime is Windows-first and depends on Windows Credential Manager.
- The public OAuth client ID used for local evaluation is not owned by LenkoBot.
- A full real-user long-polling ingress round trip remains a manual environment
  gate; synthetic ingress plus real Bot API outbound has been verified.
- DDGS is a best-effort third-party backend without a stability guarantee.
- Local process compromise can read configuration, environment variables, and
  the unencrypted SQLite database under that account's permissions.
- Telegram and xAI retain their own service-side data according to their terms.
- Linux deployment, web authentication, URL fetching, and tool sandboxing are
  future boundaries and are not implied by this document.

Report suspected vulnerabilities through [SECURITY.md](../../SECURITY.md).
