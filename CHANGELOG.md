# Changelog

All notable changes to LenkoBot will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from
the first tagged release.

## [0.1.0] - Unreleased

### Added

- Single-owner private Telegram runtime with early authorization.
- Versioned personas and independent durable session lanes.
- Scoped SQLite memory, automatic extraction, provenance, revisions, and local
  sensitive-data rules.
- OAuth-only xAI integration backed by Windows Credential Manager.
- One-time destructive confirmations and typed Telegram presentation.
- Confirmed one-shot and recurring reminders with time-zone policy, durable
  outbox delivery, bounded retry, and lifecycle reset fences.
- Optional model-directed DDGS or Tavily web search with source links.
- Locked dependencies, Linux and Windows CI, migration tests, and security
  regression coverage.

### Known limitations

- Windows-first local runtime; Linux and Docker deployment are not implemented.
- No web owner panel, URL knowledge base, or sandboxed tools yet.
- No independent security audit or external compatibility certification.
