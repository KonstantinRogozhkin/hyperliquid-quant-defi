# `hyperliquid-quant-defi` — Техническая Wiki

Полная инженерная документация автономного quantitative & on-chain DeFi стека **`hl_quant`** (`src/hl_quant/`, **135 unit-тестов**, **7 cron-контуров**).

## Разделы документации

| Раздел | О чём страница |
|---|---|
| [🧬 Альфа-фабрика и аудит издержек (`1.5 → 5.0 б.п.`)](Hyperliquid-Alpha-Research-and-Metamodel.md) | 58 гипотез в 6 семействах, 60-мин `bbo` аудит реального `ALO` fill rate (`55%`), перевод метамодели в observation-only |
| [📈 Paper L2 трекер, Микроструктура и 5-DEX v2](Hyperliquid-Paper-Tracker-and-Cross-DEX.md) | 5-DEX сканер (`810` перпов), Cross-DEX Paper v2 (`HL × Lighter × Extended`), Persistence Gate и H1 Basis+Carry |
| [🧪 Testnet `ALO` экзекутор, `ADL Rank` и Пост-трейд атрибуция](Hyperliquid-Testnet-Execution-and-Attribution.md) | Живое `ALO`-исполнение (`95.7%` fill), защита `onlyIsolated`, точный `isolated/cross` ADL Rank, Growth `0.1×` API quota |
| [📐 Маркет-мейкер Avellaneda–Stoikov v2](Avellaneda-Stoikov-Market-Making.md) | Калибровка $\kappa$ (`ETH 0.544`, `BTC 0.327`), `f: true` fast-cancel, чистые `Alo`-батчи, `Standard` mempool check, VPIN, `p50 = 5.62 µs` |
| [⛓️ HyperEVM Chain 999 (`L1Read`, Пег `USDT0`, V3 глубина) и HLP](HyperEVM-DeFi-and-HLP-Scanner.md) | Прямые `eth_call` чтения HyperSwap/PRJX V3, `L1Read` (`0.00 б.п.`), поправка на пег `USDT0/USDC` (`+11.08 б.п.`) |
| [🏛️ HIP-3 (`Growth Mode`, `Oracle Div`), Pendle Boros и Yield Stack](HIP3-Boros-and-HyperEVM-Yield-Stack.md) | `xyz/io/para` кросс-билдер арбитраж (независимая маржа, 30д кулдаун), Pendle Boros Fix, `kHYPE + Boros` vs HLP и 4ч Persistence Tracker |
| [🏆 Карта ликвидаций, `:00` OOS (52d / 60k), H6 и HTML-дашборд](Liquidation-Heatmap-Funding-Alpha-and-Tear-Sheet.md) | Направленные киты H2 (`ETH $52M`), 3 режима H6 (`CRASH_BOOK_STRESS` vs `CROWDED_FUNDING_BASIS`), Deposit-Clean HLP `pnlHistory`, 52d `:00` OOS |
