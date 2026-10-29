#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts (India edition). Standard library only.

    python3 alerts.py clock                          # is NSE open? which session? next expiry
    python3 alerts.py quote RELIANCE TCS --write     # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1163.2
    python3 alerts.py add --symbol RELIANCE --target 1187.2
    python3 alerts.py add --at 15:10 --label "flat by" --symbol RELIANCE
    python3 alerts.py add --square-off --segment equity        # 10 min before your broker squares off MIS
    python3 alerts.py add --expiry NIFTY --ring-at 13:30       # on NIFTY expiry days only
    python3 alerts.py add --symbol SAMPLECO --circuit 2 --band 5
    python3 alerts.py add --symbol RELIANCE --gap 1.5          # opening gap at 09:15/09:16
    python3 alerts.py add --symbol NIFTY --index-move 2        # with market-wide circuit levels
    python3 alerts.py add --symbol RELIANCE --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --price NIFTY26N0322700CE=118.5    # an option premium you read yourself
    python3 alerts.py check --now "2026-10-28 15:15" --offline DIR
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price" (a "side": "sell" target is at or below)
  below / stop    the price is at or below "price" (a "side": "sell" stop is at or above)
  pct_move        moved "pct" percent or more from the previous close (or "ref"),
                  optional "direction" up / down; once per day
  opening_bell    the NSE normal market opened (09:15 IST); once per trading day
  time            the IST clock passed "at" (HH:MM) on a trading day; once per day
  square_off      "minutes_before" (default 10) before the broker's auto square-off of
                  MIS positions (market.BROKERS[broker]["auto_square_off"][segment]):
                  a reminder to exit yourself instead of paying the square-off fee
  expiry          "at" (HH:MM) on an expiry day of "underlying" (market.is_expiry_day)
  circuit         price within "within_pct" of its upper / lower circuit, when a fixed
                  band is known ("band_pct" on the rule or "bands" in trader/config.json)
  gap             the opening gap vs the previous close is "pct" or more; checked from
                  09:15 until "until" (default 09:16)
  index_move      an index (NIFTY, SENSEX ...) moved "pct" or more; also rings once per
                  level when a market-wide circuit breaker level (10/15/20%) is reached

A rule with "date" is only watched on that IST date (plan rules are for the plan's
day only). above / below ring ONCE until reset, unless "rearm": true.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1m
          (query1 as fallback; market.Net retries). The latest 1-minute close is the price,
          meta.previousClose the previous close. Indexes use Yahoo's codes (^NSEI, ^BSESN).
  Stooq is not used: it does not cover NSE symbols reliably.
  Options and futures are not on Yahoo: their premiums come from trader-fno (a fresh
  quote in trader/prices.json) or from --price SYMBOL=PREMIUM.
  --offline DIR reads DIR/yahoo-<SYMBOL>.json (or .json.gz) instead of the network.
"""

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

PAUSE = 0.3          # seconds between Yahoo requests
STALE_MIN = 15       # a saved premium older than this is not used
DEFAULT_GAP_UNTIL = "09:16"
TYPES = ("above", "below", "stop", "target", "pct_move", "opening_bell", "time", "square_off",
         "expiry", "circuit", "gap", "index_move")
NO_QUOTE = ("opening_bell", "time", "square_off", "expiry")
LEVEL_TYPES = ("above", "below", "stop", "target")
PCT_TYPES = ("pct_move", "gap", "index_move")
DERIV_TYPES = ("CE", "PE", "FUT")
DERIV_RE = re.compile(r"^[A-Z0-9&\-]+?\d{2}(?:[A-Z]{3}|[1-9OND]\d{2})[\d.]*(?:CE|PE|FUT)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AlertError(M.MarketError):
    pass


def _new_net():
    return M.Net(pause=PAUSE)


NET_FACTORY = _new_net     # tests swap this for an offline opener


# ---------------------------------------------------------------- small helpers

def stamp(dt):
    """naive IST datetime -> '2026-10-28T10:15:00+05:30'."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


def from_stamp(text):
    """'2026-10-28T10:15:00+05:30' (or without offset) -> naive IST datetime, None if unreadable."""
    if not text:
        return None
    t = str(text)
    try:
        if t.endswith("Z"):
            return M.parse_now(t)
        return datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, M.MarketError):
        return None


def _hhmm(text):
    try:
        return M.parse_hhmm(text)
    except M.MarketError:
        return None


def _minus(t, minutes):
    d = datetime.datetime.combine(datetime.date(2000, 1, 1), t) - datetime.timedelta(minutes=minutes)
    return d.time()


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")


def is_derivative(sym, rule=None):
    inst = (rule or {}).get("instrument") or {}
    if str(inst.get("type") or "").upper() in DERIV_TYPES or (rule or {}).get("premium"):
        return True
    return bool(DERIV_RE.match(str(sym or "").upper()))


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- quotes

def parse_quote(doc, sym):
    """Yahoo v8 chart (range=1d, 1m or 5m bars) -> quote. The price is the latest
    close; the day's open/high/low come from bars at or after 09:15 IST."""
    s = M.parse_chart(doc, sym)
    meta = s["meta"]
    last = None
    for i in range(len(s["t"]) - 1, -1, -1):
        if s["c"][i] is not None and s["t"][i] is not None:
            last = i
            break
    if last is not None:
        price, ts = s["c"][last], s["t"][last]
    else:
        price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    when = M.ist_from_ts(ts) if ts else None
    o = h = lo = None
    if when is not None:
        for i, t in enumerate(s["t"]):
            if t is None or s["c"][i] is None:
                continue
            dt = M.ist_from_ts(t)
            if dt.date() != when.date() or dt.time() < M.OPEN:
                continue
            bo = s["o"][i] if s["o"][i] is not None else s["c"][i]
            bh = s["h"][i] if s["h"][i] is not None else s["c"][i]
            bl = s["l"][i] if s["l"][i] is not None else s["c"][i]
            if o is None:
                o = bo
            h = bh if h is None else max(h, bh)
            lo = bl if lo is None else min(lo, bl)
    prev = meta.get("previousClose")
    if prev is None:
        prev = meta.get("chartPreviousClose")
    return {"symbol": sym.upper(), "price": round(float(price), 2),
            "time": stamp(when) if when else None,
            "prev_close": round(float(prev), 2) if prev is not None else None,
            "open": round(float(o), 2) if o is not None else None,
            "high": round(float(h), 2) if h is not None else None,
            "low": round(float(lo), 2) if lo is not None else None,
            "currency": meta.get("currency"), "source": "yahoo"}


def _offline_doc(folder, sym):
    if os.path.isfile(folder):
        return json.loads(M.read_text_maybe_gz(folder))
    base = os.path.join(folder, "yahoo-%s.json" % sym)
    for path in (base, base + ".gz"):
        if os.path.isfile(path):
            try:
                return json.loads(M.read_text_maybe_gz(path))
            except ValueError as e:
                raise AlertError("%s is not valid JSON: %s" % (path, e))
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, offline=None, exchanges=None, interval="1m", net=None):
    """{SYMBOL: quote} and [errors]; one Yahoo request per symbol (NSE .NS by default)."""
    quotes, errors = {}, []
    exchanges = exchanges or {}
    for sym in _dedupe([s.upper() for s in symbols]):
        try:
            if offline:
                doc = _offline_doc(offline, sym)
            else:
                if net is None:
                    net = NET_FACTORY()
                doc = net.chart(sym, rng="1d", interval=interval, exchange=exchanges.get(sym, "NSE"))
            quotes[sym] = parse_quote(doc, sym)
        except M.MarketError as e:
            errors.append("%s: %s" % (sym, e))
    return quotes, errors


def parse_price_overrides(items, now):
    """['NIFTY26N0322700CE=118.5'] -> quotes with source 'manual'."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise AlertError("--price wants SYMBOL=PRICE, got %r" % item)
        sym, val = item.split("=", 1)
        try:
            p = float(val)
        except ValueError:
            raise AlertError("--price %s: %r is not a number" % (sym, val))
        if p <= 0:
            raise AlertError("--price %s must be above zero" % sym)
        out[sym.strip().upper()] = {"symbol": sym.strip().upper(), "price": p, "time": stamp(now),
                                    "prev_close": None, "open": None, "source": "manual"}
    return out


def saved_quote(prices_doc, sym, now, max_age_min=STALE_MIN):
    """A quote already in trader/prices.json (e.g. a premium from trader-fno), if fresh."""
    q = ((prices_doc or {}).get("quotes") or {}).get(sym)
    if not isinstance(q, dict) or not isinstance(q.get("price"), (int, float)):
        return None
    t = from_stamp(q.get("time") or q.get("fetched"))
    if t is None or t > now + datetime.timedelta(minutes=1) or now - t > datetime.timedelta(minutes=max_age_min):
        return None
    return dict(q, symbol=sym)


def write_prices(path, quotes, now):
    """Merge quotes into trader/prices.json (CONTRACTS.md)."""
    old = M.read_json(path, "prices file", required=False, default={}) or {}
    merged = dict(old.get("quotes") or {}) if isinstance(old, dict) else {}
    for s, q in quotes.items():
        merged[s] = {"price": q["price"], "time": q.get("time"), "prev_close": q.get("prev_close"),
                     "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                     "source": q.get("source", "yahoo"), "fetched": stamp(now)}
    doc = {"created_by": "trader-alerts", "source": "yahoo", "at": stamp(now),
           "note": ("Delayed, unofficial quotes for alerts and sizing checks. Your broker's price is the one "
                    "that fills. 'time' is the quote's own time, 'fetched' when it was read."),
           "quotes": merged}
    M.write_json(path, doc)
    return doc


def quote_age_min(q, now):
    t = from_stamp(q.get("time"))
    if t is None:
        return None
    return int((now - t).total_seconds() // 60)


# ---------------------------------------------------------------- rules

def rule_id(rule):
    return rule.get("id") or "%s-%s" % (str(rule.get("symbol") or rule.get("underlying") or "market").lower(),
                                        rule["type"])


def validate(rule):
    t = rule.get("type")
    rid = rule.get("id")
    if t not in TYPES:
        raise AlertError("rule %s: unknown type %r (one of %s)" % (rid, t, ", ".join(TYPES)))
    sym = str(rule.get("symbol") or "").upper()
    if t not in NO_QUOTE:
        if not sym:
            raise AlertError("rule %s needs a symbol" % rid)
    if sym and not (M.valid_symbol(sym) or DERIV_RE.match(sym)):
        raise AlertError("rule %s: %r is not an NSE symbol" % (rid, sym))
    if t in LEVEL_TYPES and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rid)
    if t in LEVEL_TYPES and str(rule.get("side") or "buy").lower() not in ("buy", "sell"):
        raise AlertError("rule %s: side must be buy or sell" % rid)
    if t in PCT_TYPES and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rid)
    if t == "index_move" and not M.is_index(sym):
        raise AlertError("rule %s: index_move wants an index (%s)" % (rid, ", ".join(M.INDEXES)))
    if t == "circuit" and not isinstance(rule.get("within_pct", 2.0), (int, float)):
        raise AlertError("rule %s: within_pct must be a number" % rid)
    if t in ("time", "expiry") and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:10\"" % rid)
    if t == "gap" and rule.get("until") and _hhmm(rule["until"]) is None:
        raise AlertError("rule %s: until must be HH:MM" % rid)
    if t == "expiry" and not rule.get("underlying"):
        raise AlertError("rule %s needs an underlying (NIFTY, SENSEX, RELIANCE ...)" % rid)
    if t == "square_off":
        if rule.get("segment", "equity") not in ("equity", "fno"):
            raise AlertError("rule %s: segment must be equity or fno" % rid)
        if not isinstance(rule.get("minutes_before", 10), (int, float)):
            raise AlertError("rule %s: minutes_before must be a number" % rid)
    if rule.get("date") and not DATE_RE.match(str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rid)


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(st, today):
    return bool(st) and str(st)[:10] == today


def square_off_time(rule, cfg):
    """(auto square-off time, ring time, broker, fee) for a square_off rule."""
    broker = str(rule.get("broker") or (cfg or {}).get("broker") or "zerodha").lower()
    seg = rule.get("segment", "equity")
    if rule.get("auto_square_off"):
        auto = _hhmm(rule["auto_square_off"])
        if auto is None:
            raise AlertError("rule %s: auto_square_off must be HH:MM" % rule.get("id"))
        fee = None
    elif broker in M.BROKERS:
        auto = M.parse_hhmm(M.BROKERS[broker]["auto_square_off"][seg])
        fee = M.BROKERS[broker].get("square_off_fee")
    else:
        raise AlertError("no auto square-off time known for broker %r: put \"auto_square_off\": \"15:20\" "
                         "on the rule (your broker's app shows it)" % broker)
    return auto, _minus(auto, float(rule.get("minutes_before", 10))), broker, fee


def _band_limits(rule, cfg, sym, prev_close):
    band = rule.get("band_pct")
    if band is None:
        band = ((cfg or {}).get("bands") or {}).get(sym)
    if isinstance(band, dict) and isinstance(band.get("upper"), (int, float)) \
            and isinstance(band.get("lower"), (int, float)):
        return {"band_pct": band.get("band_pct"), "upper": float(band["upper"]), "lower": float(band["lower"])}
    if isinstance(band, (int, float)) and prev_close:
        return M.band_limits(prev_close, band)
    return None


def _session_words(status):
    if status["open"]:
        return "the normal market closes at 15:30"
    if status["phase"] == "closing-session":
        return "the closing session runs to 16:00 (trades at the closing price only)"
    if status["phase"] in ("pre-market", "pre-open", "pre-open-matching"):
        return "the normal market opens at 09:15"
    return "the market has closed"


def _exit_hint(sym):
    return (" If %s is still open, exit it: trader-broker flatten --symbol %s builds the exit ticket, "
            "then trader-risk and your yes." % (sym, sym))


def evaluate(rules, quotes, status, state, now, cfg=None, notes=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}.
    notes (a list) collects rules that could not be checked and why."""
    cfg = cfg if cfg is not None else dict(M.DEFAULT_CONFIG)
    notes = notes if notes is not None else []
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    t = now.time()
    st = stamp(now)
    trading = M.trading_day(now.date())
    rings = []

    def ring(rid, sym, msg):
        rings.append({"id": rid, "symbol": sym, "message": msg})
        rung[rid] = st

    for r in rules:
        validate(r)
        rid = rule_id(r)
        if not active(r, today):
            continue
        typ = r["type"]
        sym = str(r.get("symbol") or "").upper()
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                extra = [i for i in ("NIFTY", "SENSEX") if M.is_expiry_day(i, now.date())]
                ring(rid, "", "Opening bell: the NSE normal market is open (09:15-15:30 IST, it is %s)%s."
                     % (M.hm(t), ("; %s expiry today" % " and ".join(extra)) if extra else ""))
            continue
        if typ == "time":
            at = _hhmm(r["at"])
            if trading and t >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s), %s." % (label[:1].upper(), label[1:], M.hm(t), M.hm(at),
                                                                _session_words(status))
                if sym:
                    msg += _exit_hint(sym)
                ring(rid, sym, msg)
            continue
        if typ == "square_off":
            auto, at, broker, fee = square_off_time(r, cfg)
            if trading and t >= at and not _rang_today(rung.get(rid), today):
                seg = "F&O" if r.get("segment") == "fno" else "equity"
                msg = ("Square-off reminder: %s squares off open MIS (intraday) %s positions itself at %s IST"
                       % (broker, seg, M.hm(auto)))
                if fee:
                    msg += " and charges about %s plus GST per order for it" % M.inr(fee, paise=False)
                msg += ". It is %s. Exit yourself before then, at a price you choose." % M.hm(t)
                if sym:
                    msg += _exit_hint(sym)
                note = M.BROKER_NOTES.get(broker)
                if note:
                    msg += " (Times: %s.)" % note
                ring(rid, sym, msg)
            continue
        if typ == "expiry":
            und = str(r["underlying"]).upper()
            at = _hhmm(r["at"])
            if M.is_expiry_day(und, now.date()) and t >= at and not _rang_today(rung.get(rid), today):
                kind = next((e["kind"] for e in M.expiries(und, now.date(), weeks=1, months=1)
                             if e["date"] == today), "")
                msg = ("Expiry day: %s %s contracts expire today (%s). It is %s IST. "
                       % (und, kind, now.strftime("%A %d %b"), M.hm(t)))
                if M.is_index(und):
                    msg += ("Index options are cash-settled at the close; an in-the-money long option held to "
                            "expiry pays STT of %.2f%% on its intrinsic value. Premiums can swing hard this "
                            "afternoon." % (M.STT_OPTION_EXERCISE * 100))
                else:
                    msg += ("Stock F&O held to expiry is PHYSICALLY settled: an in-the-money position means "
                            "taking or giving delivery of the shares. Close it or roll it before the close.")
                ring(rid, und, msg)
            continue

        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        q_day = (q.get("time") or "")[:10]
        what = "%s premium" % sym if is_derivative(sym, r) else sym
        if typ in LEVEL_TYPES:
            side = str(r.get("side") or "buy").lower()
            if typ in ("stop", "target"):
                up = (typ == "target") == (side == "buy")
            else:
                up = typ == "above"
            level = float(r["price"])
            hit = p >= level if up else p <= level
            if hit and rid not in rung:
                label = {"stop": "stop", "target": "target"}.get(typ, "alert level")
                ring(rid, sym, "%s %s %s: %s your %s %s%s." % (
                    what, "is" if status["open"] else "last", M.inr(p),
                    "at or above" if up else "at or below", label, M.inr(level),
                    (" (" + r["note"] + ")") if r.get("note") else ""))
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
            continue
        if q_day and q_day != today:
            notes.append("%s: the latest quote is from %s, not today: %s not checked" % (sym, q_day, typ))
            continue
        if typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                ring(rid, sym, "%s moved %+.2f%% to %s (from %s)." % (what, move, M.inr(p), M.inr(ref)))
        elif typ == "circuit":
            lim = _band_limits(r, cfg, sym, q.get("prev_close"))
            if not lim:
                notes.append("%s: no fixed price band known, so the circuit rule was not checked. F&O stocks "
                             "have dynamic bands; for others set \"bands\": {\"%s\": 5} in trader/config.json "
                             "or --band on the rule (NSE shows the band on the quote page)" % (sym, sym))
                continue
            within = float(r.get("within_pct", 2.0))
            side = M.near_circuit(p, lim, within)
            if side and not _rang_today(rung.get(rid), today):
                level = lim[side]
                band = ("%s%% band" % _n(lim["band_pct"])) if lim.get("band_pct") else "band"
                ring(rid, sym, "%s %s is within %s%% of its %s circuit %s (%s, previous close %s). At the "
                     "circuit only one side trades: %s, so an exit order may not fill." % (
                         sym, M.inr(p), _n(within), side, M.inr(level), band, M.inr(q.get("prev_close")),
                         "there may be no sellers" if side == "upper" else "there may be no buyers"))
        elif typ == "gap":
            until = _hhmm(r.get("until") or DEFAULT_GAP_UNTIL)
            if t < M.OPEN or t > until or q.get("open") is None or not q.get("prev_close"):
                continue
            gp = M.gap_pct(q["open"], q["prev_close"])
            want = r.get("direction")
            if abs(gp) >= r["pct"] and (not want or (want == "up") == (gp > 0)) \
                    and not _rang_today(rung.get(rid), today):
                ring(rid, sym, "%s opened at %s, a %s of %+.2f%% on the previous close %s. Opening-range rules "
                     "in this pack wait for the first bars to close before any entry." % (
                         sym, M.inr(q["open"]), M.classify_gap(gp), gp, M.inr(q["prev_close"])))
        elif typ == "index_move":
            ref = r.get("ref") or q.get("prev_close")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            if abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0)) \
                    and not _rang_today(rung.get(rid), today):
                ring(rid, sym, "%s moved %+.2f%% to %s (previous close %s). Market-wide circuit breakers halt "
                     "all equity and F&O trading when NIFTY 50 or SENSEX moves 10%%, 15%% or 20%%." % (
                         sym, move, "{:,.2f}".format(p), "{:,.2f}".format(float(ref))))
            level, desc = M.index_circuit_halt(move, t)
            key = "%s:mwcb%d" % (rid, level)
            if level and not _rang_today(rung.get(key), today):
                ring(key, sym, "MARKET-WIDE CIRCUIT BREAKER: %s is %+.2f%% on the day, past the %d%% level at %s "
                     "IST: %s. Orders cannot trade during a halt." % (sym, move, level, M.hm(t), desc))
    state["last_check"] = st
    return rings, state


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> stop, target and flat-by time rules for the plan's day.
    Option / futures plans watch the contract's premium (quotes from trader-fno or --price)."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    inst = plan.get("instrument") or {"type": "EQ"}
    itype = str(inst.get("type") or "EQ").upper()
    deriv = itype in DERIV_TYPES
    sym = str(plan["symbol"]).upper()
    if deriv:
        sym = str(inst.get("tradingsymbol") or "").upper()
        if not sym and inst.get("underlying") and inst.get("expiry") and inst.get("strike") and itype != "FUT":
            sym = M.nse_option_symbol(inst["underlying"], inst["expiry"], inst["strike"], itype)
        if not sym:
            raise AlertError("the option plan has no instrument.tradingsymbol")
    side = str(plan.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise AlertError("the plan's side must be buy or sell, got %r" % side)
    levels = {}
    for key in ("stop", "target"):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        levels[key] = float(v)
    entry = plan.get("entry")
    if isinstance(entry, (int, float)):
        ok = levels["stop"] < entry < levels["target"] if side == "buy" else levels["target"] < entry < levels["stop"]
        if not ok:
            raise AlertError("the plan's stop %s and target %s are on the wrong sides of the entry %s for a %s"
                             % (_n(levels["stop"]), _n(levels["target"]), _n(entry), side))
    out = []
    base = sym.lower()
    for key in ("stop", "target"):
        label = plan.get("%s_label" % key)
        r = {"id": "%s-plan-%s" % (base, key), "symbol": sym, "type": key, "price": levels[key], "side": side,
             "note": "plan %s%s" % (key, (": " + label) if label else "")}
        if deriv:
            r["premium"] = True
            r["instrument"] = inst
        if str(plan.get("exchange") or "NSE").upper() == "BSE":
            r["exchange"] = "BSE"
        out.append(r)
    flat = plan.get("flat_by") or cfg_flat_by or M.DEFAULT_CONFIG["flat_by"]
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:10" % flat)
    out.append({"id": "%s-flat-by" % base, "symbol": sym, "type": "time", "at": M.hm(_hhmm(flat)),
                "label": "flat by %s" % M.hm(_hhmm(flat))})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = M.read_json(a.portfolio, "portfolio", required=False, default={}) or {}
    pos = pf.get("positions") if isinstance(pf, dict) else None
    if isinstance(pos, dict):
        for key, p in pos.items():
            inst = (p or {}).get("instrument") or {}
            if str(inst.get("type") or "EQ").upper() == "EQ":
                syms.append(str(key).split("|")[0].upper())
    elif isinstance(pos, list):
        for p in pos:
            syms.append(str(p.get("symbol") or p.get("ticker") or "").upper())
    plan = M.read_json(a.plan, "plan", required=False, default={}) or {}
    if isinstance(plan, dict) and plan.get("symbol"):
        if str((plan.get("instrument") or {}).get("type") or "EQ").upper() == "EQ":
            syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _next_session(now):
    d, t = now.date(), now.time()
    if M.trading_day(d) and t < M.OPEN:
        return d
    return M.next_trading_day(d)


def clock_doc(now):
    st = M.market_status(now)
    doc = dict(st)
    doc["weekday"] = now.strftime("%A")
    if not st["open"]:
        nd = _next_session(now)
        doc["next_open"] = "%s 09:15" % nd.isoformat()
    doc["expiries"] = {}
    for idx in ("NIFTY", "SENSEX"):
        e = M.next_expiry(idx, now.date())
        if e:
            doc["expiries"][idx] = e.isoformat()
    special = []
    for d, sp in sorted(M.SPECIAL_SESSIONS.items()):
        if now.date() <= d <= now.date() + datetime.timedelta(days=14):
            special.append({"date": d.isoformat(), "weekday": d.strftime("%A"), "name": sp["name"],
                            "start": sp["start"], "end": sp["end"]})
    doc["special_sessions"] = special
    return doc


def cmd_clock(a):
    now = M.parse_now(a.now)
    doc = clock_doc(now)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0 if doc["open"] else 1
    print("India %s %s IST (%s): NSE %s, %s." % (doc["date"], doc["time"], doc["weekday"],
                                                 "OPEN" if doc["open"] else "closed", doc["label"]))
    print("Sessions: pre-open order entry 09:00-09:08, matching 09:08-09:15, normal market 09:15-15:30, "
          "closing session 15:40-16:00 (closing price only).")
    if doc.get("next_open"):
        d = datetime.date.fromisoformat(doc["next_open"][:10])
        print("Next normal market: %s (%s) from 09:15, pre-open from 09:00." % (d.isoformat(), d.strftime("%A")))
    for idx, e in doc["expiries"].items():
        d = datetime.date.fromisoformat(e)
        print("Next %s expiry: %s (%s)%s" % (idx, e, d.strftime("%A"), "  <- TODAY" if e == doc["date"] else ""))
    for sp in doc["special_sessions"]:
        print("%s: %s on %s (%s), an evening session; %s." % (
            "Today" if sp["date"] == doc["date"] else "Coming up", sp["name"], sp["date"], sp["weekday"],
            ("%s-%s IST" % (sp["start"], sp["end"])) if sp["start"] else
            "NSE announces the timing by circular, so this tool does not know it: check nseindia.com"))
    if doc.get("warning"):
        print("note: " + doc["warning"])
    return 0 if doc["open"] else 1


def cmd_quote(a):
    syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    deriv = [s for s in syms if is_derivative(s)]
    syms = [s for s in syms if s not in deriv]
    now = M.parse_now(a.now)
    ex = {s: a.exchange for s in syms}
    quotes, errors = get_quotes(syms, a.offline, ex, a.interval) if syms else ({}, [])
    for s in deriv:
        errors.append("%s: option and futures premiums are not on Yahoo; read them with trader-fno" % s)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
        return 0 if quotes else 1
    for s in syms:
        q = quotes.get(s)
        if not q:
            continue
        chg = ""
        if q.get("prev_close"):
            chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1), M.inr(q["prev_close"]))
        age = quote_age_min(q, now)
        when = ""
        if q.get("time"):
            when = "  at %s %s IST" % (q["time"][:10], q["time"][11:16])
            if age is not None and 0 <= age < 24 * 60:
                when += ", %d min old" % age
        print("%-10s %s%s%s  [%s]" % (s, M.inr(q["price"]), chg, when, q["source"]))
    for e in errors:
        print("error: " + e, file=sys.stderr)
    if errors and quotes:
        print("partial: quotes for %d of %d symbols." % (len(quotes), len(syms) + len(deriv)))
    if a.write and quotes:
        print("Wrote %d quote(s) to %s." % (len(quotes), a.write))
    print("Quotes are free, delayed and unofficial (Yahoo; Stooq does not cover NSE): for alerts only. "
          "Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    cfg = M.load_config(a.config)
    doc = M.read_json(a.rules, "alert rules", required=False)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    for r in rules:
        validate(r)
    now = M.parse_now(a.now)
    status = M.market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    manual = parse_price_overrides(a.price, now)
    saved = M.read_json(a.prices_out, "prices file", required=False, default={}) or {}
    need, ex, quotes, notes = [], {}, dict(manual), []
    for r in watched:
        if r["type"] in NO_QUOTE:
            continue
        sym = str(r["symbol"]).upper()
        if sym in quotes or sym in need:
            continue
        if is_derivative(sym, r):
            q = saved_quote(saved, sym, now)
            if q:
                quotes[sym] = q
            else:
                notes.append("%s: no premium quote (option and futures prices are not on Yahoo). Read it with "
                             "trader-fno, or pass --price %s=PREMIUM" % (sym, sym))
            continue
        need.append(sym)
        ex[sym] = str(r.get("exchange") or cfg.get("exchange") or "NSE").upper()
    fetched, errors = (get_quotes(need, a.offline, ex, a.interval) if need else ({}, []))
    quotes.update(fetched)
    state = M.read_json(a.state, "alert state", required=False, default={}) or {}
    rings, state = evaluate(rules, quotes, status, state, now, cfg, notes)
    if not a.no_write:
        M.write_json(a.state, state)
        to_save = dict(fetched)
        to_save.update(manual)
        if to_save:
            write_prices(a.prices_out, to_save, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors, "notes": notes,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
        return 0 if rings else 1
    print("NSE %s (%s %s IST, %s)." % ("OPEN" if status["open"] else "closed", status["date"], status["time"],
                                       status["label"]))
    for r in rings:
        print("ALERT " + r["message"])
    if not rings:
        print("Nothing hit. %d rule(s) watched today%s." % (
            len(watched), (", " + ", ".join("%s %s" % (s, M.inr(quotes[s]["price"])) for s in quotes))
            if quotes else ""))
    for n in notes:
        print("note: " + n)
    for e in errors:
        print("error: " + e)
    if errors and need:
        print("partial: quotes for %d of %d symbols; rules on the others were not checked."
              % (len(fetched), len(need)))
    if rings:
        print("An alert is information, not an order. Most Indian brokers have no bracket orders: your "
              "stop-loss is a separate SL-M order (SL for options) placed after the entry filled, so check in "
              "your broker app that it is still open, and record any fill (ticket.py fill). To exit yourself: "
              "ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def _save_rules(path, doc):
    M.write_json(path, doc)


def cmd_add(a):
    doc = M.read_json(a.rules, "alert rules", required=False, default=None) or {"rules": []}
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = M.read_json(a.plan, "plan", required=False)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = M.load_config(a.config)
        new = rules_from_plan(plan, cfg.get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = M.read_json(a.state, "alert state", required=False, default={}) or {}
        if state.get("rung"):
            for k in list(state["rung"]):
                if k in ids:
                    del state["rung"][k]
            M.write_json(a.state, state)
        _save_rules(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        if any(r.get("premium") for r in new):
            print("These watch the option premium. Yahoo has no NSE option quotes: run trader-fno so a fresh "
                  "premium lands in trader/prices.json, or pass check --price %s=PREMIUM." % new[0]["symbol"])
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; your SL-M order at the broker is what actually stops you out.")
        return 0
    sym = a.symbol.upper() if a.symbol else None
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if sym:
            rule["symbol"] = sym
    elif a.square_off:
        rule = {"id": a.id or ("square-off-%s" % a.segment), "type": "square_off", "segment": a.segment,
                "minutes_before": a.minutes_before}
        if a.broker:
            rule["broker"] = a.broker.lower()
        if sym:
            rule["symbol"] = sym
    elif a.expiry:
        und = a.expiry.upper()
        at = a.ring_at or "13:30"
        rule = {"id": a.id or ("%s-expiry-%s" % (und.lower(), at.replace(":", ""))), "type": "expiry",
                "underlying": und, "at": at}
    else:
        if not sym:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell, --square-off, --expiry)")
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
        elif a.gap is not None:
            rule = {"type": "gap", "pct": a.gap}
            if a.until:
                rule["until"] = a.until
        elif a.index_move is not None:
            rule = {"type": "index_move", "pct": a.index_move}
        elif a.circuit is not None:
            rule = {"type": "circuit", "within_pct": a.circuit}
            if a.band is not None:
                rule["band_pct"] = a.band
        else:
            raise AlertError("give one of --above, --below, --stop, --target, --pct, --gap, --index-move or --circuit")
        if a.direction and rule["type"] in PCT_TYPES:
            rule["direction"] = a.direction
        if a.side and rule["type"] in ("stop", "target"):
            rule["side"] = a.side
        val = rule.get("price", rule.get("pct", rule.get("within_pct")))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"].replace("_", "-"), _n(val)))},
                    symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
        if a.exchange and a.exchange.upper() == "BSE":
            rule["exchange"] = "BSE"
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _save_rules(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    if rule["type"] == "square_off":
        auto, at, broker, _ = square_off_time(rule, M.load_config(a.config))
        print("It rings at %s IST on trading days (%s auto square-off %s minus %s minutes)."
              % (M.hm(at), broker, M.hm(auto), _n(rule["minutes_before"])))
    if rule["type"] == "gap":
        print("Gap rules are checked from 09:15 to %s: schedule a check then." % (rule.get("until") or DEFAULT_GAP_UNTIL))
    return 0


def cmd_list(a):
    doc = M.read_json(a.rules, "alert rules", required=False, default={}) or {}
    state = M.read_json(a.state, "alert state", required=False, default={}) or {}
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = rule_id(r)
        rung = (state.get("rung") or {}).get(rid)
        print("%-28s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = M.read_json(a.state, "alert state", required=False, default={}) or {}
    rung = state.get("rung") or {}
    keys = [k for k in rung if k == a.id or k.startswith(a.id + ":")]
    if not keys:
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    for k in keys:
        del rung[k]
    M.write_json(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts, India).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read DIR/yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this IST time, 'YYYY-MM-DD HH:MM'")
        p.add_argument("--interval", default="1m", choices=["1m", "5m"])
        p.add_argument("--json", action="store_true")
        p.add_argument("--config", default=M.CONFIG_FILE)

    p = sub.add_parser("clock", help="is NSE open? which session?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--price", action="append", metavar="SYMBOL=PRICE",
                   help="a price you read yourself (option premiums); repeat for more")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--config", default=M.CONFIG_FILE)
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--gap", type=float, metavar="PCT", help="opening gap of PCT%% or more")
    g.add_argument("--index-move", type=float, metavar="PCT", help="index moved PCT%% (NIFTY, SENSEX ...)")
    g.add_argument("--circuit", type=float, metavar="WITHIN_PCT", help="within WITHIN_PCT%% of a circuit limit")
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--square-off", action="store_true", help="before the broker's MIS auto square-off")
    g.add_argument("--expiry", metavar="UNDERLYING", help="on expiry days of NIFTY, SENSEX, a stock ...")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--segment", default="equity", choices=["equity", "fno"], help="for --square-off")
    p.add_argument("--broker", help="for --square-off (default: broker in trader/config.json)")
    p.add_argument("--minutes-before", type=float, default=10, help="for --square-off (default 10)")
    p.add_argument("--ring-at", metavar="HH:MM", help="for --expiry (default 13:30)")
    p.add_argument("--band", type=float, help="the stock's fixed price band in %% (2, 5, 10, 20) for --circuit")
    p.add_argument("--until", metavar="HH:MM", help="for --gap: check until this time (default 09:16)")
    p.add_argument("--side", choices=["buy", "sell"], help="for --stop/--target on a short (sell) trade")
    p.add_argument("--exchange", choices=["NSE", "BSE"])
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
