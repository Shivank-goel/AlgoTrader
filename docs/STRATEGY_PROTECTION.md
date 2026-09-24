# Strategy protection plans (Phase 3.5)

Audit result: the current `momentum_6_12`, `donchian_breakout`, and
`residual_reversal` selector families define regime/score logic, but do not
previously define executable stop or target levels. They are therefore
`MISSING` protection definitions, not silently assigned a global percentage.

`src/strategies/protection.py` provides explicit, deterministic plans:

- `atr_protection`: ATR and configured multiplier from completed data; target
  is optional and can be an explicit reward/risk multiple.
- `structural_protection`: completed-bar invalidation and optional target
  levels, suitable for breakout or mean-reversion definitions once reviewed.

The caller must provide the structural inputs and configuration values. No
future or incomplete bar is read by these pure functions. The resulting
`ProtectionPlan` can be passed to `ShadowIntentScheduler.schedule(...,
protection=plan)`, which copies the levels and calculation provenance into the
intent. `PaperBroker` then validates the levels against the actual simulated
fill and persists the immutable Phase 3 protection row atomically.

Because current production configuration has `candidates: []`, no strategy is
activated by this phase. A candidate declaring `protection_required=true`
without a plan fails closed; no unprotected new shadow entry is created.
There is still no stop/target monitoring, position closing, or live order
submission.
