---
name: trader-broker
description: >-
  Order Executor for NSE/BSE, paper trading by default, never without your
  yes. Shows each order as a plain ticket in rupees (product MIS/CNC/NRML,
  order type, the separate stop-loss and target exits, max loss at the stop
  including charges, margin), the broker-neutral fields and how they map to
  Zerodha Kite Connect, Upstox and Dhan, records your explicit yes word for
  word, and sends approved orders to a built-in paper broker that fills
  realistically (slippage, stop-limit gaps, OCO exits, MIS auto square-off
  with the broker fee, full Indian charges, daily contract notes). Builds the
  square-off ticket before flat_by and logs fills and realised P&L for
  trader-risk. Covers connecting a broker MCP such as Zerodha's Kite MCP
  with order tools set to "Needs approval". Use when the user says "place
  the order", "buy it", "execute", "paper trade it", "square off", "flatten",
  "get me out", "connect Zerodha / Kite / Upstox / Dhan", or after
  /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to an
order, so it is the strictest. An order goes anywhere only after these
gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint,
   with the same stop and target).
2. **The user typed an explicit yes for this one order** in the chat.
3. Paper mode: the built-in paper broker takes it (`paper.py place`).
   Live mode: the user's broker tool is set to **Needs approval**, so
   Claude's own card asks again: Allow once places it, Deny stops it.

No yes, no order. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/paper.py" place --index 1           # paper mode: after the yes
python3 "${CLAUDE_SKILL_DIR}/paper.py" check                     # work the stop / target / square-off
python3 "${CLAUDE_SKILL_DIR}/paper.py" status
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --tradingsymbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 42 --filled-price 1171.2
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --tradingsymbol RELIANCE --side sell --qty 42 --price 1187.2 --product MIS
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

Both tools take `--now "2026-10-28 09:50"` (IST). `paper.py` takes
`--price 1171.2` (or `SYM=PRICE,...`) and `--offline FILE` (a saved Yahoo
chart) for the price; without them it asks Yahoo (equity only: Yahoo has no
NSE option or futures prices, so F&O needs `--price`).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with max loss and realised P&L |
| `trader/portfolio.json` | live mode: cash and positions, updated by `fill` / `result --filled-qty` |
| `trader/paper/ledger.json` | paper mode: orders, fills with charges, positions, cash, realised P&L |
| `trader/paper/contract_notes/YYYY-MM-DD.json` | one simulated contract note a day, charges broken down |

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user; NOT READY or BLOCKED
   goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   "Not recorded" means it was not an explicit yes for this order (for
   example "sure", "ok", "haan", "yes to all", a condition, or a yes that
   names a different stock, lot count or price). Ask again. Never turn a
   reply into a yes yourself.
4. **Paper mode** (`"paper": true`, the default): run
   `paper.py place --index N` right after the yes. LIMIT orders fill only if
   the price is at or through the limit, MARKET at the price plus slippage
   (`slippage_pct`). When the entry fills, the stop-loss and target go in as
   an OCO pair.
5. **Live mode**: place exactly the ticket's fields with the user's broker
   tool (Kite fields shown; Upstox and Dhan mappings too; they are a field
   mapping, check your broker's API docs). Then `ticket.py result`. A failed
   order is not retried without a new check and a new yes.

## Steps: while it is open, and out by flat_by

- Paper: `paper.py check` works the open orders bar by bar. A stop and a
  target in the same bar: the stop fills first (cautious). A gap fills at
  the open. An F&O SL (stop-limit) that gaps past its limit stays unfilled,
  and the tool says so loudly. MIS still open at the broker's auto
  square-off (Zerodha 15:25 equity, 15:26 F&O) is closed at the latest price
  with the broker's square-off fee (Rs 50 + GST), which is why `flat_by` is
  earlier.
- Live: place the stop-loss and target after the entry fills (MIS: two
  orders, cancel the other when one fills; CNC/NRML: a GTT OCO). Bracket and
  cover orders are discontinued at most Indian brokers.
- By `flat_by` (15:10 IST): `ticket.py flatten --tradingsymbol SYMBOL` writes
  the square-off (equity MARKET; F&O a marketable LIMIT, needs `--price`).
  Then `/trader-risk` (an exit is never blocked by the day rules), `show`,
  the user's yes, and `paper.py place` (it cancels the open exits first) or,
  live, cancel the open stop-loss / target / GTT at the broker and place it.

## Connecting a broker (live, optional)

This pack does not need a broker: paper mode is complete on its own. To go
live the user connects their broker's MCP server or connector themselves.
`claude_desktop_config.example.json` shows where it goes; the example is
Zerodha's official hosted Kite MCP (`https://mcp.kite.trade/mcp`): the user
logs in to Kite in their browser, no keys in files or chat. Then, in
Customize > Connectors (tool permissions), set every tool that places,
modifies or cancels an order, and the tools that read the account,
holdings and positions, to **Needs approval**, never Always allow. In
Claude Code keep those tools under `"ask"` in `.claude/settings.json`.
Never ask for, read, store or repeat API keys, passwords or TOTP codes; if
the user pastes one, tell them to delete it and regenerate it. Going live
(`"paper": false`) is the user's own decision in their own config.

## Example

```
ORDER 1 of 1, PAPER (built-in paper broker)
  BUY 42 RELIANCE on NSE, MIS intraday, LIMIT 1171.20, good for today
  stop-loss: SELL SL-M trigger 1163.20
  target: SELL LIMIT 1187.20
  max loss about Rs 388.08 at the stop 1163.20, charges included (a gap or a fast market fills worse)
  READY: trader-risk PASS, waiting for your yes
    Zerodha Kite Connect (place_order)
      entry  {"variety": "regular", "tradingsymbol": "RELIANCE", "exchange": "NSE", "transaction_type": "BUY",
              "order_type": "LIMIT", "quantity": 42, "product": "MIS", "validity": "DAY", "price": 1171.2}
```

The user replies "yes". `approve` records it; `paper.py place --index 1
--price 1170.5` fills 42 at 1170.50 (charges Rs 20.72) and places the
stop-loss and target. At 11:00 the target fills at 1187.20, the stop is
cancelled, and the ledger shows the realised P&L net of all charges.

## Rules

- The clock is checked again at the yes and at placement: a trader-risk PASS is valid for 30 minutes (`risk_pass_valid_minutes`), and an entry is refused after `no_entries_after`, after `flat_by` for MIS, after `expiry_day_last_entry` on expiry day, or when the market is closed (AMO rules as in trader-risk). Then run check.py again.
- A yes is only a yes: words that are not part of this order (another symbol, "short" on a buy, "as CNC" on an MIS order, "the first two", "x2") make Claude ask again.

- No order goes through without all gates: a PASS from trader-risk today for that exact order and the
  user's explicit "yes" for that one order; live orders also need the broker tool's own approval card.
- Never two orders on one yes, never "yes to all", never an order from a scheduled or background task.
- Never asks for, reads, stores or repeats broker keys, passwords or TOTP codes.
- Never switches `"paper"` to false, never enables option selling or futures. The user does that.
- Never promises a fill price: MARKET orders, SL-M stops and gaps fill at what the market gives; an SL
  stop-limit can be skipped entirely in a gap.
- Paper fills are kinder than real ones (no queue, no partial fills). Prices from Yahoo/NSE/BSE public
  endpoints are unofficial, delayed and can break.
- Not financial advice. Most retail intraday and F&O traders in India lose money.
