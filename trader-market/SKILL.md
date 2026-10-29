---
name: trader-market
description: >-
  Indian Market Rules desk for NSE and BSE, and the shared core every other
  trader-* skill imports. Answers "is the market open?", the session
  (pre-open 09:00-09:15, normal 09:15-15:30 IST, closing session), 2026
  NSE/BSE trading holidays and Muhurat trading, F&O expiry dates (NIFTY
  weekly Tuesday, SENSEX weekly Thursday, monthly last Tuesday/Thursday,
  holiday shifts), lot sizes and strike steps, tick sizes, circuit limits
  and market-wide circuit breakers, and the full cost of a trade (brokerage,
  STT, exchange charges, SEBI fee, stamp duty, GST, DP charges) for
  delivery, intraday, futures and options, plus a rough tax guide
  (speculative, F&O business income, STCG 20%, LTCG 12.5%). Use when the
  user asks "is NSE open", "when is the next expiry", "what is the NIFTY lot
  size", "how much are charges on this trade", "breakeven after charges",
  "STT on options", "circuit limit", "how is intraday taxed", or before any
  other trader-* skill needs a rule.
argument-hint: "[clock | expiry SYMBOL | lots | costs ... | tax ...]"
---

# trader-market

The rulebook. Every Indian-market fact the pack depends on lives in one
file, `market.py`, so a rule that changes (an STT rate in a Budget, a lot
size revision, a new holiday, an expiry weekday) is changed in one place
and every skill follows. The other skills import it; they never copy its
tables.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/market.py" clock                       # open? which session? next expiries
python3 "${CLAUDE_SKILL_DIR}/market.py" calendar --year 2026        # NSE/BSE holidays
python3 "${CLAUDE_SKILL_DIR}/market.py" expiry NIFTY                # weekly + monthly expiries
python3 "${CLAUDE_SKILL_DIR}/market.py" expiry BANKNIFTY --months 4 # monthly only
python3 "${CLAUDE_SKILL_DIR}/market.py" lots                        # index lots, strike steps
python3 "${CLAUDE_SKILL_DIR}/market.py" lots --refresh              # NSE fo_mktlots.csv -> trader/lots.json
python3 "${CLAUDE_SKILL_DIR}/market.py" costs --segment intraday --buy 1158 --sell 1170 --qty 50
python3 "${CLAUDE_SKILL_DIR}/market.py" costs --segment options --buy 120 --sell 150 --qty 65 --broker upstox
python3 "${CLAUDE_SKILL_DIR}/market.py" tax --kind fno --profit 200000 --other-income 1000000
python3 "${CLAUDE_SKILL_DIR}/market.py" bands --price 412.5 --band 5
python3 "${CLAUDE_SKILL_DIR}/market.py" tick --price 1158.37
python3 "${CLAUDE_SKILL_DIR}/market.py" symbol RELIANCE --exchange BSE   # Yahoo code: RELIANCE.BO
```

Every command takes `--json`. `--now "2026-11-03 10:00"` pins the clock
(India time).

## What it knows (checked 2026-10-28)

| area | rule |
| --- | --- |
| Sessions | pre-open order entry 09:00-09:08, matching 09:08-09:15, normal 09:15-15:30, closing session 15:40-16:00 (trades at the closing price only). No daylight saving. |
| Holidays 2026 | NSE's holiday master for equity and F&O: 16 weekday holidays (Republic Day, Holi, Ram Navami, Mahavir Jayanti, Good Friday, Ambedkar Jayanti, Maharashtra Day, Bakri Id, Muharram, Ganesh Chaturthi, Gandhi Jayanti, Dussehra, Diwali-Balipratipada 10 Nov, Guru Nanak Jayanti 24 Nov, Christmas, and the 15 Jan Maharashtra civic election). Muhurat trading on Sunday 8 Nov, timing by NSE circular. |
| Later years | Not built in. Save NSE's `/api/holiday-master?type=trading` answer to `trader/holidays.json` and every skill picks it up. Until then the tools warn "weekends only". |
| Expiries | One weekly index per exchange (SEBI, Nov 2024): NIFTY on Tuesday (NSE), SENSEX on Thursday (BSE). Monthly: last Tuesday (BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, stock F&O), last Thursday (SENSEX, BANKEX). A holiday moves expiry to the previous trading day. Matches NSE's own list (3, 9, 17, 23 Nov, 1 Dec, 29 Dec 2026). |
| Lots | NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120, NIFTYNXT50 25, SENSEX 20, BANKEX 30. Stock lots change twice a year: `lots --refresh`. |
| Ticks | Options and futures 0.05. Equity by price: under Rs 250 0.01, to 1,000 0.05, to 5,000 0.10, to 10,000 0.50, to 20,000 1.00, above 5.00. |
| Circuits | Stocks outside F&O: 2/5/10/20% bands. F&O stocks: dynamic bands, no fixed circuit. Index circuit breaker 10/15/20% with halts by time of day. |
| STT | delivery 0.1% both sides, intraday 0.025% sell, futures 0.05% sell, options 0.15% of premium on sell, exercised options 0.15% of intrinsic value (Budget 2026, from 1 Apr 2026). |
| Other charges | NSE transaction 0.00307% equity, 0.00183% futures, 0.03553% options premium; BSE 0.00375% equity, nil futures, 0.0325% options. SEBI Rs 10 a crore. Stamp duty on the buy side: 0.015% delivery, 0.003% intraday, 0.002% futures, 0.003% options. GST 18% on brokerage + exchange + SEBI. DP charge on delivery sells. |
| Brokers | zerodha, upstox, angelone, flat20, or your own table under `"brokerage"` in `trader/config.json`. Auto square-off times differ by broker (Zerodha 15:25 equity, 15:26 F&O, 15:12 for some stocks); we default to being flat by 15:10. |
| Tax | intraday equity = speculative business income (slab rate, losses carried 4 years); F&O = non-speculative business income (slab rate, 8 years); delivery: STCG 20%, LTCG 12.5% above Rs 1.25 lakh. A rough guide only. |

## Data access (shared by every skill)

`Net` in `market.py` does polite HTTP with retries: Yahoo Finance (query2
then query1) for NSE (`.NS`) and BSE (`.BO`) charts and indexes (`^NSEI`,
`^NSEBANK`, `^BSESN`, `^INDIAVIX`); NSE's website APIs (it first visits
the home page for the cookies NSE requires); NSE archives (lot sizes,
symbol lists); BSE's API (with the Referer BSE wants). NSE blocks many
scripts and most cloud servers: when it does, every tool accepts
`--offline FILE` with the JSON saved from your browser.

## How to talk about it

- Quote the rule and the date it was checked. Statutory rates change in
  Budgets; lot sizes change twice a year; holidays are set each December.
- For costs, lead with the breakeven move: "you need Rs 1.24 a share just
  to cover charges".
- For tax, say it is a rough guide and that a CA should confirm, every
  time.

## Rules

- Not financial advice. Not tax advice. These are public rules and
  arithmetic.
- Most retail intraday and F&O traders in India lose money (SEBI's studies
  of FY22-FY24: about 9 in 10 F&O traders lost money).
- The data sources are free, unofficial and can be delayed, change shape or
  block you. Check anything that matters with your broker.
- This skill never places orders. Nothing in the pack does without the
  user's explicit yes for that one order.
