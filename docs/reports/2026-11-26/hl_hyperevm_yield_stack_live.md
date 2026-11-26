# HyperEVM Composable Delta-Neutral Yield Stack · kHYPE / Felix / HyperLend / Boros (2026-11-26 20:17 UTC)

- **Живые ставки:** `HYPE` Mark `$62.57` · HYPE Perp Funding **`+10.95% APR`** · Pendle Boros Fix: `+8.94%` (Hyperliquid HYPE 25 Dec 2026, 28.2д)
- **TVL экосистемы HyperEVM:** `kHYPE` `$554.7M` (`1.88%`), `stHYPE` `$135.6M` (`1.99%`), `HyperLend kHYPE` `$158.5M`, `Felix WHYPE` `$15.9M`, `Morpho FEUSDCV2` `$13.4M` (`5.20%`), **HLP** `$152.6M` (**`15.68%`**).

## Сравнение дельта-нейтральных связок (с учётом расщепления маржи и буфера ликвидации)

| Стратегия | Тип ставки | Gross APR (на ногу) | Эфф. капитала | **Чистый APR на депозит** | vs HLP | Плечо | Буфер ликв. | Главные риски |
|---|---|---:|---:|---:|---:|---:|---:|---|
| **Composable Loop: kHYPE -> Felix/HyperLend (50% LTV) -> Short HYPE** | `Fixed (Boros)` | +10.82% | 100.0% | **+9.07%** | -6.61% | 2.0x | 36% | Двусторонний риск ликвидации (обвал HYPE на CDP + ралли HYPE на перпе), рост borrow rate |
| **kHYPE + Short HYPE Perp (Floating)** | `Floating` | +12.83% | 66.7% | **+8.55%** | -7.13% | 2.0x | 42% | Падение/отрицательный фандинг HYPE, kHYPE депег |
| **stHYPE + Short HYPE Perp + Boros Fix** | `Fixed (Boros)` | +10.93% | 66.7% | **+7.29%** | -8.39% | 2.0x | 42% | stHYPE ликвидность/депег, доливка маржи при ралли HYPE |
| **kHYPE + Short HYPE + Boros Fix (28d)** | `Fixed (Boros)` | +10.82% | 66.7% | **+7.21%** | -8.47% | 2.0x | 42% | kHYPE депег, контракт Boros/Kinetiq, доливка маржи при ралли HYPE |
| **Benchmark: HLP Vault (Hyperliquidity Provider)** | `Benchmark` | +15.68% | 100.0% | **+15.68%** | +0.00% | 1.0x | 100% | Токсичный поток / инвентарь ликвидаций (кейс JELLYJELLY), 4д lockup |
| **Benchmark: Liminal Basis (xHYPE)** | `Benchmark` | +9.07% | 100.0% | **+9.07%** | -6.61% | 1.0x | 100% | Смарт-контракт протокола, плавающий фандинг HYPE |
