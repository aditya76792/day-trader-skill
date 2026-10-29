# File contracts (India edition)

Every skill reads what the last one wrote, in a `trader/` folder in the
user's project. Times are India time (IST, `HH:MM` 24-hour, or ISO with
`+05:30`). Money is rupees (plain numbers, two decimals). Symbols are NSE
trading symbols in upper case (`RELIANCE`, `M&M`, `BAJAJ-AUTO`) unless
`exchange` says `BSE`. Indexes use the names in `market.INDEXES`
(`NIFTY`, `BANKNIFTY`, `SENSEX` ...).

All shared rules (calendar, sessions, expiries, lot sizes, ticks, circuits,
charges, tax, Yahoo/NSE/BSE access, bar parsing, indicators, config) live in
`trader-market/market.py`. Tools import it; they never copy its tables.

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker (paper or live ticket)
fno (option chain, Greeks, payoff) -> exit / budget for option trades
alerts (stop, target, square-off, expiry)      news / analyst / insider (on a schedule)
```

## trader/config.json
`market.DEFAULT_CONFIG` plus the user's overrides (see
`trader-budget/config.example.json`). Keys used across tools:
`capital_inr, broker, exchange, paper, cash_buffer_pct, max_position_pct,
max_order_inr, min_order_inr, max_loss_per_trade_inr, max_daily_loss_inr,
max_trades_per_day, max_orders_per_day, min_reward_risk, require_stop,
no_entries_before, no_entries_after, flat_by, default_product,
use_intraday_leverage, intraday_margin_pct, allow_short, allow_swing,
allow_futures, allow_option_buying, allow_option_selling,
max_premium_per_trade_inr, max_lots_per_trade, expiry_day_last_entry,
avoid_circuit_within_pct, min_avg_turnover_cr, block_series, blocked,
asm_gsm, fno_ban, risk_free_rate, slippage_pct`.

## trader/universe.txt
One NSE symbol per line (`# comments` allowed). Written by `scan.py universe`.

## trader/watchlist.json (scanner)
```json
{"created_by": "trader-scanner", "created_at": "2026-10-28T10:15:00+05:30", "scan": "orb",
 "date": "2026-10-28", "hits": [{"symbol": "RELIANCE", "setup": "orb", "price": 1171.2,
 "opening_range": {"high": 1168.0, "low": 1158.4, "minutes": 15}, "gap_pct": -0.4,
 "turnover_cr": 412.5, "note": "..."}]}
```

## trader/entry.json (entry)
```json
{"created_by": "trader-entry", "symbol": "RELIANCE", "exchange": "NSE", "setup": "orb",
 "side": "buy", "date": "2026-10-28", "status": "triggered|waiting|expired|no-data",
 "time": "09:50", "bar_start": "09:45", "bar_index": 6, "price": 1171.2, "level": 1168.0,
 "interval": "5m", "opening_range": {"high": 1168.0, "low": 1158.4, "minutes": 15},
 "vwap": 1164.3, "gap_pct": -0.4, "source": "yahoo", "checked_at": "...", "reason": "..."}
```
`side` is `buy` (long) or `sell` (short, intraday MIS only, needs `allow_short`).

## trader/plan.json (exit)
```json
{"created_by": "trader-exit", "symbol": "RELIANCE", "exchange": "NSE",
 "instrument": {"type": "EQ"}, "product": "MIS", "side": "buy", "date": "2026-10-28",
 "setup": "orb", "entry": 1171.2, "entry_time": "09:50", "entry_bar": 6, "interval": "5m",
 "opening_range": {"high": 1168.0, "low": 1158.4, "minutes": 15},
 "stop": 1163.2, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
 "target": 1187.2, "target_r": 2.0, "target_label": "2R",
 "risk_per_share": 8.0, "reward_risk": 2.0, "tick": 0.1,
 "flat_by": "15:10", "costs_per_share": 0.9, "net_reward_risk": 1.78,
 "circuit": {"band_pct": null, "upper": null, "lower": null}}
```
For an option trade (from trader-fno) `instrument` is
`{"type": "CE"|"PE", "underlying": "NIFTY", "expiry": "2026-11-03", "strike": 22700,
"lot_size": 65, "tradingsymbol": "NIFTY26N0322700CE"}` and entry/stop/target are
premiums; `product` is `MIS` or `NRML`. Futures: `{"type": "FUT", "underlying": ...,
"expiry": ..., "lot_size": ...}`.

## trader/timemachine.json
`{"created_by": "trader-timemachine", "symbol", "plan": {...}, "replays", "seed",
"target_first", "stop_first", "neither", "net_expectancy_r", "history": {...},
"verdict", "costs_per_share"}`.

## trader/orders.json (budget, broker-neutral)
```json
{"created_by": "trader-budget", "created_at": "...", "status": "proposed", "paper": true,
 "note": "Proposals only ...", "source": "trader/plan.json (RELIANCE orb 2026-10-28)",
 "orders": [{
   "exchange": "NSE", "symbol": "RELIANCE", "tradingsymbol": "RELIANCE",
   "instrument": {"type": "EQ"}, "side": "BUY", "qty": 125, "lots": null,
   "product": "MIS", "order_type": "LIMIT", "price": 1171.2, "trigger_price": null,
   "validity": "DAY",
   "exits": {"stop_loss": {"order_type": "SL-M", "trigger_price": 1163.2, "price": null},
             "target": {"order_type": "LIMIT", "price": 1187.2}},
   "why": "...",
   "plan": {"entry": 1171.2, "stop": 1163.2, "target": 1187.2, "risk_per_share": 8.0,
            "max_loss": 1000.0, "max_loss_with_costs": 1120.4, "setup": "orb",
            "entry_time": "09:50", "flat_by": "15:10"},
   "costs": {"round_trip_at_target": 140.2, "round_trip_at_stop": 120.4},
   "margin": {"required": 29280.0, "basis": "MIS 20% of 1,46,400"}}]}
```
`side` BUY/SELL. `qty` is units (for F&O a multiple of the lot; `lots` set).
`product` CNC (delivery), MIS (intraday), NRML (F&O carry). `order_type`
MARKET, LIMIT, SL, SL-M. Exits are separate orders placed after the entry
fills (no bracket orders on most Indian brokers; GTT OCO for CNC/NRML).
F&O stop-losses use `SL` (stop-limit) with a `price` buffer, because brokers
restrict SL-M and market orders in options.

## trader/state/risk.json (risk)
`{"checks": [{"fingerprint", "verdict": "PASS"|"BLOCK", "reasons": [...], "date", "time"}]}`.
Fingerprint = sha1 of the order's identity fields
(`exchange, tradingsymbol, side, qty, product, order_type, price, trigger_price`).

## trader/state/broker.json and trader/portfolio.json (broker)
broker.json: the log of yes / no / placed / failed / filled, per fingerprint, with
max loss. portfolio.json: `{"cash_inr": ..., "positions": {"RELIANCE|MIS": {"qty",
"avg_price", "product", "exchange", "instrument"}}}`.

## trader/paper/ (paper broker)
`ledger.json`: fills with charges, positions, realised P&L net of all charges,
cash. `contract_notes/<date>.json`: one per day, charges broken down like a
real contract note.

## trader/prices.json (alerts)
`{"created_by": "trader-alerts", "source": "yahoo", "at": "...", "quotes":
{"RELIANCE": {"price": 1171.2, "time": "...", "prev_close": 1196.2}}}`.

## trader/fno/chain-<SYMBOL>-<EXPIRY>.json (fno)
The normalised option chain: `{"symbol", "expiry", "underlying_value", "at",
"lot_size", "strikes": [{"strike", "CE": {"ltp","oi","chg_oi","iv","volume","bid","ask"},
"PE": {...}}]}`.

## trader/watchlist.json extras (scanner)
Hits may also carry `side`, `turnover_cr`, `prev_close_basis` (official / last trade);
the document carries `context` (NIFTY, BANKNIFTY, India VIX) and `skipped` (symbol -> reason).
`scan.py universe` also writes `trader/universe.json` (symbol, name, series, ISIN), which
trader-news reads for company names.

## trader/backtest.json (timemachine backtest)
Per symbol: trades, win rate (net of charges), average net P&L, expectancy in R, max
drawdown, total charges, charges as a share of gross profit, and the trade list.

## trader/metrics.json (analyst)
Per symbol: quarters (period end, fiscal label, basis consolidated/standalone, revenue,
EBITDA, net profit to owners, EPS, all in Rs crore except EPS), YoY/QoQ growth, margins,
notes. Sources: NSE `/api/integrated-filing-results` (from 2025) and
`/api/corporates-financial-results` (older), each row's `xbrl` file.

## trader/state/insider.json and news.json
What has already been shown (fingerprints with dates), so `new` and the news monitor only
report items not seen before.
