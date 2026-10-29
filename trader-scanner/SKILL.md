---
name: trader-scanner
description: >-
  Chart Scanner for the Indian market (NSE). Builds the list of NSE equities
  (series EQ from EQUITY_L.csv, or NIFTY 50/100/200/500 constituents, or the
  F&O stocks), then scans them on free Yahoo data (.NS): a pre-market and
  swing scan of daily bars (20-day breakout, near the 52-week high, bull
  flag, reclaim of the 20-day average, pullback to the 20 EMA, oversold RSI,
  oversold bounce, NR7, volume surge), a gap scan at the 09:15 open
  (gap-and-go or gap-fill, near the circuit limit) and an opening-range
  breakout scan on 5-minute bars from 09:15 IST, with liquidity in rupees
  (turnover in crore) and the NIFTY, BANKNIFTY and India VIX move. Writes
  trader/watchlist.json for trader-news and trader-entry. Use when the user
  says "scan the market", "scan NSE", "find me setups", "what is breaking
  out", "opening range breakouts", "gap up stocks", "gap and go", "stocks
  near upper circuit", "NR7 stocks", "swing setups", "Nifty 500 scan",
  "pre-market scan", "build my watchlist", or at the start of a trading day
  before /trader-news.
argument-hint: "[universe | daily | gap | orb] [--at HH:MM]"
---

# trader-scanner (India)

The Chart Scanner is the first step of a trade on NSE. It reads hundreds or
thousands of free charts and lists the ones that match a rule. That is all it
does. A scan is a list of charts that match a rule, not a list of stocks to
buy, and a setup on a chart is not a prediction. SEBI's own studies found
that about 9 in 10 F&O traders lost money; intraday cash traders do not fare
much better. The scanner does not change that. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
The Indian market rules (holidays, 09:15 to 15:30 session, lots, circuits)
come from `trader-market/market.py`; this skill never keeps its own copy.

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                        # NSE EQ stocks -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --index nifty500       # or nifty50 / nifty100 / nifty200 / fno
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                           # before 09:15 or after 15:30
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,SBIN --deep
python3 "${CLAUDE_SKILL_DIR}/scan.py" gap                             # after 09:20 IST (09:30 for go / fill)
python3 "${CLAUDE_SKILL_DIR}/scan.py" gap --bands sec_list.csv        # circuit limits from NSE's band file
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                             # after 09:35 IST (15-minute range)
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15 --short          # cut the clock, include breakdowns
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Common flags: `--min-price 20` (rupees: penny stocks out),
`--min-turnover-cr` (default `min_avg_turnover_cr` in the config, 5 crore),
`--max-confirm 200` (stage 2 charts, most traded first; 0 = all),
`--now "2026-10-28 10:15"` (IST) to pin the clock, `--offline PATH` to read
saved files, `--no-index`, `--no-write`, `--show N`, `--json`,
`--config trader/config.json`. Exit code 0 when the scan found something, 1
when it found nothing or could not run (one plain `error: ...` line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one NSE symbol a line, read by `daily`, `gap`, `orb` |
| `trader/universe.json` | written by `universe`: names, series, ISINs (trader-news reads the names) |
| `trader/watchlist.json` | written by every scan: `created_by`, `created_at` (+05:30), `scan`, `date`, `as_of`, `universe`, `scanned`, `failed`, `skipped`, `context`, `hits[]` with `symbol`, `setup`, `price`, `opening_range`, `gap_pct`, `turnover_cr`, `note` |
| `trader/config.json` | `min_avg_turnover_cr`, `no_entries_after`, `allow_short`, `blocked`, `asm_gsm`, `fno_ban`, `block_series`, `avoid_circuit_within_pct`, `bands` |

## The universe

`universe` downloads NSE's `EQUITY_L.csv` and keeps series **EQ**. Series
**BE** and **BZ** are trade-for-trade: every trade settles by delivery, so
there is no intraday (MIS) in them; they stay out unless `--include-t2t`,
and the intraday scans skip them even then. SME (SM/ST), gold bonds (GB) and
institutional series are out. `--index nifty50|nifty100|nifty200|nifty500`
reads NSE's constituent CSV instead (Company Name, Industry, Symbol, Series,
ISIN Code); `--index fno` takes the F&O stocks from market.py's lot table
(`market.py lots --refresh` saves NSE's fo_mktlots.csv). NSE serves these
files from nsearchives.nseindia.com and sometimes blocks scripts: save the
file in a browser and pass `--offline FILE`.

## The daily scan (pre-market and swing)

Six months of daily bars from Yahoo. Stage 1 is the spark endpoint, 20
symbols a request, closes only. Stage 2 fetches the v8 daily chart (open,
high, low, close, volume) for each candidate, most traded first, at most
`--max-confirm`; `--deep` does it for every liquid chart. Liquidity: average
daily turnover (close x volume, 20 sessions) in crore and the last close in
rupees. Today's daily bar is left out until the 15:30 close.

| setup | the rule |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `near_52w_high` | the last close is within 3 % of the 52-week high (Yahoo's, or the data's) |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then a 5-close range under 5 % |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `ema_pullback` | swing: 20 EMA over a rising 50 EMA, price back within 1.5 % of the 20 EMA |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |
| `nr7` | the last day's high-low range is the narrowest of 7 (needs highs and lows: stage 2) |
| `volume_surge` | the last day's volume over 2x its 20-day average (needs volumes: stage 2) |

The output says how many charts were checked with highs and lows; NR7 and
volume surge are only known for those.

## The gap scan (09:15 open)

NSE's opening price comes out of the 09:00 to 09:08 pre-open auction, so a
stock can open far from yesterday's close. From 09:20 IST, `gap` compares the
open with yesterday's close (`gap_pct`), classes it gap-up / gap-down (2 %+)
or small, and after the first `--hold-minutes` (15) says **gap-and-go** (held
beyond the open, under half the gap filled) or **gap-fill** (trading back
toward yesterday's close), or **gap filled**. It flags a price within
`avoid_circuit_within_pct` (2 %) of its circuit limit when the band is known:
`"bands": {"SYMBOL": 5}` in the config or NSE's `sec_list.csv` via
`--bands`. F&O stocks have no fixed band (dynamic bands that NSE widens in
steps), so none is shown for them. At a circuit a stock can freeze: no
sellers at the upper limit, no buyers at the lower.

## The opening-range breakout scan

The opening range is the first `--or-minutes` (15) from 09:15 IST: the 09:15,
09:20 and 09:25 five-minute bars. A session has 75 such bars. A hit is the
first completed bar to CLOSE above the range high (long) and, with
`--short`, below the range low (short: intraday MIS only, needs
`allow_short` in the config). Stage 1 uses spark closes for every symbol (a
necessary condition, so no breakout is lost); stage 2 fetches the v8 chart
with real highs and lows for the candidates, most traded first, at most
`--max-confirm`, and prints PARTIAL with the count it skipped. Each hit
carries its `gap_pct` and its turnover. A chart is usable only when every
5-minute bar from 09:15 to the cutoff is there. Before 09:35, in pre-open,
on a weekend or NSE holiday, after the close, or after `no_entries_after` it
prints one plain line and writes nothing.

## Skips and context

Every scan skips the symbols in the config's `blocked`, `asm_gsm` (ASM/GSM
surveillance) and `fno_ban` (F&O ban period, MWPL over 95 %) lists and says
which and why. Each run prints the NIFTY 50, NIFTY BANK and India VIX move
when Yahoo answers (`--no-index` skips it).

## Data and limits

- Yahoo Finance (.NS) and NSE's public files are free, unofficial and
  delayed, and they can break or block scripts without notice. Bars are
  5-minute bars, never ticks. Use `--offline` with saved files when blocked.
- NSE's official close is a volume-weighted price of the last 30 minutes;
  when Yahoo does not give it, the previous session's last 5-minute close
  stands in, and the hit says so (`prev_close_basis`).
- Partial results are said as partial ("Fetched 480 of 500 charts (20
  failed or had no data)"). Never round a partial scan up to "the market".

## Steps

1. Once a week: `scan.py universe` (or `--index nifty500`).
2. Before 09:15: `scan.py daily`. Tell the user the counts and the top few.
3. From 09:20: `scan.py gap`; from 09:35: `scan.py orb`. Say when each hit
   happened and that a later hit is closer to the price now.
4. The user picks one chart: `/trader-news SYMBOL`, then `/trader-entry
   SYMBOL`. Never pick for them.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable or skipped, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.
- Not financial advice. Nothing here places an order: only trader-broker
  does, after trader-risk passes that exact order and the user types an
  explicit "yes" for it. Paper trading is the default.
