---
name: trader-timemachine
description: >-
  Time Machine for NSE and BSE trades. Replays a planned day trade before you
  risk a rupee: 10,000 seeded Monte Carlo replays of the rest of the
  09:15-15:30 session built from the stock's own 5-minute bars over its last
  ~60 NSE sessions (holidays left out), plus the same rule re-run on every
  past session, all net of Indian charges (STT, exchange, SEBI, stamp, GST,
  brokerage, DP) and slippage, with the flat_by exit. Shows target first,
  stop first or neither, net expectancy in R and rupees, overnight gap risk
  for CNC swing plans, and one honest verdict. Also backtests a simple rule
  (ORB long or short, gap-and-go, VWAP reclaim) over 60 days for one or more
  symbols with win rate, expectancy, max drawdown and charges paid. Use when
  the user says "run the time machine", "replay this trade", "what are the
  odds", "monte carlo", "backtest ORB on RELIANCE", "is this a coin flip",
  "what do charges do to this", or after /trader-exit, before /trader-budget.
argument-hint: "[run|chart|backtest] [plan.json or SYMBOL ...]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks: if today plays out like the stock's own last 60 sessions, how
often does this plan hit the target first, the stop first, or neither, and
what is left after Indian charges and slippage? It answers with 10,000
replays and with the real record of the same rule on every past session. It
is a simulation, not a prediction. Not financial advice. SEBI's own studies
found about 9 in 10 retail F&O traders lost money; a good-looking replay does
not change the odds.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:30      # override the plan's flat_by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-28 \
    --entry 1171.2 --stop 1163.2 --target 1187.2 --entry-time 09:50
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" backtest RELIANCE TCS --rule orb --target 2R --write
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" backtest SBIN --rule orb-short
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" backtest TMPV --rule gap
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--qty N` (size for the charges), `--json`, `--offline FILE` (a saved
Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>-60d5m.json`), `--now "2026-10-28 15:45"` (IST). Backtest:
`--stop-rule or-mid|or-low|vwap|pct:X|atr:K`, `--or-minutes`, `--flat-by`,
`--product MIS|CNC`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, exchange, product, side, date, entry, entry_time / entry_bar, stop, stop_rule, target, opening range, flat_by |
| `trader/config.json` | `broker`, `slippage_pct` (0.05), `max_loss_per_trade_inr`, `max_order_inr`, `max_lots_per_trade`, `flat_by`, `no_entries_before`, `no_entries_after` |
| Yahoo chart API (through market.py) | one request a symbol: `range=60d&interval=5m`, the most Yahoo keeps; free, unofficial, delayed, can break |
| `trader/timemachine.json` | with `run --write`: replays, seed, target_first, stop_first, neither, net_expectancy_r, costs_per_share, history, verdict, plus the fan-chart bands and gap risk |
| `trader/backtest.json` | with `backtest --write`: per-symbol and total trades, win rate, average net P&L, expectancy, max drawdown, total charges, charges as % of gross profit, every trade |

Data from Yahoo, NSE and BSE public endpoints is unofficial and delayed; the
endpoints can change or block scripts at any time.

## How the replays work

1. **The pool.** Only FULL NSE sessions before the plan's date: all 75
   five-minute bars, 09:15 to 15:30 IST, on NSE trading days. Short
   sessions, a Muhurat session, holiday data and today are left out (and
   listed). Every bar becomes four ratios to the bar before it.
2. **One replay.** Start at the entry. For each bar left, pick a past bar
   from the same time of day (plus or minus 2 bars) at random and scale it.
   The stop is checked before the target; a bar touching both counts as the
   stop; a bar that opens through the stop fills at its open.
3. **Until.** A trade still open at the plan's `flat_by` (15:10 by default)
   exits at the close of the bar ending then. CNC plans run to 15:30.
4. **Costs.** Every replay's round trip goes through market.py's charges
   for the plan's product (MIS intraday, CNC delivery with DP, futures by
   lot) at the size your max loss allows, with `slippage_pct` against you
   on both fills. Net expectancy is shown in R and in rupees.
5. **Overnight gaps (CNC).** Replays still open at 15:30 take a next-morning
   gap drawn from the data's own close-to-open gaps; a gap through the stop
   fills at the open. It shows how often that happened, and how many real
   gaps in the data were bigger than the stop distance.
6. **History.** The plan's own rule (orb, gap-and-go or VWAP reclaim, the
   same opening range, stop rule and R) on every past session, inside your
   entry window, with the same charges. A level setup has no history.
7. **The verdict.** Of the replays that hit a side, how often the target
   came first against the break-even share 1 / (1 + R). Under 5 points:
   "Close to a coin flip" (1R) or "Close to break-even". 5 to 15: "Leans
   toward". 15 or more: "Strongly toward". Lines under it: many replays hit
   neither, the average R points the other way, or charges and slippage turn
   a gross edge negative.

Option plans (CE/PE) are refused: a premium moves with delta, decay and IV,
not with the stock's bars. Replay the underlying's plan instead.

## The backtest

`backtest` runs one simple rule on every full session in the 60-day data for
each symbol: `orb` (long, or `orb-short` / `--side sell`), `gap` (gap-and-go:
open 2% beyond the previous close, gap not filled in the range), or
`vwap_reclaim`. Fixed risk sizing (`max_loss_per_trade_inr` / the trade's
risk, capped by `max_order_inr`; an index as whole futures lots), full Indian
charges, slippage, the `flat_by` exit. It prints trades, win rate after
charges, average net P&L, expectancy in R, net total, total charges, max
drawdown and charges as a share of gross profit. Past results do not predict
future results, and 60 days is a small sample: say both every time.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`.
2. Run `timemachine.py run --write`. Tell the user: the odds, the price
   range at flat_by, the average R before costs and the net R and rupees
   after, the gap line for CNC, the history line, then the verdict and the
   lines under it exactly as printed. Say the seed.
3. If it is a coin flip, close to break-even, or costs eat the edge, say so
   plainly.
4. For "does this setup work at all", run `backtest` and read back the table
   with the small-sample warning.
5. If the user still wants the trade, hand off to `/trader-budget`.

## Rules

- Not financial advice. Simulated odds and backtests are not predictions or
  promises; past results do not predict future results.
- No order goes through from here. Only trader-broker places one, after
  trader-risk passes it and the user types an explicit "yes" for that order.
- Every number comes from the run: never round a coin flip into an edge, and
  never quote the gross figure without the net one.
- Always say the seed, so any run can be repeated.
- Never use a bar that has not closed, never mix today's bars into the pool.
