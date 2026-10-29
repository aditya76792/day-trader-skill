---
name: trader-entry
description: >-
  Entry Timer for NSE and BSE stocks (and NIFTY / BANKNIFTY / SENSEX for
  analysis). Watches a setup bar by bar on 5-minute or 1-minute bars from
  09:15 IST and says the moment to get in: an opening-range breakout (the
  09:15-09:30 range by default), a close beyond a level, a VWAP reclaim, or a
  gap-and-go. Reports the gap against the previous close, price against
  VWAP, circuit proximity, expiry-day cutoffs, and blocks a long that sits
  next to the upper circuit. Long by default; a short only with allow_short
  and MIS. Writes trader/entry.json for /trader-exit and never places an
  order. Use when the user says "tell me when to get in", "watch RELIANCE",
  "has it broken out", "did it break the opening range", "ORB on TCS",
  "entry signal", "VWAP reclaim", "gap and go", "check my watchlist", or after
  /trader-scanner and /trader-news, before /trader-exit.
argument-hint: "[SYMBOL] [--setup orb|level|vwap_reclaim|gap] [--interval 5m|1m] [--side buy|sell]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads 5-minute (or 1-minute) bars of an NSE or BSE stock and waits for each
bar to finish. A price that pokes above the range high in the middle of a bar
is not an entry. A bar that CLOSES above it is. Then it says the time and the
price in India time, writes them down for the Exit Planner, and stops. It
never buys anything. Most retail intraday traders in India lose money, and a
clean signal does not change that. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                 # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write         # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TCS --setup level --level 3120.50
python3 "${CLAUDE_SKILL_DIR}/entry.py" check SBIN --setup vwap_reclaim
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TMPV --setup gap         # gap-and-go after the first 15 minutes
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY --side sell         # needs "allow_short": true and MIS
python3 "${CLAUDE_SKILL_DIR}/entry.py" check NIFTY --instrument CE    # index: analysis only
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:45   # one line per closed bar
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                      # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--interval 1m`, `--min-volume-x
X`, `--product MIS|CNC|NRML`, `--instrument EQ|FUT|CE|PE` (what you will
trade on the signal), `--exchange NSE|BSE`, `--series EQ|BE|...`, `--band 5`
(the stock's circuit band in percent), `--prev-close`, `--json`, `--offline
FILE` (a saved Yahoo chart JSON or a folder of `<SYMBOL>.json`), `--now
"2026-10-28 09:50"` (IST) to replay a day. `watch` with `--now` or
`--offline` runs on a simulated clock and replays the day without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`, optional `setup`, `level`, `series`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30), `no_entries_after` (14:45), `expiry_day_last_entry` (13:30), `allow_short`, `default_product`, `avoid_circuit_within_pct` (2), `bands` ({"SYM": 5}), `blocked`, `asm_gsm`, `fno_ban`, `block_series` |
| Yahoo chart API (through market.py) | `RELIANCE.NS` bars, `range=5d&interval=5m`: free, unofficial, delayed, can break |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, exchange, setup, side, status, time (bar close, IST), bar, price, level, opening range, VWAP, gap, circuit, warnings |

Yahoo, NSE and BSE public endpoints are unofficial: the data is delayed, can
lag a bar or two, and the endpoints can change or block scripts without
notice. Say so when a minute matters.

## The Indian session

- **Hours.** The pre-open call auction runs 09:00-09:15 and only sets the
  opening price: no bars. The normal market is 09:15-15:30 IST: 75
  five-minute bars (375 one-minute bars). No daylight saving, no early
  closes. NSE holidays come from market.py; on Diwali there may be a short
  Muhurat evening session, which this tool does not time.
- **Time of a bar.** The 09:45 bar closes at 09:50; that close time is the
  entry time, and the close is the entry price.
- **Only closed bars count.** A bar counts once the clock passes its close
  and Yahoo has caught up (it waits a minute at most).

## The setups

- **orb.** The high and low of 09:15-09:30 (the 09:15, 09:20 and 09:25
  bars). Long: the first completed bar after the range that CLOSES above the
  high. Short: closes below the low. A wick is not a trigger; a close equal
  to the high is not above it.
- **level.** The first close beyond a price you give (a 20-day high from
  `/trader-scanner`, for example).
- **vwap_reclaim.** After the range, a bar that closes back above the
  session VWAP when the bar before it closed below. Needs volume, so not for
  an index.
- **gap.** Gap-and-go: the open is 2% or more beyond the previous close,
  the gap did not fill in the first 15 minutes, and a bar then closes beyond
  the range.

## India-specific filters (always reported)

- **Gap.** The 09:15 open against the previous close (NSE's official close
  when Yahoo gives it; after a holiday, the session before the holiday). An
  ORB on a gap of more than 2% is flagged gap-and-go: extra risk, gaps often
  fill.
- **VWAP.** The session VWAP so far, and whether the price is above or below.
- **Circuits.** With a known price band (`--band` or `"bands"`), a long
  within 2% of the upper circuit is BLOCKED: it can lock at the upper
  circuit and you may not be able to exit. A short near the lower circuit,
  the same. F&O stocks have no fixed band.
- **Expiry days.** NIFTY weekly expiry is Tuesday, SENSEX Thursday, monthly
  index and stock F&O the last Tuesday (moved earlier for a holiday). On the
  expiry day of what you trade, F&O entries stop at `expiry_day_last_entry`
  (13:30).
- **Refused outright:** symbols on `blocked` or `asm_gsm`, trade-for-trade
  series (BE, BZ ...: no intraday), and F&O on a stock in the F&O ban.
- **Shorts.** Only with `"allow_short": true` and MIS. A cash short in India
  must be bought back the same day; one that is not goes to the exchange
  auction with a penalty.
- **Indexes.** NIFTY, BANKNIFTY and SENSEX work for analysis (Yahoo ^NSEI,
  ^NSEBANK, ^BSESN). You cannot buy an index in the cash market: for an
  option trade go to trader-fno, then `/trader-exit --instrument CE|PE`.

## Steps

1. Get the symbol, or run `entry.py watchlist` after `/trader-scanner`.
   Check `/trader-news` for it first (results, block deals, circulars).
2. Run `entry.py check SYMBOL`. Tell the user the status in one line, with
   the gap, VWAP and any circuit or expiry note it printed.
3. To wait, run `entry.py watch SYMBOL --until 14:45` in the background, or
   `entry.py check SYMBOL --write` on a `/loop` every 5 minutes.
4. When it triggers, say the time (IST) and the price, and that nothing was
   placed. If the trigger is more than a bar old, say a late entry is a
   different trade.
5. Hand off to `/trader-exit` to set the stop and target BEFORE any order.

## Rules

- Not financial advice. A signal is information, not an order and not a
  promise of profit.
- Nothing is ever ordered from here. Only trader-broker places an order,
  and only after trader-risk passes it and the user types an explicit "yes"
  for that one order. Never run /trader-broker from a background watch:
  nobody is there to say yes.
- One trigger per setup per day: the first close beyond. Never call an
  entry early because a bar looks like it will close beyond.
- A BLOCKED or refused symbol stays blocked: never loosen a filter or the
  config on the user's behalf.
- Paper trading is the default (`"paper": true`); going live is the user's
  own decision in their own config.
- Data from Yahoo, NSE and BSE public endpoints is unofficial and delayed.
