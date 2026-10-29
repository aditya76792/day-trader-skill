---
name: trader-budget
description: >-
  Budget Manager for NSE/BSE trades in rupees. Sizes one trade from the plan
  (trader-exit or trader-fno) so the loss at the stop INCLUDING every charge
  (brokerage, STT, exchange fees, SEBI fee, stamp duty, GST, DP) stays inside
  your max loss per trade, then bounds it by your position cap, max order,
  cash and margin (MIS full value or SEBI's 5x cap, CNC full value, futures
  margin estimate) and, for F&O, whole lots, max lots and a premium cap.
  Writes one broker-neutral order (LIMIT entry, separate SL-M or SL stop-loss
  and LIMIT target exits, MIS/CNC/NRML) to trader/orders.json. Refuses option
  selling and futures unless your config allows them. Use when the user says
  "how many shares", "how many lots", "size this trade", "I have 1 lakh",
  "I only want to risk Rs 1,000 a trade", "set my rules", "intraday or
  delivery", or after /trader-exit writes a plan.
argument-hint: "[capital in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1171.20, stop 1163.20")
into an exact order ("buy 42 shares, MIS, LIMIT 1171.20"), inside rules the
user sets once. The stop decides the size, and in India the charges are big
enough to matter: on a small intraday trade STT, exchange fees, stamp duty
and GST can be 10-15% of the risk. So the size is worked out on the loss AT
THE STOP INCLUDING CHARGES. It writes a proposal only. trader-risk checks it
and trader-broker asks the user yes or no. Most retail intraday and F&O
traders in India lose money; sizing keeps each loss small, it does not make
a trade win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1171.00 --write # LIMIT at 1171.00 (default: at the plan's entry)
python3 "${CLAUDE_SKILL_DIR}/size.py" --market --json         # MARKET order, sized at the worse price
python3 "${CLAUDE_SKILL_DIR}/size.py" --amo --write           # an after-market order (AMO)
python3 "${CLAUDE_SKILL_DIR}/size.py" --now "2026-10-28 09:50"  # pin the clock (IST)
```

A plan for another day is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: instrument, product, entry, stop, target (trader-exit / trader-fno) |
| `trader/prices.json` | optional, `--market` only: a fresh price (under 15 minutes) worse than the plan's entry |
| `trader/portfolio.json` | optional: its `cash_inr` caps the size |
| `trader/orders.json` | the one proposed order it writes with `--write` (see docs/CONTRACTS.md) |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` there and
ask the user for each value in plain words:

- **capital_inr** (1,00,000): money for this, and only money they can afford to lose.
- **max_loss_per_trade_inr** (1,000) and **max_daily_loss_inr** (3,000).
- **max_position_pct** (50), **max_order_inr** (80,000), **min_order_inr** (500), **cash_buffer_pct** (10).
- **broker** ("zerodha", "upstox", "angelone" or "flat20"): sets brokerage and the auto square-off time.
- **default_product** ("MIS"), **use_intraday_leverage** (false) and **intraday_margin_pct** (20).
- Day rules trader-risk enforces: **max_trades_per_day** (3), **min_reward_risk** (1.5, net of
  charges), **no_entries_before** / **no_entries_after** ("09:30" / "14:45"), **flat_by** ("15:10"),
  **expiry_day_last_entry** ("13:30"), all IST.
- F&O: **allow_option_buying** (true), **allow_option_selling** (false), **allow_futures** (false),
  **max_lots_per_trade** (2), **max_premium_per_trade_inr** (5,000), **futures_margin_pct** (15, an estimate).
- Lists: **blocked**, **asm_gsm**, **fno_ban**, **block_series** (BE, BZ ...: trade-for-trade).
- Keep **paper** true until they have watched it work on the built-in paper broker.

## Step 2: size the trade

```
qty = floor(max_loss_per_trade_inr / (risk per unit + charges per unit at the stop))
```

Charges are not linear (brokerage caps at Rs 20, DP is per scrip), so the
qty is checked again with the exact charges and stepped down until the loss
at the stop including charges fits. Then the smallest of these wins:

| limit | units |
| --- | --- |
| max loss | as above, charges included |
| position cap | capital x `max_position_pct` / value per unit (futures: margin per unit) |
| max order | `max_order_inr` / value per unit |
| cash / margin | (cash minus the buffer) / margin per unit: CNC and option buying full value; MIS full value, or 20% (SEBI's 5x cap) with `use_intraday_leverage`; futures `futures_margin_pct` of the value |
| premium cap | option buying: `max_premium_per_trade_inr` / premium |
| max lots | F&O: `max_lots_per_trade` x lot size |

F&O is sized in whole lots (lot size from the plan, else `market.py lots`).
If one lot already breaks the max loss, it refuses with the rupee number.

Prices go on the exchange tick (Rs 0.01 under 250, 0.05 to 1,000, 0.10 to
5,000 ... options and futures 0.05). A long stop rounds down and a target
rounds down, so the rounding never hides risk.

It refuses, and writes no order, when: the size is zero, the order is under
the minimum, net reward:risk after charges is under `min_reward_risk`, the
plan is for another day, the symbol is blocked or in the F&O ban, the plan
is an option SELL without `allow_option_selling`, a future without
`allow_futures`, a short without `allow_short`, or a CNC short (impossible:
it ends in the exchange auction). Say the reason in one line.

## Step 3: the order it writes

One entry order (LIMIT at the plan's entry by default, MARKET with
`--market`), product MIS, CNC or NRML, plus its exits as separate orders:

- **stop-loss**: equity SL-M at the stop trigger; F&O SL (stop-limit) with a
  limit buffer (`sl_limit_buffer_pct`, 2%), because brokers restrict SL-M
  and market orders in options. If the price gaps past the limit, the stop
  does not fill: say so.
- **target**: a LIMIT order.
- MIS: place both right after the entry fills; when one fills, cancel the
  other. CNC / NRML: use a GTT OCO. Bracket and cover orders are
  discontinued at most Indian brokers, so none are used.

It also writes the costs (round trip at the stop and at the target), the
margin and its basis, and a plain "why".

## Example: the reel

RELIANCE on Wednesday 2026-10-28, opening-range breakout: entry 1171.20 at
09:50, stop 1163.20 (opening-range midpoint), target 1187.20 (2R). Example
config, Zerodha charges.

| limit | the math | shares |
| --- | --- | --- |
| max loss | Rs 1,000 / (8.00 + 0.83 charges) | 113 |
| position cap | 50% of Rs 1,00,000 / 1171.20 | **42 (decides it)** |
| max order | Rs 80,000 / 1171.20 | 68 |
| cash | Rs 90,000 / 1171.20 | 76 |

42 shares = Rs 49,190.40. At the stop: 42 x 8.00 = Rs 336.00 plus Rs 52.08
charges = Rs 388.08. At the target: Rs 672.00 minus Rs 52.73 charges = Rs
619.27, net reward:risk 1.60. With the caps lifted the max loss decides: 113
shares, not the 125 that Rs 1,000 / 8.00 suggests, because of the charges.

A NIFTY 22700 CE on its expiry day 2026-11-03 (lot 65) at premium 70, stop
64, target 85: one lot (Rs 4,550 of premium, under the Rs 5,000 cap), stop
SL trigger 64.00 limit 62.70, loss at the stop Rs 447.24 with charges.

## Rules

- Proposals only. This skill never calls a broker tool and never places, approves or schedules an order.
- No order goes through without the user's explicit "yes" for that one order (trader-broker), after a
  PASS from trader-risk.
- Never size money the user has not said is for this. Ask for the capital and the max loss.
- Every number comes from the config, the plan, the prices or the core charges table (market.py). Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Never shrinks a stop to fit more shares, never raises a limit on its own.
- Option selling and futures stay off unless the user switches them on in their own config.
- Prices from Yahoo/NSE/BSE public endpoints are unofficial, delayed and can break. The broker's price
  is the one that fills; a stop can fill worse than its price in a gap.
- Not financial advice. Paper trading by default (`"paper": true`).
