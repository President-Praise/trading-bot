# Trading Bot

Crypto trading bot with a modular architecture — backtesting engine, live execution on Deriv, Telegram alerts, and a performance dashboard.

## Features

- **Signal engine** — generates buy/sell signals from candle data
- **Risk manager** — position sizing, stop-loss, and drawdown limits
- **Backtesting** — replay historical data to validate strategies before going live
- **Live trading** — connects to Deriv's WebSocket API for real-time execution
- **Telegram alerts** — trade notifications with auto-generated charts
- **Dashboard** — monitor open positions and P&L

## Architecture

```
main.py
├── core/
│   ├── signal_engine.py    # signal generation
│   ├── trade_manager.py    # order execution
│   ├── risk_manager.py     # position sizing & limits
│   ├── candle_store.py     # OHLC data management
│   ├── deriv_client.py     # Deriv API wrapper
│   └── state_manager.py    # persistence
├── notifications/
│   ├── telegram_alerts.py  # Telegram integration
│   └── chart_generator.py  # trade chart images
├── backtest.py             # backtesting engine
└── dashboard.py            # live monitoring
```

## Setup

```bash
cp .env.example .env   # add Deriv API token, Telegram bot token, chat ID
pip install -r requirements.txt
python main.py
```

## Tests

```bash
pytest tests/
```
