---
name: trader-fno
description: >-
  F&O Desk for NSE and BSE index and stock options. Reads the live NSE option
  chain (or one saved from your browser) and explains positioning: OI and
  change in OI by strike, PCR, max pain, call and put OI walls, ATM straddle
  and the move it prices in, IV and skew, OI build-up (long build-up, short
  covering ...). Solves IV and Greeks (Black-Scholes for European options),
  draws the payoff of up to four legs with full Indian charges (STT 0.15% on
  premium, exchange, stamp, GST), lists weekly and monthly expiries with lot
  sizes, and builds a cautious option-BUYING plan (strike by delta, premium
  stop, target in R) into trader/plan.json for trader-budget, trader-risk
  and trader-broker. Warns about expiry day, theta and gamma, and physical
  settlement of stock options. Use when the user says "NIFTY option chain",
  "PCR", "max pain", "which strike", "Greeks", "IV", "payoff", "bull call
  spread", "buy a call on BANKNIFTY", "expiry day trade".
argument-hint: "[chain SYMBOL | plan SYMBOL --view bullish | payoff --leg ...]"
---

# trader-fno

The F&O desk. Futures and options are where most Indian retail traders
lose money, so this skill reads the market carefully, prices everything
after charges, and only ever proposes option BUYING by default. Selling
options and trading futures are blocked downstream (trader-risk) unless
the user turns on `allow_option_selling` or `allow_futures` in their
config.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/fno.py" expiries NIFTY                    # dates, lot, strike step
python3 "${CLAUDE_SKILL_DIR}/fno.py" chain NIFTY                       # nearest expiry
python3 "${CLAUDE_SKILL_DIR}/fno.py" chain BANKNIFTY --expiry 2026-11-23 --strikes 8
python3 "${CLAUDE_SKILL_DIR}/fno.py" chain RELIANCE --offline chain.json
python3 "${CLAUDE_SKILL_DIR}/fno.py" greeks --symbol NIFTY --spot 22698.85 --strike 22700 --type CE \
    --premium 181.15 --expiry 2026-11-03
python3 "${CLAUDE_SKILL_DIR}/fno.py" payoff --symbol NIFTY --spot 22698.85 \
    --leg "BUY CE 22700 181.15" --leg "SELL CE 22900 93.5"
python3 "${CLAUDE_SKILL_DIR}/fno.py" plan NIFTY --view bullish --stop-pct 30 --target-r 2 --write
```

## What it reads and writes

| file | what |
| --- | --- |
| NSE `/api/option-chain-v3` | the option chain (needs NSE's cookies; `--offline FILE` otherwise) |
| NSE `/api/option-chain-contract-info` | the list of expiries |
| `trader/fno/chain-SYMBOL-EXPIRY.json` | the normalised chain + summary (trader-exit reads option prices from it) |
| `trader/plan.json` | an option-buying plan (with `--write`) |
| `trader/config.json` | `risk_free_rate`, `expiry_day_last_entry`, `allow_option_buying`, broker for charges |

If NSE blocks the script (common on cloud machines and some networks):
open `https://www.nseindia.com/option-chain` in a browser, then open
`https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=03-Nov-2026`
in the same browser, save the JSON, and pass `--offline` with that file.
For stocks use `type=Equity`.

## Reading the chain honestly

- **PCR** (put OI / call OI): high means many puts are open. Writers and
  hedgers both sell options, so it is not a direction signal on its own.
- **Max pain**: the expiry price where option buyers collectively get the
  least. Prices do not have to go there.
- **OI walls**: the biggest call OI strike is often read as resistance and
  the biggest put OI as support. They move during the day.
- **ATM straddle**: the price of the at-the-money call plus put, about the
  move the market prices in by expiry. A buyer needs a move bigger than
  that just to break even.
- **IV and skew**: high IV makes options expensive to buy. Puts usually
  carry higher IV than calls (skew).
- **OI build-up**: price up + OI up = long build-up; price down + OI up =
  short build-up; price up + OI down = short covering; price down + OI
  down = long unwinding.

## Plans (option buying)

`plan` picks the strike with delta nearest `--delta` (0.5 = at the money)
on the nearest expiry, enters at the ask, puts the stop `--stop-pct` (30%)
under the premium and the target at `--target-r` (2R), rounds to the 0.05
tick, adds the round-trip charges for one lot and the net reward:risk
after charges. It writes nothing about size: trader-budget sizes in whole
lots against your max loss per trade (charges included), trader-risk
checks lots, premium cap, expiry-day cut-off and your daily loss limit,
and trader-broker shows the ticket. One NIFTY lot is 65 units, so a 30%
stop on a Rs 180 premium risks about Rs 3,500 a lot: say so plainly when
it is bigger than the user's limit.

## India-specific rules it applies

- Weekly expiries exist only for NIFTY (Tuesday) and SENSEX (Thursday).
  BANKNIFTY, FINNIFTY, MIDCPNIFTY and stocks are monthly.
- Expiry day: theta (time decay) is fastest and gamma is highest; new
  entries stop at `expiry_day_last_entry` (13:30). Short options pay an
  extra 2% ELM and lose calendar-spread margin benefit that day.
- Stock options that expire in the money are physically settled: the
  buyer must take or give delivery of the whole lot. Stock option plans
  always carry a flat-by time.
- An option exercised in the money pays STT of 0.15% of intrinsic value.
- Option orders use stop-limit (SL) exits with a buffer: brokers restrict
  market and SL-M orders in options.
- Margin for selling options or futures runs to lakhs per lot and changes
  daily (SPAN + exposure). This skill never calls a short safe; check your
  broker's margin calculator.

## Rules

- Not financial advice. SEBI found about 9 in 10 individual F&O traders
  lost money in FY22-FY24. Say this when the user is about to trade options.
- No order goes through without the user's explicit "yes" for that one
  order, through trader-broker. This skill never places anything.
- Option buying only by default. Never suggest turning on option selling
  or futures; it is the user's decision in their own config.
- The chain is NSE's public data, unofficial for this use, delayed by
  minutes, and NSE can block or change it. Check prices with your broker
  before acting.
