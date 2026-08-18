# 🤖 Cipher — Automated Prediction Market Trading Engine & C2

> A low-latency, algorithmic execution system and remote Command & Control (C2) suite designed for Polymarket prediction markets, powered by real-time Binance WebSocket price action, dynamic Kelly criterion sizing, and automated risk management.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://www.python.org/)
[![Chain](https://img.shields.io/badge/Chain-Polygon%20(ChainID%20137)-8247E5?logo=polygon)](https://polygon.technology/)
[![Platform](https://img.shields.io/badge/Exchange-Polymarket%20CLOB%20v2-1F82EB)](https://polymarket.com)
[![Interface](https://img.shields.io/badge/Telegram-Command_%26_Control-2CA5E0?logo=telegram)](https://telegram.org)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL_v3.0-orange.svg)](https://www.gnu.org/licenses/agpl-3.0)

---

## 📌 Architecture Overview

Cipher operates 24/7 on a cloud VPS to ingest microsecond price feeds from Binance, detect directional momentum divergences (Delta-to-Open), evaluate order book microstructure and liquidity on the Polymarket CLOB, and manage positions across isolated paper and live execution books[cite: 3, 4, 5, 6].

```text
  ┌─────────────────────────────────────────────────────────────┐
  │                    Real-Time Ingestion                     │
  │     Binance Trade WebSocket  ◄──►  Polymarket Gamma/CLOB    │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                    Signal & Filter Funnel                   │
  │  • Delta-to-Open Gap          • Time Guardian (T-20 Expiry) │
  │  • Wick Grace Period          • Macro Trend Filter          │
  │  • Microstructure Skew Check  • Multi-Factor Quality Score  │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                 Quantitative Risk Engine                    │
  │  • Fractional Kelly Sizing    • Max Concurrent Exposure Cap │
  │  • Daily Drawdown Breaker     • Per-Symbol Asset Limits     │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
            ┌────────────────────┴────────────────────┐
            ▼                                         ▼
  ┌──────────────────┐                      ┌──────────────────┐
  │  Order Execution │                      │  Telegram C2 &   │
  │  • CLOB REST API │                      │   Telemetry Hub  │
  │  • Dual-Layer    │                      │  • Async Polling │
  │    Reconciliation│                      │  • 1600x900 Card │
  │  • Web3 Gas Mgmt │                      │    Synthesis     │
  └──────────────────┘                      └──────────────────┘
