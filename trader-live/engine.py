#!/usr/bin/env python3
"""The live engine for trader-live: real-time monitoring, signals, orders, stops and targets.
Standard library only. Run it with `live.py run`; this file is the machinery.

One loop, every `poll_seconds` (2) during NSE hours:

  1. quotes      last prices for the watchlist and every open trade (Upstox LTP v3,
                 or the replay feed in validation; Yahoo only as a delayed paper feed)
  2. broker      order states from the broker (Upstox order book), or the paper
                 matcher fills open orders against the live prices
  3. trades      each trade walks a state machine:
                   pending_confirm -> entry_open -> open -> closed
                                   \\-> expired / rejected / cancelled
                 an entry that fills gets a stop-loss and a target straight away;
                 when one fills the engine cancels the other (OCO); if both ever fill
                 in a race, it flattens the extra quantity and halts new entries
  4. guards      kill switch file, daily loss (realised + open), stale data,
                 repeated broker errors, position mismatch: each halts new entries,
                 the hard ones flatten
  5. square-off  at flat_by (15:10) every MIS trade is closed at market and
                 unfilled entries are cancelled; nothing new after no_entries_after
  6. signals     on each completed 5-minute bar: trader-entry's evaluate() on the
                 day's bars; a fresh trigger becomes a plan (trader-exit's compute),
                 a size (trader-budget's size_trade, loss at the stop incl. charges),
                 a risk check (trader-risk's check) and, on PASS, a pending trade
  7. journal     the whole state is written atomically after every change, so a
                 crash or restart resumes; on start it reconciles with the broker

Entries wait for the user's explicit yes in live mode (`live.py confirm ID`, typed at a
terminal). Exits (stop, target, square-off) never wait. Paper mode can auto-confirm so
the whole workflow can be validated unattended.
"""

import datetime
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402


def _sibling(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("live_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ENTRY = _sibling("trader-entry", "entry")
EXIT = _sibling("trader-exit", "exit")
SIZE = _sibling("trader-budget", "size")
RISK = _sibling("trader-risk", "check")
TICKET = _sibling("trader-broker", "ticket")

LIVE_DIR = os.path.join("trader", "live")
LIVE_DEFAULTS = {
    "mode": "paper",                 # paper | live (live also needs live.py approve-live)
    "feed": "upstox",                # upstox | yahoo (paper only, delayed) | replay (validation)
    "watchlist": [],
    "strategy": "orb",               # orb | vwap_reclaim | gap
    "or_minutes": 15,
    "sides": ["buy"],                # add "sell" only with allow_short
    "stop": "or_mid",                # or_mid | or_low | vwap | pct:0.8 | atr:1.5
    "target_r": 2.0,
    "poll_seconds": 2,
    "max_open_trades": 1,
    "one_trade_per_symbol": True,
    "entry_buffer_ticks": 2,         # marketable LIMIT: a couple of ticks past the last price
    "max_chase_r": 0.25,             # skip if price ran more than 0.25R past the planned entry
    "entry_timeout_seconds": 120,
    "confirm_timeout_seconds": 120,
    "paper_auto_confirm": True,
    "stale_quote_seconds": 30,
    "max_consecutive_errors": 5,
    "fresh_signal_minutes": 6,
}


def live_cfg(cfg):
    out = dict(LIVE_DEFAULTS)
    out.update((cfg or {}).get("live") or {})
    return out


class EngineError(M.MarketError):
    pass


# ================================================================ logging

def make_logger(mode, day, base=LIVE_DIR, echo=True):
    d = os.path.join(base, "logs")
    os.makedirs(d, exist_ok=True)
    log = logging.getLogger("trader-live-%s-%s-%s" % (mode, day, id(base)))
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(os.path.join(d, "engine-%s-%s.log" % (day, mode)), encoding="utf-8")
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        if echo:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(sh)
    return log


def notify(title, text):
    """A desktop notification on macOS (best effort), plus the terminal bell."""
    sys.stdout.write("\a")
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e", 'display notification %s with title %s' % (
                json.dumps(text[:200]), json.dumps(title[:60]))], timeout=5, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass


# ================================================================ feeds

class Feed(object):
    name = "feed"
    delayed = False

    def ltp(self, symbols, now):
        raise NotImplementedError

    def candles(self, symbol, now):
        raise NotImplementedError


class UpstoxFeed(Feed):
    name = "upstox"

    def __init__(self, client, instruments, exchange="NSE"):
        self.client, self.inst, self.exchange = client, instruments, exchange
        self.keys = {}

    def key(self, sym):
        if sym not in self.keys:
            self.keys[sym] = self.inst.key_for(sym, self.exchange)
        return self.keys[sym]

    def ltp(self, symbols, now):
        keys = {self.key(s): s for s in symbols}
        got = self.client.ltp(list(keys))
        return {keys[k]: dict(v, time=now) for k, v in got.items() if k in keys and v.get("price")}

    def candles(self, symbol, now):
        return [b for b in self.client.intraday_candles(self.key(symbol), 1) if b["dt"].date() == now.date()]


class YahooFeed(Feed):
    """Paper only: Yahoo's NSE data is delayed, so live trading refuses this feed."""
    name = "yahoo"
    delayed = True

    def __init__(self, net=None, exchange="NSE"):
        self.net, self.exchange = net or M.Net(), exchange

    def _bars(self, symbol, now):
        s = M.parse_chart(self.net.chart(symbol, "1d", "1m", self.exchange), symbol)
        return s, [b for b in M.bars_from_series(s, 60, now=now) if b["dt"].date() == now.date()]

    def ltp(self, symbols, now):
        out = {}
        for sym in symbols:
            s, bars = self._bars(sym, now)
            if bars:
                out[sym] = {"price": bars[-1]["c"], "prev_close": s["meta"].get("previousClose"), "time": now}
        return out

    def candles(self, symbol, now):
        return self._bars(symbol, now)[1]


class ReplayFeed(Feed):
    """A recorded or synthetic day of 1-minute bars played against a simulated clock."""
    name = "replay"

    def __init__(self, bars_by_symbol, prev_close=None):
        self.bars = {s: sorted(b, key=lambda x: x["dt"]) for s, b in bars_by_symbol.items()}
        self.prev = prev_close or {}

    def ltp(self, symbols, now):
        out = {}
        for s in symbols:
            done = [b for b in self.bars.get(s, []) if b["dt"] + datetime.timedelta(minutes=1) <= now]
            cur = [b for b in self.bars.get(s, []) if b["dt"] <= now < b["dt"] + datetime.timedelta(minutes=1)]
            if cur:
                px = cur[0]["o"]
            elif done:
                px = done[-1]["c"]
            else:
                continue
            out[s] = {"price": px, "prev_close": self.prev.get(s), "time": now}
        return out

    def candles(self, symbol, now):
        return [b for b in self.bars.get(symbol, []) if b["dt"] + datetime.timedelta(minutes=1) <= now]


def five_minute_chart(symbol, one_min, prev_close, exchange="NSE"):
    """1-minute bars -> the chart dict trader-entry's evaluate() reads (5-minute bars on the 09:15 grid)."""
    buckets = {}
    for b in one_min:
        slot = M.bar_slot(b["dt"], 5)
        if slot < 0:
            continue
        start = datetime.datetime.combine(b["dt"].date(), M.OPEN) + datetime.timedelta(minutes=5 * slot)
        k = buckets.setdefault(start, {"dt": start, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": 0.0, "n": 0})
        k["h"] = max(k["h"], b["h"])
        k["l"] = min(k["l"], b["l"])
        k["c"] = b["c"]
        k["v"] += b["v"] or 0
        k["n"] += 1
    bars = []
    for start in sorted(buckets):
        k = buckets[start]
        if k["n"] < 5 and start + datetime.timedelta(minutes=5) > one_min[-1]["dt"] + datetime.timedelta(minutes=1):
            continue                                           # still forming
        bars.append({"t": M.ts_from_ist(start), "dt": start, "o": k["o"], "h": k["h"], "l": k["l"], "c": k["c"],
                     "v": k["v"]})
    return {"symbol": symbol, "exchange": exchange, "meta": {"previousClose": prev_close}, "bars": bars,
            "data_time": one_min[-1]["dt"] + datetime.timedelta(minutes=1) if one_min else None}


# ================================================================ brokers

class PaperBroker(object):
    """Fills orders against live prices: realistic enough to validate the workflow."""
    name = "paper"

    def __init__(self, cfg):
        self.cfg = cfg
        self.orders = {}
        self.seq = 0
        self.slip = float(cfg.get("slippage_pct", 0.05)) / 100.0

    def restore(self, saved):
        self.orders = dict(saved.get("orders") or {})
        self.seq = int(saved.get("seq") or 0)

    def dump(self):
        return {"orders": self.orders, "seq": self.seq}

    def place(self, order, tag, now):
        self.seq += 1
        oid = "P%s-%04d" % (now.strftime("%m%d"), self.seq)
        self.orders[oid] = {"id": oid, "tag": tag, "symbol": order["symbol"], "side": order["side"],
                            "qty": int(order["qty"]), "order_type": order["order_type"],
                            "price": order.get("price"), "trigger_price": order.get("trigger_price"),
                            "status": "open", "filled_qty": 0, "avg_price": None, "triggered": False,
                            "placed_at": now.isoformat(timespec="seconds")}
        return oid

    def cancel(self, oid, now):
        o = self.orders.get(oid)
        if o and o["status"] in ("open", "partial"):
            o["status"] = "cancelled"

    def status(self, oid):
        return dict(self.orders.get(oid) or {"id": oid, "status": "unknown"})

    def refresh(self, quotes, now):
        for o in self.orders.values():
            if o["status"] != "open":
                continue
            q = quotes.get(o["symbol"])
            if not q or not q.get("price"):
                continue
            p = float(q["price"])
            buy = o["side"] == "BUY"
            ot = o["order_type"]
            fill = None
            if ot == "MARKET":
                fill = p * (1 + self.slip) if buy else p * (1 - self.slip)
            elif ot == "LIMIT":
                if (buy and p <= o["price"]) or (not buy and p >= o["price"]):
                    fill = min(p, o["price"]) if buy else max(p, o["price"])
            elif ot in ("SL-M", "SL"):
                trig = float(o["trigger_price"])
                if not o["triggered"] and ((buy and p >= trig) or (not buy and p <= trig)):
                    o["triggered"] = True
                if o["triggered"]:
                    if ot == "SL-M":
                        fill = p * (1 + self.slip) if buy else p * (1 - self.slip)
                    elif (buy and p <= o["price"]) or (not buy and p >= o["price"]):
                        fill = p
            if fill is not None:
                o.update(status="filled", filled_qty=o["qty"], avg_price=round(fill, 2),
                         filled_at=now.isoformat(timespec="seconds"))

    def positions(self):
        return None


class UpstoxBroker(object):
    """Real orders through Upstox. Never retries a write; reconciles by tag."""
    name = "upstox"

    def __init__(self, client, instruments, upstox_mod):
        self.client, self.inst, self.U = client, instruments, upstox_mod
        self.book = {}

    def restore(self, saved):
        pass

    def dump(self):
        return {}

    def place(self, order, tag, now):
        payload = self.U.order_payload(order, self.inst.key_for(order), tag)
        ids = self.client.place(payload)
        return ids[0]

    def cancel(self, oid, now):
        try:
            self.client.cancel(oid)
        except self.U.UpstoxError as e:
            st = self.status(oid)
            if st["status"] in ("filled", "cancelled", "rejected"):
                return
            raise e

    def refresh(self, quotes, now):
        self.book = {}
        for row in self.client.order_book():
            n = self.U.normalise_order(row)
            if n["id"]:
                self.book[n["id"]] = n

    def status(self, oid):
        if oid in self.book:
            return dict(self.book[oid])
        return self.U.normalise_order(self.client.order(oid) or {})

    def find_by_tag(self, tag):
        return [o for o in self.book.values() if o.get("tag") == tag]

    def positions(self):
        out = {}
        for p in self.client.positions():
            q = int(p.get("quantity") or 0)
            if q:
                out[(p.get("instrument_token"), self.U.PRODUCT_BACK.get(p.get("product"), p.get("product")))] = q
        return out


# ================================================================ the engine

FINAL = ("closed", "cancelled", "rejected", "expired", "error")


class Engine(object):
    def __init__(self, cfg, feed, broker, mode="paper", base=LIVE_DIR, log=None, notifier=notify, upstox_keys=None):
        self.cfg = dict(M.DEFAULT_CONFIG)
        self.cfg.update(cfg or {})
        self.live = live_cfg(cfg)
        self.feed, self.broker, self.mode, self.base = feed, broker, mode, base
        self.notifier = notifier
        self.log = log
        self.journal = None
        self.quotes = {}
        self.quote_at = {}
        self.errors = 0
        self.last_bar_slot = None
        self.keys = upstox_keys

    # ---- journal
    def jpath(self, day):
        return os.path.join(self.base, "journal-%s-%s.json" % (day.isoformat(), self.mode))

    def load(self, day):
        p = self.jpath(day)
        self.journal = M.read_json(p, "journal", required=False) or {
            "date": day.isoformat(), "mode": self.mode, "trades": {}, "seq": 0, "events": [], "halted": None,
            "broker": {}, "started_at": None, "ended": False, "checks": 0, "invariants": []}
        self.broker.restore(self.journal.get("broker") or {})
        return self.journal

    def save(self):
        self.journal["broker"] = self.broker.dump()
        M.write_json(self.jpath(datetime.date.fromisoformat(self.journal["date"])), self.journal)

    def event(self, now, text, level="info"):
        line = "%s %s" % (now.strftime("%H:%M:%S"), text)
        self.journal["events"].append(line)
        self.journal["events"] = self.journal["events"][-500:]
        if self.log:
            (self.log.warning if level == "warn" else self.log.info)("[%s] %s" % (self.mode.upper(), text))

    def halt(self, now, why, flatten=False):
        if not self.journal["halted"]:
            self.journal["halted"] = {"at": now.isoformat(timespec="seconds"), "why": why, "flatten": flatten}
            self.event(now, "HALT: %s%s" % (why, " (flattening open trades)" if flatten else ""), "warn")
            self.notifier("trader-live HALT", why)
        if flatten:
            self.flatten_all(now, "halt: " + why)

    # ---- helpers
    def trades(self, states=None):
        ts = list(self.journal["trades"].values())
        return [t for t in ts if states is None or t["state"] in states]

    def tag(self, trade, leg):
        return ("TDR%s-%02d%s" % (self.journal["date"][5:].replace("-", ""), trade["n"], leg))[:20]

    def place(self, trade, leg, order, now):
        tag = self.tag(trade, leg)
        rec = {"tag": tag, "oid": None, "status": "placing", "order": order, "at": now.isoformat(timespec="seconds")}
        trade["legs"][leg] = rec
        self.save()                                  # write intent first: a crash here is reconciled by tag
        try:
            rec["oid"] = self.broker.place(order, tag, now)
            rec["status"] = "open"
            self.event(now, "%s #%d %s %s %d %s %s%s" % (
                {"E": "ENTRY", "S": "STOP", "T": "TARGET", "X": "EXIT"}[leg[0]], trade["n"], order["side"],
                order["symbol"], order["qty"], order["order_type"],
                ("@ %s" % order["price"]) if order.get("price") else "",
                (" trigger %s" % order["trigger_price"]) if order.get("trigger_price") else ""))
            self.errors = 0
        except M.MarketError as e:
            rec["status"] = "unknown" if getattr(e, "retryable", False) else "rejected"
            rec["error"] = str(e)
            self.errors += 1
            self.event(now, "order %s for #%d failed: %s" % (tag, trade["n"], e), "warn")
        self.save()
        return rec

    def leg_status(self, rec):
        if not rec or not rec.get("oid"):
            return rec.get("status") if rec else None
        st = self.broker.status(rec["oid"])
        rec["status"] = st.get("status", rec["status"])
        if st.get("filled_qty"):
            rec["filled_qty"] = st["filled_qty"]
        if st.get("avg_price"):
            rec["avg_price"] = st["avg_price"]
        if st.get("message"):
            rec["message"] = st["message"]
        return rec["status"]

    def resolve_unknown(self, rec, now):
        """A place that timed out: did it reach the broker? Look it up by tag."""
        if rec.get("status") != "unknown":
            return
        finder = getattr(self.broker, "find_by_tag", None)
        hits = finder(rec["tag"]) if finder else []
        if hits:
            rec["oid"], rec["status"] = hits[0]["id"], hits[0]["status"]
            self.event(now, "order %s found at the broker after a timeout (%s)" % (rec["tag"], rec["oid"]))
        else:
            rec["checks"] = rec.get("checks", 0) + 1
            if rec["checks"] >= 3:
                rec["status"] = "rejected"
                self.event(now, "order %s never reached the broker" % rec["tag"], "warn")

    def exit_side(self, trade):
        return "SELL" if trade["side"] == "BUY" else "BUY"

    def segment(self, trade):
        return M.segment_for(trade["product"], (trade.get("instrument") or {}).get("type", "EQ"))

    def fill_charges(self, trade, side, price, qty):
        return M.leg_charges(self.segment(trade), side.lower(), price, qty, trade.get("exchange", "NSE"),
                             self.cfg.get("broker", "zerodha"), self.cfg)["total"]

    def record_fill(self, trade, leg, price, qty, now):
        side = trade["legs"][leg]["order"]["side"]
        ch = self.fill_charges(trade, side, price, qty)
        trade["fills"].append({"leg": leg, "side": side, "qty": qty, "price": price, "charges": ch,
                               "at": now.isoformat(timespec="seconds")})

    def pnl(self, trade, mark=None):
        buys = sum(f["qty"] * f["price"] for f in trade["fills"] if f["side"] == "BUY")
        sells = sum(f["qty"] * f["price"] for f in trade["fills"] if f["side"] == "SELL")
        qb = sum(f["qty"] for f in trade["fills"] if f["side"] == "BUY")
        qs = sum(f["qty"] for f in trade["fills"] if f["side"] == "SELL")
        open_q = qb - qs
        unreal = open_q * mark if (mark is not None and open_q) else 0.0
        charges = sum(f["charges"] for f in trade["fills"])
        gross = sells - buys + unreal
        return round(gross, 2), round(charges, 2), round(gross - charges, 2), open_q

    # ---- the order flow
    def make_exits(self, trade, qty):
        o = trade["order"]
        ex = o.get("exits") or {}
        sl, tg = ex.get("stop_loss") or {}, ex.get("target") or {}
        base = {"symbol": o["symbol"], "exchange": o.get("exchange", "NSE"), "instrument": o.get("instrument"),
                "side": self.exit_side(trade), "qty": qty, "product": o["product"], "validity": "DAY"}
        stop = dict(base, order_type=sl.get("order_type") or "SL-M", trigger_price=sl.get("trigger_price"),
                    price=sl.get("price"))
        target = dict(base, order_type="LIMIT", price=tg.get("price"), trigger_price=None)
        return stop, target

    def start_entry(self, trade, now):
        q = self.quotes.get(trade["symbol"])
        if not q:
            trade["state"] = "expired"
            trade["why"] = "no live price at confirmation"
            return
        plan = trade["plan"]
        long = trade["side"] == "BUY"
        risk = abs(plan["entry"] - plan["stop"])
        ltp = float(q["price"])
        chase = (ltp - plan["entry"]) if long else (plan["entry"] - ltp)
        if chase > float(self.live["max_chase_r"]) * risk:
            trade["state"] = "expired"
            trade["why"] = "price %.2f ran %.2f past the planned entry %.2f (over %.2fR)" % (
                ltp, chase, plan["entry"], float(self.live["max_chase_r"]))
            self.event(now, "#%d %s expired: %s" % (trade["n"], trade["symbol"], trade["why"]))
            return
        if (long and ltp <= plan["stop"]) or (not long and ltp >= plan["stop"]):
            trade["state"] = "expired"
            trade["why"] = "price %.2f is already through the stop %.2f" % (ltp, plan["stop"])
            return
        tick = M.tick_size(ltp)
        buf = int(self.live["entry_buffer_ticks"]) * tick
        limit = M.round_tick(max(ltp, plan["entry"]) + buf if long else min(ltp, plan["entry"]) - buf,
                             mode="up" if long else "down")
        o = trade["order"]
        entry = {"symbol": o["symbol"], "exchange": o.get("exchange", "NSE"), "instrument": o.get("instrument"),
                 "side": trade["side"], "qty": trade["qty"], "product": o["product"], "order_type": "LIMIT",
                 "price": limit, "trigger_price": None, "validity": "DAY"}
        trade["state"] = "entry_open"
        trade["entry_started"] = now.isoformat(timespec="seconds")
        rec = self.place(trade, "E", entry, now)
        if rec["status"] == "rejected":
            trade["state"] = "rejected"
            trade["why"] = rec.get("error")

    def step_trade(self, trade, now, flat):
        st = trade["state"]
        legs = trade["legs"]
        for rec in legs.values():
            self.resolve_unknown(rec, now)
        if st == "pending_confirm":
            self.check_confirmation(trade, now)
            return
        if st == "entry_open":
            e = legs["E"]
            s = self.leg_status(e)
            if s == "filled":
                qty = int(e.get("filled_qty") or trade["qty"])
                self.record_fill(trade, "E", float(e["avg_price"]), qty, now)
                trade["state"] = "open"
                trade["filled_qty"] = qty
                self.event(now, "#%d %s entry filled %d @ %.2f" % (trade["n"], trade["symbol"], qty, e["avg_price"]))
                stop, target = self.make_exits(trade, qty)
                self.place(trade, "S", stop, now)
                self.place(trade, "T", target, now)
                if legs["S"]["status"] == "rejected":
                    self.event(now, "#%d stop-loss rejected: closing the position now" % trade["n"], "warn")
                    self.close_now(trade, now, "stop-loss could not be placed")
            elif s in ("rejected", "cancelled"):
                trade["state"] = s
                trade["why"] = e.get("message") or e.get("error") or s
                self.event(now, "#%d entry %s: %s" % (trade["n"], s, trade["why"]), "warn")
            else:
                started = datetime.datetime.fromisoformat(trade["entry_started"])
                if flat or (now - started).total_seconds() > float(self.live["entry_timeout_seconds"]):
                    self.broker.cancel(e["oid"], now) if e.get("oid") else None
                    s2 = self.leg_status(e)
                    if s2 == "filled":
                        return               # filled while cancelling: handled next tick
                    filled = int(e.get("filled_qty") or 0)
                    if filled:
                        e["status"] = "filled"
                        e["filled_qty"] = filled
                        return
                    trade["state"] = "expired"
                    trade["why"] = "entry not filled in %ss" % self.live["entry_timeout_seconds"]
                    self.event(now, "#%d %s entry cancelled: not filled" % (trade["n"], trade["symbol"]))
            return
        if st in ("open", "exiting"):
            sstat = self.leg_status(legs.get("S"))
            tstat = self.leg_status(legs.get("T"))
            xstat = self.leg_status(legs.get("X"))
            done = [k for k, v in (("S", sstat), ("T", tstat), ("X", xstat)) if v == "filled"]
            for k in done:
                if not legs[k].get("recorded"):
                    self.record_fill(trade, k, float(legs[k]["avg_price"]),
                                     int(legs[k].get("filled_qty") or legs[k]["order"]["qty"]), now)
                    legs[k]["recorded"] = True
            if len(done) > 1:
                self.event(now, "#%d BOTH exits filled (a race): flattening the extra quantity" % trade["n"], "warn")
                self.flatten_extra(trade, now)
                self.halt(now, "two exits of trade #%d filled; check your positions at the broker" % trade["n"])
            if done:
                for k in ("S", "T"):
                    if k not in done and legs.get(k) and legs[k].get("oid") and legs[k]["status"] in ("open", "partial",
                                                                                                    "pending"):
                        self.broker.cancel(legs[k]["oid"], now)
                        self.leg_status(legs[k])
                _, _, _, open_q = self.pnl(trade)
                if open_q == 0:
                    how = {"S": "stop-loss", "T": "target"}.get(done[0]) or trade.get("exit_reason") or "square-off"
                    self.close_trade(trade, now, how)
                return
            if flat and st == "open" and trade["product"] == "MIS":
                self.close_now(trade, now, "flat_by %s" % self.cfg.get("flat_by"))

    def close_now(self, trade, now, why):
        legs = trade["legs"]
        for k in ("S", "T"):
            if legs.get(k) and legs[k].get("oid") and legs[k]["status"] in ("open", "partial", "pending"):
                self.broker.cancel(legs[k]["oid"], now)
                self.leg_status(legs[k])
                if legs[k]["status"] == "filled":
                    return                                     # it filled first: next tick books it
        _, _, _, open_q = self.pnl(trade)
        if open_q:
            o = trade["order"]
            x = {"symbol": o["symbol"], "exchange": o.get("exchange", "NSE"), "instrument": o.get("instrument"),
                 "side": self.exit_side(trade), "qty": abs(open_q), "product": o["product"], "order_type": "MARKET",
                 "price": None, "trigger_price": None, "validity": "DAY"}
            trade["state"] = "exiting"
            trade["exit_reason"] = why
            self.place(trade, "X", x, now)

    def flatten_extra(self, trade, now):
        _, _, _, open_q = self.pnl(trade)
        if open_q:
            o = trade["order"]
            side = "SELL" if open_q > 0 else "BUY"
            x = {"symbol": o["symbol"], "exchange": o.get("exchange", "NSE"), "instrument": o.get("instrument"),
                 "side": side, "qty": abs(open_q), "product": o["product"], "order_type": "MARKET", "price": None,
                 "trigger_price": None, "validity": "DAY"}
            self.place(trade, "X2", x, now)

    def close_trade(self, trade, now, how):
        gross, charges, net, _ = self.pnl(trade)
        trade.update(state="closed", closed_at=now.isoformat(timespec="seconds"), exit_reason=how,
                     gross=gross, charges=charges, net=net)
        self.event(now, "#%d %s CLOSED by %s: gross %s, charges %s, net %s" % (
            trade["n"], trade["symbol"], how, M.inr(gross), M.inr(charges), M.inr(net)))
        self.notifier("trader-live %s" % self.mode, "%s closed by %s: net %s" % (trade["symbol"], how, M.inr(net)))

    def flatten_all(self, now, why):
        for t in self.trades(("open",)):
            self.close_now(t, now, why)
        for t in self.trades(("entry_open",)):
            e = t["legs"].get("E")
            if e and e.get("oid"):
                self.broker.cancel(e["oid"], now)
        for t in self.trades(("pending_confirm",)):
            t["state"] = "expired"
            t["why"] = why

    # ---- confirmations
    def pending_path(self, trade):
        return os.path.join(self.base, "pending", "%s-%02d.json" % (self.journal["date"], trade["n"]))

    def confirm_path(self, trade):
        return os.path.join(self.base, "confirmed", "%s-%02d.json" % (self.journal["date"], trade["n"]))

    def check_confirmation(self, trade, now):
        made = datetime.datetime.fromisoformat(trade["created_at"])
        if self.mode == "paper" and self.live.get("paper_auto_confirm", True):
            trade["confirmed"] = {"by": "paper_auto_confirm", "at": now.isoformat(timespec="seconds")}
            self.start_entry(trade, now)
            return
        p = self.confirm_path(trade)
        doc = M.read_json(p, "confirmation", required=False)
        if doc:
            if doc.get("fingerprint") != TICKET.fingerprint(trade["order"]):
                trade["state"] = "expired"
                trade["why"] = "the confirmation does not match this order"
            elif doc.get("decision") == "no":
                trade["state"] = "cancelled"
                trade["why"] = "you said no"
            else:
                trade["confirmed"] = {"by": "user", "reply": doc.get("reply"), "at": doc.get("at")}
                self.event(now, "#%d confirmed by you: %r" % (trade["n"], doc.get("reply")))
                self.start_entry(trade, now)
            return
        if (now - made).total_seconds() > float(self.live["confirm_timeout_seconds"]):
            trade["state"] = "expired"
            trade["why"] = "no confirmation within %ss" % self.live["confirm_timeout_seconds"]
            self.event(now, "#%d %s expired: no yes in time" % (trade["n"], trade["symbol"]))

    # ---- signals
    def risk_inputs(self, now):
        day = now.date().isoformat()
        realised, placed = [], []
        for t in self.trades():
            if t["state"] not in ("cancelled", "rejected", "expired") or t.get("legs", {}).get("E", {}).get(
                    "filled_qty"):
                placed.append({"date": day, "status": "placed", "kind": "entry", "tradingsymbol": t["symbol"],
                               "max_loss": t["max_loss"], "at": t["created_at"]})
            if t["state"] == "closed":
                realised.append({"date": day, "net": t["net"], "tradingsymbol": t["symbol"]})
        cash = float(self.cfg.get("capital_inr") or 0) + sum(r["net"] for r in realised)
        positions = {}
        for t in self.trades(("open", "exiting")):
            _, _, _, q = self.pnl(t)
            positions["%s|%s" % (t["symbol"], t["product"])] = {"qty": q}
        if self.mode == "paper":
            ledger = {"cash_inr": cash, "positions": positions, "realised": realised}
            return {"ledger": ledger, "state": {"orders": placed}, "portfolio": None}
        state = {"orders": placed + [{"date": day, "status": "filled", "realised_pnl": r["net"],
                                      "tradingsymbol": r["tradingsymbol"], "paper": False} for r in realised]}
        return {"ledger": None, "state": state, "portfolio": {"cash_inr": self.live_cash or cash,
                                                               "positions": positions}}

    live_cash = None

    def signal_for(self, sym, now):
        one = self.feed.candles(sym, now)
        if not one:
            return None
        q = self.quotes.get(sym) or {}
        chart = five_minute_chart(sym, one, q.get("prev_close"), self.cfg.get("exchange", "NSE"))
        for side in self.live["sides"]:
            res = ENTRY.evaluate(chart, now, setup=self.live["strategy"], or_minutes=int(self.live["or_minutes"]),
                                 interval="5m", side=side, cfg=self.cfg, ctx={"product": "MIS"})
            if res["status"] != "triggered":
                continue
            if res.get("minutes_since", 99) > int(self.live["fresh_signal_minutes"]):
                continue
            return res, chart
        return None

    def plan_from(self, res, chart, now):
        long = res["side"] == "buy"
        stop_rule, stop_param = EXIT.parse_stop(self.live["stop"], long) if ":" not in str(self.live["stop"]) else (
            str(self.live["stop"]).split(":")[0], float(str(self.live["stop"]).split(":")[1]))
        orr = res.get("opening_range") or {}
        bars = [b for b in chart["bars"] if b["dt"].date() == now.date()]
        p = EXIT.compute(res["trigger"]["price"], stop_rule, stop_param, "r", float(self.live["target_r"]),
                         side=res["side"], segment="equity", or_high=orr.get("high"), or_low=orr.get("low"),
                         atr=M.atr(bars), atr_bars=min(len(bars), 14), vwap=res.get("vwap"))
        return {"created_by": "trader-live", "symbol": res["symbol"], "exchange": res.get("exchange", "NSE"),
                "instrument": {"type": "EQ"}, "product": "MIS", "side": res["side"], "date": now.date().isoformat(),
                "setup": res["setup"], "entry": p["entry"], "entry_time": res["trigger"]["close_time"],
                "stop": p["stop"], "stop_label": p["stop_label"], "target": p["target"],
                "target_label": "%gR" % float(self.live["target_r"]), "flat_by": self.cfg.get("flat_by", "15:10"),
                "tick": M.tick_size(p["entry"])}

    def scan(self, now):
        live_trades = self.trades(("pending_confirm", "entry_open", "open", "exiting"))
        if len(live_trades) >= int(self.live["max_open_trades"]):
            return
        traded = {t["symbol"] for t in self.trades()} if self.live["one_trade_per_symbol"] else set()
        traded |= set(self.journal.get("skip_today") or [])
        room = int(self.live["max_open_trades"]) - len(live_trades)
        for sym in self.live["watchlist"]:
            if room <= 0:
                return
            if sym in traded or sym not in self.quotes:
                continue
            try:
                got = self.signal_for(sym, now)
                if not got:
                    continue
                res, chart = got
                plan = self.plan_from(res, chart, now)
                s = SIZE.size_trade(plan, self.cfg, today=now.date().isoformat())
                order = SIZE.build_order(s)
                if not order:
                    self.skip(sym, now, "trader-budget: %s" % s.get("refusal"))
                    continue
                ri = self.risk_inputs(now)
                q = self.quotes[sym]
                prices = {sym: {"price": q["price"], "time": now, "prev_close": q.get("prev_close")}}
                r = RISK.check([order], self.cfg, ri["portfolio"], prices, ri["state"], ri["ledger"], now)
                rv = r["checks"][0]
                if rv["verdict"] != "PASS":
                    self.skip(sym, now, "BLOCKED by trader-risk: %s" % "; ".join(rv["reasons"]))
                    continue
                self.new_trade(plan, order, s, rv, now)
                room -= 1
            except M.MarketError as e:
                self.skip(sym, now, str(e))

    def skip(self, sym, now, why):
        """A signal that cannot be traded is reported once and the symbol rests for the day."""
        self.journal.setdefault("skip_today", []).append(sym)
        self.journal.setdefault("skipped", []).append({"symbol": sym, "at": now.isoformat(timespec="seconds"),
                                                       "why": why})
        self.event(now, "%s signal skipped: %s" % (sym, why))

    def new_trade(self, plan, order, s, rv, now):
        self.journal["seq"] += 1
        n = self.journal["seq"]
        trade = {"n": n, "id": "%s-%02d" % (self.journal["date"], n), "symbol": order["symbol"],
                 "exchange": order.get("exchange", "NSE"), "side": order["side"], "qty": int(order["qty"]),
                 "product": order["product"], "instrument": order.get("instrument"), "plan": plan, "order": order,
                 "max_loss": s["max_loss_with_costs"], "state": "pending_confirm", "legs": {}, "fills": [],
                 "created_at": now.isoformat(timespec="seconds"), "risk": {"verdict": rv["verdict"]},
                 "ticket": TICKET.plain(order, rv)}
        self.journal["trades"][str(n)] = trade
        M.write_json(self.pending_path(trade), {"id": trade["id"], "n": n, "mode": self.mode, "order": order,
                                                "fingerprint": TICKET.fingerprint(order), "ticket": trade["ticket"],
                                                "expires_at": (now + datetime.timedelta(
                                                    seconds=float(self.live["confirm_timeout_seconds"]))).isoformat(
                                                    timespec="seconds")})
        self.event(now, "SIGNAL #%d %s: %s" % (n, order["symbol"], trade["ticket"]))
        if self.mode == "live":
            self.notifier("trader-live: confirm #%d" % n, "%s. Confirm in Terminal: live.py confirm %d" % (
                trade["ticket"][:120], n))

    # ---- guards
    def guards(self, now):
        kill = os.path.join(self.base, "KILL")
        if os.path.exists(kill):
            with open(kill, encoding="utf-8", errors="replace") as fh:
                flatten = "flatten" in fh.read().lower()
            self.halt(now, "kill switch (%s)" % kill, flatten=flatten)
        daily = float(self.cfg.get("max_daily_loss_inr") or 0)
        if daily:
            net = 0.0
            for t in self.trades():
                if t["state"] == "closed":
                    net += t["net"]
                elif t["state"] in ("open", "exiting"):
                    q = self.quotes.get(t["symbol"])
                    _, _, n, _ = self.pnl(t, float(q["price"]) if q else None)
                    net += n
            self.journal["day_net"] = round(net, 2)
            if net <= -daily:
                self.halt(now, "daily loss %s reached your %s limit" % (M.inr(net), M.inr(daily, False)),
                          flatten=True)
        if self.errors >= int(self.live["max_consecutive_errors"]):
            self.halt(now, "%d broker/data errors in a row" % self.errors)

    # ---- one tick
    def tick(self, now):
        day = now.date()
        if self.journal is None or self.journal["date"] != day.isoformat():
            self.load(day)
        if not self.journal["started_at"]:
            self.journal["started_at"] = now.isoformat(timespec="seconds")
        self.journal["checks"] += 1
        st = M.market_status(now)
        syms = set(self.live["watchlist"]) | {t["symbol"] for t in self.trades(("open", "exiting", "entry_open",
                                                                                 "pending_confirm"))}
        if st["phase"] in ("open",) and syms:
            try:
                got = self.feed.ltp(sorted(syms), now)
                for k, v in got.items():
                    self.quotes[k] = v
                    self.quote_at[k] = now
                self.errors = 0
            except M.MarketError as e:
                self.errors += 1
                self.event(now, "quotes failed: %s" % e, "warn")
            try:
                self.broker.refresh(self.quotes, now)
            except M.MarketError as e:
                self.errors += 1
                self.event(now, "broker refresh failed: %s" % e, "warn")
        flat_t = M.parse_hhmm(self.cfg.get("flat_by", "15:10"))
        flat = now.time() >= flat_t or st["phase"] != "open"
        if st["phase"] == "open":
            self.guards(now)
            for t in list(self.trades()):
                if t["state"] not in FINAL:
                    try:
                        self.step_trade(t, now, flat)
                    except M.MarketError as e:
                        self.errors += 1
                        self.event(now, "trade #%d: %s" % (t["n"], e), "warn")
            stale = [s for s in syms if (now - self.quote_at.get(s, now - datetime.timedelta(days=1))).total_seconds()
                     > float(self.live["stale_quote_seconds"])]
            slot = M.bar_slot(now, 5)
            window_ok = (not flat and now.time() < M.parse_hhmm(self.cfg.get("no_entries_after", "14:45")))
            if (slot != self.last_bar_slot and not self.journal["halted"] and window_ok and not stale
                    and (now - datetime.datetime.combine(day, M.OPEN)).total_seconds() % 300 < 60):
                self.last_bar_slot = slot
                self.scan(now)
        if now.time() >= M.CLOSE and not self.journal["ended"]:
            self.end_of_day(now)
        self.save()
        M.write_json(os.path.join(self.base, "heartbeat.json"), {
            "at": now.isoformat(timespec="seconds"), "mode": self.mode, "feed": self.feed.name,
            "phase": st["phase"], "halted": self.journal["halted"], "open_trades": len(self.trades(("open",))),
            "pid": os.getpid()})

    # ---- end of day
    def invariants(self):
        bad = []
        flat_by = self.cfg.get("flat_by", "15:10")
        for t in self.trades():
            if t.get("confirmed") is None and t["legs"].get("E"):
                bad.append("#%d had an entry order without a confirmation" % t["n"])
            if t["state"] in ("open", "exiting", "entry_open"):
                bad.append("#%d still %s at the end of the day" % (t["n"], t["state"]))
            e = t["legs"].get("E")
            if e and e.get("status") == "filled" and not (t["legs"].get("S") or t["state"] == "closed"):
                bad.append("#%d filled without a stop-loss" % t["n"])
            if e and e.get("at", "")[11:16] >= self.cfg.get("no_entries_after", "14:45"):
                bad.append("#%d entry placed at %s, after no_entries_after" % (t["n"], e["at"][11:16]))
            if t["state"] == "closed":
                _, _, _, q = self.pnl(t)
                if q:
                    bad.append("#%d closed with %d still open" % (t["n"], q))
                if t["product"] == "MIS" and t.get("closed_at", "")[11:16] > M.hm(M.CLOSE):
                    bad.append("#%d MIS closed after the market close" % t["n"])
            if t.get("max_loss") and t["state"] == "closed" and t["net"] < -1.5 * t["max_loss"] - 50:
                bad.append("#%d lost %s, far beyond its planned %s (gap or slippage)" % (
                    t["n"], M.inr(t["net"]), M.inr(t["max_loss"])))
        return bad

    def end_of_day(self, now):
        closed = self.trades(("closed",))
        bad = self.invariants()
        rep = {"date": self.journal["date"], "mode": self.mode, "feed": self.feed.name, "delayed_feed": self.feed.delayed,
               "trades": len(self.trades()), "filled": len(closed),
               "net": round(sum(t["net"] for t in closed), 2), "charges": round(sum(t["charges"] for t in closed), 2),
               "wins": len([t for t in closed if t["net"] > 0]), "halted": self.journal["halted"],
               "invariant_violations": bad, "checks": self.journal["checks"],
               "completed_session": True, "validated": not bad and self.journal["checks"] > 10,
               "ended_at": now.isoformat(timespec="seconds")}
        self.journal["ended"] = True
        self.journal["invariants"] = bad
        M.write_json(os.path.join(self.base, "reports", "%s-%s-%s.json" % (self.journal["date"], self.mode,
                                                                           self.feed.name)), rep)
        self.event(now, "END OF DAY: %d trades, %d closed, net %s after %s charges%s" % (
            rep["trades"], rep["filled"], M.inr(rep["net"]), M.inr(rep["charges"]),
            "" if not bad else "; PROBLEMS: " + "; ".join(bad)))
        return rep

    # ---- start-up reconciliation (live)
    def reconcile(self, now):
        """After a restart: compare the journal with the broker; halt on anything unexplained."""
        if self.mode != "live":
            return []
        issues = []
        self.broker.refresh(self.quotes, now)
        ours = {r.get("oid") for t in self.trades() for r in t["legs"].values() if r.get("oid")}
        prefix = "TDR%s-" % self.journal["date"][5:].replace("-", "")
        for oid, o in self.broker.book.items():
            if str(o.get("tag") or "").startswith(prefix) and oid not in ours:
                adopted = False
                for t in self.trades():
                    for rec in t["legs"].values():
                        if rec.get("tag") == o["tag"] and not rec.get("oid"):
                            rec["oid"], adopted = oid, True
                if not adopted:
                    issues.append("broker order %s (tag %s) is not in the journal" % (oid, o.get("tag")))
        pos = self.broker.positions() or {}
        expect = {}
        for t in self.trades(("open", "exiting")):
            _, _, _, q = self.pnl(t)
            key = (self.keys(t) if self.keys else t["symbol"], t["product"])
            expect[key] = expect.get(key, 0) + q
        for k, q in pos.items():
            if k[1] == "MIS" and expect.get(k, 0) != q:
                issues.append("broker shows %s %s qty %d, the journal expects %d" % (k[0], k[1], q, expect.get(k, 0)))
        if issues:
            self.halt(now, "start-up reconciliation: " + "; ".join(issues))
        return issues


# ================================================================ the runner

def run(engine, clock=None, sleep=time.sleep, until=None, max_ticks=None):
    """Tick until the session ends (or `until`)."""
    clock = clock or M.now_ist
    n = 0
    while True:
        now = clock()
        if until and now >= until:
            break
        engine.tick(now)
        n += 1
        if max_ticks and n >= max_ticks:
            break
        if now.time() >= datetime.time(15, 31):
            break
        st = M.market_status(now)
        sleep(float(engine.live["poll_seconds"]) if st["open"] else 20)
    return n
