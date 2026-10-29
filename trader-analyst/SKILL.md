---
name: trader-analyst
description: >-
  The Earnings Analyst for Indian listed companies. Reads the quarterly
  results a company files with NSE under SEBI LODR (SEBI's integrated
  filings since 2025, NSE's older results listing before that, and each
  filing's Ind AS XBRL file), lays out the last quarters in
  Rs crore and says plainly what changed: revenue from operations, operating
  margin (EBITDA, approximate), net profit, net margin and EPS, each against
  the same quarter a year earlier (YoY) and the quarter before (QoQ), with
  consolidated preferred over standalone, Indian fiscal quarters (Q2 FY27),
  one-off flags (other income, exceptional items, odd tax rates) and when the
  next results are due. Falls back to a CSV of numbers typed from the results
  PDF. Use when the user says "how were Reliance's results", "analyse TCS
  quarterly results", "what changed this quarter", "is profit growing",
  "compare margins", "Q2 results", "when are results due", or before trading
  a stock around its board meeting.
argument-hint: "[NSE symbols]"
---

# trader-analyst

Results move Indian stocks more than anything else on the calendar, and the
press release puts the best number first. This skill reads the numbers the
company actually filed with the exchange, lines them up quarter by quarter,
and says plainly what changed. For a day trader it answers two questions
before a trade: what did the last results say, and are the next ones (the
board meeting) inside this trade?

Tool in this folder (standard library Python, no keys). It imports the shared
India core `trader-market/market.py` (NSE access, dates, money formatting).

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE TCS INFY --quarters 8 --filings 12   # YoY on every row
python3 "${CLAUDE_SKILL_DIR}/facts.py" HDFCBANK --standalone
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --json
python3 "${CLAUDE_SKILL_DIR}/facts.py" SAMPLECO --from-file trader/sampleco-quarters.csv
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --offline saved/   # integrated-RELIANCE.json, results-RELIANCE.json, XBRL files
```

## What it reads and writes

- Reads SEBI's "Integrated Filing - Financials" on NSE first (every
  quarterly result since early 2025):
  `https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=SYMBOL&type=Integrated%20Filing-%20Financials&page=1&size=20`
  (`{"data": [...], "size", "page", "totalCount"}`; each row has `qe_Date`,
  `consolidated` ("Consolidated" / "Non-Consolidated"), `audited`,
  `broadcast_Date`, `type_Sub` ("Original" / "Revised"), `revised_Date` and
  the `xbrl` URL). A revised filing replaces the original for its quarter.
- Falls back to NSE's older listing for quarters before 2025 (its rows stop
  around Dec 2024 and are not sorted, so the tool sorts them by period end):
  `https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=SYMBOL&period=Quarterly`.
  It is also used when the integrated endpoint cannot be read (said as
  `partial`).
- Then reads each quarter's XBRL instance from `nsearchives.nseindia.com`, by
  local tag name (namespaces vary): `RevenueFromOperations`, `OtherIncome`,
  `Income`, `Expenses`, `FinanceCosts`,
  `DepreciationDepletionAndAmortisationExpense`, `ExceptionalItemsBeforeTax`,
  `ProfitBeforeTax`, `TaxExpense`, `ProfitLossForPeriod`,
  `ProfitOrLossAttributableToOwnersOfParent`, basic and diluted EPS.
- **Contexts by date, not id.** The quarter is the undimensioned three-month
  duration ending on the quarter end. An integrated filing has only that
  quarter (`OneD`) and the year to date (`FourD`): no year-ago or
  previous-quarter column. So YoY and QoQ come from the earlier quarters'
  own filings: `--filings` (default quarters + 1) sets how many are read,
  and `--filings` of quarters + 4 gives YoY on every row. Old-format filings
  that carry comparison columns still use them (rows marked `c`). In a Q4
  filing the year's figures sit in another context; the dates decide.
- **Net profit** is the profit attributable to owners of the parent when the
  filing has it (consolidated; EPS is based on it), else profit for the
  period. Both are in `--json`.
- Writes `trader/metrics.json` (one row per symbol: quarter, revenue and net
  profit in crore, EPS, growth, margins, TTM when four quarters are there).
  `--no-write` skips it. `metrics.example.json` shows the shape (built from
  the test fixtures: the latest quarter is TCS's real Q2 FY27 filing, the
  earlier quarters are made-up test values).
- `--from-file` reads a CSV or JSON with `quarter_end, revenue, net_profit,
  eps` (Rs crore, EPS in Rs) and optional `other_income, finance_costs,
  depreciation, pbt, tax`, for when NSE blocks the script or a filing has no
  XBRL: type the numbers from the results PDF.

## India-specific rules

- **Consolidated first.** Most groups file both. The table uses consolidated
  when it exists (`--standalone` to switch) and never mixes the two.
- **Crore, not millions.** XBRL money facts are full rupees (`unitRef` INR,
  `decimals` "-7" = rounded to the crore), e.g. TCS's Q2 FY27 revenue is
  731880000000 = Rs 73,188 crore. The `LevelOfRounding` fact ("Crores") says
  how it was rounded, not a unit: values are never multiplied or divided by
  it, only divided by 1e7 for display. EPS (`INRPerShare`) is in rupees.
  `--scale` is a manual override for a filing you know is tagged otherwise.
- **Fiscal quarters.** April to March: the quarter ending 30 Sep 2026 is Q2 FY27.
- **Due dates.** SEBI LODR reg 33: quarterly results within 45 days of the
  quarter end, the March quarter (with the audited annual results) within 60.
  The tool says when the next results are due and when the listing looks late.
- **EBITDA is approximate**: profit before tax plus finance costs plus
  depreciation minus other income. Banks and NBFCs report interest as revenue; read their
  margins differently.
- **Flags.** Other income above a fifth of profit before tax, exceptional
  items, a tax rate outside 15-35 %, and net profit above EBITDA are all
  called out: profit that did not come from the business.

## Steps

1. Run it for the companies in question (or the symbols on
   `trader/watchlist.json`).
2. When a number matters, open the results PDF on nseindia.com (corporate
   filings, financial results) and check it. This is unofficial parsing of
   exchange filings and tags can be mis-mapped.
3. Write the summary: one line on the quarter (revenue, net profit, EPS,
   YoY), one on margins, one on anything unusual (a one-off, a restated
   comparison, data that lags), and the filing link. Plain words, real numbers.
4. If the next results (or the board meeting that approves them) fall inside
   the planned trade, say so before `/trader-exit` sets the stop: a stop does
   not protect against a gap, and stocks can open near a circuit on results.

## Example

`facts.py TCS` reads TCS's Q2 FY27 consolidated integrated filing (filed
08-Oct-2026): revenue Rs 73,188.00 cr, operating margin (EBITDA, approx.)
25.7 %, net profit to owners Rs 13,884.00 cr, EPS Rs 38.37. Growth needs the
earlier quarters' filings; in the test fixtures those are made-up values, so
the YoY there is an example of the output, not TCS's real growth.

## Sources and their limits

Data from NSE's public endpoints is unofficial, can lag and can break. NSE
often blocks scripts and data-centre networks: open the listing URL in a
browser, save the JSON as `integrated-SYMBOL.json` (and the older listing as
`results-SYMBOL.json`) with the XBRL files beside them, and pass
`--offline DIR`; or use `--from-file`. When some XBRL files cannot be
read, the report says `partial` and names the newer filing it could not read.

## Rules

- Filed numbers only. Never invent a number the filing does not have.
- Say the basis (consolidated or standalone), the quarter and the source.
- Unofficial parsing of exchange filings: verify against the PDF before
  relying on a number, and say so.
- Never says a stock is cheap, expensive, a buy or a sell. Not financial advice.
- This skill only reads filings. Nothing places an order except
  `/trader-broker`, after `/trader-risk` passes and the user types an
  explicit yes for that order.
