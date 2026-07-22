# Contributing to LenkoBot

LenkoBot accepts focused contributions that preserve its single-owner security
model and explicit module boundaries. English and Russian discussion are both
welcome; public-facing documentation should include English.

## Before starting

Open an issue before work that changes architecture, persisted data, external
contracts, security policy, or user workflows. Small fixes, tests, and
documentation corrections can go directly to a pull request.

Security vulnerabilities must follow [SECURITY.md](SECURITY.md), not the public
issue tracker.

## Development setup

Required: Python `3.13` and [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/bunchtrail/LenkoBot.git
Set-Location LenkoBot
uv sync --locked --python 3.13 --group dev
uv run --locked --python 3.13 --group dev pytest
```

Runtime credentials are not needed for the automated suite. Never commit
`config.toml`, `.env`, OAuth state, Telegram tokens, SQLite files, or captured
conversation data.

## Change contract

1. Start behavior changes with a failing test that demonstrates the requirement.
2. Implement the smallest change that makes the test pass.
3. Keep transport, domain logic, persistence, and presentation responsibilities
   separate.
4. Update architecture documentation before changing a long-lived contract or
   invariant.
5. Record upstream provenance before copying any third-party source.
6. Run the complete local verification set before requesting review.

```powershell
uv run --locked --python 3.13 --group dev pytest
uv run --locked --python 3.13 --group dev ruff check src tests
uv run --locked --python 3.13 python -m compileall -q src tests
uv lock --check
git diff --check
```

## Pull requests

Keep each pull request to one coherent feature or fix. The description should
state observable behavior, security or data impact, tests run, and any remaining
limitation. Review prioritizes authorization, data ownership, migration safety,
external side effects, and regression coverage.

The maintainer may ask for a smaller scope when unrelated refactoring obscures
the behavioral change.
