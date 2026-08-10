"""Trading cost model: fees, slippage and perpetual funding.

Used by both the backtester and paper-mode execution, so a simulated fill and
a backtested fill are priced by the same code. That shared path is the point:
a backtest that prices fills differently from paper trading cannot be
validated against it.

Scale of what was being ignored: Delta India charges 0.05% taker plus 18% GST,
so a round trip costs about 12 bps before slippage or funding. On the recorded
scoreboard BTCUSD/macd scored -0.21 over 139 trades *with zero costs modelled*.
Costs are not a rounding error here; they plausibly decide whether the existing
strategies have any edge at all.

Defaults reflect Delta Exchange India as of 2026-08:
  - taker 0.05%, maker 0.02%, both +18% GST
  - funding exchanged every 8h at 05:30 / 13:30 / 21:30 IST
    (= 00:00 / 08:00 / 16:00 UTC), typically capped near 1% per period
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from src.core.models import Direction, OrderSide

GST_MULTIPLIER = 1.18

DEFAULT_TAKER_FEE_BPS = 5.0 * GST_MULTIPLIER   # 0.05% -> 5.9 bps
DEFAULT_MAKER_FEE_BPS = 2.0 * GST_MULTIPLIER   # 0.02% -> 2.36 bps

BPS = 1e-4


@dataclass(frozen=True)
class CostModel:
    """Deterministic, always-adverse cost estimates.

    Every method is pure so the backtester and the live paper path can share
    it without coordination.
    """

    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS

    # Slippage = base + impact (size vs bar liquidity) + volatility term.
    base_slippage_bps: float = 2.0
    impact_coef_bps: float = 5000.0    # bps per unit of notional/bar_volume_usd
    vol_coef: float = 0.05             # fraction of (atr/price) in bps terms
    max_slippage_bps: float = 50.0

    # Funding: UTC hours at which funding is exchanged.
    funding_hours_utc: tuple[int, ...] = (0, 8, 16)
    funding_bps_per_period: float = 1.0
    max_funding_bps_per_period: float = 100.0

    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    # -- construction ---------------------------------------------------

    @classmethod
    def from_config(cls, config: Optional[dict[str, Any]]) -> CostModel:
        """Build from the `costs:` block of config/risk.yaml."""
        cfg = (config or {}).get("costs", {}) or {}

        def _get(key: str, default: float) -> float:
            return float(cfg.get(key, default))

        hours = cfg.get("funding_hours_utc", [0, 8, 16])
        return cls(
            taker_fee_bps=_get("taker_fee_bps", DEFAULT_TAKER_FEE_BPS),
            maker_fee_bps=_get("maker_fee_bps", DEFAULT_MAKER_FEE_BPS),
            base_slippage_bps=_get("base_slippage_bps", 2.0),
            impact_coef_bps=_get("impact_coef_bps", 5000.0),
            vol_coef=_get("vol_coef", 0.05),
            max_slippage_bps=_get("max_slippage_bps", 50.0),
            funding_hours_utc=tuple(int(h) for h in hours),
            funding_bps_per_period=_get("funding_bps_per_period", 1.0),
            max_funding_bps_per_period=_get("max_funding_bps_per_period", 100.0),
        )

    @classmethod
    def zero(cls) -> CostModel:
        """A frictionless model, for isolating cost impact in A/B runs."""
        return cls(
            taker_fee_bps=0.0,
            maker_fee_bps=0.0,
            base_slippage_bps=0.0,
            impact_coef_bps=0.0,
            vol_coef=0.0,
            max_slippage_bps=0.0,
            funding_bps_per_period=0.0,
        )

    @property
    def is_zero(self) -> bool:
        return (
            self.taker_fee_bps == 0.0
            and self.maker_fee_bps == 0.0
            and self.base_slippage_bps == 0.0
            and self.funding_bps_per_period == 0.0
        )

    # -- slippage -------------------------------------------------------

    def slippage_bps(
        self,
        *,
        price: float,
        atr: Optional[float] = None,
        bar_volume_usd: Optional[float] = None,
        notional: Optional[float] = None,
    ) -> float:
        """Adverse slippage in basis points, clamped to a sane band.

        The volatility term is what matters on this venue's thin symbols, where
        bar volume is small enough that the impact term alone would understate
        the true cost of getting filled.
        """
        if self.max_slippage_bps <= 0:
            return 0.0

        slippage = self.base_slippage_bps

        if notional and bar_volume_usd and bar_volume_usd > 0:
            slippage += self.impact_coef_bps * (notional / bar_volume_usd)

        if atr and price > 0:
            slippage += self.vol_coef * (atr / price) / BPS

        return float(min(self.max_slippage_bps, max(self.base_slippage_bps, slippage)))

    def _apply_slippage(self, price: float, *, buying: bool, slippage_bps: float) -> float:
        """Move the price against the trader, always."""
        factor = 1.0 + slippage_bps * BPS if buying else 1.0 - slippage_bps * BPS
        return max(0.0, price * factor)

    def entry_fill_price(
        self,
        mid: float,
        direction: Direction,
        *,
        atr: Optional[float] = None,
        bar_volume_usd: Optional[float] = None,
        notional: Optional[float] = None,
    ) -> float:
        """Fill price for opening a position: buys pay up, sells sell down."""
        slip = self.slippage_bps(
            price=mid, atr=atr, bar_volume_usd=bar_volume_usd, notional=notional
        )
        return self._apply_slippage(mid, buying=direction == Direction.LONG, slippage_bps=slip)

    def exit_fill_price(
        self,
        mid: float,
        direction: Direction,
        *,
        atr: Optional[float] = None,
        bar_volume_usd: Optional[float] = None,
        notional: Optional[float] = None,
    ) -> float:
        """Fill price for closing a position of the given direction.

        Closing a long is a sell, so the adverse direction flips.
        """
        slip = self.slippage_bps(
            price=mid, atr=atr, bar_volume_usd=bar_volume_usd, notional=notional
        )
        return self._apply_slippage(mid, buying=direction != Direction.LONG, slippage_bps=slip)

    def fill_price_for_side(
        self,
        mid: float,
        side: OrderSide,
        *,
        atr: Optional[float] = None,
        bar_volume_usd: Optional[float] = None,
        notional: Optional[float] = None,
    ) -> float:
        """Fill price from an order side — the execution-path entry point."""
        slip = self.slippage_bps(
            price=mid, atr=atr, bar_volume_usd=bar_volume_usd, notional=notional
        )
        return self._apply_slippage(mid, buying=side == OrderSide.BUY, slippage_bps=slip)

    # -- fees -----------------------------------------------------------

    def fee_usd(self, notional: float, *, maker: bool = False) -> float:
        """Fee on one side of a trade, in USD. Never negative."""
        rate = self.maker_fee_bps if maker else self.taker_fee_bps
        return abs(notional) * rate * BPS

    def round_trip_fee_bps(self, *, maker: bool = False) -> float:
        rate = self.maker_fee_bps if maker else self.taker_fee_bps
        return 2.0 * rate

    # -- funding --------------------------------------------------------

    def funding_periods(self, entry_ts: datetime, exit_ts: datetime) -> int:
        """Count funding exchanges in the half-open interval (entry, exit].

        A position opened at 07:59 and closed at 08:01 UTC pays once; one held
        entirely between boundaries pays nothing.
        """
        if exit_ts <= entry_ts or not self.funding_hours_utc:
            return 0

        count = 0
        day = entry_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = exit_ts.replace(hour=0, minute=0, second=0, microsecond=0)

        while day <= end_day:
            for hour in self.funding_hours_utc:
                boundary = day + timedelta(hours=hour)
                if entry_ts < boundary <= exit_ts:
                    count += 1
            day += timedelta(days=1)

        return count

    def funding_usd(
        self,
        notional: float,
        direction: Direction,
        entry_ts: datetime,
        exit_ts: datetime,
        *,
        rate_bps: Optional[float] = None,
    ) -> float:
        """Funding paid (positive) or received (negative) over the hold.

        With a positive funding rate longs pay shorts, which is the usual state
        in a bull market. Returns a cost: positive means it reduces P&L.
        """
        periods = self.funding_periods(entry_ts, exit_ts)
        if not periods:
            return 0.0

        rate = self.funding_bps_per_period if rate_bps is None else rate_bps
        rate = max(-self.max_funding_bps_per_period, min(self.max_funding_bps_per_period, rate))

        cost = abs(notional) * rate * BPS * periods
        return cost if direction == Direction.LONG else -cost

    # -- aggregate ------------------------------------------------------

    def round_trip_cost_pct(
        self,
        *,
        entry_price: float,
        exit_price: float,
        direction: Direction,
        entry_ts: Optional[datetime] = None,
        exit_ts: Optional[datetime] = None,
        size: float = 1.0,
        maker: bool = False,
        funding_rate_bps: Optional[float] = None,
    ) -> float:
        """Total round-trip cost as a percentage of entry notional.

        Returned as a positive percentage to subtract from a gross return.
        """
        if entry_price <= 0 or size <= 0:
            return 0.0

        entry_notional = entry_price * size
        exit_notional = exit_price * size

        fees = self.fee_usd(entry_notional, maker=maker) + self.fee_usd(
            exit_notional, maker=maker
        )

        funding = 0.0
        if entry_ts is not None and exit_ts is not None:
            funding = self.funding_usd(
                entry_notional, direction, entry_ts, exit_ts, rate_bps=funding_rate_bps
            )

        return (fees + funding) / entry_notional * 100.0

    def fingerprint(self) -> str:
        """Stable identity for reproducibility keys on stored backtest runs."""
        import hashlib

        parts = (
            f"{self.taker_fee_bps:.6f}|{self.maker_fee_bps:.6f}|"
            f"{self.base_slippage_bps:.6f}|{self.impact_coef_bps:.6f}|"
            f"{self.vol_coef:.6f}|{self.max_slippage_bps:.6f}|"
            f"{','.join(str(h) for h in self.funding_hours_utc)}|"
            f"{self.funding_bps_per_period:.6f}"
        )
        return hashlib.sha256(parts.encode()).hexdigest()[:16]
