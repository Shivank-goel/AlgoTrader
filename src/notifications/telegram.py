"""Telegram bot notifications for trades and alerts."""

from __future__ import annotations

import logging
import os

from src.core.models import Signal

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends trade alerts and system notifications via Telegram."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._bot = None

        if self.enabled and self.token and self.chat_id:
            try:
                from telegram import Bot

                self._bot = Bot(token=self.token)
            except ImportError:
                logger.warning("python-telegram-bot not installed")
                self.enabled = False

    async def send_message(self, text: str) -> None:
        if not self.enabled or not self._bot:
            logger.info("Notification: %s", text)
            return
        try:
            await self._bot.send_message(chat_id=self.chat_id, text=text, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send Telegram message")

    async def send_alert(self, message: str) -> None:
        await self.send_message(f"<b>Alert</b>\n{message}")

    async def notify_trade(self, signal: Signal, size: float) -> None:
        emoji = "🟢" if signal.direction.value == "long" else "🔴"
        entry = f"{signal.entry_price:.2f}" if signal.entry_price is not None else "N/A"
        stop = f"{signal.stop_loss:.2f}" if signal.stop_loss is not None else "N/A"
        target = f"{signal.take_profit:.2f}" if signal.take_profit is not None else "N/A"
        text = (
            f"{emoji} <b>Trade Signal</b>\n"
            f"Symbol: {signal.symbol}\n"
            f"Direction: {signal.direction.value.upper()}\n"
            f"Strategy: {signal.strategy_name}\n"
            f"Size: {size:.4f}\n"
            f"Entry: {entry}\n"
            f"Stop: {stop}\n"
            f"Target: {target}\n"
            f"Confidence: {signal.confidence:.0%}\n"
            f"Regime: {signal.regime.value if signal.regime else 'N/A'}"
        )
        await self.send_message(text)

    async def daily_summary(
        self,
        equity: float,
        daily_pnl: float,
        trades_count: int,
        win_rate: float,
    ) -> None:
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        text = (
            f"{pnl_emoji} <b>Daily Summary</b>\n"
            f"Equity: ${equity:,.2f}\n"
            f"Daily P&L: ${daily_pnl:,.2f}\n"
            f"Trades: {trades_count}\n"
            f"Win Rate: {win_rate:.0%}"
        )
        await self.send_message(text)
