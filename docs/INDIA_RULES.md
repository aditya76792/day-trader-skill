# Indian market rules used, with sources

Checked on 28 October 2026. Each rule lives in `trader-market/market.py`
and is pinned by `tests/test_market.py`, so a change is deliberate. When a
rule changes, update the constant, its test and the date here.

"Live" means read directly from the exchange's own website or API in a
browser on 28 Oct 2026. "Secondary" means broker or education pages.

## Sessions (NSE equity)
| item | value | source |
| --- | --- | --- |
| Pre-open | 09:00-09:08 order entry, 09:08-09:15 matching/buffer | NSE market timings page; Bajaj Broking (secondary) |
| Normal market | 09:15-15:30 IST | NSE market timings (live) |
| Closing session | 15:40-16:00 | NSE market timings (live) |
| Block deal windows | 08:45-09:00, 14:05-14:20 | NSE market timings (live) |
| Yahoo bars | 5-minute bars from 09:15, 75 a session, `gmtoffset` 19800 | Yahoo chart API for RELIANCE.NS (live) |

## Holidays 2026
NSE `/api/holiday-master?type=trading` (live): CM and FO lists identical.
Weekday holidays: 15 Jan, 26 Jan, 3 Mar, 26 Mar, 31 Mar, 3 Apr, 14 Apr,
1 May, 28 May, 26 Jun, 14 Sep, 2 Oct, 20 Oct, 10 Nov, 24 Nov, 25 Dec.
Weekend: 15 Feb, 21 Mar, 15 Aug, 8 Nov (Diwali Laxmi Pujan; Muhurat
trading, timing by NSE circular).

## Derivatives
| item | value | source |
| --- | --- | --- |
| Weekly expiries | one index per exchange: NIFTY (NSE) Tuesday, SENSEX (BSE) Thursday | SEBI (Nov 2024); Quantsapp, NSE contract info (live) |
| Monthly | NSE last Tuesday; BSE last Thursday | Quantsapp; NSE expiry list (live) |
| Holiday rule | previous trading day | NSE expiry list (live): 9 Nov and 23 Nov 2026 |
| NIFTY expiries listed by NSE on 28 Oct 2026 | 03-Nov, 09-Nov, 17-Nov, 23-Nov, 01-Dec, 29-Dec, 25-Jan-2027 ... | NSE `/api/option-chain-contract-info` (live) |
| Lot sizes | NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120, NIFTYNXT50 25, SENSEX 20, BANKEX 30; RELIANCE 500, TCS 225, HDFCBANK 650, INFY 400, SBIN 750, ICICIBANK 700 | Quantsapp, OneTradeJournal (secondary, agree) |
| Strike step | NIFTY 50 (NSE chain, live), BANKNIFTY/SENSEX 100 | NSE chain (live); exchange specs |
| Option chain API | `/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=03-Nov-2026` | NSE (live) |
| Option style | European (index and stock options) | exchange specs |
| Settlement | index: cash; stock F&O: physical delivery | exchange specs |

## Charges (per executed order)
| item | rate | source |
| --- | --- | --- |
| STT delivery | 0.1% buy and sell | NSE, Zerodha, Upstox, Cleartax |
| STT intraday | 0.025% sell | same |
| STT futures | 0.05% sell (from 1 Apr 2026, was 0.02%) | Budget 2026; Upstox, Zerodha, NSE |
| STT options | 0.15% of premium, sell (from 1 Apr 2026, was 0.1%) | same |
| STT on exercise | 0.15% of intrinsic value | NSE, Zerodha |
| NSE transaction | equity 0.00307%, futures 0.00183%, options 0.03553% (from 1 Mar 2026) | Upstox, Zerodha, Angel One |
| BSE transaction | equity 0.00375% (A/B groups), futures nil, options 0.0325% | Upstox, Zerodha |
| SEBI fee | Rs 10 per crore | all |
| IPFT (NSE) | Rs 0.01 per crore | Zerodha, Angel One |
| Stamp duty (buy) | delivery 0.015%, intraday 0.003%, futures 0.002%, options 0.003% | all |
| GST | 18% on brokerage + exchange + SEBI fee | Zerodha |
| DP charge | Zerodha Rs 13 + GST = Rs 15.34; Upstox and Angel One Rs 20 + GST, per scrip sold from demat | broker pages |
| Brokerage | Zerodha: delivery 0, intraday/futures 0.03% or Rs 20, options Rs 20. Upstox: delivery Rs 20, intraday 0.1% or Rs 20, futures 0.05% or Rs 20, options Rs 20. Angel One: 0.1% or Rs 20 (min Rs 5) delivery, Rs 20 F&O | broker pages, read 28 Oct 2026 |

Sources disagree on small details (the GST base, BSE group rates, IPFT
notation). This edition follows Zerodha's published method; set your own
broker table in `trader/config.json` if your contract note differs.

## Margin and leverage
| item | value |
| --- | --- |
| Intraday equity (MIS) | SEBI peak margin rules cap leverage at 5x: margin at least 20% (higher for many stocks). The pack uses full value unless `use_intraday_leverage` is true. |
| Futures | SPAN + exposure, roughly 10-20% of value and changing daily: an estimate only (`futures_margin_pct`). |
| Short options | SPAN + exposure; an extra 2% ELM on expiry day; no calendar-spread benefit on expiry day (SEBI, from Nov 2024). |

## Circuits
| item | value |
| --- | --- |
| Stock bands | 2%, 5%, 10%, 20% for stocks outside F&O; F&O stocks have dynamic bands (start at 10%, widened by the exchange) |
| Index circuit breaker | 10%: before 13:00 a 45-minute halt, 13:00-14:30 15 minutes, after 14:30 none. 15%: before 13:00 1h45m, 13:00-14:00 45 minutes, after 14:00 rest of day. 20%: rest of day. A pre-open call auction follows a halt. |

The circuit-breaker durations are SEBI's long-standing MWCB table, not
re-read from a primary source in this check. Verify against NSE's circuit
breaker page before relying on them.

## Broker auto square-off (MIS)
Zerodha (support page): 15:12 for some stocks, 15:25 other stocks, 15:26
F&O; Rs 50 + GST per order squared off. Upstox and Angel One: not
re-checked; the pack uses 15:20 and 15:15 and keeps `flat_by` at 15:10.

## Tax (individuals, a guide)
Intraday equity: speculative business income at slab rates; losses only
against speculative income, carried 4 years. F&O: non-speculative business
income at slab rates; losses carried 8 years; ITR-3; tax audit above the
turnover limit (Rs 10 crore for FY24-25 per Zerodha Varsity). Delivery:
STCG 20%, LTCG 12.5% above Rs 1.25 lakh a year (holding over 12 months).
New-regime slabs FY2026-27 (Cleartax): 0-4L nil, 4-8L 5%, 8-12L 10%,
12-16L 15%, 16-20L 20%, 20-24L 25%, above 30%; plus 4% cess. The Income-tax
Act 2025 applies from 1 April 2026 and kept the capital-gains provisions
per Cleartax; the LTCG figures were not re-verified from a primary source.

## SEBI retail algo framework
Circular of 4 Feb 2025, in force in full from 1 April 2026 (secondary
sources: Antsheel, Sahi). API orders are covered; below 10 orders a second
per exchange no algo registration is needed; brokers may require a static
IP and order tags. This pack only builds tickets; every order is approved
by the user.

## Data sources (checked live)
| source | used for | status on 28 Oct 2026 |
| --- | --- | --- |
| Yahoo chart `/v8/finance/chart/RELIANCE.NS`, `^NSEI`, `^NSEBANK`, `^BSESN`, `^INDIAVIX` | bars and quotes | works; `.BO` with a BSE scrip code (500325.BO) does not, use the ticker |
| Yahoo spark `/v7/finance/spark` | scans | works for `.NS`, up to 20 symbols |
| Yahoo options `/v7/finance/options` | | needs a crumb (401): not used |
| Yahoo RSS headlines | | empty for Indian tickers: not used |
| NSE `/api/option-chain-v3`, `/api/option-chain-contract-info` | trader-fno | works with NSE cookies |
| NSE `/api/holiday-master?type=trading` | calendar | works |
| NSE `/api/corporate-announcements` | trader-news | works |
| NSE `/api/corporates-pit`, `/api/snapshot-capital-market-largedeal` | trader-insider | works |
| NSE `/api/integrated-filing-results` + XBRL | trader-analyst | works; the older `/api/corporates-financial-results` stops at Dec 2024 |
| NSE `/api/quote-equity` | | 403: not used |
| NSE archives (`nsearchives.nseindia.com`) | lots, symbol lists | serve files to browsers; scripts may need the browser's cookies |
| BSE `api.bseindia.com` announcements | trader-news | works with a bseindia.com Referer |
| Google News RSS (India) | trader-news | works; personal, non-commercial use only per its terms |
