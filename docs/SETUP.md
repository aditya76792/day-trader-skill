# Setup

## 1. Requirements

- Python 3.8 or newer (`python3 --version`). Standard library only: no pip
  installs, no API keys for data.
- Optional: `pdftotext` is not needed by this edition.
- A normal home or office internet connection. NSE's website blocks many
  scripts and most cloud servers; run the tools on your own computer.

## 2. Install the skills

Claude Code plugin:

```
/plugin marketplace add aditya76792/day-trader-skill
/plugin install day-trader-skill-india@day-trader-skill-india
```

Or copy the folders. Keep all `trader-*` folders side by side: every tool
imports `trader-market/market.py`.

```bash
git clone https://github.com/aditya76792/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

## 3. Your config

```bash
mkdir -p trader
cp ~/.claude/skills/trader-budget/config.example.json trader/config.json
```

Edit `trader/config.json`. The important keys:

| key | default | what |
| --- | --- | --- |
| `capital_inr` | 100000 | the money this pack may plan with |
| `broker` | zerodha | charges and square-off time: zerodha, upstox, angelone, flat20, or set `"brokerage"` yourself |
| `paper` | true | keep it true until you have weeks of paper results |
| `max_loss_per_trade_inr` | 1000 | the loss at the stop, charges included |
| `max_daily_loss_inr` | 3000 | stop trading for the day after this |
| `max_trades_per_day` | 3 | |
| `min_reward_risk` | 1.5 | after charges |
| `no_entries_before` / `no_entries_after` | 09:30 / 14:45 | entry window, IST |
| `flat_by` | 15:10 | exit all MIS before the broker's auto square-off |
| `default_product` | MIS | MIS intraday, CNC delivery |
| `use_intraday_leverage` | false | MIS margin 20% (SEBI 5x cap) only if true |
| `allow_short` | false | intraday (MIS) shorts |
| `allow_option_buying` | true | |
| `allow_option_selling` / `allow_futures` | false | leave off unless you understand the margin and the unlimited risk |
| `max_premium_per_trade_inr`, `max_lots_per_trade` | 5000, 2 | option buying caps |
| `expiry_day_last_entry` | 13:30 | no new F&O entries after this on expiry day |
| `blocked`, `asm_gsm`, `fno_ban` | [] | symbols to refuse (paste today's F&O ban list) |
| `bands` | {} | known circuit bands, e.g. `{"SUZLON": 5}` |
| `bse_codes` | {} | for BSE announcements, e.g. `{"RELIANCE": "500325"}` |
| `insider_watch`, `watch_clients` | | names and symbols for the insider tracker |

## 4. Refresh the data that changes

```bash
python3 ~/.claude/skills/trader-market/market.py lots --refresh     # NSE fo_mktlots.csv -> trader/lots.json
python3 ~/.claude/skills/trader-scanner/scan.py universe --index nifty500
```

Holidays: 2026 is built in. For 2027, once NSE publishes it, open
`https://www.nseindia.com` in your browser, then
`https://www.nseindia.com/api/holiday-master?type=trading`, and save the
JSON as `trader/holidays.json`.

When NSE blocks a script ("NSE did not answer ..."), do the same for any
NSE API: open nseindia.com in the browser first, open the API URL the
error names, save the JSON, and pass it with `--offline FILE`.

## 5. Paper trade

```bash
python3 ~/.claude/skills/trader-broker/paper.py reset --capital 100000
python3 ~/.claude/skills/trader-broker/paper.py status
```

After you say yes to a ticket, Claude runs `paper.py place --index N`.
`paper.py check` works the open stop and target, and squares off MIS at
the broker's time. Contract notes land in `trader/paper/contract_notes/`.

## 6. A live broker (optional, later)

This pack never sends orders by itself. If you connect a broker's MCP
server (for example Zerodha's hosted Kite MCP), see
`trader-broker/claude_desktop_config.example.json`, and in Claude set
every order tool to **Needs approval**. Under SEBI's retail algo
framework (from 1 April 2026) your broker may require a registered static
IP for API orders; a few orders a day approved by hand stays far below the
10 orders-a-second level that needs algo registration.

## 6b. Real time

For real-time monitoring, signals and order management through Upstox (paper first,
live only after your approval), see [LIVE.md](LIVE.md).

## 7. Run the tests

```bash
cd day-trader-skill
python3 -m unittest discover -s tests -v
```

All tests run offline. At the time of this edition: 390+ tests, all
passing on Python 3.8+.

## 8. A quick live check

```bash
python3 trader-market/market.py clock
python3 trader-alerts/alerts.py quote RELIANCE TCS
python3 trader-fno/fno.py chain NIFTY
python3 trader-news/news.py --tickers RELIANCE --hours 24
```

If Yahoo answers but NSE does not, you are on a network NSE blocks; use
`--offline` with files saved from your browser.
