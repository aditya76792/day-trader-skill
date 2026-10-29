#!/usr/bin/env python3
"""F&O desk for trader-fno: NSE/BSE option chains, Greeks, payoffs and option plans.
Standard library only.

    python3 fno.py expiries NIFTY                         # weekly / monthly expiries, lot, strike step
    python3 fno.py chain NIFTY                            # nearest expiry: OI, PCR, max pain, IV, straddle
    python3 fno.py chain NIFTY --expiry 2026-11-03 --strikes 8
    python3 fno.py chain RELIANCE --offline chain.json    # a chain saved from nseindia.com
    python3 fno.py greeks --spot 22698.85 --strike 22700 --type CE --premium 181.15 --expiry 2026-11-03
    python3 fno.py payoff --spot 22698.85 --leg "BUY CE 22700 181.15" --leg "SELL CE 22900 93.5"
    python3 fno.py plan NIFTY --view bullish --stop-pct 30 --target-r 2 --write   # -> trader/plan.json

chain     Reads NSE's option chain (www.nseindia.com/api/option-chain-v3, the
          JSON the NSE option-chain page uses; it needs the cookies a page
          visit sets, which Net handles; NSE blocks many scripts and data
          centres, so --offline FILE takes the same JSON saved from your
          browser). Normalises it to trader/fno/chain-SYMBOL-EXPIRY.json and
          prints, around the at-the-money strike:
            OI and change in OI per strike, IV, last price, bid/ask
            PCR (put OI / call OI) and PCR by volume
            max pain (the strike where option buyers' total payoff is least)
            the ATM straddle price = the market's priced-in move to expiry
            the biggest call OI (often read as resistance) and put OI (support)
            IV skew (OTM put IV minus OTM call IV)
            OI build-up per strike from price change and OI change:
              long build-up (price up, OI up), short build-up (price down, OI up),
              short covering (price up, OI down), long unwinding (price down, OI down)
          Days to expiry use calendar days to 15:30 IST on expiry day, the
          way NSE quotes IV. These are readings of positioning, not signals.

greeks    Black-Scholes for European options (NSE and BSE index and stock
          options are European): price, IV from a premium (bisection),
          delta, gamma, theta per calendar day, vega per 1 IV point.
          Rate: config risk_free_rate (6.5%), no dividends.

payoff    Payoff at expiry of up to 4 legs, in rupees for --lots lots:
          max profit, max loss, breakevens, and the full Indian charges for
          opening and closing every leg (STT 0.15% on the sell side premium,
          NSE 0.03553%, stamp, SEBI, GST, brokerage). Short legs are shown
          but marked: option selling needs allow_option_selling in the config
          and margin of roughly 1-1.5 lakh+ per lot (check your broker's SPAN
          calculator); this tool never estimates margin as safe.

plan      A directional option-BUYING plan (the cautious default): picks the
          strike nearest the target delta (0.5 = ATM) on the nearest expiry,
          entry = ask (or last price), stop = --stop-pct of premium, target =
          --target-r times the risk, sizes nothing (trader-budget does), and
          writes trader/plan.json for trader-budget -> trader-risk ->
          trader-broker. On expiry day it refuses after expiry_day_last_entry
          (13:30) and warns about theta and gamma; stock options that expire
          in the money are PHYSICALLY SETTLED (you must take or give delivery),
          so stock option plans always get a flat-by time.

Times are India time (IST). --now pins the clock ("2026-10-28 15:40").
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

CHAIN_DIR = os.path.join("trader", "fno")
PLAN_FILE = os.path.join("trader", "plan.json")
OPT = ("CE", "PE")
NOTE = ("Option chain readings (PCR, max pain, OI walls) describe positioning; they do not predict the "
        "close. " + M.NOT_ADVICE)


class FnoError(M.MarketError):
    pass


# ================================================================ maths

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def years_to_expiry(now, expiry):
    """Calendar time from now to 15:30 IST on expiry day, in years (min 1 minute)."""
    if isinstance(expiry, str):
        expiry = datetime.date.fromisoformat(expiry)
    end = datetime.datetime.combine(expiry, M.CLOSE)
    secs = (end - now).total_seconds()
    return max(secs, 60.0) / (365.0 * 86400.0)


def bs_price(spot, strike, t, vol, rate, opt):
    if vol <= 0 or t <= 0:
        intrinsic = max(0.0, spot - strike) if opt == "CE" else max(0.0, strike - spot)
        return intrinsic
    sq = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    if opt == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-rate * t) * _ncdf(d2)
    return strike * math.exp(-rate * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def greeks(spot, strike, t, vol, rate, opt):
    sq = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    disc = math.exp(-rate * t)
    if opt == "CE":
        delta = _ncdf(d1)
        theta = -spot * _npdf(d1) * vol / (2 * math.sqrt(t)) - rate * strike * disc * _ncdf(d2)
    else:
        delta = _ncdf(d1) - 1.0
        theta = -spot * _npdf(d1) * vol / (2 * math.sqrt(t)) + rate * strike * disc * _ncdf(-d2)
    gamma = _npdf(d1) / (spot * sq)
    vega = spot * _npdf(d1) * math.sqrt(t) / 100.0
    return {"price": bs_price(spot, strike, t, vol, rate, opt), "delta": delta, "gamma": gamma,
            "theta_per_day": theta / 365.0, "vega_per_iv_point": vega}


def implied_vol(premium, spot, strike, t, rate, opt):
    """Bisection on Black-Scholes. None when the premium is below intrinsic value."""
    intrinsic = max(0.0, spot - strike * math.exp(-rate * t)) if opt == "CE" else \
        max(0.0, strike * math.exp(-rate * t) - spot)
    if premium <= intrinsic + 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(spot, strike, t, hi, rate, opt) < premium:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, mid, rate, opt) > premium:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return (lo + hi) / 2


def prob_above(spot, level, t, vol, rate=0.0):
    """Risk-neutral probability the underlying ends above `level` (lognormal)."""
    if t <= 0 or vol <= 0:
        return 1.0 if spot > level else 0.0
    sq = vol * math.sqrt(t)
    d2 = (math.log(spot / level) + (rate - 0.5 * vol * vol) * t) / sq
    return _ncdf(d2)


# ================================================================ the chain

def chain_url(symbol, expiry):
    kind = "Indices" if M.is_index(symbol) else "Equity"
    return "/api/option-chain-v3?type=%s&symbol=%s&expiry=%s" % (kind, symbol.upper().replace("&", "%26"),
                                                                 M.nse_date(expiry))


def _num(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _side(d):
    if not isinstance(d, dict):
        return None
    out = {"ltp": _num(d.get("lastPrice")), "oi": _num(d.get("openInterest")),
           "chg_oi": _num(d.get("changeinOpenInterest")), "iv": _num(d.get("impliedVolatility")),
           "volume": _num(d.get("totalTradedVolume")), "bid": _num(d.get("buyPrice1")),
           "ask": _num(d.get("sellPrice1")), "chg": _num(d.get("change"))}
    if out["iv"] == 0:
        out["iv"] = None    # NSE shows 0 when it could not compute an IV
    return out


def normalise_chain(doc, symbol, expiry=None):
    """NSE option-chain JSON (v3 or the older indices shape) -> our contract."""
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), dict):
        raise FnoError("that is not NSE option-chain JSON (no 'records')")
    rec = doc["records"]
    rows = rec.get("data") or []
    expiries = [M.parse_nse_date(e).isoformat() for e in rec.get("expiryDates") or []]
    want = expiry or (expiries[0] if expiries else None)
    strikes = []
    for r in rows:
        try:
            e = M.parse_nse_date(r.get("expiryDates") or r.get("expiryDate") or
                                 (r.get("CE") or r.get("PE") or {}).get("expiryDate") or "").isoformat()
        except M.MarketError:
            e = want
        if want and e != want:
            continue
        k = _num(r.get("strikePrice"))
        if k is None:
            continue
        strikes.append({"strike": k, "CE": _side(r.get("CE")), "PE": _side(r.get("PE"))})
    if not strikes:
        raise FnoError("the chain has no strikes for expiry %s" % want)
    strikes.sort(key=lambda x: x["strike"])
    spot = _num(rec.get("underlyingValue"))
    at = rec.get("timestamp")
    return {"created_by": "trader-fno", "symbol": symbol.upper(), "exchange": M.exchange_of(symbol),
            "expiry": want, "expiries": expiries, "underlying_value": spot, "at": at,
            "lot_size": M.lot_size(symbol), "strikes": strikes}


def atm_strike(chain):
    spot = chain["underlying_value"]
    return min(chain["strikes"], key=lambda s: (abs(s["strike"] - spot), s["strike"]))["strike"]


def _oi(s, o):
    return ((s.get(o) or {}).get("oi") or 0.0)


def max_pain(chain):
    """The expiry price at which option BUYERS' total intrinsic value is least."""
    best = None
    for cand in chain["strikes"]:
        p = cand["strike"]
        pain = 0.0
        for s in chain["strikes"]:
            pain += _oi(s, "CE") * max(0.0, p - s["strike"])
            pain += _oi(s, "PE") * max(0.0, s["strike"] - p)
        if best is None or pain < best[1]:
            best = (p, pain)
    return best[0] if best else None


def buildup(price_chg, oi_chg):
    if price_chg is None or oi_chg is None or price_chg == 0 or oi_chg == 0:
        return None
    if price_chg > 0:
        return "long build-up" if oi_chg > 0 else "short covering"
    return "short build-up" if oi_chg > 0 else "long unwinding"


def summary(chain, now, rate=0.065):
    spot = chain["underlying_value"]
    st = chain["strikes"]
    ce_oi = sum(_oi(s, "CE") for s in st)
    pe_oi = sum(_oi(s, "PE") for s in st)
    ce_vol = sum(((s.get("CE") or {}).get("volume") or 0) for s in st)
    pe_vol = sum(((s.get("PE") or {}).get("volume") or 0) for s in st)
    atm = atm_strike(chain)
    row = next(s for s in st if s["strike"] == atm)
    ce, pe = row.get("CE") or {}, row.get("PE") or {}
    straddle = (ce.get("ltp") or 0) + (pe.get("ltp") or 0) if ce.get("ltp") and pe.get("ltp") else None
    t = years_to_expiry(now, chain["expiry"])
    out = {"spot": spot, "atm": atm, "expiry": chain["expiry"],
           "days_to_expiry": round(t * 365.0, 2),
           "pcr_oi": round(pe_oi / ce_oi, 2) if ce_oi else None,
           "pcr_volume": round(pe_vol / ce_vol, 2) if ce_vol else None,
           "max_pain": max_pain(chain),
           "atm_straddle": round(straddle, 2) if straddle else None,
           "implied_move_pct": round(100.0 * straddle / spot, 2) if straddle and spot else None,
           "call_wall": max(st, key=lambda s: _oi(s, "CE"))["strike"],
           "put_wall": max(st, key=lambda s: _oi(s, "PE"))["strike"],
           "atm_iv": None, "skew": None}
    ivs = [x for x in (ce.get("iv"), pe.get("iv")) if x]
    if ivs:
        out["atm_iv"] = round(sum(ivs) / len(ivs), 2)
    elif straddle and spot:
        v = implied_vol(ce["ltp"], spot, atm, t, rate, "CE")
        out["atm_iv"] = round(100 * v, 2) if v else None
    step = M.strike_step(chain["symbol"], spot) or (st[1]["strike"] - st[0]["strike"] if len(st) > 1 else 0)
    if step:
        lo_k, hi_k = atm - 4 * step, atm + 4 * step
        put = next((s for s in st if abs(s["strike"] - lo_k) < 1e-6), None)
        call = next((s for s in st if abs(s["strike"] - hi_k) < 1e-6), None)
        if put and call and (put.get("PE") or {}).get("iv") and (call.get("CE") or {}).get("iv"):
            out["skew"] = round(put["PE"]["iv"] - call["CE"]["iv"], 2)
            out["skew_strikes"] = [lo_k, hi_k]
    return out


def expiry_warnings(symbol, expiry, now, cfg):
    d = datetime.date.fromisoformat(expiry)
    out = []
    if d == now.date():
        last = cfg.get("expiry_day_last_entry", "13:30")
        out.append("EXPIRY DAY: premiums decay fastest today (theta) and swing hardest near the money "
                   "(gamma). New option entries stop at %s by your config." % last)
        out.append("Short options carry an extra 2% ELM margin today and no calendar-spread margin benefit.")
        if not M.is_index(symbol):
            out.append("Stock options in the money at expiry are PHYSICALLY SETTLED: you would have to take "
                       "or give delivery of the full lot. Exit before 15:10.")
        out.append("An option exercised in the money pays STT of 0.15% of its intrinsic value.")
    elif (d - now.date()).days <= 2:
        out.append("Expiry in %d day(s): time decay accelerates; weekly options lose value quickly."
                   % (d - now.date()).days)
    return out


def load_chain(a, net=None):
    sym = a.symbol.upper()
    if not M.valid_symbol(sym):
        raise FnoError("not a symbol: %r" % a.symbol)
    expiry = a.expiry
    if a.offline:
        doc = M.read_json(a.offline, "saved option chain")
    else:
        net = net or M.Net()
        if not expiry:
            info = net.nse_json("/api/option-chain-contract-info?symbol=%s" % sym.replace("&", "%26"))
            exps = [M.parse_nse_date(e) for e in info.get("expiryDates") or []]
            if not exps:
                raise FnoError("NSE listed no expiries for %s (is it in F&O?)" % sym)
            expiry = exps[0].isoformat()
        doc = net.nse_json(chain_url(sym, expiry))
    return normalise_chain(doc, sym, expiry)


# ================================================================ formatting

def _f(x, nd=2):
    if x is None:
        return "-"
    return ("%%.%df" % nd) % x


def _k(x):
    return ("%d" % x) if float(x).is_integer() else ("%g" % x)


def _lakh(x):
    if x is None:
        return "-"
    return "%.1fL" % (x / 1e5) if abs(x) >= 1e5 else "%d" % x


def render_chain(chain, summ, n, warnings):
    lines = ["%s option chain, expiry %s (%s), spot %s, at %s IST" % (
        chain["symbol"], chain["expiry"], datetime.date.fromisoformat(chain["expiry"]).strftime("%a"),
        _f(chain["underlying_value"]), chain.get("at") or "?")]
    lot = chain.get("lot_size")
    if lot:
        lines.append("lot %d, so 1 point = Rs %d a lot" % (lot, lot))
    atm = summ["atm"]
    st = chain["strikes"]
    i = next(j for j, s in enumerate(st) if s["strike"] == atm)
    lines.append("")
    lines.append("%8s %8s %7s %6s %8s | %-7s | %-8s %-6s %-7s %-8s" % (
        "CE OI", "chg OI", "IV", "", "LTP", "STRIKE", "LTP", "IV", "chg OI", "PE OI"))
    for s in st[max(0, i - n): i + n + 1]:
        ce, pe = s.get("CE") or {}, s.get("PE") or {}
        mark = "*" if s["strike"] == atm else " "
        lines.append("%8s %8s %7s %6s %8s | %s%-6s | %-8s %-6s %-7s %-8s" % (
            _lakh(ce.get("oi")), _lakh(ce.get("chg_oi")), _f(ce.get("iv")), "", _f(ce.get("ltp")),
            mark, _k(s["strike"]), _f(pe.get("ltp")), _f(pe.get("iv")), _lakh(pe.get("chg_oi")),
            _lakh(pe.get("oi"))))
    lines.append("")
    lines.append("PCR (OI) %s, PCR (volume) %s | max pain %s | call wall %s, put wall %s" % (
        _f(summ["pcr_oi"]), _f(summ["pcr_volume"]), _k(summ["max_pain"]), _k(summ["call_wall"]),
        _k(summ["put_wall"])))
    if summ["atm_straddle"]:
        lines.append("ATM %s straddle %s = a priced-in move of about +/-%s%% by expiry (%s days)" % (
            _k(atm), _f(summ["atm_straddle"]), _f(summ["implied_move_pct"]), _f(summ["days_to_expiry"], 1)))
    if summ.get("atm_iv"):
        lines.append("ATM IV %s%%%s" % (_f(summ["atm_iv"]), (", skew (put %s IV - call %s IV) %s points" % (
            _k(summ["skew_strikes"][0]), _k(summ["skew_strikes"][1]), _f(summ["skew"])) if summ.get("skew") is not None
            else "")))
    builds = []
    for s in st[max(0, i - 3): i + 4]:
        for o in OPT:
            b = buildup((s.get(o) or {}).get("chg"), (s.get(o) or {}).get("chg_oi"))
            if b:
                builds.append("%s %s %s" % (_k(s["strike"]), o, b))
    if builds:
        lines.append("OI build-up near the money: " + "; ".join(builds))
    for w in warnings:
        lines.append("! " + w)
    lines.append(NOTE)
    return "\n".join(lines)


# ================================================================ payoff

def parse_leg(text):
    """'BUY CE 22700 181.15' | 'SELL PE 22500 @ 99.8' | 'BUY FUT 22750'."""
    toks = [t for t in re.split(r"[\s@]+", (text or "").strip().upper()) if t]
    try:
        side, kind = toks[0], toks[1]
        nums = [float(t) for t in toks[2:]]
    except (IndexError, ValueError):
        nums, side, kind = [], None, None
    if side not in ("BUY", "SELL") or kind not in ("CE", "PE", "FUT"):
        raise FnoError("a leg is 'BUY CE 22700 181.15', 'SELL PE 22500 99.8' or 'BUY FUT 22750', got %r" % text)
    if kind == "FUT":
        if len(nums) != 1:
            raise FnoError("a futures leg is 'BUY FUT PRICE', got %r" % text)
        return {"side": side, "type": "FUT", "strike": None, "price": nums[0]}
    if len(nums) != 2 or nums[0] <= 0 or nums[1] < 0:
        raise FnoError("an option leg is 'BUY CE STRIKE PREMIUM', got %r" % text)
    return {"side": side, "type": kind, "strike": nums[0], "price": nums[1]}


def leg_value_at(leg, s):
    if leg["type"] == "FUT":
        v = s - leg["price"]
    else:
        intrinsic = max(0.0, s - leg["strike"]) if leg["type"] == "CE" else max(0.0, leg["strike"] - s)
        v = intrinsic - leg["price"]
    return v if leg["side"] == "BUY" else -v


def payoff(legs, spot, units, lo=None, hi=None, steps=400):
    """Payoff at expiry over [lo, hi] (default 0 to 1.15 x the highest strike or spot).
    Prices cannot go below zero, so only the upside can be unlimited."""
    if spot <= 0:
        raise FnoError("spot must be above zero")
    top = max([leg["strike"] for leg in legs if leg["strike"]] + [leg["price"] for leg in legs if leg["type"] == "FUT"]
              + [spot])
    lo = 0.0 if lo is None else max(0.0, lo)
    hi = hi if hi is not None else top * 1.15
    if hi <= lo:
        raise FnoError("--high must be above --low")
    xs = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    ks = sorted(set([leg["strike"] for leg in legs if leg["strike"]] + [spot]))
    xs = sorted(set(xs + [k for k in ks if lo <= k <= hi]))
    ys = [sum(leg_value_at(leg, x) for leg in legs) * units for x in xs]
    be = []
    for i in range(1, len(xs)):
        y0, y1 = ys[i - 1], ys[i]
        if (y0 < 0 <= y1) or (y0 > 0 >= y1):
            x0, x1 = xs[i - 1], xs[i]
            be.append(round(x0 if y1 == y0 else x0 - y0 * (x1 - x0) / (y1 - y0), 2))
    slope_hi = sum((1 if leg["type"] in ("CE", "FUT") else 0) * (1 if leg["side"] == "BUY" else -1) for leg in legs)
    full = lo == 0.0
    max_p = "unlimited" if slope_hi > 0 else round(max(ys), 2)
    max_l = "unlimited" if slope_hi < 0 else round(min(ys), 2)
    out = {"points": list(zip(xs, ys)), "breakevens": sorted(set(be)), "max_profit": max_p, "max_loss": max_l,
           "at_spot": round(sum(leg_value_at(leg, spot) for leg in legs) * units, 2)}
    if not full:
        out["note"] = "max profit and loss are within %s-%s only" % (_f(lo), _f(hi))
    return out


def legs_charges(legs, units, exchange, broker, cfg):
    """Open + close every leg at the same price (the minimum cost to trade the structure)."""
    total = 0.0
    for leg in legs:
        seg = "futures" if leg["type"] == "FUT" else "options"
        rt = M.round_trip(seg, leg["price"], leg["price"], units, exchange, broker, cfg)
        total += rt["total"]
    return round(total, 2)


# ================================================================ plan

def pick_strike(chain, view, target_delta, now, rate):
    opt = "CE" if view == "bullish" else "PE"
    spot = chain["underlying_value"]
    t = years_to_expiry(now, chain["expiry"])
    best = None
    for s in chain["strikes"]:
        side = s.get(opt) or {}
        price = side.get("ask") or side.get("ltp")
        if not price:
            continue
        iv = (side.get("iv") or 0) / 100.0 or implied_vol(price, spot, s["strike"], t, rate, opt)
        if not iv:
            continue
        d = abs(greeks(spot, s["strike"], t, iv, rate, opt)["delta"])
        score = abs(d - target_delta)
        if best is None or score < best[0]:
            best = (score, s, opt, price, iv, d)
    if not best:
        raise FnoError("no strike with a usable price on this chain")
    return best[1:]


def build_plan(chain, a, cfg, now):
    if not cfg.get("allow_option_buying", True):
        raise FnoError("option buying is off in your config (allow_option_buying)")
    exp = chain["expiry"]
    expiry = datetime.date.fromisoformat(exp)
    if expiry < now.date():
        raise FnoError("expiry %s has passed" % exp)
    if expiry == now.date():
        last = M.parse_hhmm(cfg.get("expiry_day_last_entry", "13:30"))
        if now.time() >= last:
            raise FnoError("expiry day: no new option entries after %s (expiry_day_last_entry)" % M.hm(last))
    rate = float(cfg.get("risk_free_rate", 0.065))
    s, opt, price, iv, delta = pick_strike(chain, a.view, a.delta, now, rate)
    entry = M.round_tick(price, segment="options", mode="up")
    stop = M.round_tick(entry * (1 - a.stop_pct / 100.0), segment="options", mode="down")
    risk = round(entry - stop, 2)
    if risk <= 0:
        raise FnoError("the stop is not below the entry; use a larger --stop-pct")
    target = M.round_tick(entry + a.target_r * risk, segment="options", mode="down")
    lot = chain.get("lot_size") or M.lot_size(chain["symbol"])
    if not lot:
        raise FnoError("lot size of %s is unknown: run `market.py lots --refresh`" % chain["symbol"])
    rt = M.round_trip("options", entry, target, lot, chain["exchange"], cfg.get("broker", "zerodha"), cfg)
    rt_stop = M.round_trip("options", entry, stop, lot, chain["exchange"], cfg.get("broker", "zerodha"), cfg)
    cps = round(rt["total"] / lot, 2)
    net_rr = round(((target - entry) * lot - rt["total"]) / ((entry - stop) * lot + rt_stop["total"]), 2)
    flat_by = cfg.get("flat_by", "15:10")
    notes = expiry_warnings(chain["symbol"], exp, now, cfg)
    if rt["total"] > 0.25 * (target - entry) * lot:
        notes.append("Charges are %s of a %s reward a lot: cheap options are expensive to trade."
                     % (M.inr(rt["total"]), M.inr((target - entry) * lot)))
    plan = {
        "created_by": "trader-fno", "symbol": chain["symbol"], "exchange": chain["exchange"],
        "instrument": {"type": opt, "underlying": chain["symbol"], "expiry": exp, "strike": s["strike"],
                       "lot_size": lot, "tradingsymbol": M.nse_option_symbol(chain["symbol"], expiry, s["strike"], opt)},
        "product": a.product, "side": "buy", "date": now.date().isoformat(), "setup": "option_%s" % a.view,
        "entry": entry, "entry_time": M.hm(now.time()), "stop": stop,
        "stop_rule": "premium_pct", "stop_label": "%g%% of the premium" % a.stop_pct,
        "target": target, "target_r": a.target_r, "target_label": "%gR" % a.target_r,
        "risk_per_share": risk, "reward_risk": a.target_r, "tick": 0.05,
        "flat_by": flat_by, "costs_per_share": cps, "net_reward_risk": net_rr,
        "underlying_value": chain["underlying_value"], "iv": round(iv * 100, 2), "delta": round(delta, 3),
        "notes": notes,
    }
    if a.product == "NRML" and expiry != now.date():
        plan["flat_by"] = None
        plan["valid_until"] = exp
    if not M.is_index(chain["symbol"]):
        plan["flat_by"] = plan["flat_by"] or "15:10"
        plan["notes"].append("Stock option: physically settled if held to expiry in the money.")
    return plan


# ================================================================ commands

def _cfg(a):
    return M.load_config(a.config)


def cmd_expiries(a):
    now = M.parse_now(a.now)
    sym = a.symbol.upper()
    rows = M.expiries(sym, now.date(), weeks=a.weeks, months=a.months)
    lot = M.lot_size(sym)
    if a.json:
        print(json.dumps({"symbol": sym, "lot_size": lot, "strike_step": M.strike_step(sym),
                            "expiries": rows}, indent=2))
        return 0
    print("%s on %s: lot %s, strike step %s" % (sym, M.exchange_of(sym), lot or "unknown (market.py lots --refresh)",
                                                M.strike_step(sym) or "by price"))
    for r in rows:
        tag = "  <- today" if r["date"] == now.date().isoformat() else ""
        print("  %s %-9s %s%s" % (r["date"], r["weekday"], r["kind"], tag))
    return 0


def cmd_chain(a):
    now = M.parse_now(a.now)
    cfg = _cfg(a)
    chain = load_chain(a)
    if chain["underlying_value"] is None:
        raise FnoError("the chain has no underlying value; pass --spot")
    if a.spot:
        chain["underlying_value"] = a.spot
    summ = summary(chain, now, float(cfg.get("risk_free_rate", 0.065)))
    chain["summary"] = summ
    warns = expiry_warnings(chain["symbol"], chain["expiry"], now, cfg)
    if not a.no_write:
        M.write_json(os.path.join(CHAIN_DIR, "chain-%s-%s.json" % (chain["symbol"], chain["expiry"])), chain)
    if a.json:
        print(json.dumps({"summary": summ, "warnings": warns, "chain": chain}, indent=2))
        return 0
    print(render_chain(chain, summ, a.strikes, warns))
    return 0


def cmd_greeks(a):
    now = M.parse_now(a.now)
    cfg = _cfg(a)
    rate = a.rate if a.rate is not None else float(cfg.get("risk_free_rate", 0.065))
    opt = a.type.upper()
    t = years_to_expiry(now, a.expiry)
    if a.iv:
        vol = a.iv / 100.0
    elif a.premium:
        vol = implied_vol(a.premium, a.spot, a.strike, t, rate, opt)
        if vol is None:
            raise FnoError("premium %s is at or below intrinsic value: no IV" % a.premium)
    else:
        raise FnoError("give --premium (to solve IV) or --iv")
    g = greeks(a.spot, a.strike, t, vol, rate, opt)
    lot = M.lot_size(a.symbol) if a.symbol else None
    doc = {"spot": a.spot, "strike": a.strike, "type": opt, "expiry": a.expiry,
           "days": round(t * 365, 2), "iv_pct": round(vol * 100, 2), "rate": rate,
           "price": round(g["price"], 2), "delta": round(g["delta"], 4), "gamma": round(g["gamma"], 6),
           "theta_per_day": round(g["theta_per_day"], 2), "vega_per_iv_point": round(g["vega_per_iv_point"], 2),
           "lot_size": lot}
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("%s %s %s, expiry %s (%.2f days), spot %s" % (a.symbol or "", _k(a.strike), opt, a.expiry, doc["days"], a.spot))
    print("IV %.2f%%  price %.2f  delta %.3f  gamma %.5f  theta %.2f/day  vega %.2f per IV point"
          % (doc["iv_pct"], doc["price"], doc["delta"], doc["gamma"], doc["theta_per_day"], doc["vega_per_iv_point"]))
    if lot:
        print("per lot of %d: theta %s a day, delta %.1f units of the underlying" % (
            lot, M.inr(doc["theta_per_day"] * lot), doc["delta"] * lot))
    return 0


def cmd_payoff(a):
    cfg = _cfg(a)
    legs = [parse_leg(x) for x in a.leg]
    if not legs or len(legs) > 4:
        raise FnoError("give 1 to 4 --leg")
    lot = a.lot or (M.lot_size(a.symbol) if a.symbol else None)
    if not lot:
        raise FnoError("give --symbol (for its lot size) or --lot")
    units = lot * a.lots
    res = payoff(legs, a.spot, units, a.low, a.high)
    exch = M.exchange_of(a.symbol) if a.symbol else "NSE"
    costs = legs_charges(legs, units, exch, cfg.get("broker", "zerodha"), cfg)
    shorts = [leg for leg in legs if leg["side"] == "SELL"]
    net_premium = sum((-1 if leg["side"] == "BUY" else 1) * leg["price"] for leg in legs if leg["type"] != "FUT") * units
    doc = {"legs": legs, "units": units, "lots": a.lots, "lot_size": lot, "max_profit": res["max_profit"],
           "max_loss": res["max_loss"], "breakevens": res["breakevens"], "pnl_at_spot_on_expiry": res["at_spot"],
           "net_premium": round(net_premium, 2), "charges_round_trip": costs,
           "short_legs": len(shorts), "selling_allowed": bool(cfg.get("allow_option_selling", False))}
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("%d lot(s) x %d = %d units, spot %s" % (a.lots, lot, units, a.spot))
    for leg in legs:
        print("  %s %s %s @ %s" % (leg["side"], leg["type"], _k(leg["strike"]) if leg["strike"] else "", leg["price"]))
    print("net premium %s %s" % ("received" if net_premium > 0 else "paid", M.inr(abs(net_premium))))
    mp = res["max_profit"]
    ml = res["max_loss"]
    print("max profit %s, max loss %s (at expiry, before charges)" % (
        mp if isinstance(mp, str) else M.inr(mp), ml if isinstance(ml, str) else M.inr(ml)))
    print("breakeven(s): %s" % (", ".join(_f(b) for b in res["breakevens"]) or "none in range"))
    print("charges to open and close every leg: about %s" % M.inr(costs))
    if shorts:
        print("! %d short leg(s): option selling is %s in your config. Short options need large margin "
              "(check your broker's SPAN calculator) and can lose far more than the premium received%s."
              % (len(shorts), "ON" if doc["selling_allowed"] else "OFF (trader-risk will block it)",
                 "; the loss is UNLIMITED" if ml == "unlimited" else ""))
    print(M.NOT_ADVICE)
    return 0


def cmd_plan(a):
    now = M.parse_now(a.now)
    cfg = _cfg(a)
    if a.view not in ("bullish", "bearish"):
        raise FnoError("--view is bullish (buy a call) or bearish (buy a put)")
    chain = load_chain(a)
    plan = build_plan(chain, a, cfg, now)
    if a.write:
        M.write_json(PLAN_FILE, plan)
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    i = plan["instrument"]
    print("%s %s %s %s (%s), lot %d" % (plan["symbol"], i["expiry"], _k(i["strike"]), i["type"], i["tradingsymbol"],
                                        i["lot_size"]))
    print("buy at %s, stop %s (%s), target %s (%s); delta %.2f, IV %.1f%%" % (
        plan["entry"], plan["stop"], plan["stop_label"], plan["target"], plan["target_label"], plan["delta"], plan["iv"]))
    print("per lot: risk %s, reward %s, charges about %s, net reward:risk %s" % (
        M.inr(plan["risk_per_share"] * i["lot_size"]), M.inr((plan["target"] - plan["entry"]) * i["lot_size"]),
        M.inr(plan["costs_per_share"] * i["lot_size"]), plan["net_reward_risk"]))
    if plan.get("flat_by"):
        print("exit by %s IST" % plan["flat_by"])
    for n in plan["notes"]:
        print("! " + n)
    print("%s Next: trader-budget sizes it in lots, trader-risk checks it, and nothing is placed "
          "without your yes." % ("Wrote trader/plan.json." if a.write else "Not written (add --write)."))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="fno.py", description="NSE/BSE F&O: chains, Greeks, payoffs, option plans")
    sub = p.add_subparsers(dest="cmd")

    def common(s, chain=False):
        s.add_argument("--now")
        s.add_argument("--json", action="store_true")
        s.add_argument("--config", default=M.CONFIG_FILE)
        if chain:
            s.add_argument("symbol")
            s.add_argument("--expiry", help="YYYY-MM-DD (default: nearest)")
            s.add_argument("--offline", help="NSE option-chain JSON saved from your browser")
    s = sub.add_parser("expiries", help="upcoming expiries, lot size and strike step")
    s.add_argument("symbol")
    s.add_argument("--weeks", type=int, default=5)
    s.add_argument("--months", type=int, default=3)
    common(s)
    s.set_defaults(fn=cmd_expiries)
    s = sub.add_parser("chain", help="option chain with OI, PCR, max pain, IV, straddle")
    common(s, chain=True)
    s.add_argument("--strikes", type=int, default=6, help="strikes each side of ATM to show")
    s.add_argument("--spot", type=float)
    s.add_argument("--no-write", action="store_true")
    s.set_defaults(fn=cmd_chain)
    s = sub.add_parser("greeks", help="Black-Scholes price, IV and Greeks")
    s.add_argument("--spot", type=float, required=True)
    s.add_argument("--strike", type=float, required=True)
    s.add_argument("--type", required=True, choices=("CE", "PE", "ce", "pe"))
    s.add_argument("--expiry", required=True)
    s.add_argument("--premium", type=float)
    s.add_argument("--iv", type=float, help="IV in percent")
    s.add_argument("--rate", type=float)
    s.add_argument("--symbol")
    common(s)
    s.set_defaults(fn=cmd_greeks)
    s = sub.add_parser("payoff", help="payoff at expiry of 1-4 legs, with charges")
    s.add_argument("--leg", action="append", default=[])
    s.add_argument("--spot", type=float, required=True)
    s.add_argument("--symbol")
    s.add_argument("--lot", type=int)
    s.add_argument("--lots", type=int, default=1)
    s.add_argument("--low", type=float)
    s.add_argument("--high", type=float)
    common(s)
    s.set_defaults(fn=cmd_payoff)
    s = sub.add_parser("plan", help="an option-buying plan -> trader/plan.json")
    common(s, chain=True)
    s.add_argument("--view", required=True)
    s.add_argument("--delta", type=float, default=0.5)
    s.add_argument("--stop-pct", type=float, default=30.0)
    s.add_argument("--target-r", type=float, default=2.0)
    s.add_argument("--product", default="MIS", choices=("MIS", "NRML"))
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_plan)
    a = p.parse_args(argv)
    if not getattr(a, "cmd", None):
        p.print_help()
        return 2
    try:
        return a.fn(a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
