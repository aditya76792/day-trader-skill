# What changed from the US edition, and why

Base: `Jakeschincariol/day-trader-skill` commit `3d0f4ef` (v2.0, 20 Oct 2026).
Branch: `india-adaptation`. Every tool was rebuilt; the architecture was kept.

## What was kept

- One skill per job, `SKILL.md` plus a standard-library Python tool.
- The `trader/` folder hand-off: scanner -> entry -> exit -> timemachine ->
  budget -> risk -> broker, plus alerts, news, analyst on the side.
- The strict order gate: trader-risk must PASS the exact order today (by
  fingerprint), the user must type an explicit "yes" for that one order
  (batch words like "all" and doubt words are refused), and a live
  connector must be set to "Needs approval".
- Paper trading as the default, `--now` to pin the clock and `--offline`
  for every network read, so every number is reproducible.
- The algorithms that still hold: the two-stage spark/chart scan, the
  opening-range breakout rule, Wilder RSI, the bar-by-bar watch loop,
  the seeded Monte Carlo replay and the history replay, the plain-words
  reports.

## What was redesigned (not just translated)

### A shared core: `trader-market/market.py` (new)
The US tools each carried their own copy of the NYSE calendar, Eastern time
and Yahoo code. India has more rules that change often (STT in Budgets, lot
sizes twice a year, expiry weekdays, holidays), so they live in one module
that every tool imports, with a CLI of its own (`clock, calendar, expiry,
lots, costs, tax, bands, tick, symbol`).

| US | India |
| --- | --- |
| Eastern time with DST, 09:30-16:00, early closes | IST (no DST); pre-open 09:00-09:08 order entry, 09:08-09:15 matching; normal 09:15-15:30 (75 five-minute bars); closing session 15:40-16:00; Muhurat session |
| NYSE holidays | NSE holiday master 2026 (CM = FO), loadable for later years from `trader/holidays.json` |
| none | F&O expiry engine: NIFTY weekly Tuesday, SENSEX weekly Thursday, monthly last Tuesday/Thursday, holiday shift to the previous trading day (matches NSE's list) |
| cents | tick sizes by price band (0.01 under Rs 250 ... 5.00 above Rs 20,000), 0.05 for derivatives, safe-direction rounding |
| none | circuit bands 2/5/10/20%, dynamic bands for F&O stocks, market-wide circuit breaker halts by time of day |
| commission-free assumption | a full charges engine: brokerage profiles (Zerodha, Upstox, Angel One, flat Rs 20, custom), STT by segment and side (Budget 2026 rates), NSE/BSE transaction charges, SEBI fee, IPFT, stamp duty (buy side), GST 18%, DP charges, STT on exercised options |
| short-term gains, wash sales | Indian tax guide: speculative (intraday), non-speculative business (F&O), STCG 20%, LTCG 12.5% above Rs 1.25 lakh, new-regime slabs, carry-forward rules |
| dollar formatting | Indian digit grouping (Rs 1,23,45,678.90) |
| plain urllib per tool | `Net`: retries and backoff, Yahoo host fallback, NSE cookie bootstrap, BSE Referer, clear "use --offline" errors when NSE blocks |

### trader-scanner
- Universe: NSE `EQUITY_L.csv` (series EQ; BE/BZ trade-for-trade excluded
  from intraday), or index constituents (NIFTY 50/100/200/500) or the F&O
  list, instead of Nasdaq Trader's files.
- Liquidity by average daily rupee turnover (crore), not share volume; a
  Rs 20 price floor against penny stocks.
- New `gap` scan: gap-up/gap-down vs the previous close, gap-and-go vs
  gap-fill after the first 15 minutes, circuit proximity from NSE's
  price-band file or the config.
- Swing setups added: 52-week-high proximity, EMA 20/50 pullback, NR7,
  volume surge. Shorts (`--short`) only as MIS breakdowns.
- Skips config `blocked`, `asm_gsm` and `fno_ban` lists with the reason;
  prints NIFTY, BANKNIFTY and India VIX context.

### trader-news
- Yahoo's RSS answers nothing for NSE tickers (checked live), and SEC
  EDGAR does not apply. Replaced by NSE corporate announcements, BSE
  announcements (with scrip codes from the config) and Google News India
  search RSS.
- Catalyst tags rewritten for Indian disclosures: results, board meetings,
  dividend/record date, bonus, split, buyback, rights, QIP, block/bulk
  deals, promoter pledge, SEBI orders, auditor resignation, ratings,
  NCLT/insolvency, F&O ban, ASM/GSM.

### trader-entry
- The opening range starts at 09:15; entries default to 09:30-14:45.
- Context added to every signal: gap vs the previous close (gap-and-go
  flagged as extra risk), VWAP so far, circuit proximity (a long near the
  upper circuit is refused: it can lock and you may not get out).
- New setups: level, vwap_reclaim, gap. Shorts only with `allow_short`
  and MIS. Expiry-day cut-off for F&O signals. Trade-for-trade series and
  surveillance lists refused before any data is read.

### trader-exit
- More stop rules (ATR, VWAP, previous-day low/high, percent) and targets
  capped at the upper circuit.
- Prices rounded to the exchange tick in the safe direction.
- Charges in the plan: costs per share and the net reward:risk after
  charges, with a warning when charges eat over a quarter of the reward.
- `flat_by` can never reach the broker's auto square-off; CNC swing plans
  get a `valid_until` and an overnight-gap warning; F&O plans carry the
  instrument, lot size and exchange trading symbol; stock options on
  expiry day must be flat (physical settlement).

### trader-timemachine
- 75-bar sessions from 09:15 on NSE trading days; exits at the flat-by bar.
- Every replay costed with Indian charges and slippage: net expectancy in
  R and rupees, and a verdict line when costs turn an edge negative.
- CNC plans simulate overnight gaps from the stock's own close-to-open gaps.
- New `backtest` command: ORB long/short, gap, VWAP reclaim over 60
  sessions for several symbols with full charges, win rate, expectancy,
  max drawdown, charges as a share of gross profit.

### trader-fno (new)
US options were simply disallowed. Indian retail volume is mostly index
options, so a dedicated desk was added: NSE option chain (OI, change in
OI, PCR, max pain, OI walls, ATM straddle and implied move, IV skew,
OI build-up), Black-Scholes IV and Greeks, payoffs with charges for up to
four legs, and option-buying plans into the normal pipeline. Option
selling and futures stay blocked unless the user enables them.

### trader-budget
- Rupee limits; the loss at the stop includes charges (the size steps down
  until it fits). Whole lots for F&O; premium and lot caps.
- MIS margin: full value by default, 20% (SEBI's 5x cap) only if the user
  enables leverage. Futures margin is an explicit estimate.
- Orders are broker-neutral (exchange, tradingsymbol, product MIS/CNC/NRML,
  LIMIT/MARKET/SL/SL-M) with the stop-loss and target as separate exit
  orders: most Indian brokers dropped bracket and cover orders, and
  options use SL (stop-limit) because brokers restrict SL-M/market orders.
- The US weights/rebalance mode was removed (it does not fit lots and
  charges).

### trader-risk
New India checks: trading day and session (AMO aside), MIS cut-off before
the square-off, product rules (no CNC short, swing needs `allow_swing`),
trade-for-trade series, ASM/GSM and F&O ban lists, circuit proximity,
lot multiples and max lots, option premium cap, option selling and futures
switches, expiry-day cut-off, physical-settlement warning, tick validity,
charges-inclusive max loss and daily loss, stale quotes.

### trader-broker
- Tickets in rupees with Zerodha Kite Connect, Upstox and Dhan field
  mappings (marked "check your broker's API docs"); no Alpaca.
- New `paper.py`: a paper broker with slippage, limit and stop-limit
  logic (a stop-limit that gaps past its limit stays unfilled and says
  so), OCO exits, MIS auto square-off at the broker time with the fee,
  every charge on every fill, DP on CNC sells, a ledger and daily contract
  notes.
- The desktop config example points to a broker MCP (Zerodha's hosted Kite
  MCP as an example) with order tools on "Needs approval".

### trader-alerts
IST/NSE clock, Yahoo `.NS` quotes (Stooq dropped: it does not cover NSE
reliably), and new rule types: square-off reminder, expiry day, circuit
proximity, opening gap, index move with the market-wide circuit-breaker
levels.

### trader-analyst
SEC XBRL company facts replaced by NSE filings. Since early 2025 results
are filed as SEBI "Integrated Filing - Financials"; the tool reads NSE's
integrated-filing listing first and the older results listing as a
fallback, parses the XBRL (`in-capmkt` taxonomy, quarter context chosen by
dates, values in full rupees shown in crore), and builds YoY and QoQ from
earlier quarters' filings. Indian fiscal quarter labels and the SEBI LODR
results deadlines.

### trader-pelosi -> trader-insider
The US Congress (STOCK Act) disclosures have no Indian equivalent. The
nearest public disclosures are SEBI insider-trading (PIT) disclosures and
bulk/block deals, read from NSE: promoter market buys and sells, pledges
created, released or invoked, late disclosures counted in trading days,
and watched client names in bulk/block deals.

## Removed
`trader-pelosi`, the Alpaca configuration, SEC EDGAR access and
`sec_user_agent`, the FINRA pattern-day-trader guard, Stooq, the US
weights/rebalance sizing mode, every US fixture.

## Tests
The US suite (TSLA fixtures) was replaced by an India suite that runs
offline: the shared core, each skill, the repo layout, and an end-to-end
pipeline test (scanner to paper broker). See the test count in
[SETUP.md](SETUP.md#run-the-tests).

## Fixes from the independent review
An independent review (a separate agent with the code only) tried to break the
order gate, sizing and risk rules. Confirmed defects, all fixed with regression
tests (`tests/test_gating.py` and additions to the budget, risk and F&O tests):

- An explicit yes accepted extra words ("yes go short" on a buy, "yes as CNC",
  "yes for INFY", "yes x2", "yes, nope"). Now only the order's own words count.
- A morning PASS could be approved and placed after the expiry-day cut-off or
  flat_by. The clock rules are re-checked at the yes and at placement, and a PASS
  is valid for 30 minutes.
- The paper broker marked an order placed before it had a price; a failed price
  lookup left the order stuck. The price is fetched first now.
- The daily loss limit undercounted after a stop filled past its trigger, and the
  paper ledger counted against live trading. Fixed per symbol and per mode.
- F&O stop-limit exits could fill beyond the max loss: sizing and the risk check
  now count the loss at the SL limit, and the limit buffer is capped at half the
  planned risk.
- Stops and targets that crossed an NSE tick band were rounded on the entry's tick
  and then rejected; each price is now rounded on its own tick band.
- MIS AMOs (which fill at the 09:15 open) and AMOs during market hours passed;
  they are refused now. CNC/NRML AMOs on holidays were wrongly blocked.
- Futures position and order caps were measured on margin; they use the full
  contract value now.
- The payoff tool missed strikes outside spot +/-15% and called long-put profit
  unlimited; it now evaluates from zero and caps the downside.
- An off-grid flat_by (15:12) stopped the time machine; it rounds down to the bar.
- Malformed trader/ files produced Python tracebacks; every tool now prints one
  plain error line.
