---
name: trader-risk
description: >-
  Risk Manager for NSE/BSE orders. Checks every proposed order against your
  limits and Indian market rules before it can reach a broker, and prints
  PASS or BLOCK with the reason: a stop is required, the loss at the stop
  INCLUDING charges must fit your max loss, net reward:risk after charges,
  daily loss (realised plus today's risk), trades per day, the entry window
  in IST, NSE holidays and 09:15-15:30 hours (AMO aside), MIS after flat_by
  or the broker's square-off, expiry-day cutoffs, MIS/CNC/NRML and short-sale
  rules, trade-for-trade series, ASM/GSM and F&O ban lists, circuit limits,
  lot multiples, max lots, option premium cap, option selling and futures
  switches, ticks, margin and stale prices. Use when the user says "check my
  order", "is this safe", "risk check", "can I take this trade", "how much
  have I lost today", or before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or by trader-broker's flatten goes through it. A
BLOCK is final for that order: it does not go to the user for a yes and it
does not go to any broker, paper or live. These limits keep each loss and
each day's losses small. They do not make a trade win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-28 09:50"  # pin the clock (IST)
```

Exit code 0 when every order passes, 1 when any order is blocked or on an error.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits (`trader-budget/config.example.json`) |
| `trader/portfolio.json` | live cash and positions (`cash_inr`, `positions` keyed by tradingsymbol and product) |
| `trader/paper/ledger.json` | in paper mode: the paper broker's cash, positions and realised P&L |
| `trader/prices.json` | fresh quotes (trader-alerts); MARKET entries need one under 15 minutes old |
| `trader/state/broker.json` | what was placed today and live realised P&L (trader-broker) |
| `trader/state/risk.json` | the verdicts it writes: `checks` with fingerprint, verdict, reasons, date, time |

## Entries and exits

An **exit** only reduces a position you hold (same tradingsymbol and
product). Getting out must always be possible, so an exit is checked for
format, tick and lot size only; every day rule becomes a warning. Anything
else is an **entry**.

## The checks (entries)

| check | blocks when |
| --- | --- |
| calendar | not an NSE/BSE trading day (weekend or exchange holiday, e.g. Diwali-Balipratipada 2026-11-10) |
| session | outside 09:15-15:30 IST, unless the order is an AMO (`variety: amo`). AMOs: refused while the market is open, refused for MIS entries (they would fill at the 09:15 open, before your entry window), refused for F&O entries into an expiry day; CNC/NRML AMOs pass with a gap warning, also on holidays and weekends |
| daily loss | per symbol, the larger of today's planned max losses and the realised loss (a stop that filled past its trigger counts at the real loss), plus this order; the paper ledger counts in paper mode, live fills in live mode |
| F&O stop-limit | the loss is counted at the SL limit price (the worst price the stop can fill at), not the trigger |
| window | before `no_entries_before` / after `no_entries_after`; MIS at or after `flat_by` or the broker's auto square-off (Zerodha 15:25 equity, 15:26 F&O); F&O on its expiry day after `expiry_day_last_entry` |
| product | MIS intraday, CNC needs `allow_swing`, NRML only for F&O, CNC never for F&O |
| short | an equity sell you do not hold without `allow_short`; any CNC short (it fails settlement and goes to the auction) |
| series | a trade-for-trade series (`block_series`: BE, BZ ...) as MIS |
| lists | `blocked`, `asm_gsm`; `fno_ban` blocks new F&O positions in that underlying |
| circuit | entry within `avoid_circuit_within_pct` of the circuit you would run into, or outside the band |
| options / futures | an option SELL without `allow_option_selling`, a future without `allow_futures` |
| lots / premium | qty not a multiple of the lot, more than `max_lots_per_trade`, premium over `max_premium_per_trade_inr` |
| stop | no stop while `require_stop`; a stop on the wrong side; SL-M stops on F&O (use SL with a limit) |
| loss | qty x (entry - stop) + round-trip charges at the stop over `max_loss_per_trade_inr` |
| reward:risk | (reward - charges at the target) / (loss incl. charges) under `min_reward_risk` |
| daily loss | the larger of today's realised loss and today's entries' max losses, plus this one, over `max_daily_loss_inr` |
| trades / orders | `max_trades_per_day` entries or `max_orders_per_day` orders already placed; a duplicate |
| size / margin | over `max_position_pct` of capital or `max_order_inr`, under `min_order_inr`, margin over the cash after the buffer |
| tick | a price not on the exchange tick (the exchange rejects it) |
| stale price | a MARKET or SL-M entry without a quote under 15 minutes old |

How the numbers work:

- **The entry it measures from**: the limit price for LIMIT and SL orders;
  for MARKET the worse of the plan's entry and a fresh quote, so a plan from
  09:50 cannot understate the risk at 10:40.
- **Charges** come from `market.py` (STT, exchange charges, SEBI fee, stamp
  duty, GST, the broker's brokerage, DP on a delivery sell), so a CNC trade
  carries more cost than the same MIS trade.
- **Daily loss** never resets on a winning trade: a full-size loss used up
  is used up.
- Warnings do not block but say them: a LIVE account, an AMO, a stop that
  lives only in the plan, an SL limit that can be skipped in a gap, physical
  settlement of stock options held into expiry, a futures margin estimate.

## Steps

1. If the price may have moved, refresh it (trader-alerts quote --write).
2. Run `check.py`.
3. Tell the user, order by order: PASS or BLOCK, the max loss at the stop
   with charges, net reward:risk, and how much of today's limits it uses.
   For a BLOCK give the reason and the fix (fewer shares, a closer stop,
   wait for the window, skip the trade). Never change a limit yourself.
4. If the order changes in any way, check again: trader-broker matches each
   verdict by fingerprint (sha1 of exchange, tradingsymbol, side, qty,
   product, order type, price, trigger price) and checks the exits did not
   change; a verdict from another day does not count.

## Example

```
PASS  #1 BUY 42 RELIANCE MIS, entry
          entry 1171.20 (the limit price), stop 1163.20, target 1187.20: 42 x 8.00 = Rs 336.00 at the stop + charges Rs 52.08 = Rs 388.08
          limit Rs 1,000 a trade; net reward:risk 1.60 (min 1.5)
```

The same order on 2026-11-10 is blocked (exchange holiday). 125 shares are
blocked even though 125 x 8.00 is exactly Rs 1,000: the charges take it over.
A NIFTY call at 13:45 on its expiry day is blocked by the 13:30 cutoff.

## Rules

- A PASS only means the order may be shown to the user. It is not a yes, and no order goes through
  without the user's explicit "yes" for that one order in trader-broker.
- It never places an order, never edits an order to make it pass, never loosens a limit, never blocks
  an exit that only gets the user out.
- Every number comes from the config, the order, the account, the prices or the broker log.
- Quotes from Yahoo/NSE/BSE public endpoints are unofficial, delayed and can break. The broker's price
  is the one that fills, and a stop can fill worse than its price in a fast market or a gap.
- Not financial advice. Paper trading by default (`"paper": true`).
