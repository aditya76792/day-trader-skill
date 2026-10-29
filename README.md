# The Day Trader skill, India edition

13 Claude skills for trading Indian markets (NSE/BSE): scan charts, track filings and news, time entries and exits, read option chains, replay outcomes with real Indian charges, and paper trade before you risk a rupee.

This is an adaptation of Jake Schincariol's
[Day Trader skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT)
for the Indian market. The architecture is the same (one skill per job,
plain standard-library Python, a `trader/` folder each skill reads and
writes, nothing placed without your yes), but the market logic was rebuilt
for NSE and BSE: IST sessions from 09:15, the 2026 NSE holiday list,
weekly and monthly F&O expiries, lot sizes, tick sizes, circuit limits,
MIS/CNC/NRML products, SEBI's leverage cap, STT and every other charge,
Indian tax treatment, and Indian data sources. See
[docs/CHANGES-INDIA.md](docs/CHANGES-INDIA.md) for what changed and why.

## Install

Claude Code, as a plugin:

```
/plugin marketplace add aditya76792/day-trader-skill
/plugin install day-trader-skill-india@day-trader-skill-india
```

Or copy the folders (keep them side by side: every tool imports
`trader-market/market.py`):

```bash
git clone https://github.com/aditya76792/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

In the Claude app (Cowork or chat with code execution), paste the repo
link and say `install skill`. Full setup, including your config, a broker
and the paper account: [docs/SETUP.md](docs/SETUP.md).

Python 3.8 or newer, standard library only. Nothing to pip install. No API
keys for the data.

## The thirteen

| command | job | what it does |
| --- | --- | --- |
| `/trader-market` | Market Rules | Is NSE open, holidays, expiries, lot sizes, ticks, circuits, the full cost of a trade, a tax guide. Every other skill imports it. |
| `/trader-scanner` | Chart Scanner | Scans NSE stocks for opening-range breakouts, gaps (gap-and-go or gap-fill), swing setups, NR7, volume surges; liquidity in rupee turnover. |
| `/trader-news` | Filings and News | NSE and BSE corporate announcements, Google News headlines, catalysts tagged (results, dividends, block deals, pledges, SEBI orders), big moves and gaps. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar from 09:15 and says when to get in, with VWAP, gap and circuit checks. |
| `/trader-exit` | Exit Planner | Stop and target before you enter, rounded to the tick, net of charges, flat before the broker's square-off. |
| `/trader-timemachine` | Time Machine | 10,000 replays of the rest of the session plus the same rule on 60 real sessions, all after Indian charges; and a backtester. |
| `/trader-fno` | F&O Desk | Option chain (OI, PCR, max pain, IV, straddle), Greeks, payoffs with charges, and cautious option-buying plans. |
| `/trader-risk` | Risk Manager | Checks every order against your rupee limits, products, lots, expiry-day cut-offs, F&O ban and circuit rules: PASS or BLOCK. |
| `/trader-budget` | Budget Manager | Sizes in shares or lots so the loss at the stop, charges included, fits your max loss; MIS margin under SEBI's 5x cap. |
| `/trader-broker` | Order Executor | Broker-neutral tickets (Kite, Upstox, Dhan field maps), your explicit yes, and a realistic paper broker. |
| `/trader-alerts` | Price Alerts | Stop, target, square-off reminder, expiry day, circuit, gap and index circuit-breaker alerts. |
| `/trader-analyst` | Earnings Analyst | Quarterly results from NSE's XBRL filings: revenue, EBITDA, profit, EPS, growth, what changed. |
| `/trader-insider` | Insider and Deals Tracker | SEBI insider-trading (PIT) disclosures, promoter buys, sells and pledges, bulk and block deals. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker (paper by default)
fno (option chain, Greeks, plan) -> budget -> risk -> broker                 (an option trade)
alerts: stop, target, square-off, expiry                                      (while it is open)
news / analyst / insider                                                      (on a schedule)
```

An example, offline in the test suite (`tests/test_pipeline.py`): a
RELIANCE opening-range breakout on Wednesday 28 October 2026.

1. **`/trader-scanner`** at 09:50: RELIANCE closed above its 09:15-09:30
   range on the 09:30 bar.
2. **`/trader-entry`**: triggered at the 09:30 bar's close, price above
   VWAP, flat open, no circuit nearby.
3. **`/trader-exit`**: stop at the range midpoint, target 2R, both rounded
   to the 0.10 tick; reward:risk 2.0 before charges and 1.62 after; flat by
   15:10, before Zerodha's 15:25 auto square-off.
4. **`/trader-timemachine`**: target first 27%, stop first 55%; after
   charges and slippage the average trade loses money. The 60-session
   history agrees: "the costs eat the edge". That is the point of looking
   before you risk a rupee.
5. **`/trader-budget`** sizes it so the loss at the stop, with every
   charge, stays under your Rs 1,000 limit, and writes an entry, a
   stop-loss and a target as separate orders (MIS).
6. **`/trader-risk`**: PASS or BLOCK, with the reason.
7. **`/trader-broker`** shows the ticket in rupees and how it maps to
   Kite, Upstox and Dhan. You type yes for that one order; in paper mode
   the paper broker fills it, charges it like a contract note, and exits
   at the target or the stop.

For options: `fno.py chain NIFTY` to read the chain, `fno.py plan NIFTY
--view bullish --write` for a call-buying plan, then budget, risk, broker
as above. One NIFTY lot is 65 units, so a modest premium stop is a
four-figure rupee risk; the budget and risk skills say so.

## The tools

```bash
python3 trader-market/market.py clock                                  # open? next expiries
python3 trader-market/market.py costs --segment intraday --buy 1158 --sell 1170 --qty 50
python3 trader-scanner/scan.py universe --index nifty500               # -> trader/universe.txt
python3 trader-scanner/scan.py daily                                   # swing setups from daily bars
python3 trader-scanner/scan.py gap                                     # after 09:15: gap-and-go / gap-fill
python3 trader-scanner/scan.py orb --at 09:50                          # opening-range breakouts
python3 trader-news/news.py --tickers RELIANCE,TCS --hours 24          # filings, headlines, moves
python3 trader-entry/entry.py check RELIANCE --setup orb --write       # waiting / triggered / expired
python3 trader-exit/exit.py plan --stop or-mid --target 2R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run                          # 10,000 replays + history, after costs
python3 trader-timemachine/timemachine.py backtest RELIANCE TCS --rule orb
python3 trader-fno/fno.py chain NIFTY                                  # OI, PCR, max pain, IV
python3 trader-fno/fno.py plan NIFTY --view bullish --write            # option-buying plan
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets
python3 trader-broker/paper.py status                                  # paper account, net of charges
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, square-off
python3 trader-analyst/facts.py TCS                                    # quarterly results from XBRL
python3 trader-insider/insider.py insider --days 7                     # promoter buys / sells / pledges
python3 trader-insider/insider.py deals                                # today's bulk and block deals
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline. Fixtures are built from the real shapes of Yahoo
Finance (`RELIANCE.NS`, `^NSEI`), NSE's option chain, holiday master,
corporate announcements, insider (PIT) and bulk-deal APIs, NSE's
integrated-filing XBRL and BSE's announcements API, all checked live on
28 October 2026. The NIFTY option-chain fixture and the TCS Q2 FY27
results figures are real data.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
SEBI's study of FY22-FY24 found about 9 in 10 individual F&O traders lost
money, and most intraday traders lose money too. You can lose money.
Decide for yourself, and talk to a SEBI-registered adviser if you need
advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead
to an order, and only after `/trader-risk` passes that exact order and you
type yes for it in the chat. With a live broker connector, set its order
tools to "Needs approval", never "Always allow".

**Paper first.** `"paper": true` is the default. The paper broker charges
every fill like a real contract note (brokerage, STT, exchange charges,
SEBI fee, stamp duty, GST, DP), applies slippage and squares off MIS at
the broker's time with the fee. Paper fills are still kinder than real
ones.

**Cautious F&O by default.** Option buying is allowed; option selling and
futures are blocked until you turn them on yourself. Expiry days have an
earlier cut-off. Stock options in the money at expiry are physically
settled.

**Algorithmic trading rules.** SEBI's retail algo framework (in force from
1 April 2026) covers orders sent through broker APIs. A few orders a day,
each approved by you, is far below the 10 orders-a-second registration
threshold, but your broker may still require a static IP and order tags
for API orders. This pack never sends orders by itself.

**The data is free and unofficial.** Yahoo Finance's chart endpoint and
NSE/BSE's website APIs are not official data feeds. They can lag, change
shape, rate-limit or block you (NSE often blocks scripts and cloud
servers: save the JSON from your browser and use `--offline`). Check
anything that matters against your broker before you act.

**Rules change.** STT, exchange charges, lot sizes, expiry days and
holidays all changed in 2024-2026. Every number is in
`trader-market/market.py` with the date it was checked
([docs/INDIA_RULES.md](docs/INDIA_RULES.md)). Known gaps:
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).

**Taxes.** Intraday equity is speculative business income, F&O is
non-speculative business income, delivery trades are capital gains. The
tax command is a rough guide, not tax advice; ask a CA.

## Files

```
trader-market/      SKILL.md, market.py (the shared India core)
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-fno/         SKILL.md, fno.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json
trader-broker/      SKILL.md, ticket.py, paper.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-insider/     SKILL.md, insider.py
docs/               CHANGES-INDIA.md, SETUP.md, INDIA_RULES.md, LIMITATIONS.md, CONTRACTS.md
tests/              unittest suite + offline fixtures
```

MIT. Original by [Jake Schincariol](https://opusjake.ai); India edition by
aditya76792.
