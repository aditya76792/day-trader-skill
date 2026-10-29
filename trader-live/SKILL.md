---
name: trader-live
description: >-
  Live Engine for NSE through Upstox: real-time monitoring of a watchlist,
  signal generation (opening-range breakout, VWAP reclaim, gap) on every
  completed 5-minute bar, automatic sizing and risk checks, and full order
  management (entry, stop-loss, target as one-cancels-the-other, square-off by
  15:10, kill switch, daily loss halt, crash-safe journal with broker
  reconciliation). Paper trading on the real-time Upstox feed first, a
  replay validator for the whole workflow, and live trading only after the
  user approves it at a terminal; every live entry still waits for the user's
  typed yes. Use when the user says "start the live engine", "paper trade
  live", "monitor my watchlist", "connect Upstox", "log in to Upstox", "kill
  switch", "flatten everything", "is the engine running", or "go live".
argument-hint: "[setup | login | doctor | validate | run | status | confirm N | kill]"
---

# trader-live

The live engine. It runs on the user's own computer during market hours,
watches the watchlist in real time through the Upstox API, turns fresh
signals into sized and risk-checked orders, and manages every open trade
until it is closed. It reuses the pack's own tested logic: trader-entry's
signal rules, trader-exit's stop and target, trader-budget's sizing
(loss at the stop including charges) and trader-risk's checks.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/live.py" setup        # creates ~/.config/trader-india/.env (user fills it in)
python3 "${CLAUDE_SKILL_DIR}/live.py" login        # Upstox login in the browser, each trading morning
python3 "${CLAUDE_SKILL_DIR}/live.py" doctor       # credentials, session, funds, static IP, readiness
python3 "${CLAUDE_SKILL_DIR}/live.py" validate     # whole workflow on a simulated day, no network
python3 "${CLAUDE_SKILL_DIR}/live.py" run          # PAPER trading on real-time Upstox data (default)
python3 "${CLAUDE_SKILL_DIR}/live.py" status       # trades, P&L, halts, heartbeat
python3 "${CLAUDE_SKILL_DIR}/live.py" kill --flatten
python3 "${CLAUDE_SKILL_DIR}/live.py" record RELIANCE TCS   # save today's bars for a replay
```

## Credentials (never in the chat)

The user creates an app in the Upstox developer console with the redirect
URL `http://127.0.0.1:8765/callback`, runs `live.py setup`, and pastes the
API key and secret into `~/.config/trader-india/.env` themselves (mode
600). Never ask for the key, secret, PIN, TOTP or access token in the chat,
never print them, never put them in the repository. `live.py login` opens
Upstox's login page; the token lands in `~/.config/trader-india/` (mode
600) and lasts until 03:30 IST the next morning.

## Modes

| mode | what happens |
| --- | --- |
| validate | a scripted day (or a recorded one) replays through the engine with the paper broker; it must end with every trade protected by a stop and closed, no entry after the window, nothing left open |
| paper (default) | real-time Upstox prices, simulated fills with slippage and full charges; `paper_auto_confirm` lets it run unattended so the workflow is validated |
| live | real Upstox orders. Locked until: a passing validation, a full paper session on the real-time feed, `"paper": false`, and `live.py approve-live` typed by the user at a terminal with the exact phrase. The approval is tied to the limits and watchlist; changing them locks live mode again |

In live mode every new entry waits for `live.py confirm N`, which only works
when typed at a terminal: the engine writes the ticket, notifies the user,
and the signal expires after `confirm_timeout_seconds` (120) or if price
runs more than `max_chase_r` (0.25R) past the planned entry. Exits never
wait.

## Order management

1. Entry: a marketable LIMIT a couple of ticks past the last price;
   cancelled if not filled in `entry_timeout_seconds`.
2. On fill: stop-loss (SL-M for stocks, SL with a buffer for F&O) and a
   target LIMIT go in at once. When one fills the engine cancels the other.
   If both ever fill (a race), it flattens the extra quantity and halts.
3. At `flat_by` (15:10) every MIS trade is closed at market, before the
   broker's own square-off.
4. Order writes are never retried automatically (a retry could duplicate
   an order); each order carries a tag (`TDR1029-03E`) and an uncertain
   placement is looked up by tag.
5. Every change is journaled (`trader/live/journal-DATE-MODE.json`); after a
   crash or restart the engine resumes, and in live mode reconciles with the
   Upstox order book and positions, halting on anything unexplained.

## Guards

Kill switch file (`live.py kill`, `--flatten` closes everything), daily
loss limit on realised plus open P&L (halts and flattens), repeated
API/data errors, stale quotes (no new entries), Upstox token expiry. The
engine also respects every trader-risk rule: entry window, max trades,
max loss per trade with charges, position caps, product rules.

## Configuration (`trader/config.json`)

```json
"live": {"mode": "paper", "feed": "upstox", "watchlist": ["RELIANCE", "TCS", "INFY"],
         "strategy": "orb", "or_minutes": 15, "sides": ["buy"], "stop": "or_mid",
         "target_r": 2.0, "max_open_trades": 1, "poll_seconds": 2}
```

## Rules

- Not financial advice. Most intraday traders lose money; the engine
  makes trading faster, not better. Say so when the user moves to live.
- No live order without the user's explicit yes typed at their terminal,
  and no live mode without their typed approval. Claude never runs
  `approve-live` or `confirm` for live trades, and never edits
  `LIVE_APPROVAL.json` or the confirmation files.
- Never ask for or handle Upstox credentials or tokens in the chat.
- SEBI's retail algo framework: API orders may need a static IP registered
  with Upstox (`doctor` shows it); a few confirmed orders a day is far below
  the 10 orders a second that needs algo registration.
- Market data from Upstox is real time but can lag or drop; the engine stops
  new entries on stale data. Yahoo is delayed and is paper-only.
