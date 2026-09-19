# Architecture decisions

Each entry is permanent unless superseded by a later dated decision.

## 2026-09-18 — FYERS is the NSE broker

**Reason:** FYERS provides the required client market-data/trading APIs without Dhan's
paid data subscription. **Consequence:** new NSE integration targets FYERS; Dhan code
and historical Dhan cost results remain legacy and are not relabelled.

## 2026-09-18 — Live execution is fail-closed

**Reason:** configuration mistakes must not submit real orders. **Consequence:** paper
is the default; live requires explicit configuration, qualification, deterministic risk,
reconciliation, kill switch and manual deployment approval. No live path exists yet.

## 2026-09-18 — Strategies cannot call brokers

**Reason:** research logic must be testable and broker-independent. **Consequence:** all
orders pass through signal, deterministic risk, intent and execution boundaries.

## 2026-09-18 — Ambiguous submissions are never blindly retried

**Reason:** a timeout does not prove rejection. **Consequence:** UNKNOWN persists and
must reconcile by broker order/trade evidence; correlation IDs remain duplicate defense.

## 2026-09-18 — Research evidence is append-only

**Reason:** failed trials and the full search count are required to control selection
bias. **Consequence:** results are not overwritten or deleted; deployment gates cannot
be relaxed after observing results.

## 2026-09-18 — Agents remain outside deterministic controls

**Reason:** language-model output is nondeterministic and cannot authorize financial
risk. **Consequence:** agents may propose/analyze research but cannot access live secrets,
alter risk/gates, approve promotion, or place live orders autonomously.

## 2026-09-19 — Reproducibility precedes automation

**Reason:** automation magnifies data leakage and weak experimental controls.
**Consequence:** experiments require frozen specifications, bounded campaigns, artifact
hashes, immutable outcomes and human review before shadow qualification.
