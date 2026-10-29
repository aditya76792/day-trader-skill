# Live engine: setup, validation, deployment

`trader-live` turns the pack into a real-time system on your Mac:
Upstox market data -> signals -> sizing -> risk check -> orders -> stop-loss
and target management -> square-off. Paper trading is the default. Live
orders are locked until you validate, paper trade, and approve.

## 1. Requirements

- macOS (or Linux) with Python 3.8+. Standard library only.
- An Upstox account with F&O/equity segments active, and the Upstox API
  (free). Create an app at <https://account.upstox.com/developer/apps>:
  - Redirect URL: `http://127.0.0.1:8765/callback`
  - For live orders under SEBI's retail algo framework, Upstox may accept
    API orders only from a registered static IP: register it in the app's
    "Static IPs" section (changeable once a week). A home broadband IP is
    usually not static; ask your ISP, or run the engine from a cloud VM in
    India with a fixed IP. Paper trading does not need this.
- The Mac awake, online and on IST time from 09:00 to 15:31.

## 2. Credentials (you do this; never paste them in a chat)

```bash
cd day-trader-skill-india
python3 trader-live/live.py setup
open -e ~/.config/trader-india/.env      # paste UPSTOX_API_KEY and UPSTOX_API_SECRET, save
python3 trader-live/live.py login        # browser login; token valid until 03:30 IST tomorrow
python3 trader-live/live.py doctor
```

- The `.env` file and the token are mode 600 in `~/.config/trader-india/`,
  outside the repository; the tools refuse a secrets file other users can
  read. `.gitignore` keeps `.env` and `trader/` out of git.
- Logs never contain the token (they are passed through a redactor).
- Log in every trading morning before 09:15 (Upstox tokens expire at 03:30).

## 3. Configure (`trader/config.json`)

Start from `trader-budget/config.example.json`, set `"broker": "upstox"` (so charges and the
square-off time match your account), and add:

```json
"live": {
  "mode": "paper",
  "feed": "upstox",
  "watchlist": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"],
  "strategy": "orb",
  "or_minutes": 15,
  "sides": ["buy"],
  "stop": "or_mid",
  "target_r": 2.0,
  "max_open_trades": 1,
  "poll_seconds": 2,
  "confirm_timeout_seconds": 120,
  "paper_auto_confirm": true
}
```

Your rupee limits (`max_loss_per_trade_inr`, `max_daily_loss_inr`,
`max_trades_per_day`, `flat_by`, `no_entries_after` ...) apply to the engine
exactly as to the manual skills.

## 4. Validate (no network)

```bash
python3 trader-live/live.py validate
```

A scripted day replays through the real engine with the paper broker: one
breakout that reaches its target, one that fails to its stop, one that
drifts and is squared off at 15:10, one that never breaks out, and one that
breaks out after the entry window (and must be ignored). It passes only if
every entry was sized and risk-checked, every fill got a stop, the other
exit was cancelled, nothing was entered late and nothing is left open.

To replay a real day: `live.py record RELIANCE TCS` after 15:30, then
`live.py validate --replay trader/live/recordings/2026-10-29`.

## 5. Paper trade on real-time data

```bash
python3 trader-live/live.py run           # leave it running; Ctrl+C stops it
python3 trader-live/live.py status        # in another Terminal tab
```

Prices and candles are real time from Upstox; fills are simulated with
slippage and full charges. Run at least one full session (09:15 to 15:30)
without problems: its report (`trader/live/reports/DATE-paper-upstox.json`)
is required before live mode unlocks. Run a few weeks, honestly.

Optional, start the paper engine automatically on weekdays at 09:05 with
launchd (you still log in each morning):

```bash
sed -e "s#REPO#$PWD#g" -e "s#WORKDIR#$PWD#g" trader-live/com.trader-india.engine.plist \
  > ~/Library/LaunchAgents/com.trader-india.engine.plist
launchctl load ~/Library/LaunchAgents/com.trader-india.engine.plist
```

The launchd job only ever starts paper mode.

## 6. Going live (only when you decide)

1. `live.py doctor` shows "ready to approve".
2. In `trader/config.json`: `"paper": false` and `"live": {"mode": "live", ...}`.
3. At your Terminal: `python3 trader-live/live.py approve-live`. It shows
   your limits and asks you to type the exact sentence
   `I APPROVE LIVE TRADING WITH REAL MONEY`. It refuses to run from a script
   or an assistant. Changing any limit or the watchlist later locks live
   mode until you approve again. `live.py revoke-live` undoes it.
4. `python3 trader-live/live.py run --mode live`
5. Each signal pops a notification. Confirm at the Terminal within two
   minutes: `python3 trader-live/live.py confirm 3` and type `yes`.
   Anything else, or no answer, and it is skipped. Stops, targets and the
   square-off are placed automatically.

Emergency: `python3 trader-live/live.py kill --flatten` (cancels pending
entries and closes open trades at market on the next tick). If the engine is
not running, close positions in the Upstox app; the stop-loss and target
orders already at Upstox stay there.

## 7. What protects you

| risk | protection |
| --- | --- |
| a bug or bad signal sizes too big | trader-budget sizes so the loss at the stop with charges is under your limit; trader-risk re-checks every order |
| runaway losses | daily loss limit on realised + open P&L halts and flattens |
| duplicate orders on a network error | order writes are never retried; tags are looked up instead |
| engine crash or Mac sleep | stops and targets already sit at the broker; on restart the journal resumes and is reconciled with Upstox's order book and positions |
| both exits filling | the extra quantity is flattened at once and the engine halts |
| stale or missing data | no new entries while quotes are older than 30 s |
| MIS left open | everything is closed at `flat_by` (15:10), before the broker's own square-off |
| accidental live trading | five locks: validation, a real-time paper session, `"paper": false`, your typed approval tied to your limits, and a typed yes per entry |

## 8. Files

```
~/.config/trader-india/.env                 your Upstox key and secret (600)
~/.config/trader-india/upstox-token.json    today's session (600)
~/.cache/trader-india/                      Upstox instrument list (daily)
trader/live/journal-DATE-MODE.json          every trade and order, rewritten atomically
trader/live/logs/engine-DATE-MODE.log       the log (secrets redacted)
trader/live/reports/                        validation and end-of-day reports
trader/live/pending/, confirmed/            tickets waiting for you, and your answers
trader/live/KILL                            the kill switch (delete or `live.py resume`)
trader/live/LIVE_APPROVAL.json              your live approval
```

## 9. Limits of this version

- Data is polled over REST (LTP every 2 s, 1-minute candles each bar).
  Upstox's WebSocket feed is protobuf, which needs a library; polling is
  enough for 5-minute-bar strategies but not for tick scalping.
- The engine's automatic signals trade NSE stocks (MIS). Option and futures
  trades go through the manual flow (trader-fno -> budget -> risk -> broker).
- A stop-loss is an order, not a guarantee: gaps, circuits and fast markets
  fill past it. The validator includes such a case.
- Charges in the engine are the pack's estimate; your Upstox contract note
  is the truth.
- Not financial advice. Most intraday traders lose money.
