# Known limitations

Read this before you trust any number from the pack.

## Data
- **Unofficial sources.** Yahoo Finance and NSE/BSE website APIs are not
  licensed market-data feeds. They can be delayed (Yahoo's NSE data is
  typically delayed), change shape without notice, rate-limit, or block.
  The tools fail with one plain error line and accept `--offline` files.
- **NSE blocks scripts.** Cloud servers and some networks get HTTP 403
  from www.nseindia.com. The fix is your own computer, or saving the JSON
  from your browser. During development the live checks were done in a
  browser; the Python network paths were tested against recorded shapes,
  not against the live servers.
- **No tick data, no depth.** 1- and 5-minute bars only; no order book, so
  fills in the paper broker and the backtester are bar closes plus
  slippage, never real queue positions or partial fills.
- **Previous close.** Where Yahoo's previous close does not fit the day,
  the last trade of the previous session is used. NSE's official close is
  a volume-weighted average of the last 30 minutes, so gaps can differ
  slightly from NSE's figures.
- **No live option or futures prices from Yahoo.** Premiums come from the
  NSE option chain (trader-fno) or from prices you type in.
- **2027 holidays are not built in.** Save NSE's holiday master to
  `trader/holidays.json` when it is published.
- **Lot sizes change twice a year.** Run `market.py lots --refresh`.
- **Tick sizes.** The price-band tick table follows NSE's 2025 revision as
  understood on the check date; NSE reviews it periodically. A wrong tick
  is a rejected order, not a bad fill.

## Market logic
- **Circuit limits** are only checked when a band is known (config `bands`
  or NSE's price-band file). F&O stocks have dynamic bands that the tools
  cannot see.
- **Margins** for futures and short options are estimates. Real SPAN +
  exposure margins change daily; use your broker's margin calculator.
- **Surveillance lists** (ASM/GSM, F&O ban, trade-for-trade) are only as
  current as the lists you paste into the config; the tools do not fetch
  them automatically.
- **Expiry rules** cover NSE and BSE index weeklies and monthlies as of
  October 2026. SEBI changed expiry days in 2024 and 2025 and may again.
- **Muhurat trading** is recognised as a special day, but its timing is
  announced by NSE circular and is not modelled.
- **Earnings (trader-analyst)** parses exchange XBRL by tag name. Banks,
  NBFCs and insurers use different line items, and companies tag
  differently; check the figures against the results PDF. Year-on-year
  growth needs the earlier quarters' filings to be readable.
- **Insider tracker** covers NSE disclosures only, not BSE-only companies.

## Simulation and backtests
- The Time Machine and backtester resample about 60 sessions of the
  stock's own 5-minute bars. That is a small sample; it cannot contain
  results days, regulatory shocks or circuits that did not happen in it.
- Option plans cannot be replayed from the stock's bars (premiums are not
  modelled); the Time Machine refuses them.
- Charges use today's statutory rates and your broker's published tariff.
  Your contract note is the truth.

## Orders
- The pack never places live orders. Broker field mappings for Kite,
  Upstox and Dhan are guides; check each broker's API documentation.
- The paper broker charges a DP fee on every CNC sell fill, not once per
  scrip per day, so it is slightly pessimistic for split exits.

## Tax
The tax command is a rough guide for individuals under the new regime. It
ignores surcharge, rebates on special-rate income, presumptive schemes,
audit costs and every personal circumstance. Ask a chartered accountant.
