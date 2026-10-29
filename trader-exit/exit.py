#!/usr/bin/env python3
"""Exit Planner for trader-exit (India edition): sets the stop and the target
BEFORE you enter, rounds them to the exchange tick, prices the round trip
with Indian charges, and says when you must be flat. Standard library only.
It never places an order.

    python3 exit.py plan                                   # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-low --target 1.5R --write
    python3 exit.py plan --symbol RELIANCE --entry 1171.2 --or-high 1168 --or-low 1158.4
    python3 exit.py plan --stop atr:1.5 --offline RELIANCE.json    # ATR needs the session's bars
    python3 exit.py plan --stop vwap --target prev-day-high --offline RELIANCE.json
    python3 exit.py plan --symbol TCS --entry 3120.5 --setup level --stop pct:1 --product CNC
    python3 exit.py plan --instrument CE --underlying NIFTY --expiry 2026-11-03 --strike 22700 \\
        --entry 120 --stop 90 --target 2R               # option premiums, 1 lot
    python3 exit.py show                                   # the current trader/plan.json

Stops (rounded to the tick AWAY from the entry, so the risk is never understated):
  or-mid        the opening-range midpoint (the default for orb and gap entries)
  or-low        the opening-range low (a long); or-high for a short
  price:X, X    your price
  pct:X         X percent from the entry
  atr:K         K x the 14-bar ATR of the session's 5-minute bars to the entry bar
  vwap          one tick beyond the session VWAP at the entry (the default for vwap_reclaim)
  prev-day-low  one tick under the previous session's low (prev-day-high for a short)
Targets (rounded to the tick TOWARD the entry, so the reward is never overstated):
  2R (default), 1R, 1.5R ...   entry + R x risk
  price:X, X / pct:X / prev-day-high (prev-day-low for a short)
  With a known circuit band ("bands" in the config or --band) a target beyond
  the upper circuit (lower, for a short) is capped at the circuit.

Costs: the round trip at the target and at the stop with market.round_trip
(STT, exchange, SEBI, stamp, GST, brokerage, DP for delivery) for a reference
quantity: the shares your max_loss_per_trade_inr allows at this risk (capped
by max_order_inr), or 1 lot for F&O; --qty / --lots override. Reports
costs_per_share and net_reward_risk, and warns when charges eat more than 25%
of the reward.

flat_by: config flat_by (15:10), never at or after your broker's MIS auto
square-off (zerodha 15:25 equity / 15:26 F&O, others 15:15-15:20). CNC
(delivery) and NRML (F&O carry) have no flat_by but a valid_until date and an
overnight gap warning. On expiry day a stock option or future that is still
open is PHYSICALLY SETTLED (you must take or give delivery), so flat_by is
mandatory for it whatever the product.

Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
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

Err = M.MarketError
INTERVALS = {"1m": 60, "5m": 300}
ATR_BARS = 14
MIN_RISK_PCT = 0.05          # a risk under 0.05% of the price is inside the spread's noise
COST_WARN_PCT = 25.0         # warn when charges eat more than this share of the reward
DEFAULT_HOLD_DAYS = 5        # trading days a CNC / NRML plan stays valid
FNO = ("FUT", "CE", "PE")
NET_FACTORY = M.Net
NOTE = "A stop is a plan, not a guarantee: a gap, a circuit or a fast market can fill past it."
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def r2(x):
    return None if x is None else round(float(x) + 1e-9, 2) + 0.0


def num(x):
    return ("%.4f" % float(x)).rstrip("0").rstrip(".")


def ratio(x):
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def parse_clock(text, what):
    """'09:50', '9:50 AM', '2:05 PM' -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text or ""))
    if not m:
        raise Err("%s wants a time like 09:50 (IST), not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise Err("%s wants a time like 09:50 (IST), not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise Err("%s wants a time like 09:50 (IST), not %r" % (what, text))
    return datetime.time(h, mi)


# ---------------------------------------------------------------- rules

def parse_stop(text, long=True):
    t = str(text).strip().lower().replace("_", "-")
    if t in ("or-mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "low", "or-high", "high"):
        want = "or_low" if long else "or_high"
        got = "or_low" if "low" in t else "or_high"
        if got != want:
            raise Err("a %s's stop goes %s the entry: use --stop %s" % (
                "long" if long else "short", "under" if long else "above", want.replace("_", "-")))
        return got, None
    if t == "vwap":
        return "vwap", None
    if t in ("prev-day-low", "pdl", "prev-day-high", "pdh"):
        want = "prev_day_low" if long else "prev_day_high"
        got = "prev_day_low" if t in ("prev-day-low", "pdl") else "prev_day_high"
        if got != want:
            raise Err("a %s's stop goes %s the entry: use --stop %s" % (
                "long" if long else "short", "under" if long else "above", want.replace("_", "-")))
        return got, None
    m = re.match(r"^(?:(price|pct|atr):)?\s*(?:rs\.?\s*)?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise Err("--stop is or-mid, or-low/or-high, vwap, prev-day-low/high, price:X, pct:X or atr:K (got %r)"
                  % text)
    kind = m.group(1) or "price"
    val = float(m.group(2).replace(",", ""))
    if val <= 0:
        raise Err("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise Err("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text, long=True):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = float(m.group(1))
        if r <= 0:
            raise Err("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    low = t.lower().replace("_", "-")
    if low in ("prev-day-high", "pdh", "prev-day-low", "pdl"):
        want = "prev_day_high" if long else "prev_day_low"
        got = "prev_day_high" if low in ("prev-day-high", "pdh") else "prev_day_low"
        if got != want:
            raise Err("a %s's target is %s" % ("long" if long else "short", want.replace("_", "-")))
        return got, None
    m = re.match(r"^(?:(price|pct):)?\s*(?:rs\.?\s*)?([0-9][0-9,]*\.?[0-9]*)$", low)
    if m:
        kind = m.group(1) or "price"
        val = float(m.group(2).replace(",", ""))
        if val <= 0:
            raise Err("--target %s needs a number above 0" % kind)
        return kind, val
    raise Err("--target is 2R, 1R, 1.5R ..., price:X, pct:X or prev-day-high/low (got %r)" % text)


def r_label(r):
    n = int(r) if float(r).is_integer() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, side="buy", segment="equity",
            or_high=None, or_low=None, atr=None, atr_bars=None, vwap=None, prev_high=None, prev_low=None,
            limits=None):
    """Stop, target and risk on the exchange tick. Raises MarketError with the reason."""
    long = side == "buy"
    if entry is None or float(entry) <= 0:
        raise Err("the entry price must be above Rs 0")
    e = M.round_tick(float(entry), mode="up" if long else "down", segment=segment)
    away = "down" if long else "up"
    toward = "down" if long else "up"
    sgn = 1 if long else -1
    detail = ""
    if stop_rule in ("or_mid", "or_low", "or_high"):
        if or_high is None or or_low is None:
            raise Err("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = float(or_high), float(or_low)
        if oh <= ol:
            raise Err("the opening-range high %s must be above its low %s" % (M.inr(oh), M.inr(ol)))
        if stop_rule == "or_mid":
            raw, label = (oh + ol) / 2.0, "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (M.inr(oh), M.inr(ol), num((oh + ol) / 2.0))
        elif stop_rule == "or_low":
            raw, label = ol, "opening-range low"
        else:
            raw, label = oh, "opening-range high"
    elif stop_rule == "price":
        raw, label = float(stop_param), "your price"
    elif stop_rule == "pct":
        raw = e * (1 - sgn * stop_param / 100.0)
        label = "%s%% %s the entry" % (num(stop_param), "under" if long else "over")
        detail = "%s x (1 %s %s%%) = %s" % (M.inr(e), "-" if long else "+", num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise Err("an atr stop needs the session's bars")
        raw = e - sgn * stop_param * atr
        label = "%s x ATR(%d) %s the entry" % (num(stop_param), atr_bars, "under" if long else "over")
        detail = "ATR %s over the %d bars to the entry%s" % (num(round(atr, 4)), atr_bars,
                                                              " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    elif stop_rule == "vwap":
        if vwap is None:
            raise Err("a vwap stop needs the session VWAP at the entry (trader/entry.json, --vwap, or the bars)")
        raw = float(vwap) - sgn * M.tick_size(vwap, segment)
        label = "one tick %s VWAP %s" % ("under" if long else "over", M.inr(vwap))
    elif stop_rule in ("prev_day_low", "prev_day_high"):
        lv = prev_low if long else prev_high
        if lv is None:
            raise Err("a %s stop needs the previous session (the bars, or --prev-high/--prev-low)"
                      % stop_rule.replace("_", "-"))
        raw = float(lv) - sgn * M.tick_size(lv, segment)
        label = "one tick %s the previous day's %s %s" % ("under" if long else "over", "low" if long else "high",
                                                          M.inr(lv))
    else:
        raise Err("unknown stop rule %r" % stop_rule)
    stop = M.round_tick(raw, mode=away, segment=segment)
    if abs(stop - raw) > 1e-9:
        detail = (detail + ", " if detail else "%s " % num(raw)) + "rounded %s to the tick" % away
    if (stop >= e) if long else (stop <= e):
        raise Err("the stop %s (%s) is %s the entry %s: a %s's stop goes %s it" % (
            M.inr(stop), label, "at or above" if long else "at or below", M.inr(e), "long" if long else "short",
            "under" if long else "above"))
    if stop <= 0:
        raise Err("the stop works out to %s: pick a stop above Rs 0" % M.inr(stop))
    risk = r2(abs(e - stop))
    floor_risk = e * MIN_RISK_PCT / 100.0
    if risk < floor_risk:
        raise Err("the stop is %s from the entry, less than 0.05%% of the price (%s on %s): too tight to survive "
                  "the spread, normal wiggle would stop you out. Widen it." % (M.inr(risk), M.inr(floor_risk),
                                                                                M.inr(e)))
    capped = None
    if target_rule == "r":
        raw_t, t_label = e + sgn * target_param * risk, r_label(target_param)
    elif target_rule == "price":
        raw_t, t_label = float(target_param), None
    elif target_rule == "pct":
        raw_t, t_label = e * (1 + sgn * target_param / 100.0), "%s%% from the entry" % num(target_param)
    elif target_rule in ("prev_day_high", "prev_day_low"):
        lv = prev_high if long else prev_low
        if lv is None:
            raise Err("a %s target needs the previous session (the bars, or --prev-high/--prev-low)"
                      % target_rule.replace("_", "-"))
        raw_t, t_label = float(lv), "the previous day's %s %s" % ("high" if long else "low", M.inr(lv))
    else:
        raise Err("unknown target rule %r" % target_rule)
    target = M.round_tick(raw_t, mode=toward, segment=segment)
    if limits:
        edge = limits["upper"] if long else limits["lower"]
        if (target > edge) if long else (target < edge):
            capped = {"from": target, "to": edge}
            target = edge
    if (target <= e) if long else (target >= e):
        raise Err("the target %s is %s the entry %s: a %s's target goes %s it" % (
            M.inr(target), "at or below" if long else "at or above", M.inr(e), "long" if long else "short",
            "above" if long else "below"))
    rr = round(abs(target - e) / risk, 2)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    if capped:
        t_label += ", capped at the %s circuit %s" % ("upper" if long else "lower", M.inr(capped["to"]))
    return {"entry": r2(e), "stop": r2(stop), "stop_label": label, "stop_detail": detail, "target": r2(target),
            "target_r": target_param if target_rule == "r" and not capped else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "capped": capped}


# ---------------------------------------------------------------- costs

def ref_quantity(plan_entry, risk, cfg, lot=None, qty=None, lots=None):
    """(qty, basis words). F&O: lots x lot size. Equity: what the max loss allows."""
    if lot:
        n = int(lots or 1)
        if n <= 0:
            raise Err("--lots must be 1 or more")
        return n * lot, "%d lot%s of %d" % (n, "" if n == 1 else "s", lot)
    if qty:
        if qty <= 0:
            raise Err("--qty must be 1 or more")
        return int(qty), "your quantity"
    max_loss = float(cfg.get("max_loss_per_trade_inr") or 1000)
    q = int(math.floor(max_loss / risk + 1e-9)) if risk > 0 else 1
    cap = cfg.get("max_order_inr")
    if cap:
        q = min(q, int(math.floor(float(cap) / plan_entry + 1e-9)))
    q = max(1, q)
    return q, "%d shares: your %s max loss at %s risk a share%s" % (
        q, M.inr(max_loss, paise=False), M.inr(risk), ", capped by max_order_inr" if cap and q < int(
            math.floor(max_loss / risk + 1e-9)) else "")


def plan_costs(entry, stop, target, side, segment, qty, exchange, cfg):
    long = side == "buy"
    broker = (cfg.get("broker") or "zerodha").lower()

    def rt(exit_px):
        b, s = (entry, exit_px) if long else (exit_px, entry)
        return M.round_trip(segment, b, s, qty, exchange, broker, cfg)
    at_t, at_s = rt(target), rt(stop)
    reward, risk = abs(target - entry), abs(entry - stop)
    pu_t, pu_s = at_t["total"] / qty, at_s["total"] / qty
    net_reward, net_risk = reward - pu_t, risk + pu_s
    eat = 100.0 * at_t["total"] / (reward * qty) if reward > 0 else None
    return {"segment": segment, "broker": broker, "exchange": exchange, "ref_qty": qty,
            "at_target": at_t["total"], "at_stop": at_s["total"],
            "per_share_at_target": r2(pu_t), "per_share_at_stop": r2(pu_s),
            "by_type_at_target": at_t["by_type"], "dp": at_t["dp"],
            "breakeven_per_share": at_t["breakeven_per_unit"],
            "net_reward_per_share": r2(net_reward), "net_risk_per_share": r2(net_risk),
            "net_reward_risk": r2(net_reward / net_risk) if net_risk > 0 else None,
            "pct_of_reward": r2(eat)}


# ---------------------------------------------------------------- bars (ATR, VWAP, previous day)

def chart_range(day, today):
    n = M.trading_days_between(day, today)
    return "5d" if n <= 3 else "1mo" if n <= 18 else "60d"


def load_bars(sym, exchange, interval, day, offline=None, net=None):
    if offline:
        path = offline
        if os.path.isdir(offline):
            for name in (sym + ".json", sym + ".json.gz", M.yahoo_symbol(sym, exchange) + ".json"):
                if os.path.exists(os.path.join(offline, name)):
                    path = os.path.join(offline, name)
                    break
        if not os.path.isfile(path):
            raise Err("no offline file %s" % path)
        try:
            series = M.parse_chart(M.read_text_maybe_gz(path), sym)
        except OSError as e:
            raise Err("could not read %s: %s" % (path, e))
    else:
        if (M.now_ist().date() - day).days > 58:
            raise Err("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)
        net = net or NET_FACTORY()
        series = M.parse_chart(net.chart(sym, chart_range(day, M.now_ist().date()), interval, exchange), sym)
    want = M.yahoo_symbol(sym, exchange).upper()
    got = str(series["meta"].get("symbol") or want).upper()
    if got != want:
        raise Err("the chart holds %s, not %s (%s)" % (got, sym, want))
    return M.bars_from_series(series, INTERVALS[interval])


def session_bars(bars, day, interval, upto_slot=None, now=None):
    step = INTERVALS[interval]
    out = {}
    for b in bars:
        if b["dt"].date() != day or b["dt"].time() < M.OPEN or b["dt"].time() >= M.CLOSE:
            continue
        if now and b["dt"] + datetime.timedelta(seconds=step) > now:
            continue
        slot = M.bar_slot(b["dt"], step // 60)
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """ATR of the last n bars (all of them if fewer). The session's first bar uses its own range."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- the plan

def fno_chain_entry(underlying, expiry, strike, opt, path=None):
    path = path or os.path.join("trader", "fno", "chain-%s-%s.json" % (underlying, expiry))
    doc = M.read_json(path, "option chain", required=False)
    if not isinstance(doc, dict):
        return None, None
    for row in doc.get("strikes") or []:
        try:
            if abs(float(row.get("strike")) - float(strike)) < 1e-6:
                ltp = (row.get(opt) or {}).get("ltp")
                return (float(ltp), path) if ltp else (None, path)
        except (TypeError, ValueError):
            continue
    return None, path


def gather(a, cfg):
    """The entry facts, from trader/entry.json or the flags."""
    inst = (a.instrument or "EQ").upper()
    if inst in FNO:
        und = (a.underlying or a.symbol or "").upper()
        if not und or not M.valid_symbol(und):
            raise Err("an F&O plan needs --underlying (NIFTY, BANKNIFTY, RELIANCE ...)")
        if not a.expiry:
            raise Err("an F&O plan needs --expiry YYYY-MM-DD")
        try:
            exp = datetime.date.fromisoformat(a.expiry)
        except ValueError:
            raise Err("--expiry wants YYYY-MM-DD, not %r" % a.expiry)
        if inst in ("CE", "PE") and not a.strike:
            raise Err("an option plan needs --strike")
        entry, src = a.entry, "flags"
        if entry is None and inst in ("CE", "PE"):
            entry, src = fno_chain_entry(und, exp.isoformat(), a.strike, inst, a.chain)
            if entry is None:
                raise Err("give the option premium with --entry (no LTP for %s %s %s in %s)" % (
                    und, a.strike, inst, src or "trader/fno/"))
        if entry is None:
            raise Err("a futures plan needs --entry PRICE")
        side = {"long": "buy", "short": "sell"}.get(a.side, a.side)
        return {"symbol": und, "exchange": M.exchange_of(und), "entry": entry, "setup": a.setup or "level",
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "side": side,
                "vwap": a.vwap, "source": src, "instrument": inst, "expiry": exp, "strike": a.strike}
    if a.symbol and a.from_path:
        raise Err("use --from or --symbol, not both")
    if a.symbol:
        if a.entry is None:
            raise Err("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise Err("give both --or-high and --or-low, or neither")
        sym = a.symbol.strip().upper()
        exch = a.exchange or cfg.get("exchange") or "NSE"
        if sym.endswith((".NS", ".BO")):
            exch, sym = ("BSE" if sym.endswith(".BO") else "NSE"), sym[:-3]
        info = {"symbol": sym, "exchange": exch, "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"), "date": a.date,
                "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes,
                "side": {"long": "buy", "short": "sell"}.get(a.side, a.side), "vwap": a.vwap, "source": "flags",
                "product": None}
    else:
        path = a.from_path or "trader/entry.json"
        doc = M.read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if not isinstance(doc, dict):
            raise Err("%s is not an entry" % path)
        if doc.get("blocked"):
            raise Err("%s was blocked by trader-entry: %s" % (path, doc.get("reason") or "near a circuit"))
        if doc.get("status") != "triggered" and a.entry is None:
            raise Err("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                      % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        side = str(doc.get("side") or "buy").lower()
        if a.side:
            side = {"long": "buy", "short": "sell"}.get(a.side, a.side)
        info = {"symbol": str(doc.get("symbol") or "").upper(), "exchange": doc.get("exchange") or "NSE",
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (
                    doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "side": side,
                "vwap": a.vwap if a.vwap is not None else doc.get("vwap"),
                "prev_close": doc.get("prev_close"), "source": path, "product": doc.get("product")}
        if not info["symbol"] or info["entry"] is None:
            raise Err("%s has no symbol or price" % path)
    if M.is_index(info["symbol"]):
        raise Err("%s is an index: it cannot be bought in the cash market. Plan the option or future with "
                  "--instrument CE|PE|FUT --underlying %s --expiry YYYY-MM-DD" % (info["symbol"], info["symbol"]))
    if info["interval"] not in INTERVALS:
        raise Err("interval is 5m or 1m")
    info["instrument"] = "EQ"
    return info


def resolve_flat_by(day, explicit, cfg, entry_time, fno):
    """(HH:MM, notes). Never at or after the broker's MIS auto square-off."""
    broker = (cfg.get("broker") or "zerodha").lower()
    prof = M.BROKERS.get(broker) or M.BROKERS["flat20"]
    auto = M.parse_hhmm(prof["auto_square_off"]["fno" if fno else "equity"], "auto_square_off")
    t = M.parse_hhmm(explicit or cfg.get("flat_by") or M.DEFAULT_CONFIG["flat_by"],
                     "--flat-by" if explicit else "flat_by")
    notes = []
    if t <= M.OPEN:
        raise Err("flat_by %s is before the 09:15 open" % M.hm(t))
    if t >= auto:
        if explicit:
            raise Err("--flat-by %s is at or after %s's MIS auto square-off at %s (it closes you out itself and "
                      "charges %s plus GST for it)" % (M.hm(t), broker, M.hm(auto),
                                                         M.inr(prof.get("square_off_fee", 50), paise=False)))
        moved = (datetime.datetime.combine(day, auto) - datetime.timedelta(minutes=5)).time()
        notes.append("flat_by %s is at or after %s's MIS auto square-off %s: moved to %s" % (
            M.hm(t), broker, M.hm(auto), M.hm(moved)))
        t = moved
    if entry_time and entry_time >= t:
        raise Err("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                  % (M.hm(entry_time), M.hm(t)))
    return M.hm(t), M.hm(auto), notes


def add_trading_days(d, n):
    for _ in range(n):
        d = M.next_trading_day(d)
    return d


def market_data(info, a, day, now, need):
    """ATR / VWAP / previous-day facts from the bars, only when a rule needs them."""
    out = {}
    if not need:
        return out
    bars = load_bars(info["symbol"], info["exchange"], info["interval"], day, a.offline)
    if "prev" in need:
        pd = M.prev_trading_day(day)
        pb = [b for b in bars if b["dt"].date() == pd and M.OPEN <= b["dt"].time() < M.CLOSE]
        if not pb:
            raise Err("no bars for the previous session %s of %s in the data" % (pd, info["symbol"]))
        out.update(prev_high=max(b["h"] for b in pb), prev_low=min(b["l"] for b in pb), prev_day=pd.isoformat())
    if "atr" in need or "vwap" in need:
        sb = session_bars(bars, day, info["interval"], info["entry_bar"], now if day == now.date() else None)
        if "atr" in need:
            if len(sb) < 3:
                raise Err("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                          % (info["interval"], info["symbol"], day, len(sb)))
            out["atr"], out["atr_bars"] = atr_of(sb)
        if "vwap" in need:
            if not sb or not sum(b["v"] or 0 for b in sb):
                raise Err("no volume in the bars of %s on %s: give --vwap" % (info["symbol"], day))
            out["vwap"] = M.vwap(sb)
    return out


def build_plan(a, now):
    cfg = M.load_config(a.config)
    info = gather(a, cfg)
    inst = info["instrument"]
    fno = inst in FNO
    long = info["side"] == "buy"
    if info["side"] not in ("buy", "sell"):
        raise Err("--side is buy or sell")
    product = (a.product or info.get("product") or ("NRML" if fno and cfg.get("default_product") == "NRML"
                                                    else cfg.get("default_product")) or "MIS").upper()
    if fno and product == "CNC":
        raise Err("F&O positions are MIS (intraday) or NRML (carry), not CNC")
    if not fno and product == "NRML":
        raise Err("NRML is for F&O; a stock is MIS (intraday) or CNC (delivery)")
    warnings = []
    if not long and inst == "EQ":
        if not cfg.get("allow_short"):
            raise Err("short selling is off (\"allow_short\": false in trader/config.json)")
        if product != "MIS":
            raise Err("a cash short must be MIS (intraday): an uncovered short goes to the exchange auction "
                      "with a penalty")
    if inst in ("CE", "PE") and not long and not cfg.get("allow_option_selling"):
        warnings.append("this is option SELLING (writing): \"allow_option_selling\" is false, trader-risk will "
                        "block it; the loss on a written option is not capped by the premium")
    if inst == "FUT" and not cfg.get("allow_futures"):
        warnings.append("\"allow_futures\" is false in the config: trader-risk will block a futures order")

    stop_text = a.stop
    if not stop_text:
        if fno and inst in ("CE", "PE"):
            raise Err("an option plan needs a premium stop: --stop PRICE or pct:X (an option can fall to zero)")
        if info["setup"] in ("orb", "gap") and info["or_high"] is not None:
            stop_text = "or-mid"
        elif info["setup"] == "vwap_reclaim":
            stop_text = "vwap"
        else:
            raise Err("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X, atr:K, "
                      "vwap or prev-day-low" % info["setup"])
    for flag in ("vwap", "prev_high", "prev_low", "prev_close"):
        v = getattr(a, flag, None)
        if v is not None and v <= 0:
            raise Err("--%s must be a price above 0" % flag.replace("_", "-"))
    stop_rule, stop_param = parse_stop(stop_text, long)
    target_rule, target_param = parse_target(a.target or "2R", long)
    if inst in ("CE", "PE") and (stop_rule not in ("price", "pct") or target_rule not in ("r", "price", "pct")):
        raise Err("an option plan works on the premium: stop price:X or pct:X, target NR, price:X or pct:X")

    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise Err("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = now.date()
    if not M.trading_day(day):
        raise Err("NSE is closed on %s (%s): no session to plan" % (day, M.holiday_name(day) or "weekend"))
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    if entry_time and not (M.OPEN < entry_time <= M.CLOSE):
        raise Err("the entry time %s is outside the 09:15-15:30 session" % M.hm(entry_time))

    segment = M.segment_for(product, inst)
    tick_seg = "options" if inst in ("CE", "PE") else "futures" if inst == "FUT" else "equity"
    need = set()
    if stop_rule == "atr":
        need.add("atr")
    if stop_rule == "vwap" and info.get("vwap") is None:
        need.add("vwap")
    if stop_rule.startswith("prev_day") or target_rule.startswith("prev_day"):
        if a.prev_high is None or a.prev_low is None:
            need.add("prev")
    if inst in ("CE", "PE") and need:
        raise Err("ATR, VWAP and previous-day rules need the stock's bars; an option plan uses premiums")
    md = market_data(info, a, day, now, need)
    prev_high = a.prev_high if a.prev_high is not None else md.get("prev_high")
    prev_low = a.prev_low if a.prev_low is not None else md.get("prev_low")
    vwap = info.get("vwap") if info.get("vwap") is not None else md.get("vwap")

    band = None
    if inst == "EQ":
        if a.band is not None:
            if a.band not in M.PRICE_BANDS:
                raise Err("--band is one of %s" % ", ".join(map(str, M.PRICE_BANDS)))
            band = float(a.band)
        elif isinstance(cfg.get("bands"), dict) and cfg["bands"].get(info["symbol"]) is not None:
            band = float(cfg["bands"][info["symbol"]])
    pc = a.prev_close if a.prev_close is not None else info.get("prev_close")
    limits = M.band_limits(float(pc), band) if (band and pc) else None
    if band and not pc:
        warnings.append("a %g%% band is set but there is no previous close: give --prev-close to cap the target "
                        "at the circuit" % band)

    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["side"], tick_seg,
                info["or_high"], info["or_low"], md.get("atr"), md.get("atr_bars"), vwap, prev_high, prev_low,
                limits)

    instrument = {"type": inst}
    lot = None
    if fno:
        lot = M.lot_size(info["symbol"])
        if not lot:
            raise Err("no lot size known for %s: run market.py lots --refresh" % info["symbol"])
        exp = info["expiry"]
        if exp < day:
            raise Err("the %s contract expired on %s" % (info["symbol"], exp))
        listed = [x["date"] for x in M.expiries(info["symbol"], exp, weeks=1, months=1)]
        if exp.isoformat() not in listed:
            warnings.append("%s is not an expiry of %s by this tool's calendar (next: %s)" % (
                exp, info["symbol"], listed[0] if listed else "unknown"))
        instrument.update(underlying=info["symbol"], expiry=exp.isoformat(), lot_size=lot)
        if inst in ("CE", "PE"):
            strike = float(info["strike"])
            instrument.update(strike=int(strike) if strike.is_integer() else strike,
                              tradingsymbol=M.nse_option_symbol(info["symbol"], exp, strike, inst))
        else:
            instrument["tradingsymbol"] = "%s%s%sFUT" % (info["symbol"], exp.strftime("%y"),
                                                         exp.strftime("%b").upper())

    expiry_today = fno and info["expiry"] == day
    stock_fno = fno and not M.is_index(info["symbol"])
    flat_by = auto = None
    valid_until = None
    mandatory = False
    if product == "MIS" or (expiry_today and stock_fno):
        flat_by, auto, notes = resolve_flat_by(day, a.flat_by, cfg, entry_time, fno)
        warnings += notes
        mandatory = bool(expiry_today and stock_fno)
    else:
        hold = a.valid_days if a.valid_days is not None else DEFAULT_HOLD_DAYS
        valid_until = add_trading_days(day, hold)
        if fno and info["expiry"] < valid_until:
            valid_until = info["expiry"]
        warnings.append("%s holds overnight: the next open can gap straight through the stop (no stop works "
                        "while the market is shut)%s" % (product, "; delivery pays STT 0.1% on both sides and a "
                                                         "DP charge on the sell" if product == "CNC" else ""))
        if a.flat_by:
            warnings.append("--flat-by ignored: %s has no same-day square-off" % product)
    if expiry_today:
        warnings.append("expiry day for %s: time decay (theta) is steepest and gamma makes the premium jump; "
                        "a small move in %s can double or wipe out an option" % (info["symbol"], info["symbol"]))
        if stock_fno:
            warnings.append("stock F&O is PHYSICALLY SETTLED: an in-the-money %s still open at the close means "
                            "taking or giving delivery of %d shares of %s. Being flat by %s is mandatory"
                            % ("option" if inst != "FUT" else "future", lot, info["symbol"], flat_by))
        else:
            warnings.append("index options settle in cash; an in-the-money option left to expire pays STT of "
                            "0.15%% on its intrinsic value (e.g. %s on Rs 50 intrinsic x %d)"
                            % (M.inr(M.exercise_stt(50, lot)), lot))

    qty, basis = ref_quantity(c["entry"], c["risk"], cfg, lot, a.qty, a.lots)
    costs = plan_costs(c["entry"], c["stop"], c["target"], info["side"], segment, qty, info["exchange"], cfg)
    costs["basis"] = basis
    if costs["pct_of_reward"] is not None and costs["pct_of_reward"] > COST_WARN_PCT:
        warnings.append("charges eat %.0f%% of the reward (%s of %s at the target for %d): common for low-priced "
                        "stocks and cheap options. Net reward:risk %s" % (
                            costs["pct_of_reward"], M.inr(costs["at_target"]),
                            M.inr(abs(c["target"] - c["entry"]) * qty), qty, costs["net_reward_risk"]))
    mrr = cfg.get("min_reward_risk")
    if mrr is not None:
        try:
            if c["reward_risk"] < float(mrr):
                warnings.append("reward:risk %s is under your min_reward_risk %s: trader-risk checks that" % (
                    ratio(c["reward_risk"]), num(mrr)))
            elif costs["net_reward_risk"] is not None and costs["net_reward_risk"] < float(mrr):
                warnings.append("after charges reward:risk is %s, under your min_reward_risk %s" % (
                    costs["net_reward_risk"], num(mrr)))
        except (TypeError, ValueError):
            pass
    if c["capped"]:
        warnings.append("target capped at the %s circuit %s (it was %s): price cannot trade beyond it today" % (
            "upper" if long else "lower", M.inr(c["capped"]["to"]), M.inr(c["capped"]["from"])))
    if day != now.date():
        warnings.append("this plan is for %s, not today (%s)" % (day.isoformat(), now.date().isoformat()))

    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": r2(info["or_high"]), "low": r2(info["or_low"]), "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "exchange": info["exchange"],
            "instrument": instrument, "product": product, "side": info["side"], "date": day.isoformat(),
            "setup": info["setup"], "entry": c["entry"], "entry_time": M.hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": c["stop"], "stop_rule": stop_rule, "stop_label": c["stop_label"], "stop_detail": c["stop_detail"],
            "target": c["target"], "target_r": r2(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": c["risk"], "reward_risk": c["reward_risk"],
            "tick": M.tick_size(c["entry"], tick_seg), "flat_by": flat_by, "flat_by_mandatory": mandatory,
            "auto_square_off": auto, "valid_until": valid_until.isoformat() if valid_until else None,
            "costs_per_share": r2(max(costs["per_share_at_target"], costs["per_share_at_stop"])),
            "net_reward_risk": costs["net_reward_risk"], "costs": costs,
            "circuit": {"band_pct": band, "upper": limits["upper"] if limits else None,
                        "lower": limits["lower"] if limits else None},
            "source": info["source"], "created_at": now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30",
            "warnings": warnings, "note": NOTE}
    if md.get("atr") is not None:
        plan["atr"], plan["atr_bars"] = round(md["atr"], 4), md["atr_bars"]
    if vwap is not None:
        plan["vwap"] = r2(vwap)
    if prev_high is not None:
        plan["prev_day"] = {"high": r2(prev_high), "low": r2(prev_low), "date": md.get("prev_day")}
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    inst = (plan.get("instrument") or {}).get("type", "EQ")
    setup = {"orb": "opening-range breakout", "level": "level", "gap": "gap-and-go",
             "vwap_reclaim": "VWAP reclaim"}.get(plan.get("setup"), plan.get("setup"))
    long = plan.get("side", "buy") == "buy"
    e, risk = plan["entry"], plan["risk_per_share"]
    unit = "a share" if inst == "EQ" else "a unit"
    what = plan["symbol"]
    if inst != "EQ":
        ins = plan["instrument"]
        what = "%s (%s, lot %s, expiry %s)" % (ins.get("tradingsymbol"), inst, ins.get("lot_size"), ins.get("expiry"))
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s %s, %s %s, %s, %s" % (what, "long" if long else "short", plan.get("product", "MIS"),
                                                 day.strftime("%a %Y-%m-%d"), plan.get("exchange", "NSE"), setup)]
    out.append("  Entry         %s%s" % (M.inr(e), when))
    out.append("  Stop          %s  %s%s" % (M.inr(plan["stop"]), plan.get("stop_label", ""),
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (M.inr(plan["target"]), plan.get("target_label", "")))
    out.append("  Risk %-8s %s (%.2f%% of the entry), tick %s" % (unit, M.inr(risk), 100.0 * risk / e,
                                                                   plan.get("tick")))
    out.append("  Reward:risk   %s before charges, %s after" % (ratio(plan["reward_risk"]),
                                                               plan.get("net_reward_risk")))
    co = plan.get("costs") or {}
    if co:
        out.append("  Charges       %s at the target, %s at the stop, round trip for %s (%s, %s); %s %s" % (
            M.inr(co["at_target"]), M.inr(co["at_stop"]), co.get("basis") or co.get("ref_qty"), co["segment"],
            co["broker"], M.inr(plan.get("costs_per_share")), unit))
    c = plan.get("circuit") or {}
    if c.get("upper"):
        out.append("  Circuit       %g%% band: %s to %s" % (c["band_pct"], M.inr(c["lower"]), M.inr(c["upper"])))
    if plan.get("flat_by"):
        out.append("  Flat by       %s IST, win or lose%s (auto square-off %s)" % (
            plan["flat_by"], ", MANDATORY (physical settlement)" if plan.get("flat_by_mandatory") else "",
            plan.get("auto_square_off")))
    if plan.get("valid_until"):
        out.append("  Valid until   %s (overnight hold: gaps can skip the stop)" % plan["valid_until"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = M.parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        M.write_json(a.write, plan)
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: %s." % w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it with costs, then /trader-budget sizes it to your max loss. "
          "Nothing was placed. " + M.NOT_ADVICE)
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = M.read_json(a.plan, "plan")
    if not isinstance(plan, dict):
        raise Err("%s is not a plan" % a.plan)
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk"):
        if plan.get(k) is None:
            raise Err("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in plan.get("warnings") or []:
        print("Note: %s." % w)
    today = M.parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit, India): stop, target and charges before "
                                 "the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--exchange", choices=["NSE", "BSE"])
    p.add_argument("--entry", type=float, help="entry price (or premium for an option)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level", "vwap_reclaim", "gap"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time (the entry bar's close), e.g. 09:50")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default=None, choices=["buy", "long", "sell", "short"])
    p.add_argument("--product", choices=["MIS", "CNC", "NRML"])
    p.add_argument("--instrument", choices=["EQ", "FUT", "CE", "PE"], default="EQ")
    p.add_argument("--underlying")
    p.add_argument("--expiry")
    p.add_argument("--strike", type=float)
    p.add_argument("--chain", help="trader-fno chain JSON for the option's LTP")
    p.add_argument("--stop", help="or-mid, or-low/or-high, vwap, prev-day-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X, pct:X, prev-day-high")
    p.add_argument("--vwap", type=float, help="session VWAP at the entry, for --stop vwap")
    p.add_argument("--prev-high", type=float)
    p.add_argument("--prev-low", type=float)
    p.add_argument("--prev-close", type=float, help="for the circuit limits")
    p.add_argument("--band", type=int, help="the stock's circuit band in percent (2, 5, 10, 20)")
    p.add_argument("--qty", type=int, help="reference quantity for the charges (default: from your max loss)")
    p.add_argument("--lots", type=int, help="F&O lots for the charges (default 1)")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--valid-days", type=int, help="trading days a CNC/NRML plan stays valid (default 5)")
    p.add_argument("--config", default=M.CONFIG_FILE)
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for atr / vwap / prev-day rules")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-28 09:50"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.cmd == "plan" and a.side is None:
        a.side = "buy" if (a.symbol or (a.instrument or "EQ") != "EQ") else None
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except (Err, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
