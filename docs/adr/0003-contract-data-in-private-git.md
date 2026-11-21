# Contract data lives in one private git repo, never in this one

**Decision in one line:** this repository is public, so Contract data lives only in one private contracts repo, with one directory per Contract.

## Decision

- This repository is public, so it never holds Contract data.
- Config, credentials, Runbooks and eval Scenarios live in one private contracts repo.
- The contracts repo has one directory per Contract.
- Investigations, recorded tool results and Teachings live in the Hub's Postgres.

### Access and secrets

- The Hub reads the contracts repo through a read-only deploy key.
- The Hub never pushes to the contracts repo.
- Secrets are encrypted with SOPS, using a separate age key per Contract.
- One Contract's key can't decrypt another's.

### Audit and console

- Git history is the audit trail for config.
- The web console shows the effective config read-only and never edits it.
- So nothing exists in the console that git doesn't hold.

### Tests

- CI and the public tests run on two synthetic demo Contracts.
- The demo Contracts are built from the kind demo.

## Considered Options

- **Config in the database, edited in the web console** (the IncidentFox model).
  - It's friendlier.
  - But it is a second source of truth, with no review history.
- **One private repo per Contract.**
  - It's stronger isolation, and a client could own its repo.
  - But it's more to manage alone.
  - A Contract can move to its own repo if a client asks for that.
