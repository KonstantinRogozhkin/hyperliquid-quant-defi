# План шага 2: отбор монет по train-периоду из всей вселенной Hyperliquid

Дата: 2026-11-25. Issue: #8. Продолжение после скрининга мейкерских комиссий.

## Цель

Уйти от ручного списка 10 альтов и проверить, даёт ли устойчивое преимущество отбор монет с сильным/волатильным спредом funding из всей вселенной перпов Hyperliquid — строго по train-периоду, с фильтром ликвидности и без утечки в test.

## Контракт данных и фильтрации (без lookahead)

1. **Список кандидатов (universe discovery)**:
   - Загрузить текущий список активных (не `isDelisted`) перпов Hyperliquid через `POST /info` (`metaAndAssetCtxs`).
   - Пересечь с доступными USDT-M перпами целевой CEX (`GET /fapi/v1/exchangeInfo` на Binance или `GET /v5/market/instruments-info?category=linear` на Bybit) с учётом маппинга `kPEPE -> 1000PEPEUSDT`.
   - Зафиксировать **ограничение survivorship bias**: текущий листинг не содержит делистингнутых монет; явно указывать это в отчёте.

2. **Фильтр ликвидности (строго по train)**:
   - Нельзя фильтровать по сегодняшнему `dayNtlVlm` или `openInterest` из `metaAndAssetCtxs` как по единственному критерию, потому что это знание из конца test-окна.
   - Вместо этого для скачанных 1h свечей (`hl_bars`, `cex_bars`) считать **train-only median daily notional volume** (`close * volume`, агрегированный по дням внутри `hour_ms < split`):
     - Минимальный порог по обеим ногам: например, `min_train_daily_vol_usd >= 5_000_000` (настраиваемый CLI-флаг `--min-train-vol-usd`).
     - Минимальная полнота истории в train: не менее 80% часов train-окна с валидными ценами и funding на обеих биржах (чтобы новые листинги с 3 днями истории не ломали сравнение, либо выделять их в отдельный эксперимент).
   - Текущий `day_notional_volume` из `metaAndAssetCtxs` использовать только как мягкий pre-filter перед скачиванием (например, `>= $1M`), чтобы не качать сотни мёртвых тикеров, и фиксировать это в ограничениях.

3. **Отбор монет строго на train (`hour_ms < split`)**:
   - Для каждой прошедшей фильтр ликвидности монеты прогнать фиксированную сетку параметров на train (или оценить по единому базовому параметру, чтобы не переобучать параметры отдельно на каждую монету).
   - Ранжировать монеты по **train Sharpe** (или `train_net_apr` при минимальном `time_in_market >= 10%` и `train_apr > 0`).
   - Выбрать топ-`K` монет (например, `K in {5, 10, 15}`) и один общий набор параметров сетки по пулу выбранных монет на train.
   - На test (`hour_ms >= split`) состав корзины, веса (равные `1/K`) и параметры не меняются.

4. **Walk-forward контроль (опционально во второй итерации)**:
   - Если статический 60/40 split даст устойчивый положительный test, проверить rolling walk-forward (например, 60 дней train → 14 дней test со сдвигом каждые 14 дней), чтобы поймать короткие всплески funding без переобучения на один режим рынка.

## Планируемые модули

- `src/hl_quant/universe_selection.py`:
  - `discover_common_coins(hl_client, cex_client, venue, min_prefilter_vol_usd)`
  - `filter_by_train_liquidity(panels, split_hour_ms, min_train_daily_vol_usd, min_train_coverage)`
  - `select_top_coins_on_train(panels, split_hour_ms, costs, top_k, grid)`
- `scripts/analytics/run_hl_funding_arb_universe_backtest.py`:
  - CLI с флагами `--venue`, `--days`, `--top-k`, `--min-train-vol-usd`, `--hl-fee-bps`, `--cex-fee-bps`, `--slippage-bps`, `--output-dir`.
- Тесты в `tests/test_hl_funding_arb_universe.py`: проверка отсутствия утечки из test в фильтр ликвидности и в рейтинг монет.
