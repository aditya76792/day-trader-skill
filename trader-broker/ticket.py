#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker (India edition). Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain ticket in rupees (product,
            order type, the stop-loss and target exit orders, the max loss at
            the stop INCLUDING charges, margin), the broker-neutral fields,
            and how they map to Zerodha Kite Connect, Upstox and Dhan order
            fields. An order is READY only if trader-risk passed THIS exact
            order today (matched by fingerprint, exits unchanged) and nobody
            has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", "haan", a yes that
            names a different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened at a live broker: placed (with its order
            id), denied or failed.
  fill      records a live fill in trader/portfolio.json (cash, position,
            charges, realised P&L net of charges).
  flatten   builds the exit ticket that squares off a position before flat_by.
            It still goes through trader-risk and your yes.
  log       prints today's decisions.

In paper mode ("paper": true, the default) an approved order goes to the
built-in paper broker: python3 paper.py place --index N.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2510280001 --filled-qty 42 --filled-price 1171.2
    python3 ticket.py fill --tradingsymbol RELIANCE --side sell --qty 42 --price 1187.2 --product MIS
    python3 ticket.py flatten --tradingsymbol RELIANCE
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
India dates; --now "2026-10-28 09:50" (IST) pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

FP_FIELDS = ("exchange", "tradingsymbol", "side", "qty", "product", "order_type", "price", "trigger_price")
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "change", "instead", "only", "nope", "nah", "never", "mind",
               "actually", "although", "though", "hmm", "except", "rather"}
# After "yes", only these words may appear (plus the order's own symbol, side, product,
# instrument and numbers). Anything else ("as CNC" on an MIS order, "go short" on a buy,
# "for INFY", "the first two", "x2") is not a plain yes for this one order.
FILLER_WORDS = {"yes", "y", "please", "pls", "place", "it", "go", "ahead", "confirm", "confirmed", "proceed",
                "do", "that", "this", "one", "the", "order", "ticket", "for", "at", "rs", "inr", "trigger",
                "target", "limit", "price", "max", "loss", "lot", "lots", "share", "shares", "unit", "units",
                "qty", "quantity", "of", "on", "with", "thanks", "thank", "you", "ok", "sure", "send", "a", "an"}
PRODUCT_WORDS = {"MIS": "MIS intraday", "CNC": "CNC delivery", "NRML": "NRML carry forward"}
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel"}
MAP_NOTE = "field mapping; check your broker's API docs"

# Kite Connect v3 POST /orders/{variety}. Exchanges: NSE, BSE, NFO (NSE F&O), BFO (BSE F&O).
KITE = {"fields": ("tradingsymbol", "exchange", "transaction_type", "order_type", "quantity", "product",
                   "price", "trigger_price", "validity", "variety"),
        "exchange": {("NSE", False): "NSE", ("BSE", False): "BSE", ("NSE", True): "NFO", ("BSE", True): "BFO"}}
# Upstox v2 POST /order/place: product I (intraday) or D (delivery, and F&O carry); instrument_token from
# Upstox's instruments file ("NSE_EQ|<ISIN>", "NSE_FO|<token>").
UPSTOX = {"product": {"MIS": "I", "CNC": "D", "NRML": "D"},
          "segment": {("NSE", False): "NSE_EQ", ("BSE", False): "BSE_EQ", ("NSE", True): "NSE_FO",
                      ("BSE", True): "BSE_FO"}}
# Dhan v2 POST /orders: productType CNC / INTRADAY / MARGIN (F&O carry); securityId from Dhan's scrip master.
DHAN = {"product": {"MIS": "INTRADAY", "CNC": "CNC", "NRML": "MARGIN"},
        "order_type": {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"},
        "segment": {("NSE", False): "NSE_EQ", ("BSE", False): "BSE_EQ", ("NSE", True): "NSE_FNO",
                    ("BSE", True): "BSE_FNO"}}


class TicketError(M.MarketError):
    pass


# ---------------------------------------------------------------- the fingerprint (same in trader-risk/check.py)

def fingerprint(order):
    """sha1 of the order's identity fields (CONTRACTS.md). trader-risk/check.py has the
    identical function; a test keeps them equal."""
    core = {}
    for k in FP_FIELDS:
        v = order.get(k)
        if k == "tradingsymbol" and not v:
            v = order.get("symbol")
        if v in (None, ""):
            v = None
        elif k in ("price", "trigger_price"):
            v = round(float(v), 2)
        elif k == "qty":
            v = int(round(float(v)))
        else:
            v = str(v).strip().upper()
        core[k] = v
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- helpers

def _f(x):
    try:
        if isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _px(v):
    return "%.2f" % float(v)


def _num(x):
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else ("%.4f" % x).rstrip("0").rstrip(".")


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def iso(now):
    return now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


def instrument(order):
    inst = order.get("instrument") if isinstance(order.get("instrument"), dict) else {}
    itype = str(inst.get("type") or "EQ").upper()
    return inst, itype, itype in ("CE", "PE", "FUT")


def tsym_of(order):
    return str(order.get("tradingsymbol") or order.get("symbol") or "").upper()


def exits_of(order):
    ex = order.get("exits") if isinstance(order.get("exits"), dict) else {}
    sl = ex.get("stop_loss") if isinstance(ex.get("stop_loss"), dict) else {}
    tg = ex.get("target") if isinstance(ex.get("target"), dict) else {}
    return sl, tg, ex.get("how")


# ---------------------------------------------------------------- max loss and plain words

def max_loss(order, rv=None):
    """(rupees incl. charges, gross rupees, stop) lost if the stop fills at its trigger."""
    sl, _, _ = exits_of(order)
    stop = _f(sl.get("trigger_price")) or _f((order.get("plan") or {}).get("stop"))
    if rv and rv.get("max_loss_with_costs") is not None:
        return rv["max_loss_with_costs"], rv.get("max_loss"), stop
    plan = order.get("plan") or {}
    if _f(plan.get("max_loss_with_costs")) is not None:
        return _f(plan["max_loss_with_costs"]), _f(plan.get("max_loss")), stop
    return None, None, stop


def entry_words(order):
    side = str(order.get("side", "")).upper()
    q = int(_f(order.get("qty")) or 0)
    inst, itype, fno = instrument(order)
    lots = order.get("lots")
    what = "%d %s" % (q, tsym_of(order))
    if fno and lots:
        what = "%d lot%s (%d units) of %s" % (lots, "" if lots == 1 else "s", q, tsym_of(order))
    ot = str(order.get("order_type") or "").upper()
    if ot == "LIMIT":
        how = "LIMIT %s" % _px(order["price"])
    elif ot == "SL":
        how = "SL trigger %s limit %s" % (_px(order["trigger_price"]), _px(order["price"]))
    elif ot == "SL-M":
        how = "SL-M trigger %s" % _px(order["trigger_price"])
    else:
        how = "MARKET"
    prod = PRODUCT_WORDS.get(str(order.get("product") or "").upper(), order.get("product"))
    amo = ", after-market order (AMO)" if str(order.get("variety") or "").lower() == "amo" else ""
    return "%s %s on %s, %s, %s, %s%s" % (side, what, order.get("exchange", "NSE"), prod, how,
                                          VALIDITY_WORDS.get(str(order.get("validity") or "DAY").upper(), "day"), amo)


def plain_parts(order, rv=None):
    parts = [entry_words(order)]
    sl, tg, _ = exits_of(order)
    if sl:
        leg = "stop-loss: %s %s trigger %s" % (sl.get("side", ""), sl.get("order_type", ""), _px(sl["trigger_price"]))
        if sl.get("price"):
            leg += " limit %s" % _px(sl["price"])
        parts.append(leg)
    if tg:
        parts.append("target: %s LIMIT %s" % (tg.get("side", ""), _px(tg["price"])))
    loss, gross, stop = max_loss(order, rv)
    if loss is not None:
        parts.append("max loss about %s at the stop %s, charges included (a gap or a fast market fills worse)"
                     % (M.inr(loss), _px(stop)))
    m = order.get("margin") or {}
    if _f(m.get("required")) is not None:
        parts.append("margin %s (%s)" % (M.inr(m["required"]), m.get("basis", "")))
    return parts


def plain(order, rv=None):
    return " / ".join(plain_parts(order, rv))


# ---------------------------------------------------------------- broker field mappings

def neutral_fields(order):
    out = {k: order.get(k) for k in ("exchange", "tradingsymbol", "side", "qty", "product", "order_type", "price",
                                     "trigger_price", "validity", "variety")}
    out["tradingsymbol"] = tsym_of(order)
    return out


def _leg(order, side, otype, price, trigger):
    leg = dict(order)
    leg.update(side=side, order_type=otype, price=price, trigger_price=trigger, variety="regular")
    return leg


def exit_legs(order):
    sl, tg, _ = exits_of(order)
    legs = []
    if sl:
        legs.append(("stop-loss", _leg(order, sl.get("side"), sl.get("order_type"), sl.get("price"),
                                       sl.get("trigger_price"))))
    if tg:
        legs.append(("target", _leg(order, tg.get("side"), "LIMIT", tg.get("price"), None)))
    return legs


def kite_params(order):
    _, _, fno = instrument(order)
    ex = str(order.get("exchange") or "NSE").upper()
    p = {"variety": "amo" if str(order.get("variety") or "").lower() == "amo" else "regular",
         "tradingsymbol": tsym_of(order), "exchange": KITE["exchange"].get((ex, fno), ex),
         "transaction_type": str(order.get("side")).upper(), "order_type": str(order.get("order_type")).upper(),
         "quantity": int(_f(order.get("qty")) or 0), "product": str(order.get("product")).upper(),
         "validity": str(order.get("validity") or "DAY").upper()}
    if order.get("price") is not None and p["order_type"] in ("LIMIT", "SL"):
        p["price"] = float(order["price"])
    if order.get("trigger_price") is not None and p["order_type"] in ("SL", "SL-M"):
        p["trigger_price"] = float(order["trigger_price"])
    return p


def upstox_params(order):
    _, _, fno = instrument(order)
    ex = str(order.get("exchange") or "NSE").upper()
    ot = str(order.get("order_type")).upper()
    return {"instrument_token": "%s|<%s from Upstox's instruments file for %s>"
                                % (UPSTOX["segment"].get((ex, fno)), "instrument key" if fno else "ISIN", tsym_of(order)),
            "transaction_type": str(order.get("side")).upper(), "order_type": ot,
            "quantity": int(_f(order.get("qty")) or 0),
            "product": UPSTOX["product"].get(str(order.get("product")).upper()),
            "validity": str(order.get("validity") or "DAY").upper(),
            "price": float(order["price"]) if (order.get("price") is not None and ot in ("LIMIT", "SL")) else 0,
            "trigger_price": float(order["trigger_price"]) if order.get("trigger_price") is not None else 0,
            "disclosed_quantity": 0, "is_amo": str(order.get("variety") or "").lower() == "amo", "tag": "trader-broker"}


def dhan_params(order):
    _, _, fno = instrument(order)
    ex = str(order.get("exchange") or "NSE").upper()
    ot = str(order.get("order_type")).upper()
    amo = str(order.get("variety") or "").lower() == "amo"
    p = {"dhanClientId": "<your Dhan client id>", "transactionType": str(order.get("side")).upper(),
         "exchangeSegment": DHAN["segment"].get((ex, fno)),
         "productType": DHAN["product"].get(str(order.get("product")).upper()),
         "orderType": DHAN["order_type"].get(ot, ot), "validity": str(order.get("validity") or "DAY").upper(),
         "securityId": "<securityId from Dhan's scrip master for %s>" % tsym_of(order),
         "quantity": int(_f(order.get("qty")) or 0),
         "price": float(order["price"]) if (order.get("price") is not None and ot in ("LIMIT", "SL")) else 0,
         "triggerPrice": float(order["trigger_price"]) if order.get("trigger_price") is not None else 0,
         "afterMarketOrder": amo}
    if amo:
        p["amoTime"] = "OPEN"
    return p


def mappings(order):
    out = {"note": MAP_NOTE, "kite": {"entry": kite_params(order)}, "upstox": {"entry": upstox_params(order)},
           "dhan": {"entry": dhan_params(order)}}
    for name, leg in exit_legs(order):
        out["kite"][name] = kite_params(leg)
        out["upstox"][name] = upstox_params(leg)
        out["dhan"][name] = dhan_params(leg)
    return out


# ---------------------------------------------------------------- the explicit yes

def amount_numbers(order, rv=None):
    nums = []
    for v in (order.get("qty"), order.get("lots"), order.get("price"), order.get("trigger_price")):
        if _f(v) is not None:
            nums.append(_f(v))
    sl, tg, _ = exits_of(order)
    for v in (sl.get("trigger_price"), sl.get("price"), tg.get("price")):
        if _f(v) is not None:
            nums.append(_f(v))
    loss, gross, _ = max_loss(order, rv)
    for v in (loss, gross, (order.get("margin") or {}).get("required"), (order.get("margin") or {}).get("notional")):
        if _f(v) is not None:
            nums.append(_f(v))
    return nums


SYMBOL_TOKEN = re.compile(r"[A-Z][A-Z0-9&\-]{1,24}")


def parse_yes(reply, order, batch_symbols=(), rv=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, haan, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    mine = {tsym_of(order), str(order.get("symbol") or "").upper(),
            str((order.get("instrument") or {}).get("underlying") or "").upper()} - {""}
    upper = raw.upper()
    tokens = set(SYMBOL_TOKEN.findall(upper))
    others = {str(s).upper() for s in batch_symbols} - mine
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), tsym_of(order))
    # take the order's own symbols out, then split glued words ("1250shares", "22800CE", "x2")
    rest = upper
    for m in sorted(mine, key=len, reverse=True):
        rest = re.sub(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % re.escape(m), " ", rest)
    rest = re.sub(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])", " ", rest)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", rest)]
    if nums:
        want = amount_numbers(order, rv)
        bad = [n for n in nums if not any(abs(n - w) < 0.005 for w in want)]
        if bad:
            return False, "the reply says %s, but this order is %s" % (", ".join(_num(n) for n in bad), plain(order, rv))
    side = str(order.get("side") or "").lower()
    product = str(order.get("product") or "").lower()
    itype = str((order.get("instrument") or {}).get("type") or "EQ").lower()
    allowed = set(FILLER_WORDS)
    allowed.add(side)
    allowed.update({"buy": {"long"}, "sell": {"short"}}.get(side, set()))
    allowed.add(product)
    allowed.update({"mis": {"intraday"}, "cnc": {"delivery"}, "nrml": {"carry"}}.get(product, set()))
    allowed.add(str(order.get("order_type") or "").lower().replace("-", ""))
    allowed.add(str(order.get("exchange") or "").lower())
    if itype in ("ce", "pe", "fut"):
        allowed.update({itype, {"ce": "call", "pe": "put", "fut": "futures"}[itype], "option", "options"})
    extra = [w for w in re.findall(r"[a-z]+", rest.lower()) if w not in allowed]
    if extra:
        return False, ("the reply has words that are not part of this order (%s); a yes for this ticket is "
                       "just \"yes\" for %s" % (", ".join(sorted(set(extra))), plain(order, rv)))
    return True, "explicit yes for %s" % plain(order, rv)


# ---------------------------------------------------------------- state and status

def load_state(path):
    st = M.read_json(path, "broker state", required=False)
    return st if isinstance(st, dict) else {"orders": []}


def save_state(path, state):
    M.write_json(path, state)


def latest(state, fp, today):
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    checks = risk.get("checks") or []
    mine = [r for r in checks if r.get("fingerprint") == fp]
    if not mine:
        return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"
    r = mine[-1]
    if str(r.get("date") or risk.get("date")) != today:
        return None, "the risk check is from %s, not today; run check.py again" % (r.get("date") or risk.get("date"))
    return r, None


def exits_changed(order, rv):
    sl, tg, _ = exits_of(order)
    stop = _f(sl.get("trigger_price")) or _f((order.get("plan") or {}).get("stop"))
    if rv.get("kind") == "exit":
        return None
    if rv.get("stop_trigger") is not None and (stop is None or abs(stop - float(rv["stop_trigger"])) > 0.005):
        return "the stop changed since the risk check (%s, checked %s); run check.py again" % (
            _px(stop) if stop else "none", _px(rv["stop_trigger"]))
    tgt = _f(tg.get("price")) or _f((order.get("plan") or {}).get("target"))
    if rv.get("target") is not None and (tgt is None or abs(tgt - float(rv["target"])) > 0.005):
        return "the target changed since the risk check; run check.py again"
    return None


PASS_VALID_MINUTES = 30


def time_gate(order, rv, cfg, now):
    """Rules that depend on the clock, re-checked when the user says yes and when the
    order is placed: a PASS from 11:00 must not carry an entry past the cut-offs.
    Returns the reason it is too late, or None."""
    c = dict(M.DEFAULT_CONFIG)
    c.update(cfg or {})
    amo = str(order.get("variety") or "").lower() == "amo"
    t = now.time()
    st = M.market_status(now)
    if not amo and not st["open"]:
        return "the market is not open (%s)" % st["label"]
    if amo and st["open"]:
        return "an AMO cannot go in while the market is open; place a regular order"
    if amo and str(order.get("product") or "").upper() == "MIS" and (rv or {}).get("kind") != "exit":
        return "an MIS AMO would fill at the next open, before your entry window"
    if rv and rv.get("time") and rv.get("date") == now.date().isoformat():
        try:
            checked = datetime.datetime.combine(now.date(), M.parse_hhmm(str(rv["time"])[:5]))
            age = (now - checked).total_seconds() / 60.0
            limit = float(c.get("risk_pass_valid_minutes", PASS_VALID_MINUTES))
            if age > limit:
                return ("the risk check is %d minutes old (valid %d); prices and the clock moved: run check.py again"
                        % (age, limit))
        except (M.MarketError, ValueError):
            pass
    if (rv or {}).get("kind") == "exit":
        return None
    product = str(order.get("product") or "").upper()
    if not amo:
        late = M.parse_hhmm(c.get("no_entries_after", "14:45"))
        if t >= late:
            return "no new entries after %s IST (no_entries_after); it is %s" % (M.hm(late), M.hm(t))
        if product == "MIS" and t >= M.parse_hhmm(c.get("flat_by", "15:10")):
            return "an MIS entry at or after flat_by %s would be squared off at once" % c.get("flat_by", "15:10")
    inst = order.get("instrument") or {}
    if str(inst.get("type") or "EQ").upper() in ("CE", "PE", "FUT"):
        exp = inst.get("expiry")
        try:
            exp_d = datetime.date.fromisoformat(str(exp)) if exp else None
        except ValueError:
            exp_d = None
        if exp_d == now.date():
            cut = M.parse_hhmm(c.get("expiry_day_last_entry", "13:30"))
            if t >= cut:
                return "expiry day: no new F&O entries after %s IST (it is %s)" % (M.hm(cut), M.hm(t))
    return None


def status_of(order, risk, state, today):
    """(ready, label, risk_result, decision)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv, None
    ch = exits_changed(order, rv)
    if ch:
        return False, "NOT READY: " + ch, rv, None
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting to be placed" % rec.get("at"), rv, rec
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv, rec
    return True, "READY: trader-risk PASS, waiting for your yes", rv, None


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv, _ = status_of(order, risk, state, today)
    broker = str(cfg.get("broker") or "zerodha")
    paper = cfg.get("paper", True)
    acct = "PAPER (built-in paper broker)" if paper else "LIVE ACCOUNT, REAL MONEY (%s)" % broker
    lines = ["ORDER %d of %d, %s" % (idx, total, acct)]
    for part in plain_parts(order, rv):
        lines.append("  " + part)
    _, _, how = exits_of(order)
    if how:
        lines.append("  exits: " + how)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    lines.append("  Broker-neutral order:")
    lines.append("    " + json.dumps(neutral_fields(order)))
    mp = mappings(order)
    lines.append("  How it maps (%s):" % MAP_NOTE)
    for name, title in (("kite", "Zerodha Kite Connect (place_order)"), ("upstox", "Upstox (v2 /order/place)"),
                        ("dhan", "Dhan (v2 /orders)")):
        lines.append("    %s" % title)
        for leg, params in mp[name].items():
            lines.append("      %-9s %s" % (leg, json.dumps(params)))
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if not paper:
            lines.append("  Your broker tool (set to Needs approval) then asks again: Allow once places it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv, _ = status_of(order, risk, state, today)
    loss, gross, stop = max_loss(order, rv)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, rv),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": cfg.get("broker", "zerodha"), "order": neutral_fields(order), "exits": order.get("exits"),
            "why": order.get("why", ""), "max_loss_with_costs": loss, "max_loss": gross, "stop": stop,
            "margin": order.get("margin"), "costs": order.get("costs"), "mappings": mappings(order)}


# ---------------------------------------------------------------- the book (shared with paper.py)

def new_book(capital):
    return {"cash_inr": round(float(capital), 2), "positions": {}}


def charges_for(segment, side, price, qty, exchange, cfg):
    """One executed order's charges, plus DP charges on a delivery (CNC) sell."""
    broker = str((cfg or {}).get("broker") or "zerodha").lower()
    leg = M.leg_charges(segment, side.lower(), price, qty, exchange, broker if broker in M.BROKERS else "flat20", cfg)
    dp = 0.0
    if segment == "delivery" and side.lower() == "sell":
        _, prof = M.broker_profile(cfg or {})
        dp = round(prof.get("dp", 0.0) * (1 + M.GST), 2)
    leg = dict(leg)
    leg["dp"] = dp
    leg["total"] = round(leg["total"] + dp, 2)
    return leg


def book_fill(book, cfg, tsym, side, qty, price, product, exchange="NSE", inst=None, extra_charges=0.0):
    """Apply one fill to a book {cash_inr, positions}. Long and short, partial closes,
    flips. Returns {"charges": leg dict, "realised": None or {gross, charges, net, qty, entry, exit}}."""
    inst = dict(inst or {"type": "EQ"})
    itype = str(inst.get("type") or "EQ").upper()
    side = side.lower()
    if side not in ("buy", "sell"):
        raise TicketError("side must be buy or sell")
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    product = product.upper()
    segment = M.segment_for(product, itype)
    ch = charges_for(segment, side, price, qty, exchange, cfg)
    total_ch = round(ch["total"] + float(extra_charges or 0), 2)
    key = "%s|%s" % (tsym.upper(), product)
    pos = book["positions"].get(key) or {"qty": 0.0, "avg_price": 0.0, "product": product, "exchange": exchange,
                                         "instrument": inst, "charges_open": 0.0}
    signed = qty if side == "buy" else -qty
    held = float(pos["qty"])
    realised = None
    fut = itype == "FUT"
    if held == 0 or (held > 0) == (signed > 0):
        new_q = held + signed
        pos["avg_price"] = round((abs(held) * pos["avg_price"] + qty * price) / abs(new_q), 4)
        pos["qty"] = new_q
        pos["charges_open"] = round(pos.get("charges_open", 0.0) + total_ch, 2)
        if not fut:
            book["cash_inr"] = round(book["cash_inr"] - signed * price - total_ch, 2)
        else:
            book["cash_inr"] = round(book["cash_inr"] - total_ch, 2)
    else:
        close_q = min(abs(held), qty)
        direction = 1 if held > 0 else -1
        gross = round((price - pos["avg_price"]) * close_q * direction, 2)
        share = pos.get("charges_open", 0.0) * close_q / abs(held)
        close_ch = total_ch * close_q / qty
        net = round(gross - share - close_ch, 2)
        realised = {"qty": close_q, "entry": pos["avg_price"], "exit": price, "gross": gross,
                    "charges": round(share + close_ch, 2), "net": net, "side": "long" if held > 0 else "short"}
        pos["charges_open"] = round(pos.get("charges_open", 0.0) - share, 2)
        left = held + signed
        if not fut:
            book["cash_inr"] = round(book["cash_inr"] - signed * price - total_ch, 2)
        else:
            book["cash_inr"] = round(book["cash_inr"] + gross - total_ch, 2)
        if abs(left) < 1e-9:
            pos = None
        elif (left > 0) != (held > 0):   # flipped: the rest opens a new position
            pos.update(qty=left, avg_price=price, charges_open=round(total_ch - close_ch, 2))
        else:
            pos["qty"] = left
    if pos is None:
        book["positions"].pop(key, None)
    else:
        book["positions"][key] = pos
    return {"charges": ch, "total_charges": total_ch, "realised": realised, "key": key}


# ---------------------------------------------------------------- commands

def _clock(a):
    now = M.parse_now(getattr(a, "now", None))
    return now, now.date().isoformat()


def _load(a):
    doc = M.read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = M.load_config(a.config)
    risk = M.read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. " + M.NOT_ADVICE)
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv, _ = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    late = time_gate(order, rv, cfg, now)
    if late:
        raise TicketError("order %d cannot be approved now: %s" % (a.index, late))
    batch = set()
    for o in orders:
        batch.update({tsym_of(o), str(o.get("symbol") or "").upper()})
    ok, why = parse_yes(a.reply, order, batch - {""}, rv)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, gross, stop = max_loss(order, rv)
    inst, itype, fno = instrument(order)
    rec = {"at": iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "tradingsymbol": tsym_of(order), "symbol": str(order.get("symbol") or tsym_of(order)).upper(),
           "exchange": order.get("exchange", "NSE"), "side": str(order.get("side")).upper(),
           "qty": int(_f(order.get("qty")) or 0), "product": order.get("product"), "instrument": inst,
           "kind": (rv or {}).get("kind", "entry"), "max_loss": loss, "stop": stop, "plain": plain(order, rv),
           "status": "approved", "reply": a.reply, "paper": bool(cfg.get("paper", True)),
           "broker": cfg.get("broker", "zerodha")}
    state.setdefault("orders", []).append(rec)
    save_state(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, rv)))
    if cfg.get("paper", True):
        print("Paper mode: now send it to the built-in paper broker:")
        print("  python3 paper.py place --index %d" % a.index)
    else:
        print("LIVE: place exactly this with your broker's order tool (it must be set to Needs approval):")
        print(json.dumps(kite_params(order), indent=2))
        print("(Zerodha Kite fields; Upstox/Dhan mappings are on the ticket. %s.)" % MAP_NOTE)
        print("After the entry fills, place the stop-loss and the target exits from the ticket.")
        print("Then: ticket.py result --index %d --status placed|denied|failed" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "tradingsymbol": tsym_of(order), "side": str(order.get("side")).upper(), "plain": plain(order),
           "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    save_state(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def apply_fill(a_portfolio, cfg, tsym, side, qty, price, product, exchange, inst, date):
    pf = M.read_json(a_portfolio, "portfolio", required=False)
    if not isinstance(pf, dict):
        cap = _f(cfg.get("capital_inr"))
        if not cap:
            raise TicketError("no %s yet and no capital_inr in the config to start it from" % a_portfolio)
        pf = new_book(cap)
    pf.setdefault("positions", {})
    res = book_fill(pf, cfg, tsym, side, qty, price, product, exchange, inst)
    pf["updated"] = date
    M.write_json(a_portfolio, pf)
    return pf, res


def log_fill(state_path, now, today, tsym, side, qty, price, product, res, paper=False, extra=None):
    state = load_state(state_path)
    rec = {"at": iso(now), "date": today, "status": "filled", "tradingsymbol": tsym.upper(),
           "side": side.upper(), "qty": int(round(float(qty))), "price": float(price), "product": product.upper(),
           "charges": res["total_charges"], "paper": paper,
           "plain": "%s %s %s at %s" % (side.upper(), _num(qty), tsym.upper(), _px(price))}
    if res.get("realised"):
        rec["realised_pnl"] = res["realised"]["net"]
    rec.update(extra or {})
    state.setdefault("orders", []).append(rec)
    save_state(state_path, state)


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": iso(now), "date": today, "index": a.index, "fingerprint": fp}
    for k in ("tradingsymbol", "symbol", "exchange", "side", "qty", "product", "instrument", "kind", "max_loss",
              "stop", "plain", "paper"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    new["status"] = a.status
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    save_state(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        inst, _, _ = instrument(order)
        pf, res = apply_fill(a.portfolio, cfg, rec["tradingsymbol"], rec["side"], a.filled_qty, a.filled_price,
                             rec.get("product") or "MIS", rec.get("exchange", "NSE"), inst, today)
        log_fill(a.state, now, today, rec["tradingsymbol"], rec["side"], a.filled_qty, a.filled_price,
                 rec.get("product") or "MIS", res)
        print("Added the fill to %s (charges %s)." % (a.portfolio, M.inr(res["total_charges"])))
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --tradingsymbol %s --side %s --qty Q --price P --product %s"
              % (rec["tradingsymbol"], rec["side"].lower(), rec.get("product")))
    return 0


def cmd_fill(a):
    cfg = M.load_config(a.config)
    now = M.parse_now(a.now)
    date = a.date or now.date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-28")
    tsym = (a.tradingsymbol or "").upper()
    inst = {"type": a.instrument.upper()}
    if a.lot_size:
        inst["lot_size"] = a.lot_size
    pf, res = apply_fill(a.portfolio, cfg, tsym, a.side, a.qty, a.price, a.product, a.exchange, inst, date)
    log_fill(a.state, now, date, tsym, a.side, a.qty, a.price, a.product, res)
    msg = "Recorded %s %s %s %s at %s on %s in %s (charges %s)." % (a.side, a.qty, tsym, a.product.upper(), a.price,
                                                                   date, a.portfolio, M.inr(res["total_charges"]))
    if res["realised"]:
        msg += " Realised %s net of charges." % M.inr(res["realised"]["net"])
    print(msg + " Cash now %s." % M.inr(pf["cash_inr"]))
    return 0


def _positions_for(cfg, a):
    """Positions from the paper ledger (paper mode) or trader/portfolio.json."""
    if cfg.get("paper", True):
        led = M.read_json(a.ledger, "paper ledger", required=False)
        if isinstance(led, dict):
            return led.get("positions") or {}, a.ledger
    pf = M.read_json(a.portfolio, "portfolio", required=False)
    if isinstance(pf, dict):
        return pf.get("positions") or {}, a.portfolio
    return {}, a.portfolio


def cmd_flatten(a):
    cfg = M.load_config(a.config)
    tsym = a.tradingsymbol.strip().upper()
    now, today = _clock(a)
    positions, source = _positions_for(cfg, a)
    matches = [(k, p) for k, p in positions.items() if k.split("|")[0] == tsym and
               (not a.product or k.split("|")[-1] == a.product.upper())]
    if a.qty is not None:
        qty = int(a.qty)
        if qty <= 0:
            raise TicketError("--qty wants the number of units you hold, like --qty 42")
        if matches:
            key, pos = matches[0]
        else:
            key, pos = "%s|%s" % (tsym, (a.product or "MIS").upper()), {"qty": qty, "product": (a.product or "MIS").upper()}
        held = float(pos.get("qty") or qty)
    else:
        if not matches:
            raise TicketError("nothing to flatten: no %s position in %s. Check your broker's positions and pass "
                              "--qty (and --product) if you do hold some" % (tsym, source))
        if len(matches) > 1:
            raise TicketError("%s is held under more than one product (%s): pass --product"
                              % (tsym, ", ".join(k.split("|")[-1] for k, _ in matches)))
        key, pos = matches[0]
        held = float(pos.get("qty") or 0)
        qty = int(abs(held))
    product = str(pos.get("product") or key.split("|")[-1]).upper()
    inst = dict(pos.get("instrument") or {"type": "EQ"})
    itype = str(inst.get("type") or "EQ").upper()
    fno = itype in ("CE", "PE", "FUT")
    side = "SELL" if held > 0 else "BUY"
    order = {"exchange": pos.get("exchange", "NSE"), "symbol": str(inst.get("underlying") or tsym).upper(),
             "tradingsymbol": tsym, "instrument": inst, "side": side, "qty": qty,
             "lots": (qty // int(inst["lot_size"])) if (fno and inst.get("lot_size")) else None,
             "product": product, "order_type": "MARKET", "price": None, "trigger_price": None, "validity": "DAY",
             "variety": "regular", "intent": "exit"}
    if fno:
        px = _f(a.price)
        if px is None:
            pdoc = M.read_json(a.prices, "prices", required=False) or {}
            q = (pdoc.get("quotes") or {}).get(tsym) if isinstance(pdoc, dict) else None
            px = _f((q or {}).get("price"))
        if px is None:
            raise TicketError("an F&O square-off is a LIMIT order (brokers restrict market orders in options): "
                              "pass --price with the current premium/price")
        tick = M.tick_size(px, "options" if itype in ("CE", "PE") else "futures")
        lim = px * (0.99 if side == "SELL" else 1.01)
        order.update(order_type="LIMIT", price=max(tick, M.round_tick(lim, tick, "down" if side == "SELL" else "up")))
    flat_by = cfg.get("flat_by") or "15:10"
    late = now.time() >= M.parse_hhmm(flat_by)
    order["why"] = ("square off %s %s by %s IST (flat_by): closes %d %s from %s%s"
                    % (product, tsym, flat_by, qty, "long" if held > 0 else "short", source,
                       "; a marketable LIMIT 1% through the price" if fno else ""))
    old = M.read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": iso(now), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Square-off ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (tsym, today), "orders": [order]}
    M.write_json(a.orders, doc)
    print("Flatten %s: %s." % (tsym, entry_words(order)))
    if late:
        print("It is %s IST, already past your flat_by %s: square off now." % (M.hm(now.time()), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    print("Next:")
    print("  1. trader-risk: check.py. An order that only closes what you hold is never blocked by the day limits.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if cfg.get("paper", True):
        print("  3. paper.py place --index 1: the paper broker cancels the open stop-loss and target first.")
    else:
        print("  3. Cancel the open stop-loss and target orders (or the GTT) at the broker first, or they can fill")
        print("     after the square-off and open a new position. Then place the ticket, then ticket.py result.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + str(r["broker_order_id"])) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % M.inr(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        pnl = (" realised %s" % M.inr(r["realised_pnl"])) if r.get("realised_pnl") is not None else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, pnl, extra,
                                          " (paper)" if r.get("paper") else ""))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker, India). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default=M.CONFIG_FILE)
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-28 09:50\"")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened at a live broker")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a live fill in trader/portfolio.json")
    p.add_argument("--tradingsymbol", "--symbol", dest="tradingsymbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True, type=float)
    p.add_argument("--price", required=True, type=float)
    p.add_argument("--product", default="MIS", choices=["MIS", "CNC", "NRML", "mis", "cnc", "nrml"])
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    p.add_argument("--instrument", default="EQ", choices=["EQ", "CE", "PE", "FUT"])
    p.add_argument("--lot-size", type=int)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default=M.CONFIG_FILE)
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a square-off ticket that closes a position by flat_by")
    p.add_argument("--tradingsymbol", "--symbol", dest="tradingsymbol", required=True)
    p.add_argument("--product", help="MIS / CNC / NRML when the symbol is held under more than one")
    p.add_argument("--qty", help="units you hold (default: from the paper ledger or trader/portfolio.json)")
    p.add_argument("--price", help="F&O: the current price; the square-off is a marketable LIMIT")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--ledger", default="trader/paper/ledger.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--config", default=M.CONFIG_FILE)
    p.add_argument("--now")
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--now")
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
