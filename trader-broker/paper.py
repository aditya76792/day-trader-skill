#!/usr/bin/env python3
"""The built-in paper broker for trader-broker (India edition). Standard library only.

It acts like an Indian discount broker with fake money, so the whole flow
(plan, size, risk check, your yes) can be practised without a broker account:

  place --index N   sends ONE order from trader/orders.json that trader-risk
                    passed today AND that you approved (ticket.py approve).
                    MARKET fills at the price +/- slippage_pct; LIMIT fills
                    only if the price is at or through the limit; SL / SL-M
                    wait for their trigger. When an entry fills, its stop-loss
                    and target exit orders go in as an OCO pair (one cancels
                    the other).
  check             works the open orders against the latest price (--price,
                    --offline chart file, or Yahoo), bar by bar: stops before
                    targets in the same bar (the cautious assumption), gaps
                    fill at the open, an SL (stop-limit) that gaps past its
                    limit stays unfilled. MIS positions still open at the
                    broker's auto square-off time are closed at the latest
                    price with the broker's square-off fee.
  status            cash, positions, open orders, realised and unrealised P&L
                    net of charges.
  reset --capital   starts a new paper account (the old ledger is kept as a copy).

Every fill carries the full charges (market.leg_charges: brokerage, STT,
exchange charges, SEBI fee, IPFT, stamp duty, GST) and the DP charge on a
delivery (CNC) sell. Files: trader/paper/ledger.json and one contract note a
day, trader/paper/contract_notes/YYYY-MM-DD.json.

    python3 paper.py place --index 1 --price 1171.2 --now "2026-10-28 09:50"
    python3 paper.py check --price 1163.0 --now "2026-10-28 10:40"
    python3 paper.py check --price RELIANCE=1190,NIFTY26N0322700CE=82.5
    python3 paper.py status
    python3 paper.py reset --capital 100000

Yahoo prices are unofficial and delayed; Yahoo has no NSE option or futures
prices, so F&O needs --price (or a fresh quote in trader/prices.json).
Paper fills are kinder than real ones: no queue, no partial fills, no
freak trades. Not financial advice.
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import market as M  # noqa: E402
import ticket as T  # noqa: E402

LEDGER = "trader/paper/ledger.json"
NOTES = "trader/paper/contract_notes"
FRESH_MINUTES = 15


class PaperError(M.MarketError):
    pass


def _f(x):
    return T._f(x)


def _px(x):
    return "%.2f" % float(x)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


def from_iso(text):
    return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- ledger

def new_ledger(capital, now):
    book = T.new_book(capital)
    return {"created_by": "trader-broker paper", "capital_inr": round(float(capital), 2),
            "cash_inr": book["cash_inr"], "started": iso(now), "seq": 0, "positions": {}, "orders": [],
            "fills": [], "realised": [], "last_check": {}, "last_price": {}, "events": []}


def load_ledger(path, cfg, now):
    led = M.read_json(path, "paper ledger", required=False)
    if not isinstance(led, dict):
        cap = _f(cfg.get("capital_inr"))
        if not cap:
            raise PaperError("no paper ledger and no capital_inr in the config: run paper.py reset --capital N")
        led = new_ledger(cap, now)
    for k, v in (("positions", {}), ("orders", []), ("fills", []), ("realised", []), ("last_check", {}),
                 ("last_price", {}), ("events", []), ("seq", 0)):
        led.setdefault(k, v)
    return led


def next_id(led):
    led["seq"] = int(led.get("seq", 0)) + 1
    return "PAPER-%05d" % led["seq"]


def tick_for(price, inst):
    itype = str((inst or {}).get("type") or "EQ").upper()
    return M.tick_size(price, "options" if itype in ("CE", "PE") else ("futures" if itype == "FUT" else "equity"))


def event(led, now, text, out):
    led["events"].append({"at": iso(now), "text": text})
    led["events"] = led["events"][-200:]
    out.append(text)


# ---------------------------------------------------------------- prices

def price_map(text):
    """'1171.2' -> {None: 1171.2}; 'RELIANCE=1190,NIFTY26N0322700CE=82.5' -> {SYM: price}."""
    if text is None:
        return {}
    out = {}
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = float(v)
        else:
            out[None] = float(part)
    for v in out.values():
        if v <= 0:
            raise PaperError("--price wants positive prices")
    return out


def _interval_sec(meta):
    g = str((meta or {}).get("dataGranularity") or "1m")
    try:
        if g.endswith("m"):
            return int(g[:-1]) * 60
        if g.endswith("h"):
            return int(g[:-1]) * 3600
    except ValueError:
        pass
    return 60


def chart_bars(doc, sym, now):
    series = M.parse_chart(doc, sym)
    sec = _interval_sec(series.get("meta"))
    bars = M.bars_from_series(series, sec, now)
    return [{"dt": b["dt"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars], sec


def market_bars(tsym, inst, exchange, a, since, now, pm, net_cache):
    """Bars after `since` for one tradingsymbol -> (bars, source). A --price gives one bar."""
    if tsym in pm or (None in pm and len(pm) == 1):
        p = pm.get(tsym, pm.get(None))
        return [{"dt": now, "o": p, "h": p, "l": p, "c": p, "point": True}], "--price"
    itype = str((inst or {}).get("type") or "EQ").upper()
    if getattr(a, "offline", None):
        doc = M.read_json(a.offline, "offline chart file")
        bars, _ = chart_bars(doc, tsym, now)
        src = "offline file %s" % a.offline
    elif itype == "EQ":
        if tsym not in net_cache:
            net_cache[tsym] = M.Net().chart(tsym, rng="1d", interval="1m", exchange=exchange)
        bars, _ = chart_bars(net_cache[tsym], tsym, now)
        src = "Yahoo (delayed, unofficial)"
    else:
        pdoc = M.read_json(getattr(a, "prices", "trader/prices.json"), "prices", required=False) or {}
        q = ((pdoc.get("quotes") or {}).get(tsym)) if isinstance(pdoc, dict) else None
        p = _f((q or {}).get("price"))
        t = parse_quote_time((q or {}).get("time") or (pdoc or {}).get("at"))
        if p and t and -60 <= (now - t).total_seconds() <= FRESH_MINUTES * 60:
            return [{"dt": t, "o": p, "h": p, "l": p, "c": p}], "trader/prices.json"
        raise PaperError("no price for %s: Yahoo has no NSE option or futures prices. Pass --price %s=PRICE "
                         "(from your broker's or NSE's option chain)" % (tsym, tsym))
    if not bars:
        raise PaperError("no bars for %s from %s" % (tsym, src))
    newer = [b for b in bars if since is None or b["dt"] >= since]
    if not newer:
        last = bars[-1]
        newer = [{"dt": now, "o": last["c"], "h": last["c"], "l": last["c"], "c": last["c"]}]
    return newer, src


def parse_quote_time(text):
    if not text:
        return None
    t = str(text).strip()
    try:
        if t.endswith("Z") or (len(t) > 6 and t[-6] in "+-" and t[-3] == ":"):
            dt = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
            return dt.astimezone(M.IST).replace(tzinfo=None)
        return M.parse_now(t)
    except (ValueError, M.MarketError):
        return None


# ---------------------------------------------------------------- fills

def try_fill(o, bar, slip_pct):
    """-> fill price or None for an open order against one bar. Sets o["triggered"] for SL."""
    side = o["side"]
    buy = side == "BUY"
    ot = o["order_type"]
    tick = tick_for(bar["c"], o.get("instrument"))
    slip = float(slip_pct or 0) / 100.0
    lo, hi, op = bar["l"], bar["h"], bar["o"]

    def slipped(px):
        return M.round_tick(px * (1 + slip), tick, "up") if buy else max(tick, M.round_tick(px * (1 - slip), tick, "down"))

    if ot == "MARKET":
        return slipped(bar["c"])
    if ot == "LIMIT" or (ot == "SL" and o.get("triggered")):
        lim = float(o["price"])
        # a resting limit seen through one price point fills AT the limit (the cautious reading);
        # a real bar that opens through the limit fills at the open; a fresh order fills at the market
        better = not bar.get("point")
        if buy and lo <= lim + 1e-9:
            return min(lim, op) if (better and op <= lim) else lim
        if not buy and hi >= lim - 1e-9:
            return max(lim, op) if (better and op >= lim) else lim
        return None
    trig = float(o["trigger_price"])
    hit = (hi >= trig - 1e-9) if buy else (lo <= trig + 1e-9)
    if not hit:
        return None
    at = max(trig, op) if buy else min(trig, op)       # a gap through the trigger fills at the open
    if ot == "SL-M":
        return slipped(at)
    lim = float(o["price"])                             # SL: stop-limit
    o["triggered"] = True
    if (buy and at <= lim + 1e-9) or (not buy and at >= lim - 1e-9):
        px = slipped(at)
        return min(px, lim) if buy else max(px, lim)
    return None                                         # gapped past the limit: waits as a LIMIT


def record_fill(led, cfg, o, px, now, state_path, out, extra_fee=0.0, note=None):
    res = T.book_fill(led, cfg, o["tradingsymbol"], o["side"].lower(), o["qty"], px, o["product"],
                      o.get("exchange", "NSE"), o.get("instrument"), extra_fee)
    o.update(status="filled", fill_price=px, filled_at=iso(now))
    if res["key"] in led["positions"]:
        led["positions"][res["key"]].setdefault("opened", now.date().isoformat())
    seg = M.segment_for(o["product"], (o.get("instrument") or {}).get("type") or "EQ")
    f = {"id": "F-%s" % o["id"], "order_id": o["id"], "date": now.date().isoformat(), "time": M.hm(now.time()),
         "tradingsymbol": o["tradingsymbol"], "exchange": o.get("exchange", "NSE"), "side": o["side"],
         "qty": o["qty"], "price": px, "product": o["product"], "segment": seg, "role": o.get("role"),
         "charges": res["charges"], "extra_charges": round(float(extra_fee or 0), 2),
         "total_charges": res["total_charges"]}
    if note:
        f["note"] = note
    led["fills"].append(f)
    led["last_price"][o["tradingsymbol"]] = px
    if res["realised"]:
        r = dict(res["realised"])
        r.update(date=now.date().isoformat(), time=M.hm(now.time()), tradingsymbol=o["tradingsymbol"],
                 product=o["product"])
        led["realised"].append(r)
    words = "%s %d %s %s filled at %s (%s), charges %s" % (o["side"], o["qty"], o["tradingsymbol"], o["product"],
                                                           _px(px), o.get("role", "order"), M.inr(res["total_charges"]))
    if res["realised"]:
        words += "; realised %s net of charges" % M.inr(res["realised"]["net"])
    event(led, now, words, out)
    T.log_fill(state_path, now, now.date().isoformat(), o["tradingsymbol"], o["side"].lower(), o["qty"], px,
               o["product"], res, paper=True, extra={"broker_order_id": o["id"], "role": o.get("role")})
    return res


def open_exits_for(led, entry_order):
    sl, tg, _ = T.exits_of(entry_order)
    out = []
    for role, leg in (("stop_loss", sl), ("target", tg)):
        if not leg:
            continue
        out.append({"role": role, "side": str(leg.get("side")).upper(), "order_type": str(leg.get("order_type")).upper(),
                    "price": _f(leg.get("price")), "trigger_price": _f(leg.get("trigger_price"))})
    return out


def add_exits(led, o, entry_src, now, out):
    for leg in open_exits_for(led, entry_src):
        e = {"id": next_id(led), "tradingsymbol": o["tradingsymbol"], "exchange": o.get("exchange", "NSE"),
             "instrument": o.get("instrument"), "side": leg["side"], "qty": o["qty"], "product": o["product"],
             "order_type": leg["order_type"], "price": leg["price"], "trigger_price": leg["trigger_price"],
             "status": "open", "role": leg["role"], "oco": o["id"], "placed_at": iso(now)}
        led["orders"].append(e)
        what = ("trigger %s" % _px(e["trigger_price"])) + (" limit %s" % _px(e["price"]) if e["price"] else "") \
            if leg["role"] == "stop_loss" else _px(e["price"])
        event(led, now, "%s %s %s %s placed (OCO with %s)" % (leg["role"].replace("_", "-"), e["side"], e["order_type"],
                                                              what, "the target" if leg["role"] == "stop_loss" else "the stop"),
              out)


def cancel_oco(led, filled, now, out):
    if not filled.get("oco"):
        return
    for o in led["orders"]:
        if o["status"] == "open" and o.get("oco") == filled["oco"] and o["id"] != filled["id"]:
            o.update(status="cancelled", cancelled_at=iso(now), cancel_reason="OCO: %s filled" % filled.get("role"))
            event(led, now, "%s %s cancelled (one cancels the other)" % (o.get("role", "order"), o["id"]), out)


def cancel_exits_of(led, tsym, product, now, out, why):
    for o in led["orders"]:
        if o["status"] == "open" and o["tradingsymbol"] == tsym and o["product"] == product and o.get("role") in (
                "stop_loss", "target"):
            o.update(status="cancelled", cancelled_at=iso(now), cancel_reason=why)
            event(led, now, "%s %s cancelled (%s)" % (o["role"], o["id"], why), out)


def contract_note(led, cfg, date, notes_dir):
    fills = [f for f in led["fills"] if f["date"] == date]
    keys = ("brokerage", "stt", "exchange_txn", "sebi", "ipft", "stamp", "gst", "dp")
    totals = {k: 0.0 for k in keys}
    buy = sell = other = 0.0
    trades = []
    for f in fills:
        ch = f["charges"]
        row = {"order_id": f["order_id"], "time": f["time"], "tradingsymbol": f["tradingsymbol"],
               "exchange": f["exchange"], "side": f["side"], "qty": f["qty"], "price": f["price"],
               "product": f["product"], "segment": f["segment"], "turnover": ch.get("turnover")}
        for k in keys:
            row[k] = ch.get(k, 0.0)
            totals[k] = round(totals[k] + ch.get(k, 0.0), 2)
        row["other"] = f.get("extra_charges", 0.0)
        other += row["other"]
        if f.get("note"):
            row["note"] = f["note"]
        trades.append(row)
        if f["side"] == "BUY":
            buy += ch.get("turnover", 0.0)
        else:
            sell += ch.get("turnover", 0.0)
    total = round(sum(totals.values()) + other, 2)
    doc = {"created_by": "trader-broker paper", "date": date, "broker": cfg.get("broker", "zerodha"),
           "note": "SIMULATED paper contract note: not a legal document. Charges per market.py (statutory rates "
                   "as of %s)." % M.VERIFIED_ON,
           "trades": trades, "charges": dict(totals, other=round(other, 2), total=total),
           "buy_turnover": round(buy, 2), "sell_turnover": round(sell, 2),
           "net_amount": round(sell - buy - total, 2),
           "realised_net": round(sum(r["net"] for r in led["realised"] if r["date"] == date), 2)}
    M.write_json(os.path.join(notes_dir, "%s.json" % date), doc)
    return doc


# ---------------------------------------------------------------- the work

def square_off_time(cfg, inst):
    _, prof = M.broker_profile(cfg)
    itype = str((inst or {}).get("type") or "EQ").upper()
    t = (prof.get("auto_square_off") or {}).get("fno" if itype != "EQ" else "equity")
    return M.parse_hhmm(t) if t else M.parse_hhmm(cfg.get("flat_by") or "15:10")


def work(led, cfg, a, now, pm, out, state_path):
    """Work every open order bar by bar, then the MIS square-off. Returns the price used per symbol."""
    slip = cfg.get("slippage_pct", 0.05)
    net_cache, last = {}, {}
    open_orders = [o for o in led["orders"] if o["status"] == "open"]
    syms = []
    for o in open_orders:
        if o["tradingsymbol"] not in syms:
            syms.append(o["tradingsymbol"])
    for tsym in syms:
        mine = [o for o in open_orders if o["tradingsymbol"] == tsym]
        since = None
        if led["last_check"].get(tsym):
            since = from_iso(led["last_check"][tsym])
        else:
            since = min(from_iso(o["placed_at"]) for o in mine)
        bars, src = market_bars(tsym, mine[0].get("instrument"), mine[0].get("exchange", "NSE"), a, since, now, pm,
                                net_cache)
        for bar in bars:
            if not M.market_status(bar["dt"])["open"]:
                continue                                # the exchange is closed: nothing fills
            active = [o for o in led["orders"] if o["status"] == "open" and o["tradingsymbol"] == tsym
                      and from_iso(o["placed_at"]) <= bar["dt"]]
            active.sort(key=lambda o: {"entry": 0, "exit": 0, "stop_loss": 1, "target": 2}.get(o.get("role"), 0))
            for o in active:
                if o["status"] != "open":
                    continue
                px = try_fill(o, bar, slip)
                if px is None:
                    if o.get("triggered") and o["order_type"] == "SL" and not o.get("told"):
                        o["told"] = True
                        event(led, now, "stop %s TRIGGERED but NOT FILLED: the price gapped past its limit %s; "
                                        "it waits as a limit order and the loss is growing"
                              % (o["id"], _px(o["price"])), out)
                    continue
                record_fill(led, cfg, o, px, bar["dt"], state_path, out)
                if o.get("role") == "entry" and o.get("exits_src"):
                    add_exits(led, o, o.pop("exits_src"), bar["dt"], out)
                cancel_oco(led, o, bar["dt"], out)
            last[tsym] = bar["c"]
        led["last_check"][tsym] = iso(now)
        led["last_price"][tsym] = last.get(tsym, led["last_price"].get(tsym))
    # MIS auto square-off
    for key, pos in list(led["positions"].items()):
        if pos.get("product") != "MIS":
            continue
        tsym = key.split("|")[0]
        cut = square_off_time(cfg, pos.get("instrument"))
        opened = str(pos.get("opened") or now.date().isoformat())
        stale = opened < now.date().isoformat()
        if not stale and now.time() < cut:
            flat = M.parse_hhmm(cfg.get("flat_by") or "15:10")
            if now.time() >= flat:
                out.append("%s MIS is still open after your flat_by %s: flatten it (ticket.py flatten --tradingsymbol %s) "
                           "or the paper broker squares it off at %s with the broker's fee"
                           % (tsym, M.hm(flat), tsym, M.hm(cut)))
            continue
        px = pm.get(tsym, pm.get(None) if len(pm) == 1 else None) or last.get(tsym) or led["last_price"].get(tsym)
        if px is None:
            try:
                bars, _ = market_bars(tsym, pos.get("instrument"), pos.get("exchange", "NSE"), a, None, now, pm,
                                      net_cache)
                px = bars[-1]["c"]
            except M.MarketError as e:
                out.append("cannot square off %s yet: %s" % (tsym, e))
                continue
        cancel_exits_of(led, tsym, "MIS", now, out, "auto square-off")
        _, prof = M.broker_profile(cfg)
        fee = round(float(prof.get("square_off_fee", 0.0)) * (1 + M.GST), 2)
        o = {"id": next_id(led), "tradingsymbol": tsym, "exchange": pos.get("exchange", "NSE"),
             "instrument": pos.get("instrument"), "side": "SELL" if pos["qty"] > 0 else "BUY",
             "qty": int(abs(pos["qty"])), "product": "MIS", "order_type": "MARKET", "price": None,
             "trigger_price": None, "status": "open", "role": "auto_square_off", "placed_at": iso(now)}
        led["orders"].append(o)
        tick = tick_for(px, pos.get("instrument"))
        slip = float(cfg.get("slippage_pct", 0.05) or 0) / 100.0
        fpx = M.round_tick(px * (1 - slip), tick, "down") if o["side"] == "SELL" else M.round_tick(px * (1 + slip), tick, "up")
        when = "%s (opened %s; an MIS position cannot be carried)" % (M.hm(cut), opened) if stale else M.hm(cut)
        record_fill(led, cfg, o, fpx, now, state_path, out, fee,
                    note="broker auto square-off at %s: square-off fee %s (Rs %s + GST)"
                         % (when, M.inr(fee), T._num(prof.get("square_off_fee", 0))))
        out.append("AUTO SQUARE-OFF: %s MIS closed by the broker at %s IST, fee %s charged. Square off yourself "
                   "before %s to avoid it." % (tsym, M.hm(now.time()), M.inr(fee), M.hm(cut)))
    return last


def approved_order(a, orders, cfg, risk, state, today):
    order = T._pick(orders, a.index)
    fp = T.fingerprint(order)
    rv, why = T.risk_verdict(risk, fp, today)
    if rv is None:
        raise PaperError("order %d is not ready: %s" % (a.index, why))
    if rv.get("verdict") != "PASS":
        raise PaperError("order %d was BLOCKED by trader-risk: %s" % (a.index, "; ".join(rv.get("reasons", []))))
    ch = T.exits_changed(order, rv)
    if ch:
        raise PaperError("order %d: %s" % (a.index, ch))
    rec = T.latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise PaperError("order %d has no yes from you today (%s): show the ticket, get an explicit yes, run "
                         "ticket.py approve --index %d --reply \"...\" first"
                         % (a.index, "status %s" % rec.get("status") if rec else "nothing recorded", a.index))
    return order, rv, rec


def cmd_place(a):  # noqa: C901
    cfg = M.load_config(a.config)
    now = M.parse_now(a.now)
    today = now.date().isoformat()
    if not cfg.get("paper", True):
        raise PaperError("\"paper\" is false in your config: this is the paper broker; live orders go through "
                         "your broker's own tool")
    doc = M.read_json(a.orders, "orders file")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    risk = M.read_json(a.risk, "risk check", required=False)
    state = T.load_state(a.state)
    order, rv, rec = approved_order(a, orders, cfg, risk, state, today)
    late = T.time_gate(order, rv, cfg, now)
    if late:
        raise PaperError("order %d cannot be placed now: %s" % (a.index, late))
    amo = str(order.get("variety") or "").lower() == "amo"
    st = M.market_status(now)
    if not st["open"] and not amo:
        raise PaperError("the market is not open (%s): the paper exchange takes orders 09:15-15:30 IST, or mark it AMO"
                         % st["label"])
    led = load_ledger(a.ledger, cfg, now)
    out = []
    inst = dict(order.get("instrument") or {"type": "EQ"})
    tsym = T.tsym_of(order)
    product = str(order.get("product")).upper()
    kind = rv.get("kind", "entry")
    pm = price_map(a.price)
    bars, src = (None, None) if amo else market_bars(tsym, inst, order.get("exchange", "NSE"), a, None, now, pm, {})
    if kind == "exit":
        cancel_exits_of(led, tsym, product, now, out, "replaced by your square-off order")
    o = {"id": next_id(led), "fingerprint": T.fingerprint(order), "tradingsymbol": tsym,
         "exchange": order.get("exchange", "NSE"), "instrument": inst, "side": str(order["side"]).upper(),
         "qty": int(order["qty"]), "product": product, "order_type": str(order["order_type"]).upper(),
         "price": _f(order.get("price")), "trigger_price": _f(order.get("trigger_price")),
         "variety": "amo" if amo else "regular", "status": "open", "role": "entry" if kind == "entry" else "exit",
         "placed_at": iso(now), "date": today}
    if kind == "entry" and order.get("exits"):
        o["exits_src"] = {"exits": order["exits"]}
    led["orders"].append(o)
    event(led, now, "placed %s: %s" % (o["id"], T.entry_words(order)), out)
    placed = dict((k, rec.get(k)) for k in ("tradingsymbol", "symbol", "exchange", "side", "qty", "product",
                                            "instrument", "kind", "max_loss", "stop", "plain", "index"))
    placed.update(at=T.iso(now), date=today, fingerprint=o["fingerprint"], status="placed", broker_order_id=o["id"],
                  paper=True)
    state.setdefault("orders", []).append(placed)
    T.save_state(a.state, state)
    if not amo:
        bar = {"dt": now, "o": bars[-1]["c"], "h": bars[-1]["c"], "l": bars[-1]["c"], "c": bars[-1]["c"]}
        px = try_fill(o, bar, cfg.get("slippage_pct", 0.05))
        if px is not None:
            record_fill(led, cfg, o, px, now, a.state, out)
            if o.get("exits_src"):
                add_exits(led, o, o.pop("exits_src"), now, out)
        else:
            event(led, now, "%s is open: %s %s not reached at %s (%s)" % (o["id"], o["order_type"],
                                                                        _px(o["price"] or o["trigger_price"]),
                                                                        _px(bar["c"]), src), out)
        led["last_check"][tsym] = iso(now)
        led["last_price"][tsym] = bar["c"]
    else:
        event(led, now, "%s is an AMO: it goes to the exchange at the next open" % o["id"], out)
    led["cash_inr"] = round(led["cash_inr"], 2)
    M.write_json(a.ledger, led)
    contract_note(led, cfg, today, a.notes)
    print("PAPER broker (fake money):")
    for line in out:
        print("  " + line)
    print("Cash %s. Next: paper.py check (with --price, or the latest Yahoo price) works the stop and target." %
          M.inr(led["cash_inr"]))
    return 0


def cmd_check(a):
    cfg = M.load_config(a.config)
    now = M.parse_now(a.now)
    led = load_ledger(a.ledger, cfg, now)
    out = []
    pm = price_map(a.price)
    work(led, cfg, a, now, pm, out, a.state)
    M.write_json(a.ledger, led)
    dates = sorted({f["date"] for f in led["fills"]})
    for d in dates[-3:]:
        contract_note(led, cfg, d, a.notes)
    print("PAPER broker check at %s IST:" % now.strftime("%Y-%m-%d %H:%M"))
    if not out:
        out.append("nothing filled; %d open order(s)" % sum(1 for o in led["orders"] if o["status"] == "open"))
    for line in out:
        print("  " + line)
    return 0


def summary(led, cfg, now, pm):
    rows = []
    unreal = 0.0
    for key, pos in sorted(led["positions"].items()):
        tsym, product = key.split("|")
        px = pm.get(tsym, pm.get(None) if len(pm) == 1 else None) or led["last_price"].get(tsym) or pos["avg_price"]
        q = float(pos["qty"])
        gross = (px - pos["avg_price"]) * q
        seg = M.segment_for(product, (pos.get("instrument") or {}).get("type") or "EQ")
        ex = T.charges_for(seg, "sell" if q > 0 else "buy", px, abs(q), pos.get("exchange", "NSE"), cfg)["total"]
        net = round(gross - pos.get("charges_open", 0.0) - ex, 2)
        unreal += net
        rows.append({"key": key, "tradingsymbol": tsym, "product": product, "qty": q, "avg_price": pos["avg_price"],
                     "price": px, "unrealised_net": net, "charges_paid": pos.get("charges_open", 0.0)})
    today = now.date().isoformat()
    return {"cash_inr": round(led["cash_inr"], 2), "capital_inr": led.get("capital_inr"), "positions": rows,
            "open_orders": [o for o in led["orders"] if o["status"] == "open"],
            "realised_today": round(sum(r["net"] for r in led["realised"] if r["date"] == today), 2),
            "realised_total": round(sum(r["net"] for r in led["realised"]), 2),
            "charges_total": round(sum(f["total_charges"] for f in led["fills"]), 2),
            "unrealised_net": round(unreal, 2)}


def cmd_status(a):
    cfg = M.load_config(a.config)
    now = M.parse_now(a.now)
    led = load_ledger(a.ledger, cfg, now)
    s = summary(led, cfg, now, price_map(a.price))
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    print("PAPER account (fake money), %s IST" % now.strftime("%Y-%m-%d %H:%M"))
    print("  cash %s (started with %s)" % (M.inr(s["cash_inr"]), M.inr(s["capital_inr"])))
    if not s["positions"]:
        print("  no open positions")
    for r in s["positions"]:
        print("  %-20s %-4s %+d @ %s, now %s, unrealised %s net of charges"
              % (r["tradingsymbol"], r["product"], int(r["qty"]), _px(r["avg_price"]), _px(r["price"]),
                 M.inr(r["unrealised_net"])))
    for o in s["open_orders"]:
        what = ("trigger %s" % _px(o["trigger_price"])) if o.get("trigger_price") else ""
        if o.get("price"):
            what += (" limit %s" % _px(o["price"])) if what else "LIMIT %s" % _px(o["price"])
        print("  open %s %s %s %d %s %s %s" % (o["id"], o.get("role", ""), o["side"], o["qty"], o["tradingsymbol"],
                                               o["order_type"], what))
    print("  realised today %s, all time %s (net of all charges; charges paid %s)"
          % (M.inr(s["realised_today"]), M.inr(s["realised_total"]), M.inr(s["charges_total"])))
    print("  " + M.NOT_ADVICE)
    return 0


def cmd_reset(a):
    cfg = M.load_config(a.config)
    now = M.parse_now(a.now)
    cap = a.capital if a.capital is not None else _f(cfg.get("capital_inr"))
    if not cap or cap <= 0:
        raise PaperError("--capital wants a positive amount in rupees")
    if os.path.exists(a.ledger):
        keep = a.ledger.replace(".json", "-%s.json" % now.strftime("%Y%m%d-%H%M%S"))
        os.replace(a.ledger, keep)
        print("Kept the old ledger as %s." % keep)
    M.write_json(a.ledger, new_ledger(cap, now))
    print("New paper account with %s of fake money." % M.inr(cap))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The built-in paper broker (trader-broker, India). Fake money only.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", default=M.CONFIG_FILE)
        p.add_argument("--ledger", default=LEDGER)
        p.add_argument("--notes", default=NOTES, help="contract notes folder")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-28 09:50\"")

    p = sub.add_parser("place", help="send one approved order to the paper broker")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--risk", default="trader/state/risk.json")
    p.add_argument("--price", help="the price now (or SYM=PRICE,...)")
    p.add_argument("--offline", help="a saved Yahoo chart JSON instead of the network")
    p.add_argument("--prices", default="trader/prices.json")
    p = sub.add_parser("check", help="work the open orders against the latest price")
    common(p)
    p.add_argument("--price", help="the price now (or SYM=PRICE,...)")
    p.add_argument("--offline", help="a saved Yahoo chart JSON instead of the network")
    p.add_argument("--prices", default="trader/prices.json")
    p = sub.add_parser("status", help="cash, positions, open orders, P&L")
    common(p)
    p.add_argument("--price", help="prices for unrealised P&L (SYM=PRICE,...)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("reset", help="start a new paper account")
    common(p)
    p.add_argument("--capital", type=float)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"place": cmd_place, "check": cmd_check, "status": cmd_status, "reset": cmd_reset}[a.cmd](a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
