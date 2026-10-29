---
name: trader-insider
description: >-
  The Insider Tracker for NSE. Reads India's public trade disclosures: SEBI
  PIT (insider trading) disclosures by promoters, the promoter group,
  directors, KMP and designated employees, including promoter pledges and
  pledge invocations, and NSE's daily bulk, block and short deals. Highlights
  promoter market buys and sells, pledges and invocations, sizes in Rs crore,
  late disclosures (over 2 trading days), institutions you follow
  ("watch_clients") and your own symbols, and alerts once per new item.
  Public disclosures, not inside information. Use when the user says "are
  promoters buying", "insider trading in RELIANCE", "promoter pledge",
  "pledge invoked", "bulk deals today", "block deals", "which fund bought",
  "did any mutual fund buy X", "alert me when promoters buy", or "run the
  insider skill".
argument-hint: "[insider|deals|new|watch] [--symbol SYMBOL]"
---

# trader-insider

In India, insiders must tell the exchange when they trade, and big trades
by anyone are published the same day. This skill reads both, notices what is
new, and says plainly what each item is. It replaces the US Congress tracker:
the Indian equivalents of "who is buying" are SEBI PIT disclosures and NSE's
bulk and block deals.

It is fast. It is not early. An insider has up to 2 trading days to disclose
and the company 2 more to pass it on; bulk deals come out after the close.
There is no inside information here, only public filings read quickly.
Following them is not a strategy. Say that every time. Not financial advice.

Tool in this folder (standard library Python; it imports the shared India
core `trader-market/market.py` for NSE access, trading days and Rs formatting):

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" --help
```

## What it reads and writes

- Reads `https://www.nseindia.com/api/corporates-pit?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`
  (`{"acqNameList": [...], "data": [...]}`: symbol, company, acqName,
  personCategory, secType, secAcq, secVal, tdpTransactionType, acqMode,
  acqfromDt, acqtoDt, date, holdings before and after; missing fields are
  tolerated).
- Reads `https://www.nseindia.com/api/snapshot-capital-market-largedeal`
  (`as_on_date`, `BULK_DEALS_DATA`, `BLOCK_DEALS_DATA`, `SHORT_DEALS_DATA`).
- Writes `trader/state/insider.json` (ids already shown and the last check
  time, so each item alerts once).
- Reads `trader/config.json` for `"watch_clients"` (institution names to flag
  in deals) and `"insider_watch": {"symbols": [...], "names": [...]}`, and
  `trader/portfolio.json` to watch what you hold.

## Commands

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" insider                                   # last 7 days, highlights first
python3 "${CLAUDE_SKILL_DIR}/insider.py" insider --symbol RELIANCE --days 30
python3 "${CLAUDE_SKILL_DIR}/insider.py" insider --category promoter --kind market
python3 "${CLAUDE_SKILL_DIR}/insider.py" insider --kind pledge --from 2026-10-01 --to 2026-10-28
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals                                     # today's bulk / block / short
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals --client "mutual fund" --min-cr 10
python3 "${CLAUDE_SKILL_DIR}/insider.py" new                                       # only what is new
python3 "${CLAUDE_SKILL_DIR}/insider.py" new --all                                 # every new disclosure
python3 "${CLAUDE_SKILL_DIR}/insider.py" watch                                     # config insider_watch
```

Every command takes `--json`, `--now "YYYY-MM-DD HH:MM"` (IST) and
`--config`. `insider` and `deals` take `--offline FILE` (a saved NSE answer);
`new` and `watch` take `--offline-pit FILE` and `--offline-deals FILE`.
`new` exits 0 when something new was shown, 1 when nothing was. If one source
fails it says `partial:` and still reports the other.

## How to read the items

- **Promoter market buy / sell**: a promoter or promoter-group entity bought
  or sold on the exchange. The clearest disclosure there is, and still old
  news by the time you read it.
- **Pledge created / released / invoked**: promoters borrow against their
  shares. An invocation means the lender enforced the pledge and took or
  sold the shares: worth knowing, not a signal to act on.
- **Not a market trade**: ESOP allotments, gifts, inter-se transfers between
  promoter entities, off-market moves. They change who holds the shares, not
  what the market paid.
- **Late**: more than 2 trading days between the last trade date (`acqtoDt`)
  and the day the insider told the company (`intimDt`; NSE's publication
  `date` when that is missing): reg 7(2)(a). Separately, more than 2 trading
  days between `intimDt` and NSE's `date` means the company passed it on late:
  reg 7(2)(b). Counted on the NSE calendar, so holidays do not count.
- **Off Market / Other**: live rows often say `acqMode` "Off Market" and
  `personCategory` "Other"; quantity and value may sit only in `buyQuantity` /
  `sellquantity` and `buyValue` / `sellValue`, which the tool reads too.
- **Bulk deal**: one client's trades in one stock on one day above 0.5 % of
  its listed shares, reported the same day. A buy and a sell of almost the
  same size by different clients is often one block changing hands.
- **Block deal**: one large trade in the block windows 08:45-09:00 or
  14:05-14:20 IST (`market.BLOCK_WINDOWS`), at a price close to the market.
- **Short deal**: institutional short sales reported for the day.

## Steps

1. `insider` or `deals` to see what was disclosed; `watch` for the user's
   own symbols, people and institutions.
2. For an alert loop, run `new` after the close on weekdays (a scheduled task
   or `/loop 1h`): NSE posts bulk and block deals after market hours and PIT
   disclosures through the evening.
3. Write it up in one line per item: who, what, how much in Rs crore, market
   or not, when traded, when disclosed. Then the standard line: "These are
   public disclosures made after the trade, not inside information."
4. If the user wants to act on one, it goes the long way: `/trader-scanner`
   and `/trader-entry` decide if there is a setup today; `/trader-budget`
   sizes it, `/trader-risk` checks it, `/trader-broker` places it only after
   the user's explicit yes.

## Example

```
2026-10-28 18:12  SAMPLECO   Ramesh Kumar Sample (Promoters): BUY 1,20,000 Equity Shares, Rs 1.41 cr,
Market Purchase; traded 2026-10-26 to 2026-10-27, told the company 1 trading day later; shares held
4,51,00,000 -> 4,52,20,000
   <- PROMOTER MARKET BUY
```

(Fictional company from the test fixtures.) Then: "A promoter bought about
Rs 1.4 crore of shares on the market on 26-27 October and disclosed it a day
later. It says what they did, not what the price will do."

## Sources and their limits

Data from NSE's public endpoints is unofficial, delayed and can break. NSE
blocks many scripts and data-centre networks: open the URL in a browser
after visiting nseindia.com, save the JSON, and pass `--offline FILE`. BSE
publishes its own disclosures for BSE-only companies; this tool reads NSE.

## Rules

- Public disclosures, not inside information. Never say or imply anyone has
  inside information, or that copying promoters or funds beats the market.
- Quote sizes as filed (Rs crore), with the disclosure date and the trade
  dates; never present a disclosure as a live trade.
- Never places an order and never turns a disclosure into a trade on its own.
- Not financial advice. Paper trading is the default (`"paper": true`).
- Nothing in this pack places an order except `/trader-broker`, and only after
  `/trader-risk` passes AND the user types an explicit yes for that order in
  the chat AND Claude's own "Allow once / Deny" card is approved (the order
  tool set to "Needs approval"). Never "Always allow" an order tool.
