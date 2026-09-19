# Research evidence and recovery

Use Python 3.12. This workflow records research; it never enables live execution.
The full-trial K-60 threshold remains unchanged. Failed attempts remain evidence.

## Lifecycle

Register a frozen specification in a bounded campaign. The runner writes
`started → result → published`; registration itself is the initial state.
Result validation checks status, aligned finite returns, timestamps, registered
date boundaries and the bootstrap sample requirement before publication.

Only one unfinished experiment per campaign may execute. Any started experiment
without a published result blocks further campaign execution. Status cannot infer
whether a worker crashed just from elapsed time: inspect the worker first.
Registered experiments that have not started do not block execution.

The runner holds an experiment lock until completion. Resolution refuses an active
cooperating worker. Publication uses a separate lock, and `record_once()` performs
identity comparison plus append under the ordinary trial-registry lock. An identical
retry preserves the first trial timestamp; changed results/identity fail.

## Commands

```sh
.venv312/bin/python main.py research status
.venv312/bin/python main.py research status --campaign CAMPAIGN
.venv312/bin/python main.py research recover-publication EXPERIMENT --trials data/trials.json
.venv312/bin/python main.py research resolve-interruption EXPERIMENT --operator NAME --reason "worker stopped before storing output" --trials data/trials.json
.venv312/bin/python main.py research report EXPERIMENT --trials data/trials.json
```

- `unfinished`: started without a result; investigate and explicitly resolve failure.
- `unpublished`: durable result exists; recover publication without rerunning it.
- `inconsistent`: invalid stored sequence or evidence; preserve it for review.
- `published`: historical report exists; use `report` to check current freshness.

Failure resolution appends an `InterruptedExperiment` result with operator/reason.
The attempt is counted once with zero observations and placeholder zero statistics;
these are missing-result accounting, not measured zero trading returns. It consumes
the existing registered budget slot. Repeating the same resolution is recoverable;
it never overwrites an actual completed result. A rerun needs a new experiment ID.
Keep operator/reason text free of secrets.

## Publication and compatibility

Publications contain specification, result and exact trial-file SHA-256 hashes,
plus a publication timestamp. Metrics use the same captured trial snapshot as its
hash. `report` now returns an envelope with `publication`, `freshness` and the current
trial hash. Consumers of the previous raw report JSON must read `publication`.
Freshness is `current`, `stale`, `unverifiable` or `unpublished`; this never grants
qualification. Even whitespace changes to the bound trial file make it stale.

SQLite schema version 2 is additive and retains all existing experiment/event/legacy
rows. Old reports without evidence hashes remain available as unverifiable. Invalid
old records are reported, never silently repaired. Conflicting pre-upgrade trial
identities require review rather than rewriting research history.

Import validates one captured byte snapshot, retaining its hash and JSON contents.
SQL triggers guard accidental edits, not a malicious filesystem owner. Back up the
SQLite database with its backup API; preserve independent data/code artifacts.
No trial-count reset, automatic repeat of interrupted work or production broker
access is part of this workflow.
