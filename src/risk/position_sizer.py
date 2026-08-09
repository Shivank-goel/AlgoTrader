"""Position sizing: fixed fractional, volatility-adjusted, and half-Kelly."""

from __future__ import annotations

from typing import Any, Optional

from src.core.models import Signal


class PositionSizer:
    """Calculates position size based on risk parameters."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        sizing = config.get("position_sizing", {})
        self.method = sizing.get("method", "fixed_fractional")
        self.risk_pct = sizing.get("risk_per_trade_pct", 2.0) / 100
        self.max_leverage = sizing.get("max_leverage", 5)
        self.min_order_size = sizing.get("min_order_size_usd", 10)

    def calculate(
        self,
        signal: Signal,
        equity: float,
        atr: Optional[float] = None,
        win_rate: float = 0.5,
        payoff_ratio: float = 1.5,
        size_multiplier: float = 1.0,
    ) -> float:
        if equity <= 0:
            return 0.0

        if self.method == "volatility_adjusted":
            size = self._volatility_adjusted(equity, signal, atr)
        elif self.method == "half_kelly":
            size = self._half_kelly(equity, win_rate, payoff_ratio, signal)
        else:
            size = self._fixed_fractional(equity, signal)

        size *= size_multiplier
        size = self._apply_leverage_cap(size, equity, signal.entry_price or 0)
        size = max(size, 0)

        notional = size * (signal.entry_price or 0)
        if notional < self.min_order_size:
            return 0.0

        return round(size, 6)

    def _fixed_fractional(self, equity: float, signal: Signal) -> float:
        risk_amount = equity * self.risk_pct
        entry = signal.entry_price or 0
        stop = signal.stop_loss or 0
        if entry == 0 or stop == 0:
            return 0.0
        risk_per_unit = abs(entry - stop)
        if risk_per_unit == 0:
            return 0.0
        return risk_amount / risk_per_unit

    def _volatility_adjusted(
        self, equity: float, signal: Signal, atr: Optional[float]
    ) -> float:
        if not atr or atr == 0:
            return self._fixed_fractional(equity, signal)
        risk_amount = equity * self.risk_pct
        return risk_amount / (atr * 2.5)

    def _half_kelly(
        self,
        equity: float,
        win_rate: float,
        payoff_ratio: float,
        signal: Signal,
    ) -> float:
        if payoff_ratio == 0:
            return self._fixed_fractional(equity, signal)
        kelly = win_rate - (1 - win_rate) / payoff_ratio
        kelly = max(0, kelly) * 0.5
        entry = signal.entry_price or 0
        if entry == 0:
            return 0.0
        return (equity * kelly) / entry

    def _apply_leverage_cap(self, size: float, equity: float, price: float) -> float:
        if price == 0:
            return size
        max_size = (equity * self.max_leverage) / price
        return min(size, max_size)

    def cap_to_available_balance(
        self, size: float, price: float, available_balance: float
    ) -> float:
        """Ensure order margin doesn't exceed available balance (with safety buffer).

        Isolated margin required = notional / leverage.
        We keep 30% headroom for fees, funding, and slippage.
        """
        if price <= 0 or available_balance <= 0:
            return size
        # ponytail: 0.7 headroom covers fees + funding + price move between
        # sizing and fill; raise if margin errors persist.
        max_notional = available_balance * self.max_leverage * 0.7
        max_size = max_notional / price
        return min(size, max_size)
