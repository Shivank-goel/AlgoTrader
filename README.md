# Adaptive Crypto Trading System

A professional-grade, self-improving algorithmic trading system for **Delta Exchange India**. It detects market regimes, selects optimal strategies, manages risk, and continuously learns from performance.

## Features

- **7 trading strategies** across trend, mean-reversion, momentum, and breakout categories
- **Regime detection** using ADX, Bollinger Band Width, ATR, and volume
- **Meta-strategy selector** that ranks strategies by regime fit and rolling performance
- **Risk management** with position sizing, stop-loss enforcement, and circuit breakers
- **Walk-forward optimization** to prevent overfitting
- **Paper trading** on Delta Exchange testnet
- **Web dashboard** with live portfolio monitoring
- **Telegram notifications** for trades and alerts

## Quick Start

### 1. Prerequisites

- Python 3.10+
- Delta Exchange India account with API keys ([testnet](https://testnet.delta.exchange))

### 2. Installation

```bash
cd Crypto-trader
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

### 3. Configuration

Edit `config/settings.yaml` for trading pairs and mode, `config/risk.yaml` for risk limits, and `config/strategies.yaml` for strategy parameters.

Default assumptions:
- **Moderate risk** (2% per trade, 15% max drawdown)
- **BTC + ETH** perpetuals
- **Paper trading** on testnet

### 4. Run

```bash
# Paper trading (testnet)
python main.py paper

# Live trading engine
python main.py run

# Web dashboard
python main.py dashboard

# Portfolio status
python main.py status
```

## Project Structure

```
config/          # YAML configuration files
src/
  core/          # Engine, events, models
  data/          # Data fetching, indicators, WebSocket
  market/        # Regime detection, market state
  strategies/    # 7 strategies + selector
  risk/          # Position sizing, circuit breakers
  execution/     # Exchange API, order management
  portfolio/     # Portfolio tracking, trade journal
  learning/      # Performance scoring, optimization
  dashboard/     # FastAPI web UI
  notifications/ # Telegram alerts
tests/           # Unit tests
scripts/         # Backtest and optimization scripts
```

## Risk Management

| Parameter | Default |
|---|---|
| Risk per trade | 2% of equity |
| Max daily loss | 5% → halt trading |
| Max drawdown | 15% → close all |
| Max leverage | 5x |
| Max open positions | 3 |

## Testing

```bash
python -m pytest tests/ -v
```

## Backtesting & Optimization

```bash
python scripts/backtest.py --symbol BTCUSDT --start 2026-01-01 --end 2026-06-30
python scripts/optimize.py --symbol BTCUSDT --is-window 90 --oos-window 30
```

## Risk Disclosure

Algorithmic crypto trading involves substantial risk. This system includes robust risk management, but **no system guarantees profits**. Start with paper trading and minimum position sizes before going live.

## License

Private use only. Not financial advice.
