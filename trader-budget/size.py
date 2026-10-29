#!/usr/bin/env python3
"""Position sizing for trader-budget (India edition). Standard library only, no network.

Sizes ONE trade from trader/plan.json (written by trader-exit or trader-fno)
in rupees, so the loss AT THE STOP INCLUDING ALL CHARGES (brokerage, STT,
exchange charges, SEBI fee, stamp duty, GST, DP on a delivery sell) stays
inside max_loss_per_trade_inr. Writes one broker-neutral entry order with its
stop-loss and target exit orders to trader/orders.json. It never places
anything: trader-risk checks the order and trader-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit 1171.00 --write          # a LIMIT order at 1171.00 (default: at the plan's entry)
    python3 size.py --market                         # a MARKET order (sized at the worse of plan and fresh price)
    python3 size.py --amo                            # mark it an after-market order (AMO)
    python3 size.py --now "2026-10-28 09:50" --json  # pin the clock (IST)

How many units:

    qty = floor(max_loss_per_trade_inr / (risk per unit + charges per unit at the stop)),
          checked again with the exact charges for that qty (charges are not linear),
    then bounded by
      position cap   capital_inr x max_position_pct / value per unit
      max order      max_order_inr / value per unit
      cash / margin  (cash minus the cash buffer) / margin per unit
                     CNC and option buying: full value; MIS: full value, or
                     intraday_margin_pct (20% = SEBI's 5x cap) with use_intraday_leverage;
                     futures: futures_margin_pct of the value (an ESTIMATE: check
                     your broker's SPAN calculator)
      premium cap    option buying: max_premium_per_trade_inr / premium
      max lots       F&O: max_lots_per_trade x lot size
    F&O is sized in whole LOTS. If one lot already breaks the max loss, it refuses.

Option selling and futures are refused unless allow_option_selling /
allow_futures are true in the config. The US weights mode is not part of the
India edition.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

FRESH_MINUTES = 15
EPS = 1e-9
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce",
               "vwap": "VWAP reclaim", "gap": "gap trade", "option": "option trade"}
OPTION_TYPES = ("CE", "PE")
EXTRA_DEFAULTS = {"futures_margin_pct": 15, "sl_limit_buffer_pct": 2.0}


class SizeError(M.MarketError):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        if isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(x):
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else ("%.2f" % x).rstrip("0").rstrip(".")


def _px(x):
    return "%.2f" % float(x)


def parse_time(text):
    """A quote time -> naive IST datetime, or None. Accepts ISO with an offset
    (+05:30 or Z) or a plain 'YYYY-MM-DD HH:MM' taken as IST."""
    if not text:
        return None
    t = str(text).strip()
    try:
        if t.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", t):
            dt = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
            return dt.astimezone(M.IST).replace(tzinfo=None)
        return M.parse_now(t)
    except (ValueError, M.MarketError):
        return None


def quote_of(pdoc, *keys):
    """(price, time IST or None) from trader/prices.json. Contract shape:
    {"at": ..., "quotes": {SYM: {"price", "time"}}}; an older {"prices": {SYM: p}} is read too."""
    if not isinstance(pdoc, dict):
        return None, None
    quotes = pdoc.get("quotes")
    for k in keys:
        if not k:
            continue
        k = str(k).upper()
        if isinstance(quotes, dict) and isinstance(quotes.get(k), dict):
            q = quotes[k]
            p = _f(q.get("price"))
            if p and p > 0:
                return p, parse_time(q.get("time")) or parse_time(pdoc.get("at"))
        old = pdoc.get("prices")
        if isinstance(old, dict) and _f(old.get(k)):
            det = ((pdoc.get("detail") or {}).get(k)) or {}
            return _f(old[k]), parse_time(det.get("time")) or parse_time(pdoc.get("as_of") or pdoc.get("at"))
    return None, None


def fresh(t, now, minutes=FRESH_MINUTES):
    if t is None:
        return False
    age = (now - t).total_seconds()
    return -60 <= age <= minutes * 60


def config_with_defaults(cfg):
    c = dict(M.DEFAULT_CONFIG)
    c.update(EXTRA_DEFAULTS)
    c.update(cfg or {})
    return c


def tick_segment(itype):
    return "options" if itype in OPTION_TYPES else ("futures" if itype == "FUT" else "equity")


def sl_limit_price(trigger, long, c, tick, risk=None):
    """F&O stop-loss as SL (stop-limit): the limit sits a buffer beyond the trigger,
    because brokers restrict SL-M and market orders in options. The buffer is
    sl_limit_buffer_pct of the price, but never more than half the planned risk, and
    the size is computed at this limit (the worst price the stop can fill at)."""
    buf = trigger * float(c.get("sl_limit_buffer_pct") or 2.0) / 100.0
    if risk:
        buf = min(buf, 0.5 * risk)
    buf = max(2 * tick, buf)
    if long:   # the exit is a sell: limit below the trigger
        return max(tick, M.round_tick(trigger - buf, tick, "down"))
    return M.round_tick(trigger + buf, tick, "up")


# ---------------------------------------------------------------- the sizing (pure)

def size_trade(plan, cfg, limit=None, market=False, price=None, cash=None, today=None, amo=False):
    """One plan + your rules -> the sizing (a dict). qty may be 0; then "refusal" says why.

    limit   a LIMIT price (None = at the plan's entry unless market is True)
    market  a MARKET order: sized at the worse of the plan's entry and a FRESH price
    price   a fresh latest price (only used for a MARKET order)
    cash    the cash in trader/portfolio.json, if there is one
    today   IST date 'YYYY-MM-DD'; a plan for another day is refused"""
    c = config_with_defaults(cfg)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym or not M.valid_symbol(sym):
        raise SizeError("the plan has no valid symbol (run trader-exit)")
    inst = dict(plan.get("instrument") or {"type": "EQ"})
    itype = str(inst.get("type") or "EQ").upper()
    if itype not in ("EQ", "CE", "PE", "FUT"):
        raise SizeError("unknown instrument type %r (EQ, CE, PE or FUT)" % itype)
    fno = itype != "EQ"
    exchange = str(plan.get("exchange") or (M.exchange_of(inst.get("underlying") or sym) if fno else None)
                   or c.get("exchange") or "NSE").upper()
    if exchange not in ("NSE", "BSE"):
        raise SizeError("exchange must be NSE or BSE, got %r" % exchange)
    side = str(plan.get("side") or "buy").strip().lower()
    if side not in ("buy", "sell"):
        raise SizeError("the plan's side must be buy or sell, got %r" % side)
    long = side == "buy"
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s (IST): make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit): every order here carries "
                            "a stop-loss and a target" % name)
    product = str(plan.get("product") or ("MIS" if fno else c.get("default_product") or "MIS")).upper()
    if itype == "EQ" and product not in ("MIS", "CNC"):
        raise SizeError("an equity order is MIS (intraday) or CNC (delivery), not %s" % product)
    if fno and product not in ("MIS", "NRML"):
        raise SizeError("an F&O order is MIS (intraday) or NRML (carry forward), not %s" % product)

    underlying = str(inst.get("underlying") or sym).upper()
    lot = 1
    if fno:
        lot = int(_f(inst.get("lot_size")) or M.lot_size(underlying) or 0)
        if lot <= 0:
            raise SizeError("no lot size for %s: add instrument.lot_size to the plan or run "
                            "market.py lots --refresh" % underlying)
        if not inst.get("expiry"):
            raise SizeError("an F&O plan needs instrument.expiry")
    tradingsymbol = str(inst.get("tradingsymbol") or "").upper()
    if not tradingsymbol:
        if itype in OPTION_TYPES:
            if inst.get("strike") is None:
                raise SizeError("an option plan needs instrument.strike")
            tradingsymbol = M.nse_option_symbol(underlying, inst["expiry"], float(inst["strike"]), itype)
        elif itype == "FUT":
            e = datetime.date.fromisoformat(str(inst["expiry"]))
            tradingsymbol = "%s%s%sFUT" % (underlying, e.strftime("%y"), e.strftime("%b").upper())
        else:
            tradingsymbol = sym
    inst["type"] = itype
    if fno:
        inst.setdefault("underlying", underlying)
        inst["lot_size"] = lot
        inst["tradingsymbol"] = tradingsymbol

    # policy: what this pack does not size unless your config says so
    policy = []
    names = {sym, underlying, tradingsymbol}
    if names & {str(b).upper() for b in (c.get("blocked") or [])}:
        policy.append("%s is on your blocked list" % sym)
    if itype == "EQ" and not long:
        if product == "CNC":
            policy.append("a CNC (delivery) short is not possible: selling shares you do not hold ends in "
                          "the exchange's auction with a penalty. Shorts are intraday MIS only")
        elif not c.get("allow_short"):
            policy.append("short selling is off (\"allow_short\": false)")
    if itype == "EQ" and product == "CNC" and not c.get("allow_swing", True):
        policy.append("CNC (delivery, held overnight) is off (\"allow_swing\": false)")
    if itype in OPTION_TYPES and long and not c.get("allow_option_buying", True):
        policy.append("option buying is off (\"allow_option_buying\": false)")
    if itype in OPTION_TYPES and not long and not c.get("allow_option_selling"):
        policy.append("option selling is off (\"allow_option_selling\": false). A sold option can lose many "
                      "times the premium, needs SPAN + exposure margin of lakhs and is how most F&O accounts "
                      "blow up")
    if itype == "FUT" and not c.get("allow_futures"):
        policy.append("futures are off (\"allow_futures\": false). One NIFTY lot moves about Rs 65 per point; "
                      "a 1% day is over Rs 16,000 on one lot")
    if fno and underlying in {str(b).upper() for b in (c.get("fno_ban") or [])}:
        policy.append("%s is in the F&O ban period: no new F&O positions" % underlying)

    # prices on the tick grid
    tseg = tick_segment(itype)
    if market:
        cands = [entry_plan] + ([price] if price else [])
        ref = max(cands) if long else min(cands)
        ref_from = "price" if (price and abs(ref - entry_plan) > EPS and ref == price) else "plan"
        order_type = "MARKET"
    elif limit is not None:
        ref, ref_from, order_type = float(limit), "limit", "LIMIT"
    else:
        ref, ref_from, order_type = entry_plan, "plan", "LIMIT"
    plan_tick = _f(plan.get("tick")) if itype == "EQ" else None

    def tk(p):
        # each price on its own tick band (a stop at 251 is on the 0.05 grid even if the entry is at 249.50)
        return max(plan_tick or 0.0, M.tick_size(p, tseg))
    tick = tk(ref)
    ref = M.round_tick(ref, tick)
    stop = M.round_tick(stop, tk(stop), "down" if long else "up")
    stop = M.round_tick(stop, tk(stop), "down" if long else "up")
    target = M.round_tick(target, tk(target), "down" if long else "up")
    if long and stop >= ref - EPS:
        raise SizeError("the stop %s is not below the entry %s; a buy needs its stop underneath" % (_px(stop), _px(ref)))
    if not long and stop <= ref + EPS:
        raise SizeError("the stop %s is not above the entry %s; a short needs its stop above" % (_px(stop), _px(ref)))
    if (long and target <= ref + EPS) or (not long and target >= ref - EPS):
        raise SizeError("the target %s is not %s the entry %s: the move has already happened. Skip it or make "
                        "a new plan" % (_px(target), "above" if long else "below", _px(ref)))
    risk = round(abs(ref - stop), 2)
    reward = round(abs(target - ref), 2)

    segment = M.segment_for(product, itype)
    broker = str(c.get("broker") or "zerodha").lower()

    def rt(q, exit_px):
        if long:
            return M.round_trip(segment, ref, exit_px, q, exchange, broker, c)
        return M.round_trip(segment, exit_px, ref, q, exchange, broker, c)

    # the worst price the stop can fill at: the SL limit for F&O stop-limits, else the trigger
    sl_px = sl_limit_price(stop, long, c, tk(stop), risk) if fno else stop
    risk_fill = round(abs(ref - sl_px), 2)

    def loss_with_costs(q):
        return round(risk_fill * q + rt(q, sl_px)["total"], 2) if q > 0 else 0.0

    capital = float(_f(c.get("capital_inr")) or 0)
    if capital <= 0:
        raise SizeError("no capital: set \"capital_inr\" in trader/config.json")
    buf_pct = float(_f(c.get("cash_buffer_pct")) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    cash_buffer = round(capital * buf_pct / 100.0, 2)
    available = capital - cash_buffer
    cash_note = "capital %s minus the %s cash buffer" % (M.inr(capital, False), M.inr(cash_buffer, False))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (M.inr(cash), M.inr(cash_buffer, False))
    available = max(round(available, 2), 0.0)

    # money per unit
    notional_pu = ref
    if itype == "FUT":
        mpct = float(_f(c.get("futures_margin_pct")) or 15)
        margin_pu = ref * mpct / 100.0
        margin_basis = "futures margin ESTIMATE %s%% of the value (check your broker's SPAN calculator)" % _num(mpct)
    elif itype in OPTION_TYPES and long:
        margin_pu = ref
        margin_basis = "option premium paid in full"
    elif itype in OPTION_TYPES:
        margin_pu = ref * 10   # never used for a refused sell; a rough floor if the user allows selling
        margin_basis = "option SELLING margin is SPAN + exposure (lakhs per lot): check your broker's calculator"
    elif product == "MIS" and c.get("use_intraday_leverage"):
        imp = float(_f(c.get("intraday_margin_pct")) or 20)
        if imp < 20:
            imp = 20.0   # SEBI peak-margin rule: intraday leverage is capped at 5x (20%)
        margin_pu = ref * imp / 100.0
        margin_basis = "MIS %s%% of the value (use_intraday_leverage; SEBI caps intraday leverage at 5x)" % _num(imp)
    else:
        margin_pu = ref
        margin_basis = ("%s full value (no leverage)" % product)
    exposure_pu = notional_pu   # position cap and max order are on the full value (a futures lot is ~Rs 15-20 lakh)

    def units(rupees, per):
        if per <= 0 or rupees <= 0:
            return 0
        n = int(math.floor(rupees / per + EPS))
        return (n // lot) * lot

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if not max_loss or max_loss <= 0:
        raise SizeError("set \"max_loss_per_trade_inr\" in trader/config.json: the size comes from it")
    q = units(max_loss, risk_fill)
    guard = 0
    while q > 0 and loss_with_costs(q) > max_loss + 0.005 and guard < 1000:
        per = risk_fill + rt(q, sl_px)["total"] / float(q)
        q = min(q - lot, units(max_loss, per))
        guard += 1
    q_loss = max(q, 0)
    cpu = round(rt(q_loss, stop)["total"] / float(q_loss), 2) if q_loss else None
    limits.append({"name": "max loss", "inr": max_loss, "qty": q_loss,
                   "how": "%s / (risk %s + charges %s a unit at the stop)"
                          % (M.inr(max_loss, False), _px(risk), _px(cpu) if cpu is not None else "n/a")})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(capital * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "qty": units(cap, exposure_pu),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), M.inr(capital, False), M.inr(cap, False),
                                                       _px(exposure_pu))})
    mo = _f(c.get("max_order_inr"))
    if mo:
        limits.append({"name": "max order", "inr": mo, "qty": units(mo, exposure_pu),
                       "how": "%s / %s" % (M.inr(mo, False), _px(exposure_pu))})
    limits.append({"name": "cash/margin", "inr": available, "qty": units(available, margin_pu),
                   "how": "%s = %s / margin %s a unit" % (cash_note, M.inr(available, False), _px(margin_pu))})
    if itype in OPTION_TYPES and long and _f(c.get("max_premium_per_trade_inr")):
        mp = _f(c["max_premium_per_trade_inr"])
        limits.append({"name": "premium cap", "inr": mp, "qty": units(mp, ref),
                       "how": "%s / premium %s" % (M.inr(mp, False), _px(ref))})
    if fno and _f(c.get("max_lots_per_trade")):
        ml = int(_f(c["max_lots_per_trade"]))
        limits.append({"name": "max lots", "inr": None, "qty": ml * lot,
                       "how": "%d lot%s x %d" % (ml, "" if ml == 1 else "s", lot)})

    qty = max(0, min(l["qty"] for l in limits))
    bound = [l["name"] for l in limits if l["qty"] == qty][0]
    for l in limits:
        l["binds"] = l["name"] == bound
    rt_stop = rt(qty, sl_px) if qty else None
    rt_target = rt(qty, target) if qty else None
    gross_loss = round(risk_fill * qty, 2)
    loss_c = loss_with_costs(qty)
    net_reward = round(reward * qty - rt_target["total"], 2) if qty else 0.0
    net_rr = round(net_reward / loss_c, 2) if qty and loss_c > 0 else None
    notional = round(ref * qty, 2)
    margin = round(margin_pu * qty, 2)

    refusal = None
    if policy:
        refusal = policy[0] + ". No order."
    elif qty == 0:
        if bound == "max loss":
            one = lot
            one_loss = loss_with_costs(one)
            what = ("one lot (%d units)" % lot) if fno else "one share"
            refusal = ("%s risks %s at the stop including charges (entry %s, stop %s), more than your %s max loss "
                       "per trade. No order. Skip it, or plan a closer stop." % (what, M.inr(one_loss), _px(ref),
                                                                                 _px(stop), M.inr(max_loss, False)))
        else:
            lim = [l for l in limits if l["name"] == bound][0]
            refusal = ("not even %s fits your %s limit (%s). No order."
                       % ("one lot" if fno else "one share", bound, lim["how"]))
    elif _f(c.get("min_reward_risk")) and (net_rr is None or net_rr < _f(c["min_reward_risk"]) - EPS):
        refusal = ("net reward:risk after charges is %s (reward %s minus charges %s, against a loss of %s at the "
                   "stop), under your minimum %s, so trader-risk would block it. No order."
                   % ("n/a" if net_rr is None else "%.2f" % net_rr, M.inr(reward * qty), M.inr(rt_target["total"]),
                      M.inr(loss_c), _num(c["min_reward_risk"])))
    elif notional < float(_f(c.get("min_order_inr")) or 0) - 0.005:
        refusal = ("%s is under your %s minimum order. No order." % (M.inr(notional), M.inr(c["min_order_inr"], False)))

    return {
        "symbol": sym, "underlying": underlying, "tradingsymbol": tradingsymbol, "exchange": exchange,
        "instrument": inst, "type": itype, "fno": fno, "lot": lot, "side": side, "product": product,
        "segment": segment, "broker": broker, "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": entry_plan, "entry": ref, "entry_from": ref_from,
        "order_type": order_type, "amo": bool(amo), "tick": tick, "stop": stop, "target": target,
        "risk_per_unit": risk, "reward_per_unit": reward, "stop_label": plan.get("stop_label", ""),
        "target_label": plan.get("target_label", ""), "capital": capital, "cash_buffer": cash_buffer,
        "cash_available": available, "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty,
        "lots": (qty // lot) if fno else None, "bound_by": bound, "notional": notional, "margin": margin,
        "margin_basis": margin_basis, "max_loss": gross_loss, "max_loss_with_costs": loss_c,
        "costs_per_unit_at_stop": round(rt_stop["total"] / qty, 2) if qty else None,
        "costs_at_stop": rt_stop["total"] if qty else None, "costs_at_target": rt_target["total"] if qty else None,
        "breakeven_per_unit": rt_target["breakeven_per_unit"] if qty else None,
        "gross_reward_risk": round(reward / risk, 2) if risk else None, "net_reward": net_reward,
        "net_reward_risk": net_rr, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "circuit": plan.get("circuit"), "series": plan.get("series"), "refusal": refusal,
        "sl_order_type": "SL" if fno else "SL-M",
        "sl_price": sl_px if fno else None, "risk_at_sl_limit": risk_fill if fno else None,
    }


def build_order(s):
    """The one proposed order: broker-neutral fields (CONTRACTS.md), its exits, costs, margin and why."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    long = s["side"] == "buy"
    exit_side = "SELL" if long else "BUY"
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "trade")
    bound_words = {"max loss": "your %s max loss per trade (charges included)" % M.inr(s["max_loss_per_trade_inr"], False),
                   "position cap": "your position cap", "max order": "your max order",
                   "cash/margin": "the cash after your buffer", "premium cap": "your premium cap per trade",
                   "max lots": "your max lots per trade"}[s["bound_by"]]
    units_word = ("%d lot%s (%d units)" % (s["lots"], "" if s["lots"] == 1 else "s", s["qty"])) if s["fno"] \
        else "%d shares" % s["qty"]
    why = ("%s on %s: %s, bound by %s. Loss at the stop %s + charges %s = %s."
           % (setup, s["tradingsymbol"], units_word, bound_words, M.inr(s["max_loss"]), M.inr(s["costs_at_stop"]),
              M.inr(s["max_loss_with_costs"])))
    if s["product"] in ("CNC", "NRML"):
        exit_how = ("Place the stop-loss and the target together as a GTT OCO (one cancels the other) after the "
                    "entry fills. Bracket and cover orders are discontinued at most Indian brokers.")
    else:
        exit_how = ("Place the stop-loss and the target as two separate orders right after the entry fills. When "
                    "one fills, cancel the other. Anything still open at the broker's square-off time is closed by "
                    "the broker for a fee; be flat by %s." % s["flat_by"])
    if s["fno"]:
        exit_how += (" F&O stop is SL (stop-limit) with a limit buffer, because brokers restrict SL-M and market "
                     "orders in options: if the price gaps past the limit, the stop does not fill.")
    o = {
        "exchange": s["exchange"], "symbol": s["underlying"] if s["fno"] else s["symbol"],
        "tradingsymbol": s["tradingsymbol"], "instrument": s["instrument"],
        "side": "BUY" if long else "SELL", "qty": int(s["qty"]), "lots": s["lots"], "product": s["product"],
        "order_type": s["order_type"], "price": s["entry"] if s["order_type"] == "LIMIT" else None,
        "trigger_price": None, "validity": "DAY", "variety": "amo" if s["amo"] else "regular",
        "exits": {
            "stop_loss": {"side": exit_side, "order_type": s["sl_order_type"], "trigger_price": s["stop"],
                          "price": s["sl_price"]},
            "target": {"side": exit_side, "order_type": "LIMIT", "price": s["target"]},
            "how": exit_how,
        },
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"], "risk_per_share": s["risk_per_unit"],
                 "max_loss": s["max_loss"], "max_loss_with_costs": s["max_loss_with_costs"],
                 "net_reward_risk": s["net_reward_risk"], "setup": s["setup"], "date": s["date"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"], "entry_from": s["entry_from"]},
        "costs": {"round_trip_at_target": s["costs_at_target"], "round_trip_at_stop": s["costs_at_stop"],
                  "per_unit_at_stop": s["costs_per_unit_at_stop"], "broker": s["broker"], "segment": s["segment"]},
        "margin": {"required": s["margin"], "basis": "%s of %s" % (s["margin_basis"], M.inr(s["notional"])),
                   "notional": s["notional"]},
    }
    if s["type"] == "FUT":
        o["margin"]["note"] = "estimate only: check your broker's SPAN calculator before you trade"
    if s.get("circuit"):
        o["circuit"] = s["circuit"]
    if s.get("series"):
        o["series"] = s["series"]
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "trade")
    what = s["tradingsymbol"] + (" (%s, lot %d, expiry %s)" % (s["type"], s["lot"], s["instrument"].get("expiry"))
                                 if s["fno"] else "")
    lines.append("%s %s, %s, %s, entry %s%s" % (s["side"].upper(), what, setup, s["product"], _px(s["entry_plan"]),
                                                (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_px(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _px(s["target"]), (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "a fresh price worse than the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s, %s order, tick %s). Risk %s a unit." % (_px(s["entry"]), src, s["order_type"],
                                                                          _num(s["tick"]), _px(s["risk_per_unit"])))
    lines.append("")
    lines.append("Limits (%s; the smallest one wins):" % ("whole lots of %d" % s["lot"] if s["fno"] else "whole shares"))
    for l in s["limits"]:
        lines.append("  %-13s %-66s %6d%s" % (l["name"], l["how"], l["qty"], "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append(M.NOT_ADVICE)
        return "\n".join(lines)
    lines.append("Size: %s%s = %s at %s." % (_num(s["qty"]), (" (%d lot%s)" % (s["lots"], "" if s["lots"] == 1 else "s"))
                                           if s["fno"] else " shares", M.inr(s["notional"]), _px(s["entry"])))
    lines.append("Max loss at the stop: %s x %s = %s, plus charges %s = %s (limit %s)."
                 % (_num(s["qty"]), _px(s["risk_per_unit"]), M.inr(s["max_loss"]), M.inr(s["costs_at_stop"]),
                    M.inr(s["max_loss_with_costs"]), M.inr(s["max_loss_per_trade_inr"], False)))
    lines.append("At the target: %s gross minus charges %s = %s net. Net reward:risk %.2f (gross %.2f). Breakeven "
                 "move %s a unit." % (M.inr(s["reward_per_unit"] * s["qty"]), M.inr(s["costs_at_target"]),
                                      M.inr(s["net_reward"]), s["net_reward_risk"], s["gross_reward_risk"],
                                      M.inr(s["breakeven_per_unit"])))
    lines.append("Margin: %s (%s)." % (M.inr(s["margin"]), s["margin_basis"]))
    lines.append("A stop is an order, not a promise: in a fast market or a gap it fills worse than %s." % _px(s["stop"]))
    lines.append("")
    lines.append("Proposed orders (broker-neutral, nothing has been placed):")
    how = ("LIMIT %s" % _px(order["price"])) if order["order_type"] == "LIMIT" else "MARKET"
    lines.append("  entry   %s %d %s %s, %s, %s, %s" % (order["side"], order["qty"], order["tradingsymbol"], order["exchange"],
                                                     order["product"], how, order["variety"]))
    sl = order["exits"]["stop_loss"]
    lines.append("  stop    %s %s trigger %s%s" % (sl["side"], sl["order_type"], _px(sl["trigger_price"]),
                                                  (" limit %s" % _px(sl["price"])) if sl["price"] else ""))
    lines.append("  target  %s LIMIT %s" % (order["exits"]["target"]["side"], _px(order["exits"]["target"]["price"])))
    lines.append("  " + order["exits"]["how"])
    if s["product"] == "MIS" and s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets squared off (trader-alerts rings, "
                     "ticket.py flatten)." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append(M.NOT_ADVICE)
    return "\n".join(lines)


def write_orders(path, orders, cfg, source, now):
    doc = {
        "created_by": "trader-budget",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30",
        "status": "proposed",
        "paper": bool(config_with_defaults(cfg).get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order and you say yes to it "
                 "in the chat."),
        "source": source,
        "orders": orders,
    }
    M.write_json(path, doc)


# ---------------------------------------------------------------- main

def run(a):
    cfg = M.load_config(a.config)
    if a.capital is not None:
        cfg["capital_inr"] = a.capital
    now = M.parse_now(a.now)
    today = now.date().isoformat()
    plan = M.read_json(a.plan, "plan (run trader-exit first)")
    if a.limit is not None and a.market:
        raise SizeError("pick one: --limit or --market")
    limit = None
    if a.limit is not None and a.limit != "plan":
        limit = _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 1171.00")
    inst = (plan or {}).get("instrument") or {}
    keys = (inst.get("tradingsymbol"), (plan or {}).get("symbol"))
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and a.market:
        pdoc = M.read_json(ppath, "prices")
        p, t = quote_of(pdoc, *keys)
        if p is not None and fresh(t, now):
            price = p
            notes.append("Fresh price %s (at %s IST) taken into account for the MARKET order." % (_px(p), M.hm(t)))
        elif p is not None:
            notes.append("The price in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (ppath, ("from " + t.strftime("%Y-%m-%d %H:%M") + " IST") if t else "undated",
                            FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = M.read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash_inr")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, limit, a.market, price, cash, today, a.amo)
    if s["amo"]:
        notes.append("AMO: it reaches the exchange at the next open (pre-open 09:00-09:08 for equity). A gap can "
                     "fill it far from the plan.")
    if s["fno"] and M.is_expiry_day(s["underlying"], now.date()) and s["instrument"].get("expiry") == today:
        notes.append("Expiry day for this contract: premiums can collapse fast; trader-risk stops new F&O entries "
                     "after %s." % config_with_defaults(cfg).get("expiry_day_last_entry"))
    order = build_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["tradingsymbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2, default=str))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order (entry + stop-loss + target) to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size one trade in rupees, charges included (trader-budget, India).")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--config", default=M.CONFIG_FILE)
    ap.add_argument("--capital", type=float, help="override capital_inr from the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT order at the plan's entry (the default) or at PRICE")
    ap.add_argument("--market", action="store_true", help="a MARKET order instead of LIMIT")
    ap.add_argument("--amo", action="store_true", help="mark it an after-market order")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json", help="its cash_inr caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-28 09:50\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
