# Pendle Boros — Фиксация ставок фандинга Hyperliquid и Spread Markets (2026-11-26 20:17 UTC)

- **Охват Boros Open API:** просканировано `249` рынков → активных (`TTM > 0`): **`39`**, из них Hyperliquid / Spread: **`17`**.
- **Зачем нужен Boros:** шорт `Yield Units` по `bestBid` превращает плавающий фандинг перпа Hyperliquid в **фиксированный APR до экспирации**, полностью снимая риск схлопывания ставки.

## Активные рынки Hyperliquid и Spread Markets на Pendle Boros

| ID | Рынок | До эксп. | Fixed Bid / Ask | Live Float | Disloc (Bid−Float) | Цена спреда | OI | Действие / Идея |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| `241` | **Hyperliquid BTC 25 Dec 2026** | 28.2д | **+6.69%** / +7.19% | +9.00% | -2.31% | 3.8 б.п. | 5,201,628 | Short Perp + Short Boros (Lock +6.69%) |
| `245` | **Hyperliquid HYPE 25 Dec 2026** | 28.2д | **+8.94%** / +9.37% | +10.95% | -2.01% | 3.4 б.п. | 391,777 | Short Perp + Short Boros (Lock +8.94%) |
| `244` | **Hyperliquid xyz:BRENTOIL 18 Dec 2026** | 21.2д | **-25.88%** / -20.00% | -28.07% | +2.19% | 34.1 б.п. | 17,623 | Long Perp + Long Boros (Lock 20.00%) |
| `242` | **Hyperliquid ETH - Gate ETHUSDT 25 Dec 2026** `[SPREAD]` | 28.3д | **+2.28%** / +2.30% | -4.08% | +6.36% | 0.2 б.п. | 9,958 | Short Boros Spread (Lock Fixed > Float) |
| `128` | **Hyperliquid ETH 25 Dec 2026** | 28.2д | **+5.95%** / +5.97% | +6.75% | -0.80% | 0.2 б.п. | 7,363 | Monitor |
| `250` | **Hyperliquid ETH - Gate ETHUSDT 29 Jan 2027** `[SPREAD]` | 63.3д | **+2.58%** / +2.60% | -4.08% | +6.67% | 0.4 б.п. | 1,396 | Short Boros Spread (Lock Fixed > Float) |
| `243` | **Hyperliquid BTC - Gate BTCUSDT 25 Dec 2026** `[SPREAD]` | 28.3д | **+2.07%** / +2.09% | -0.81% | +2.88% | 0.2 б.п. | 150 | Short Boros Spread (Lock Fixed > Float) |
| `247` | **Hyperliquid ETH 29 Jan 2027** | 63.2д | **+6.44%** / +6.46% | +6.75% | -0.32% | 0.4 б.п. | 138 | Short Perp + Short Boros (Lock +6.44%) |
| `249` | **Hyperliquid BTC - Gate BTCUSDT 29 Jan 2027** `[SPREAD]` | 63.3д | **+2.11%** / +2.13% | -0.81% | +2.92% | 0.3 б.п. | 63 | Short Boros Spread (Lock Fixed > Float) |
| `229` | **Hyperliquid BTC 25 Dec 2026** | 28.2д | **+6.75%** / +6.77% | +9.00% | -2.26% | 0.2 б.п. | 45 | Short Perp + Short Boros (Lock +6.75%) |
| `248` | **Hyperliquid BTC 29 Jan 2027** | 63.2д | **+7.31%** / +7.34% | +9.00% | -1.69% | 0.4 б.п. | 0 | Short Perp + Short Boros (Lock +7.31%) |
| `219` | **Hyperliquid BTC 27 Nov 2026** | 0.2д | **+3.38%** / +3.80% | +9.00% | -5.63% | 0.0 б.п. | 4,651,109 | Long Boros YU (Float > Ask) |
| `230` | **Hyperliquid HYPE 27 Nov 2026** | 0.2д | **+8.37%** / +10.05% | +10.95% | -2.58% | 0.1 б.п. | 1,185,051 | Monitor |
| `209` | **Hyperliquid ETH 27 Nov 2026** | 0.2д | **+2.14%** / +2.16% | +6.75% | -4.61% | 0.0 б.п. | 25,595 | Long Boros YU (Float > Ask) |
| `232` | **Hyperliquid ETH - Gate ETHUSDT 27 Nov 2026** `[SPREAD]` | 0.3д | **+0.38%** / +0.40% | -4.08% | +4.46% | 0.0 б.п. | 16,120 | Short Boros Spread (Lock Fixed > Float) |
| `233` | **Hyperliquid BTC - Gate BTCUSDT 27 Nov 2026** `[SPREAD]` | 0.3д | **-0.68%** / -0.66% | -0.81% | +0.13% | 0.0 б.п. | 339 | Watch Spread |
| `220` | **Hyperliquid BTC 27 Nov 2026** | 0.2д | **+3.95%** / +4.45% | +9.00% | -5.06% | 0.0 б.п. | 171 | Long Boros YU (Float > Ask) |
