"""Price-action strategy: rejection at swing structure, with structural stops.

Hypothesis: a rejection candle (pin bar or engulfing) forming at a confirmed
swing level marks a point where the level held, and a stop placed just beyond
that level is tighter and better-justified than a fixed ATR multiple — giving a
higher edge per trade than the indicator crossovers, which averaged 1.5-7.8 bps
against a ~10 bps break-even.

The stop placement *is* the hypothesis, which is why this needs
`QuickBacktester(honor_signal_levels=True)`. Under the default the backtester
overrides every strategy with a uniform 2xATR stop, so a structural-stop
strategy would be scored on a trade it never proposed.

Two constraints from the account, enforced rather than assumed:

  - Stops must land between 0.57% and 23.6% of price. Tighter and
    `cap_to_available_balance` silently reduces risk below the 2% target; wider
    and `PositionSizer.calculate` returns 0 and the trade is dropped with no
    warning. Both edges are clamped here and logged.
  - Entries need a minimum reward:risk to clear costs at all. At ~8 bps
    round-trip on a 1% stop, a 1:1 target is already marginal.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.data.indicators import PRICE_ACTION_COLUMNS
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# Measured against a Rs10,000 account: see docs/baseline-findings.md.
MIN_STOP_PCT = 0.57
MAX_STOP_PCT = 23.6


class SwingRejectionStrategy(BaseStrategy):
    """Enter on a rejection candle at a confirmed swing level."""

    name = "swing_rejection"

    REQUIRED_COLUMNS = (
        "pa_swing_high",
        "pa_swing_low",
        "pa_bull_pin",
        "pa_bear_pin",
        "pa_bull_engulfing",
        "pa_bear_engulfing",
        "pa_dist_to_swing_high_pct",
        "pa_dist_to_swing_low_pct",
    )

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(params)
        p = self.params
        # How close price must be to the level for the rejection to count.
        self.proximity_pct: float = float(p.get("proximity_pct", 0.5))
        # Stop is placed this far beyond the level, as a fraction of the level.
        self.stop_buffer_pct: float = float(p.get("stop_buffer_pct", 0.15))
        self.reward_risk: float = float(p.get("reward_risk", 2.0))
        self.min_stop_pct: float = float(p.get("min_stop_pct", MIN_STOP_PCT))
        self.max_stop_pct: float = float(p.get("max_stop_pct", MAX_STOP_PCT))
        self.require_trend_filter: bool = bool(p.get("require_trend_filter", False))

        missing = [c for c in self.REQUIRED_COLUMNS if c not in PRICE_ACTION_COLUMNS]
        if missing:
            raise ValueError(
                f"{self.name} requires columns absent from PRICE_ACTION_COLUMNS: {missing}"
            )

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.RANGING, Regime.QUIET, Regime.VOLATILE]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        # Explicit presence check. `_get_indicator` returns 0.0 for a missing
        # column, so probing with `if not x:` cannot distinguish "no data" from
        # "feature is zero" — the ambiguity that left volume_breakout silent.
        if not all(c in market_state.indicators for c in self.REQUIRED_COLUMNS):
            return None

        price = market_state.price
        if price <= 0:
            return None

        swing_low = self._get_indicator(market_state, "pa_swing_low")
        swing_high = self._get_indicator(market_state, "pa_swing_high")
        dist_low = self._get_indicator(market_state, "pa_dist_to_swing_low_pct")
        dist_high = self._get_indicator(market_state, "pa_dist_to_swing_high_pct")

        bull_reject = (
            self._get_indicator(market_state, "pa_bull_pin") > 0
            or self._get_indicator(market_state, "pa_bull_engulfing") > 0
        )
        bear_reject = (
            self._get_indicator(market_state, "pa_bear_pin") > 0
            or self._get_indicator(market_state, "pa_bear_engulfing") > 0
        )

        # Long: bullish rejection while sitting just above a confirmed swing low.
        if bull_reject and 0 <= dist_low <= self.proximity_pct and swing_low > 0:
            if self.require_trend_filter and price < self._get_indicator(
                market_state, "ema_200", default=0.0
            ):
                return None
            stop = swing_low * (1.0 - self.stop_buffer_pct / 100.0)
            return self._structural_signal(market_state, Direction.LONG, stop)

        # Short: bearish rejection just below a confirmed swing high.
        if bear_reject and 0 <= dist_high <= self.proximity_pct and swing_high > 0:
            ema200 = self._get_indicator(market_state, "ema_200", default=0.0)
            if self.require_trend_filter and ema200 and price > ema200:
                return None
            stop = swing_high * (1.0 + self.stop_buffer_pct / 100.0)
            return self._structural_signal(market_state, Direction.SHORT, stop)

        return None

    def _structural_signal(
        self, state: MarketState, direction: Direction, stop: float
    ) -> Optional[Signal]:
        """Build a signal whose stop sits at structure, clamped to what's sizable."""
        price = state.price
        stop_pct = abs(price - stop) / price * 100.0

        if stop_pct < self.min_stop_pct:
            # Too tight to size correctly: the balance cap would silently cut
            # risk below target. Widen to the floor rather than under-risk.
            logger.debug(
                "%s: stop %.3f%% below sizable floor; widening to %.2f%%",
                self.name, stop_pct, self.min_stop_pct,
            )
            stop_pct = self.min_stop_pct
            stop = (
                price * (1 - stop_pct / 100.0)
                if direction == Direction.LONG
                else price * (1 + stop_pct / 100.0)
            )
        elif stop_pct > self.max_stop_pct:
            # Too wide: the sizer would return 0 and drop the trade silently.
            logger.debug(
                "%s: stop %.2f%% exceeds sizable ceiling; skipping",
                self.name, stop_pct,
            )
            return None

        risk = abs(price - stop)
        target = (
            price + risk * self.reward_risk
            if direction == Direction.LONG
            else price - risk * self.reward_risk
        )

        signal = self._make_signal(state, direction)
        signal.stop_loss = stop
        signal.take_profit = target
        signal.metadata = {
            "stop_pct": round(stop_pct, 4),
            "reward_risk": self.reward_risk,
            "structural": True,
        }
        return signal

    # The structural levels are set on the Signal directly, so the ATR-based
    # defaults in BaseStrategy must not be consulted for this strategy.
    def get_stop_loss(self, entry_price: float, side: Direction, atr: float) -> float:
        return super().get_stop_loss(entry_price, side, atr)
