---
name: trader-exit
description: >-
  Exit Planner for NSE and BSE trades. Sets the stop and the target before
  you enter: opening-range midpoint or low, ATR, percent, your price, just
  under VWAP or the previous day's low; targets at 2R (or any R, a price, a
  percent, the previous day's high), capped at the upper circuit when the
  band is known. Rounds every price to the exchange tick in the safe
  direction, prices the round trip with Indian charges (STT, exchange, SEBI,
  stamp, GST, brokerage, DP), reports reward:risk before and after costs,
  and sets flat_by before your broker's MIS auto square-off. Also plans index
  and stock options and futures by lot (expiry-day theta, physical
  settlement). Writes trader/plan.json. Use when the user says "where's my
  stop", "set my stop and target", "plan the exit", "stop loss", "what's my
  R", "risk reward after charges", "plan this NIFTY call", or right after
  /trader-entry triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|vwap|prev-day-low|price:X|pct:X|atr:K] [--target 2R|price:X|prev-day-high] [--product MIS|CNC|NRML]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is
where the setup has failed. The target is where you take what you came for.
Both are set BEFORE the entry, rounded to a price the exchange will accept,
priced with every Indian charge, and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a circuit or a fast market can fill past it. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: or-mid stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-low --target 1.5R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop vwap --target prev-day-high
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1171.2 --or-high 1168 --or-low 1158.4
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol TCS --entry 3120.5 --stop pct:2 --product CNC
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --instrument CE --underlying NIFTY --expiry 2026-11-03 \
    --strike 22700 --entry 120 --stop 90 --target 2R
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--side sell` (a short: needs `allow_short` and MIS), `--flat-by
15:10`, `--band 5 --prev-close 1000` (circuit), `--qty` / `--lots` (size for
the charges), `--valid-days 5` (CNC/NRML), `--chain FILE` (trader-fno's chain
for an option's LTP), `--json`, `--offline FILE`, `--now "2026-10-28 09:50"`
(IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, exchange, side, price, time, bar, opening range, VWAP, previous close |
| `trader/config.json` | `flat_by` (15:10), `broker` (for charges and auto square-off), `min_reward_risk` (1.5), `max_loss_per_trade_inr`, `max_order_inr`, `allow_short`, `allow_futures`, `allow_option_selling`, `bands` |
| `trader/fno/chain-<SYM>-<EXPIRY>.json` | optional: the option's LTP when `--entry` is not given |
| Yahoo chart API (through market.py) | only for atr, vwap and prev-day rules: free, unofficial, delayed |
| `trader/plan.json` | with `--write`: the CONTRACTS.md shape (entry, stop and rule, target and R, risk, tick, flat_by, costs_per_share, net_reward_risk, circuit, instrument) plus costs, warnings, valid_until |

Data from Yahoo, NSE and BSE public endpoints is unofficial and delayed, and
the endpoints can change or block scripts without notice.

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (default for orb and gap entries) |
| `or-low` / `or-high` | the range low for a long, the range high for a short |
| `atr:K` | K x ATR(14) of the session's 5-minute bars up to the entry bar |
| `pct:X`, `price:X` | X percent from the entry; your price |
| `vwap` | one tick beyond the session VWAP (default for vwap_reclaim) |
| `prev-day-low` / `prev-day-high` | one tick beyond the previous session's low (long) or high (short) |

Targets: `2R` (default), any `NR`, `price:X`, `pct:X`, `prev-day-high` (long)
or `prev-day-low` (short).

- **The tick.** NSE equity ticks depend on price (0.01 under Rs 250, 0.05 to
  Rs 1,000, 0.10 to Rs 5,000 ...); options and futures 0.05. The stop
  rounds AWAY from the entry (the risk is never understated), the target
  TOWARD it (the reward is never overstated), a long's entry up.
- **Circuits.** With a known band, a long's target above the upper circuit
  is capped at the circuit (a short's at the lower one).
- **Refused, with the reason:** a stop on the wrong side, a target on the
  wrong side, a risk under 0.05% of the price, an index in the cash market,
  a short without `allow_short` or outside MIS, F&O as CNC, a date the
  exchange is shut, an expired contract, an entry that has not triggered.

## Charges

The round trip at the target and at the stop goes through market.py's
`round_trip` for the plan's product: MIS = intraday (STT 0.025% on the sell),
CNC = delivery (STT 0.1% both sides plus the DP charge), options (STT 0.15%
of the premium sold), futures. The reference size is what your
`max_loss_per_trade_inr` allows at this risk (capped by `max_order_inr`), or
one lot for F&O. It reports `costs_per_share`, `net_reward_risk` and warns
when charges eat more than 25% of the reward, common on low-priced stocks and
cheap options.

## Flat by, overnight and expiry

- **MIS.** `flat_by` from the config (15:10), never at or after your
  broker's MIS auto square-off (Zerodha 15:25 equity, 15:26 F&O; Upstox
  15:20; Angel One 15:15), which closes you out itself and charges for it.
- **CNC / NRML.** No flat_by: a `valid_until` date in trading days, and a
  warning that the next open can gap straight through the stop.
- **Expiry day.** Theta and gamma warnings for options. Stock options and
  futures are PHYSICALLY SETTLED: an in-the-money position open at the close
  means delivery of the whole lot, so flat_by is mandatory whatever the
  product. Index options settle in cash (STT on an exercised option).

## Steps

1. Run it right after `/trader-entry` triggers, or with `--entry` to plan
   ahead. For an option, take the premium from trader-fno.
2. Use the default stop unless the user names another. A level setup and
   every option plan need one: ask.
3. Run `exit.py plan --write`. Read back entry, stop and rule, target and
   rule, risk, reward:risk before and after charges, and flat by.
4. If it refuses, say the reason and the fix. Never loosen anything on your
   own.
5. Hand off to `/trader-timemachine`, then `/trader-budget`. Offer
   `/trader-alerts` for the stop, target and flat-by time.

## Rules

- Not financial advice. Nothing here promises a profit; a 2R target is not a
  prediction that the price gets there.
- No order goes through from here. Only trader-broker places one, after
  trader-risk passes it and the user types an explicit "yes" for that one
  order.
- The stop and the target are set before the entry, never after; never move
  a stop further from the entry once the trade is on.
- Never round a stop toward the entry or a target away from it.
- Paper trading is the default; going live is the user's own decision.
