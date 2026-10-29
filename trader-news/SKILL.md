---
name: trader-news
description: >-
  News Monitor for Indian stocks (NSE/BSE). Tracks the company's own
  exchange filings (NSE corporate announcements, and BSE announcements when
  a BSE scrip code is set), Google News headlines from Indian media, and how
  far each stock has moved and gapped today against yesterday's close, with
  a NIFTY 50 line for context. Shows only what is new since the last check,
  newest first, in IST, and tags India-relevant catalysts: results, board
  meetings, dividend and record dates, bonus, split, buyback, rights, QIP and
  preferential issues, block and bulk deals, promoter pledges, SEBI orders,
  auditor resignations, credit ratings, mergers and demergers, order wins,
  CEO/MD/CFO changes, insolvency/NCLT, F&O ban, ASM/GSM surveillance and
  circuits. Use when the user says "any news on RELIANCE", "why is this
  stock moving", "check NSE announcements", "corporate actions", "results
  today", "morning briefing", "news on my watchlist", "did anything break",
  or after /trader-scanner finds setups.
argument-hint: "[symbols]"
---

# trader-news (India)

A chart can look perfect and still be about to gap on a results date, a SEBI
order or a promoter pledge. Before a setup becomes a trade, this skill reads
what the company told the exchange and what the press wrote, for every stock
on the watchlist, and tells you only what is new. It prints how far each
stock has moved today and how it opened, so a filing and a move sit side by
side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                               # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-google --json            # exchange filings only
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers SBIN --all --hours 72
```

Flags: `--hours 48` (window), `--all` (seen items too), `--no-filings`,
`--no-google`, `--no-moves`, `--move-pct 3`, `--now "2026-10-28 22:00"` (IST),
`--offline DIR`, `--dry-run` (leave the state alone), `--json`,
`--config trader/config.json`. Exit code 0 when something new showed, 1 when
nothing new or on an error (one plain `error: ...` line).

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade being planned (trader-exit); an option's underlying stock |
| `trader/portfolio.json` | positions (`"RELIANCE\|MIS": {...}`), index F&O left out |
| `trader/config.json` | `names` {SYMBOL: company name}, `bse_codes` {SYMBOL: scrip code} |
| `trader/universe.json` | company names saved by `scan.py universe` |
| NSE announcements | `www.nseindia.com/api/corporate-announcements?index=equities&symbol=SYMBOL` |
| BSE announcements | `api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?...&strScrip=CODE...` (only with a code) |
| Google News RSS | `news.google.com/rss/search?q="Company Name" when:2d&hl=en-IN&gl=IN&ceid=IN:en` |
| Yahoo chart (SYMBOL.NS) | the last price, yesterday's close, today's open |
| `trader/state/news.json` | what has been shown (kept 30 days) |

Yahoo's own headline RSS answers an empty feed for NSE symbols, so it is not
used. Google's feed terms allow personal, non-commercial use only. All of
these are free public endpoints: unofficial, delayed, and they can break or
block scripts (NSE often blocks data-centre networks). When NSE blocks, open
the URL in a browser, save the JSON as `nse-SYMBOL.json` in a folder and pass
`--offline FOLDER` (likewise `bse-CODE.json`, `gnews-SYMBOL.xml`,
`chart-SYMBOL.json`).

## India-specific rules

- **Exchange filings beat headlines.** An NSE or BSE announcement is what
  the company told the exchange under SEBI's LODR rules; a headline is a
  lead. Open the PDF (the link) before telling the user something happened.
- **Catalyst tags.** Every item lists the symbols it touches and its tags.
  `!` marks a high-impact one: results, bonus, split, buyback, rights issue,
  QIP/preferential issue, block/bulk deal, promoter pledge, SEBI action,
  auditor resignation, merger/demerger/acquisition, CEO/MD/CFO change,
  insolvency/NCLT, F&O ban, ASM/GSM surveillance. Board meetings, dividends,
  credit ratings, order wins and circuit news are tagged without `!`.
- **Results come after hours.** Indian companies often file results in the
  evening; the price reacts at the next 09:15 open, through the pre-open
  auction. Say that a result filed after 15:30 means tomorrow can gap.
- **MOVING** marks 3 % or more from yesterday's close; **GAP UP / GAP DOWN**
  marks an open 2 % or more away. A move with no filing or headline is worth
  saying out loud too (check for a block deal or an F&O ban).
- **Record dates.** A stock trades ex-dividend / ex-bonus / ex-split on the
  record date (T+1 settlement): a price drop that day is the adjustment, not
  news.
- Times are shown in IST. NSE `an_dt` and BSE `NEWS_DT` are IST; Google's
  `pubDate` is GMT and is converted.

## Steps

1. Run the check. Read every `!` item and every MOVING or GAP line first.
2. Write a short brief per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup ("results tonight: the
   stock can gap at 09:15 past any stop").
3. If a filing changes the picture (results inside the trade, a SEBI order,
   an F&O ban, ASM), say so before `/trader-entry` and `/trader-exit`.
4. During the session: `/loop 15m /trader-news`, or a scheduled task before
   09:00.

## Rules

- Never invent or embellish a story, a quote or a number.
- No predictions: report what was filed or published and how far the price
  is from yesterday's close. Never say what the stock will do next.
- Never trade on news and never tell the user to buy or sell because of a
  headline.
- One request per feed per symbol, with a pause between calls.
- Not financial advice. This skill only reads and reports. Nothing places an
  order except `/trader-broker`, after `/trader-risk` passes and the user
  types an explicit "yes" for that order.
