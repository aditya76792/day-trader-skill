---
name: trader-alerts
description: >-
  Price Alerts for NSE and BSE. Rings when a price, a stop, a target or the
  opening bell hits, and knows the Indian market day: IST clock with the NSE
  pre-open (09:00-09:08), matching (09:08-09:15), normal market (09:15-15:30)
  and closing session (15:40-16:00), the 2026 exchange holidays and Muhurat
  trading. Turns trader/plan.json into stop, target and flat-by alerts
  (equity or option premium), reminds you before your broker's MIS auto
  square-off, rings on NIFTY / SENSEX / stock expiry days, near upper or
  lower circuit, on a big opening gap, and on NIFTY or SENSEX moves with the
  market-wide circuit breaker levels. Fetches free delayed Yahoo quotes
  (RELIANCE.NS, ^NSEI) and writes trader/prices.json. Use when the user says
  "alert me when", "tell me if RELIANCE hits", "watch my stop", "remind me to
  square off", "is the market open", "is today expiry", "when does NSE open",
  "get me a price", or right after /trader-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. Most Indian brokers have no bracket orders, so
the user's stop-loss is a separate SL-M order (SL for options) placed after
the entry fills; the alert tells the user what happened and when it is time
to be flat. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
The tool imports the shared India core in `trader-market/market.py` (IST time,
holidays, sessions, expiries, circuits, brokers' square-off times).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                            # NSE session now, next expiries
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                  # stop, target, flat-by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1163.2
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --stop 3071.5 --side sell    # a short (MIS) stop is above
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by" --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --square-off --segment equity              # 10 min before auto square-off
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --expiry NIFTY --ring-at 13:30
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SAMPLECO --circuit 2 --band 5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --gap 1.5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol NIFTY --index-move 2
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                            # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check --price NIFTY26N0322700CE=118.5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE TCS --write       # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-28 15:15"` pins the clock (IST); `--offline DIR` reads saved
`yahoo-<SYMBOL>.json` files instead of the network; `--json` is machine output;
`--config` points at `trader/config.json` (broker, flat_by, bands).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit (or trader-fno), for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when (IST), so each rings once |
| `trader/prices.json` | latest quotes, merged in (`created_by`, `source`, `at`, `quotes`), for trader-budget and trader-risk |
| `trader/config.json` | `broker` (square-off time), `flat_by` (default 15:10), `bands` (circuit bands per symbol) |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | how often |
| --- | --- | --- |
| `above` / `target` | price at or above `price` (a `sell` target: at or below) | once, until `reset` or `"rearm": true` |
| `below` / `stop` | price at or below `price` (a `sell` stop: at or above) | same |
| `pct_move` | moved `pct` % from the previous close (or `ref`) | once a day |
| `time` | the IST clock passed `at` on a trading day | once a day |
| `opening_bell` | the normal market opened at 09:15 (not the pre-open) | once a trading day |
| `square_off` | `minutes_before` (10) before the broker's MIS auto square-off | once a trading day |
| `expiry` | `at` on an expiry day of `underlying` (holiday shifts included) | once that day |
| `circuit` | within `within_pct` % of the upper or lower circuit, when the band is known | once a day |
| `gap` | the 09:15 open is `pct` % or more from the previous close, checked 09:15 to `until` (09:16) | once a day |
| `index_move` | NIFTY / SENSEX moved `pct` %, plus one ring per market-wide circuit level | once a day per level |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today.

## India-specific rules it knows

- **Sessions.** Pre-open order entry 09:00-09:08 (nothing trades), matching
  09:08-09:15, normal market 09:15-15:30, closing session 15:40-16:00 (trades
  only at the closing price). The opening bell is 09:15.
- **Holidays.** The NSE 2026 list (Diwali-Balipratipada 10 Nov, Guru Nanak
  Jayanti 24 Nov, Christmas ...). No alert rings on a holiday. Muhurat trading
  (8 Nov 2026, a Sunday) is a one-hour evening session whose time NSE sets by
  circular: `clock` says it is coming and that the time is not known here.
- **Auto square-off.** Brokers close open MIS positions themselves (Zerodha
  15:25 equity, 15:26 F&O; Upstox 15:20; Angel One 15:15) and charge about
  Rs 50 plus GST per order. `square_off` rings 10 minutes before, so the user
  exits at a price they choose.
- **Expiry days.** NIFTY weekly on Tuesday, SENSEX weekly on Thursday, monthly
  contracts on the last Tuesday (NSE) or Thursday (BSE), moved to the previous
  trading day on a holiday (the November 2026 NSE monthly is Monday 23 Nov).
  Stock F&O held to expiry is physically settled.
- **Circuits.** Stocks outside F&O have fixed bands (2, 5, 10, 20 %); F&O
  stocks have dynamic bands, so the tool needs the band from you (`--band` or
  `"bands"` in config). At a circuit only one side trades: an exit may not fill.
- **Market-wide circuit breakers.** A 10 %, 15 % or 20 % move in NIFTY 50 or
  SENSEX halts all equity and F&O trading; the halt length depends on the time
  of day (`market.index_circuit_halt`).

## Quotes

- `yahoo`: `query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1m`
  (query1 as fallback, retries in `market.Net`). The price is the latest
  one-minute close, the previous close is `meta.previousClose`. Indexes use
  Yahoo's codes (`^NSEI`, `^BSESN`); BSE symbols use `.BO`.
- Stooq is not used: it does not cover NSE symbols reliably.
- Options and futures are not on Yahoo. Option premiums come from
  trader-fno (a quote under 15 minutes old in `trader/prices.json`) or from
  `check --price SYMBOL=PREMIUM` with a price the user reads from the broker.
- Data from Yahoo, NSE and BSE public endpoints is unofficial, delayed and can
  break. Fine for alerts and for checking a size, never for timing an order.
  When some symbols fail, the output says `partial:` and which rules were not
  checked.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm the
   three alerts back to the user: the stop, the target and the flat-by time.
   For MIS trades offer `add --square-off`; on expiry days `add --expiry`.
2. Run `check` while the trade is open: every few minutes from a scheduled
   task or `/loop`. A check reads the latest delayed quote; it is not a live
   feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means the
   SL-M or target order has probably filled: check the broker and record the
   fill with trader-broker. "Flat by" or "square-off" means exit anything still
   open: trader-broker `flatten`, then trader-risk, then the user's explicit
   yes. Never act for them.

## Example

RELIANCE on Wednesday 2026-10-28: entry 1171.20 at 09:50, stop 1163.20
(opening-range midpoint), target 1187.20 (2R), flat by 15:10. `add --from-plan`
writes `reliance-plan-stop`, `reliance-plan-target` and `reliance-flat-by`. A
`check` at 10:15 reads 1171.20 and says "Nothing hit". At 15:10:

```
ALERT Flat by 15:10: it is 15:10 IST (set for 15:10), the normal market closes at 15:30. If RELIANCE is
still open, exit it: trader-broker flatten --symbol RELIANCE builds the exit ticket, then trader-risk and your yes.
```

## Rules

- An alert is a fact about a price or the clock. Say it as a fact, in IST.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.
- Never places, cancels or changes an order, and never predicts a price.
- Not financial advice. Paper trading is the default (`"paper": true`).
- Nothing in this pack places an order except trader-broker, and only when
  trader-risk passed that exact order today AND the user typed an explicit
  yes for that one order in the chat AND the user clicked Allow once on
  Claude's own card (the order tool set to "Needs approval"). Never set an
  order tool to "Always allow". No order goes through without the user's yes.
