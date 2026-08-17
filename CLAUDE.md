# Crypto-trader / NSE quant research

**Before proposing, testing, or claiming anything: read [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md).**

It holds the current-state facts — costs, constraints, what has been tested, what failed, and
the errors already made with their root causes. It exists so research does not loop.

- **[docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)** — the answer. Deduplicated facts with `K-xx` IDs,
  provenance and confidence tags. **Corrected in place** when something turns out wrong.
- **[docs/RESEARCH_LOG.md](docs/RESEARCH_LOG.md)** — the journey. Append-only, one entry per
  hypothesis, `R-xx` IDs. Check here before testing anything, to see if it has been tried.
- **[docs/baseline-findings.md](docs/baseline-findings.md)** — the original long-form write-ups.
  Superseded by the two files above for day-to-day use; kept for detail.

## The rules that matter

1. **The pass mark is pre-committed and must not be weakened** (K-60): deflated Sharpe > 0.95
   against the *full* trial count, positive in both halves, ≥100 observations, net of measured
   costs. It has already killed three convincing false positives.
2. **Every tested configuration goes into `data/trials.json`** via `TrialsRegistry`. The
   deflated-Sharpe bar rises as the search widens — that self-penalty is the main defence
   against an automated search reporting its luckiest result.
3. **Report a break-even spread, not a net return** (K-26). Cost models charge no bid-ask
   spread by default, so a raw net figure hands the strategy free execution.
4. **Check whether the signal and the return share a price** (K-71). That single mistake
   produced a Sharpe of 6.48.
5. **Assume any long-only equity backtest here is inflated** until the universe is
   survivorship-free (K-80). The current NSE panel is today's F&O list backfilled.

## Environment

- Python **3.12** venv at `.venv312` (the old `.venv` is 3.9 and has no pytest). Run tests with
  `.venv312/bin/python -m pytest`.
- 324 tests currently pass. `tests/test_train_serve_parity.py` is a permanent CI guard: the
  backtest path silently drops non-numeric columns, so feature flags must be floats, not bools.
- Data is gitignored. Rebuild with `main.py fetch-history` (crypto) or
  `src/data/nse/history.py` (NSE).

## Working style the user has asked for

Quantitative, not simplified — they code and understand maths. State numbers with provenance.
When a result looks too good, find the bug before reporting it; three of the four best-looking
results so far were artefacts.
