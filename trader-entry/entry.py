#!/usr/bin/env python3
"""Entry Timer for trader-entry (India edition): watches a setup on an NSE/BSE
stock (or index, for analysis) bar by bar and says the moment to get in.
Standard library only. It never places an order.

    python3 entry.py check RELIANCE                      # opening-range breakout, 5-minute bars
    python3 entry.py check RELIANCE --now "2026-10-28 09:50" --offline RELIANCE.json
    python3 entry.py check TCS --setup level --level 3120.50   # first close above a price
    python3 entry.py check SBIN --setup vwap_reclaim           # a close back above VWAP
    python3 entry.py check TMPV --setup gap                    # gap-and-go after the first 15 minutes
    python3 entry.py check INFY --side sell                    # a short: needs allow_short and MIS
    python3 entry.py check NIFTY                               # index: analysis only, trade via trader-fno
    python3 entry.py watch RELIANCE --until 14:45              # one line per closed bar
    python3 entry.py watchlist                                 # every hit in trader/watchlist.json

Session (NSE/BSE cash and F&O, IST, no daylight saving): pre-open call
auction 09:00-09:15 sets the opening price and makes no bars; the normal
market runs 09:15-15:30 = 75 five-minute bars (375 one-minute bars). The
first bar is 09:15-09:20; a bar's time is its CLOSE time (the 09:45 bar
closes at 09:50) and a bar counts only once it has closed.

Setups:
  orb           opening-range breakout. The range is the first --or-minutes
                (default 15: the 09:15, 09:20 and 09:25 bars, set at 09:30).
                Long: the first completed bar after the range that CLOSES above
                the range high. Short: closes below the range low.
  level         the first completed bar that closes above (short: below) --level.
  vwap_reclaim  after the range, the first bar that closes back above the
                session VWAP after the bar before it closed below (short: the
                mirror). Needs volume, so not for an index.
  gap           gap-and-go: the stock opened 2% or more beyond the previous
                close (up for a long, down for a short), the gap did not fill in
                the first 15 minutes, and a bar then closes beyond the range.

Time limits (trader/config.json, IST): no_entries_before (09:30) and
no_entries_after (14:45). On an expiry day of the instrument (NIFTY weekly on
Tuesday, monthly index and stock F&O on the last Tuesday, SENSEX on Thursday,
moved earlier for a holiday) an F&O entry also stops at expiry_day_last_entry
(13:30). Brokers square off MIS positions around 15:15-15:26 on their own.

Filters, reported in words and in trader/entry.json: the gap against the
previous close, VWAP so far (price above or below it), and circuit
proximity when the stock's price band is known ("bands": {"SYM": 5} in the
config, or --band 5). A long within avoid_circuit_within_pct (2%) of the upper
circuit (a short near the lower one) is blocked: it can lock at the circuit
and you may not be able to exit. Symbols on the config's blocked, asm_gsm or
(for F&O) fno_ban lists, and trade-for-trade series (block_series: BE, BZ ...),
are refused before any data is read.

Shorts: only with "allow_short": true and product MIS. A cash short in India
must be bought back the same day; one that is not covered goes to the
exchange auction with a penalty.

Data: Yahoo's free chart JSON through market.Net (unofficial, delayed, can
break). --offline PATH reads a saved chart JSON (.json/.json.gz) or
PATH/<SYMBOL>.json. Without --now an offline file is read as of its own time.

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

Err = M.MarketError
INTERVALS = {"1m": 60, "5m": 300}
SETUPS = ("orb", "level", "vwap_reclaim", "gap")
FNO = ("FUT", "CE", "PE")
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
GAP_LARGE = 2.0  # percent: a gap-and-go
NET_FACTORY = M.Net
SETUP_WORDS = {"orb": "opening-range breakout", "level": "close beyond a level",
               "vwap_reclaim": "VWAP reclaim", "gap": "gap-and-go"}


# ---------------------------------------------------------------- small helpers

def r2(x):
    return None if x is None else round(float(x), 2) + 0.0


def short(t):
    """9:50 AM, for sentences."""
    return M.ampm(t)


def stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


def norm_symbol(raw, exchange):
    """'reliance' -> ('RELIANCE', 'NSE'); 'RELIANCE.BO' -> ('RELIANCE', 'BSE')."""
    s = str(raw or "").strip().upper()
    if s.endswith(".BO"):
        exchange, s = "BSE", s[:-3]
    elif s.endswith(".NS"):
        exchange, s = "NSE", s[:-3]
    if s.startswith("^"):
        s = M.plain_symbol(s)
    if not M.valid_symbol(s):
        raise Err("%r is not an NSE/BSE trading symbol (like RELIANCE, M&M, BAJAJ-AUTO or NIFTY)" % raw)
    if M.is_index(s):
        exchange = M.exchange_of(s)
    return s, (exchange or "NSE").upper()


# ---------------------------------------------------------------- data

def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds `day` and the session before it."""
    if day > today:
        raise Err("%s is in the future" % day)
    n = M.trading_days_between(day, today)
    if n <= 3:
        return "5d"
    if interval == "1m":
        raise Err("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if n <= 18:
        return "1mo"
    if n <= 57:
        return "60d"
    raise Err("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def offline_file(path, sym, exchange):
    if os.path.isdir(path):
        ys = M.yahoo_symbol(sym, exchange)
        for name in (sym, ys, sym.replace("&", "_")):
            for ext in (".json", ".json.gz"):
                p = os.path.join(path, name + ext)
                if os.path.exists(p):
                    return p
        raise Err("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise Err("no offline file %s" % path)
    return path


def get_chart(sym, interval, day, today, exchange="NSE", offline=None, net=None):
    """Bars for one symbol from Yahoo (through market.Net) or a saved file."""
    if offline:
        f = offline_file(offline, sym, exchange)
        try:
            text = M.read_text_maybe_gz(f)
        except OSError as e:
            raise Err("could not read %s: %s" % (f, e))
        series = M.parse_chart(text, sym)
    else:
        net = net or NET_FACTORY()
        series = M.parse_chart(net.chart(sym, chart_range(day, today, interval), interval, exchange), sym)
    meta = series["meta"]
    want = M.yahoo_symbol(sym, exchange).upper()
    got = str(meta.get("symbol") or want).upper()
    if got != want:
        raise Err("the chart holds %s, not %s (%s)" % (got, sym, want))
    gran = meta.get("dataGranularity")
    if gran and gran != interval:
        raise Err("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    bars = M.bars_from_series(series, INTERVALS[interval])
    dt = meta.get("regularMarketTime")
    return {"symbol": sym, "exchange": exchange, "yahoo": want, "meta": meta, "bars": bars,
            "data_time": M.ist_from_ts(dt) if isinstance(dt, (int, float)) else None}


def resolve_prev_close(chart, day, override=None):
    """(previous close, where it came from)."""
    if override:
        return float(override), "given"
    meta = chart.get("meta") or {}
    dates = sorted({b["dt"].date() for b in chart["bars"]})
    if dates and dates[-1] == day and meta.get("previousClose"):
        return float(meta["previousClose"]), "NSE close (Yahoo previousClose)"
    prev = M.prev_trading_day(day)
    pb = [b for b in chart["bars"] if b["dt"].date() == prev]
    if pb:
        return float(pb[-1]["c"]), ("last trade of %s (approximate: the official close is the volume-weighted "
                                    "price of the last 30 minutes)" % prev.isoformat())
    if dates and dates[0] == day and meta.get("chartPreviousClose"):
        return float(meta["chartPreviousClose"]), "Yahoo chartPreviousClose"
    return None, None


# ---------------------------------------------------------------- config, lists, sides

def refusals(sym, cfg, instrument="EQ", series="EQ"):
    """(refuse reasons, warnings) from the config lists. Checked before any data is read."""
    bad, warn = [], []
    up = lambda xs: [str(x).upper() for x in (xs or [])]  # noqa: E731
    if sym in up(cfg.get("blocked")):
        bad.append("%s is on your blocked list (config \"blocked\")" % sym)
    if sym in up(cfg.get("asm_gsm")):
        bad.append("%s is under ASM/GSM surveillance (config \"asm_gsm\"): higher margins, trade-for-trade "
                   "or price limits apply; this pack does not trade it" % sym)
    if series and str(series).upper() in up(cfg.get("block_series")):
        bad.append("%s trades in the %s series, trade-for-trade: every trade must be delivered, no intraday "
                   "(MIS) and no same-day exit" % (sym, str(series).upper()))
    if sym in up(cfg.get("fno_ban")):
        if instrument in FNO:
            bad.append("%s is in the F&O ban period (config \"fno_ban\"): no new F&O positions, only closing "
                       "ones" % sym)
        else:
            warn.append("%s is in the F&O ban period: the cash stock still trades, but expect jumpy prices" % sym)
    return bad, warn


def band_for(sym, cfg, flag=None):
    if flag is not None:
        if flag not in M.PRICE_BANDS:
            raise Err("--band is one of %s (percent)" % ", ".join(map(str, M.PRICE_BANDS)))
        return float(flag)
    bands = cfg.get("bands") or {}
    if isinstance(bands, dict):
        v = bands.get(sym) or bands.get(sym.upper())
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                raise Err("config bands[%s] must be a percent, got %r" % (sym, v))
    return None


def check_side(side, product, instrument, cfg, sym):
    side = {"long": "buy", "short": "sell"}.get(side, side)
    if side not in ("buy", "sell"):
        raise Err("--side is buy or sell")
    if instrument == "CE" and side == "sell":
        raise Err("a call (CE) gains when %s rises: for a bearish signal plan a put (PE)" % sym)
    if instrument == "PE" and side == "buy":
        raise Err("a put (PE) gains when %s falls: for a bullish signal plan a call (CE)" % sym)
    if side == "sell" and instrument == "EQ":
        if not cfg.get("allow_short"):
            raise Err("short selling is off (\"allow_short\": false in trader/config.json). In India a cash "
                      "short is intraday only (MIS); turn it on only if you mean it")
        if product != "MIS":
            raise Err("a cash short must be MIS (intraday): you cannot sell shares you do not hold for "
                      "delivery (CNC); an uncovered short goes to the exchange auction with a penalty")
    return side


# ---------------------------------------------------------------- the rule

def _bad_inputs(setup, or_minutes, interval, level):
    if interval not in INTERVALS:
        raise Err("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step or or_minutes >= M.SESSION_MINUTES:
        raise Err("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in SETUPS:
        raise Err("--setup is one of %s" % ", ".join(SETUPS))
    if setup == "level" and level is None:
        raise Err("--setup level needs --level PRICE (for example a 20-day high)")
    if setup != "level" and level is not None:
        raise Err("--level is for --setup level")


def evaluate(chart, now, setup="orb", or_minutes=15, interval="5m", level=None, min_volume_x=None,
             side="buy", cfg=None, ctx=None):
    """Pure function: the setup's status at `now` (naive IST) from the chart's bars."""
    cfg = dict(M.DEFAULT_CONFIG, **(cfg or {}))
    ctx = ctx or {}
    _bad_inputs(setup, or_minutes, interval, level)
    step = INTERVALS[interval]
    n_or = or_minutes * 60 // step
    long = side == "buy"
    day = now.date()
    sym = chart["symbol"]
    instrument = ctx.get("instrument") or "EQ"
    is_index = M.is_index(sym)
    fno = instrument in FNO or is_index
    res = {"symbol": sym, "exchange": chart.get("exchange", "NSE"), "setup": setup, "side": side,
           "date": day.isoformat(), "interval": interval, "or_minutes": or_minutes,
           "now": now.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting", "phase": "", "reason": "",
           "opening_range": None, "level": None, "trigger": None, "last": None, "forming": None,
           "min_volume_x": min_volume_x, "product": ctx.get("product") or "MIS", "instrument": instrument,
           "index": is_index, "prev_close": None, "prev_close_source": None, "gap_pct": None, "gap_class": None,
           "gap_and_go": False, "vwap": None, "vwap_side": None, "blocked": False, "expiry_day": False,
           "circuit": {"band_pct": None, "upper": None, "lower": None, "near": None},
           "warnings": list(ctx.get("warnings") or []), "calendar_known": M.calendar_known(day.year),
           "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
    if is_index:
        res["warnings"].append("%s is an index: you cannot buy it in the cash market. Use this for analysis; "
                               "for an option trade go to trader-fno, then trader-exit --instrument CE|PE" % sym)
    if not M.trading_day(day):
        name = M.holiday_name(day)
        why = ("NSE holiday: %s" % name) if name else "weekend"
        res.update(status="no-data", phase="closed", reason="NSE is closed today (%s)" % why,
                   next_open="%s 09:15" % M.next_trading_day(day).isoformat())
        sp = M.SPECIAL_SESSIONS.get(day)
        if sp:
            res["warnings"].append("%s today: a short evening session (%s). This tool does not time entries in "
                                   "it: thin, ceremonial trading" % (sp["name"], ("%s-%s" % (sp["start"], sp["end"]))
                                                                     if sp["start"] else "timing by NSE circular"))
        return res

    open_dt = datetime.datetime.combine(day, M.OPEN)
    close_dt = datetime.datetime.combine(day, M.CLOSE)
    before = max(open_dt, datetime.datetime.combine(day, M.parse_hhmm(cfg["no_entries_before"], "no_entries_before")))
    after_t = M.parse_hhmm(cfg["no_entries_after"], "no_entries_after")
    underlying = (ctx.get("underlying") or sym).upper()
    if fno and M.is_expiry_day(underlying, day):
        res["expiry_day"] = True
        exp_t = M.parse_hhmm(cfg.get("expiry_day_last_entry") or "13:30", "expiry_day_last_entry")
        if exp_t < after_t:
            after_t = exp_t
            res["expiry_cutoff"] = True
        res["warnings"].append("%s expiry day: F&O entries stop at %s (expiry_day_last_entry); premiums decay "
                               "fast and gamma makes the last hours jumpy" % (underlying, M.hm(after_t)))
    elif not fno and M.is_expiry_day("NIFTY", day):
        res["warnings"].append("NIFTY weekly expiry today: index-heavy stocks can swing late in the day")
    cutoff = min(close_dt, datetime.datetime.combine(day, after_t))
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=M.hm(M.OPEN), closes=M.hm(M.CLOSE), no_entries_before=M.hm(before.time()),
               no_entries_after=M.hm(cutoff.time()))

    by_slot = {}
    for b in chart["bars"]:
        bdt = b["dt"]
        if bdt.date() != day or not (open_dt <= bdt < close_dt):
            continue
        slot = M.bar_slot(bdt, step // 60)
        by_slot[slot] = dict(b, slot=slot, end=bdt + datetime.timedelta(seconds=step))
    bars, pending = [], None
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        if b["end"] <= now:
            # closed by the clock; on live data it counts once Yahoo has caught up
            if dt is None or dt >= b["end"] or slot < top or now >= b["end"] + datetime.timedelta(seconds=SETTLE):
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": M.hm(b["dt"]), "closes": short(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (dt.strftime("%H:%M:%S"), M.hm(b["dt"])))
        elif b["dt"] <= now:
            res["forming"] = {"bar_start": M.hm(b["dt"]), "closes": short(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    pc, pc_src = resolve_prev_close(chart, day, ctx.get("prev_close"))
    res["prev_close"], res["prev_close_source"] = r2(pc), pc_src
    band = ctx.get("band_pct")
    limits = M.band_limits(pc, band) if (band and pc) else None
    within = float(cfg.get("avoid_circuit_within_pct") or 2.0)
    if limits:
        res["circuit"].update(band_pct=band, upper=limits["upper"], lower=limits["lower"])
    if now < open_dt:
        res.update(phase="pre-open", reason="NSE's normal market opens at 9:15 AM (the 9:00-9:15 pre-open "
                   "auction sets the opening price and makes no bars)%s" % (
                       "; the %d-minute range is set at %s" % (or_minutes, short(or_end))
                       if setup != "level" else ""))
        return res
    if bars and bars[0]["slot"] == 0 and pc:
        gp = M.gap_pct(bars[0]["o"], pc)
        res["gap_pct"], res["gap_class"] = r2(gp), M.classify_gap(gp)
    vols = sum(b["v"] or 0 for b in bars)
    if bars and vols > 0:
        res["vwap"] = r2(M.vwap(bars))
    elif bars:
        res["warnings"].append("no volume in the data (indexes have none on Yahoo): VWAP and volume checks "
                               "are not available")

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": now >= or_end, "avg_volume": int(round(avg_vol or 0))}
    if setup == "vwap_reclaim" and bars and vols <= 0:
        res.update(status="no-data", phase="open", reason="a VWAP reclaim needs volume, and %s has none in "
                   "the data" % sym)
        return res
    if setup != "level":
        if now < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end.time())))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
    orr = res["opening_range"]
    if setup in ("orb", "gap"):
        lvl = orr["high"] if long else orr["low"]
    elif setup == "level":
        lvl = float(level)
    else:
        lvl = None
    if setup == "gap":
        gp = res["gap_pct"]
        if gp is None:
            res.update(status="no-data", phase="open", reason="gap-and-go needs the previous close and the "
                       "9:15 bar; give --prev-close")
            return res
        if (gp < GAP_LARGE) if long else (gp > -GAP_LARGE):
            res.update(status="expired", phase="open", reason="no gap %s of %g%% or more today (opened %+.2f%% vs "
                       "the previous close %s): gap-and-go does not apply" % ("up" if long else "down", GAP_LARGE,
                                                                            gp, M.inr(pc)))
            return res
        if (orr["low"] <= pc) if long else (orr["high"] >= pc):
            res.update(status="expired", phase="open", reason="the gap filled in the first %d minutes (range %s "
                       "to %s, previous close %s): no gap-and-go" % (or_minutes, M.inr(orr["low"]),
                                                                    M.inr(orr["high"]), M.inr(pc)))
            return res
        res["gap_and_go"] = True
    res["level"] = r2(lvl)

    first_slot = 0 if setup == "level" else n_or
    trig, thin = None, []
    pv = vv = 0.0
    prev_wrong = None
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += typical * (b["v"] or 0)
        vv += b["v"] or 0
        vw_b = pv / vv if vv else None
        if setup == "vwap_reclaim":
            wrong_now = vw_b is not None and ((b["c"] < vw_b) if long else (b["c"] > vw_b))
            beyond = vw_b is not None and prev_wrong is True and ((b["c"] > vw_b) if long else (b["c"] < vw_b))
            prev_wrong = wrong_now
            b["_vw"] = vw_b
        else:
            beyond = (b["c"] > lvl) if long else (b["c"] < lvl)
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if beyond:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    if setup == "vwap_reclaim":
        res["level"] = r2(trig["_vw"]) if trig else res["vwap"]
        lvl = res["level"]
    last = bars[-1] if bars else None
    if last and lvl:
        res["last"] = {"bar_start": M.hm(last["dt"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}
    if last and res["vwap"]:
        res["vwap_side"] = "above" if last["c"] >= res["vwap"] else "below"

    if res["gap_class"] in (("gap-up",) if long else ("gap-down",)) and setup in ("orb", "gap"):
        res["gap_and_go"] = True
        res["warnings"].append("gap-and-go: it opened %+.2f%% beyond the previous close %s and is breaking the "
                               "range. Extra risk: big gaps often fill, so a smaller size and a firm stop"
                               % (res["gap_pct"], M.inr(pc)))
    if trig:
        price = trig["c"]
        vx = (trig["v"] / avg_vol) if avg_vol else None
        vw_at = trig.get("_vw") if setup == "vwap_reclaim" else None
        if vw_at is None and res["vwap"] is not None:
            sub = [b for b in bars if b["slot"] <= trig["slot"]]
            vw_at = M.vwap(sub) if sum(b["v"] or 0 for b in sub) else None
        res["trigger"] = {"time": short(trig["end"]), "close_time": M.hm(trig["end"]), "bar_start": M.hm(trig["dt"]),
                          "bar_index": trig["slot"], "price": r2(price), "volume": trig["v"], "volume_x": r2(vx),
                          "pct_over": r2(100.0 * (price / lvl - 1)) if lvl else None,
                          "vwap": r2(vw_at), "vwap_side": None if vw_at is None else
                          ("above" if price >= vw_at else "below")}
        res["vwap_side"] = res["trigger"]["vwap_side"] or res["vwap_side"]
        if vw_at is not None and (price < vw_at if long else price > vw_at):
            res["warnings"].append("price %s is %s VWAP %s: a %s against the session's average price" % (
                M.inr(price), "below" if long else "above", M.inr(vw_at), "long" if long else "short"))
        res["minutes_since"] = int((now - trig["end"]).total_seconds() // 60)
        near = M.near_circuit(price, limits, within)
        res["circuit"]["near"] = near
        if (long and near == "upper") or (not long and near == "lower"):
            edge = limits["upper"] if long else limits["lower"]
            res.update(status="expired", blocked=True, phase="open" if now < close_dt else "after-close",
                       reason="the %s bar closed %s at %s, within %g%% of the %s circuit %s (%g%% band): it can "
                              "lock at the %s circuit; you may not be able to exit. Blocked" % (
                                  M.hm(trig["dt"]), "above" if long else "below", M.inr(price), within,
                                  "upper" if long else "lower", M.inr(edge), band,
                                  "upper" if long else "lower"))
            return res
        res.update(status="triggered", phase="open" if now < close_dt else "after-close",
                   reason="the %s bar closed %s %s" % (M.hm(trig["dt"]), "above" if long else "below", {
                       "orb": "the range high" if long else "the range low", "gap": "the range high" if long else
                       "the range low", "level": "the level", "vwap_reclaim": "VWAP"}[setup]))
        if dt and dt < trig["end"]:
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a "
                                "tick or two" % dt.strftime("%H:%M:%S"))
        return res
    if last is not None:
        near = M.near_circuit(last["c"], limits, within)
        res["circuit"]["near"] = near
        if near:
            res["warnings"].append("last close %s is within %g%% of the %s circuit %s" % (
                M.inr(last["c"]), within, near, M.inr(limits[near])))
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    word = "above" if long else "below"
    what = {"orb": "close %s the range %s %s" % (word, "high" if long else "low", M.inr(lvl) if lvl else ""),
            "gap": "close %s the range %s %s" % (word, "high" if long else "low", M.inr(lvl) if lvl else ""),
            "level": "close %s %s" % (word, M.inr(lvl) if lvl else ""),
            "vwap_reclaim": "close back %s VWAP" % word}[setup]
    if now >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if now >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, short(cutoff.time()), " (expiry day)" if res.get("expiry_cutoff") else ""))
        return res
    res["phase"] = "open"
    beyond_last = lvl is not None and ((last["c"] > lvl) if long else (last["c"] < lvl))
    if beyond_last and last in thin:
        res["reason"] = "the %s bar closed %s at %s but on %.2fx the range's volume, under your %.2fx" % (
            M.hm(last["dt"]), word, M.inr(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif beyond_last and last["end"] < before:
        res["reason"] = "the %s bar closed %s at %s, but no entries before %s (config)" % (
            M.hm(last["dt"]), word, M.inr(last["c"]), short(before.time()))
    elif setup == "vwap_reclaim":
        res["reason"] = "last close %s (%s bar) is %s VWAP %s; waiting for a close back %s it" % (
            M.inr(last["c"]), M.hm(last["dt"]), res["vwap_side"] or "near", M.inr(res["vwap"]), word)
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
            M.inr(last["c"]), M.hm(last["dt"]), M.inr(abs(gap)), "under" if gap >= 0 else "over",
            "the range %s %s" % ("high" if long else "low", M.inr(lvl)) if setup in ("orb", "gap")
            else "the level %s" % M.inr(lvl), 100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now):
    """trader/entry.json, the contract shape plus the India filters."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "exchange": res["exchange"],
            "setup": res["setup"], "side": res["side"], "date": res["date"], "status": res["status"],
            "time": t.get("close_time"), "bar_start": t.get("bar_start"), "bar_index": t.get("bar_index"),
            "price": t.get("price"), "level": res.get("level"), "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "vwap": t.get("vwap") if t.get("vwap") is not None else res.get("vwap"),
            "vwap_side": res.get("vwap_side"), "gap_pct": res.get("gap_pct"), "gap_class": res.get("gap_class"),
            "gap_and_go": res.get("gap_and_go"), "prev_close": res.get("prev_close"),
            "product": res.get("product"), "instrument": res.get("instrument"), "index": res.get("index"),
            "expiry_day": res.get("expiry_day"), "no_entries_after": res.get("no_entries_after"),
            "circuit": res.get("circuit"), "blocked": res.get("blocked"),
            "warnings": res.get("warnings") or [], "source": "yahoo", "checked_at": stamp(now),
            "reason": res.get("reason")}


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT_STATUS = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    side = "long" if res["side"] == "buy" else "short"
    if res["setup"] in ("orb", "gap"):
        return "%s (%d-minute range, %s)" % (SETUP_WORDS[res["setup"]], res["or_minutes"], side)
    if res["setup"] == "level":
        return "close %s %s (%s)" % ("above" if side == "long" else "below",
                                     M.inr(res["level"]) if res["level"] is not None else "a level", side)
    return "VWAP reclaim (%s)" % side


def beyond_words(res):
    long = res["side"] == "buy"
    if res["setup"] in ("orb", "gap"):
        return "above the range high" if long else "below the range low"
    if res["setup"] == "vwap_reclaim":
        return "back above VWAP" if long else "back below VWAP"
    return "above the level" if long else "below the level"


def describe(res):
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s (%s)  %s %s IST  %s on %s bars (Yahoo: free, unofficial, delayed)" % (
        res["symbol"], res["exchange"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.combine(datetime.date(2000, 1, 1), M.OPEN)
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            M.hm(start), M.hm(end), M.inr(orr["high"]), M.inr(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    facts = []
    if res.get("gap_pct") is not None:
        facts.append("opened %+.2f%% vs the previous close %s (%s)" % (res["gap_pct"], M.inr(res["prev_close"]),
                                                                        res["gap_class"]))
    if res.get("vwap") is not None:
        facts.append("VWAP so far %s%s" % (M.inr(res["vwap"]), ", price %s VWAP" % res["vwap_side"]
                                           if res.get("vwap_side") else ""))
    c = res.get("circuit") or {}
    if c.get("upper"):
        facts.append("%g%% band: circuits %s / %s" % (c["band_pct"], M.inr(c["lower"]), M.inr(c["upper"])))
    if facts:
        out.append("Context: " + "; ".join(facts) + ".")
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t and res["status"] == "triggered":
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed %s %s (%+.2f%%%s)." % (
            t["time"], M.inr(t["price"]), t["bar_start"], t["bar_index"], beyond_words(res), M.inr(res["level"]),
            t.get("pct_over") or 0.0,
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different trade: "
                       "the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], M.inr(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
    else:
        out.append("%s  %s." % ("BLOCKED" if res.get("blocked") else label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"], M.inr(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    for w in res.get("warnings") or []:
        out.append("Note: %s." % w)
    if res["status"] == "triggered":
        out.append("Next: set the stop and target BEFORE you %s: /trader-exit (exit.py plan --from "
                   "trader/entry.json)." % ("buy" if res["side"] == "buy" else "sell"))
    if not res.get("calendar_known"):
        out.append("No %s holiday list loaded: check nseindia.com for this date." % res["date"][:4])
    return out


def bar_line(b, res):
    start = M.hm(b["dt"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    long = res["side"] == "buy"
    n_or = orr.get("of") or 0
    if res["setup"] != "level" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (M.inr(min(x["l"] for x in seen)), M.inr(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1 and orr:
            note = "opening range set: high %s, low %s" % (M.inr(orr["high"]), M.inr(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "%s: closed %s %s. %s" % ("BLOCKED" if res.get("blocked") else "TRIGGERED",
                                          "above" if long else "below", M.inr(lvl),
                                          "Near the circuit: no entry" if res.get("blocked") else
                                          "Get in now if you take it: %s, %s" % (t["time"], M.inr(t["price"])))
    elif lvl is None:
        note = ""
    elif (b["c"] > lvl) if long else (b["c"] < lvl):
        note = "%s %s" % ("above" if long else "below", M.inr(lvl))
    else:
        note = "%s %s %s" % (M.inr(abs(lvl - b["c"])), "under" if long else "over", M.inr(lvl))
    return "%5s bar  closed %s  %s" % (start, M.inr(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now) -> chart, clock() -> naive IST, sleep(seconds), emit(line)."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        day = now.date()
        if not M.trading_day(day):
            name = M.holiday_name(day)
            emit("%s: NSE is closed today (%s). Nothing to watch." % (day.isoformat(), name or "weekend"))
            return 1, None
        open_dt = datetime.datetime.combine(day, M.OPEN)
        close_dt = datetime.datetime.combine(day, M.CLOSE)
        if now < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    now.strftime("%H:%M"), "not open yet" if now < open_dt else "open, no bar closed yet",
                    short((open_dt + datetime.timedelta(seconds=step)).time())))
                said_preopen = True
            next_fetch = open_dt + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except Err as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (now.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or now >= close_dt:
            emit("%s  %s." % ("BLOCKED" if res.get("blocked") else "EXPIRED",
                               res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and now >= until:
            emit("Stopped at --until %s without a trigger. %s." % (M.hm(until), res["reason"]))
            return 1, res
        cur_slot = int((now - open_dt).total_seconds()) // step
        behind = (not bars) or bars[-1]["slot"] < cur_slot - 1
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - now).total_seconds()
        cutoff = datetime.datetime.combine(day, M.parse_hhmm(res.get("no_entries_after") or "15:30", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > now:
                wait = min(wait, (stop_at - now).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _context(a, cfg, sym, exchange, series="EQ", warnings=None):
    instrument = (a.instrument or "EQ").upper()
    product = (a.product or cfg.get("default_product") or "MIS").upper()
    bad, warn = refusals(sym, cfg, instrument, series)
    if bad:
        raise Err("refused: " + "; ".join(bad))
    side = check_side(a.side, product, instrument, cfg, sym)
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise Err("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise Err("--level must be a price above 0")
    ctx = {"exchange": exchange, "instrument": instrument, "product": product, "underlying": sym,
           "band_pct": band_for(sym, cfg, a.band), "prev_close": a.prev_close,
           "warnings": list(warnings or []) + warn}
    return side, ctx


def _opts(a, cfg, side, ctx, setup=None, level=None):
    s = setup or a.setup or "orb"
    lv = a.level if level is None else level
    _bad_inputs(s, a.or_minutes, a.interval, lv)
    return {"setup": s, "or_minutes": a.or_minutes, "interval": a.interval, "level": lv,
            "min_volume_x": a.min_volume_x, "side": side, "cfg": cfg, "ctx": ctx}


def _load(a, sym, exchange, now, net=None):
    """(chart, now). An offline file without --now is read as of its own time."""
    today = M.now_ist().date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, today, today, exchange, a.offline)
        if ch.get("data_time"):
            return ch, ch["data_time"]
        if ch["bars"]:
            return ch, ch["bars"][-1]["dt"] + datetime.timedelta(seconds=INTERVALS[a.interval])
        return ch, M.now_ist()
    now = now or M.now_ist()
    return get_chart(sym, a.interval, now.date(), max(today, now.date()), exchange, a.offline, net), now


def cmd_check(a):
    cfg = M.load_config(a.config)
    sym, exchange = norm_symbol(a.symbol, a.exchange or cfg.get("exchange") or "NSE")
    side, ctx = _context(a, cfg, sym, exchange, a.series)
    opts = _opts(a, cfg, side, ctx)
    chart, now = _load(a, sym, exchange, M.parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        M.write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = M.load_config(a.config)
    sym, exchange = norm_symbol(a.symbol, a.exchange or cfg.get("exchange") or "NSE")
    side, ctx = _context(a, cfg, sym, exchange, a.series)
    opts = _opts(a, cfg, side, ctx)
    if a.every <= 0:
        raise Err("--every must be above 0 seconds")
    start = M.parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, exchange, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = M.now_ist, time.sleep
    net = None if a.offline else NET_FACTORY()

    def provider(now):
        return get_chart(sym, a.interval, now.date(), max(M.now_ist().date(), now.date()), exchange, a.offline, net)

    until = None
    if a.until:
        until = datetime.datetime.combine(now_fn().date(), M.parse_hhmm(a.until, "--until"))

    def on_trigger(res, now):
        M.write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until, on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = M.load_config(a.config)
    doc = M.read_json(a.watchlist, "watchlist (run /trader-scanner first: it writes the hits there)")
    hits = [h for h in ((doc or {}).get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise Err("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise Err("watchlist --offline wants a folder of <SYMBOL>.json files")
    now = M.parse_now(a.now) if a.now else None
    net = None if a.offline else NET_FACTORY()   # one Net: polite pauses between symbols
    rows = []
    for h in hits:
        raw = str(h["symbol"])
        try:
            sym, exchange = norm_symbol(raw, h.get("exchange") or a.exchange or cfg.get("exchange") or "NSE")
            setup = a.setup or (h.get("setup") if h.get("setup") in SETUPS else ("level" if h.get("level") else "orb"))
            level = a.level if a.level is not None else (h.get("level") if setup == "level" else None)
            if setup == "level" and level is None:
                raise Err("needs a level: --level PRICE or a \"level\" in the watchlist")
            side, ctx = _context(a, cfg, sym, exchange, h.get("series") or a.series)
            o = _opts(a, cfg, side, ctx, setup, level)
            chart, when = _load(a, sym, exchange, now, net)
            rows.append((sym, evaluate(chart, when, **o), None))
        except Err as e:
            rows.append((raw.upper(), None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2))
    else:
        print("%-11s  %-9s  %-6s  %-13s  %-13s  %-13s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST",
                                                            "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-11s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = "blocked" if r.get("blocked") else (
                SHORT_STATUS.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered")
            print("%-11s  %-9s  %-6s  %-13s  %-13s  %-13s  %s" % (
                s, status[:9], t.get("close_time") or "-", M.inr(t["price"]) if t else "-",
                M.inr(r["level"]) if r.get("level") is not None else "-",
                M.inr(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /trader-exit." % (n, len(rows)))
        print("Yahoo bars: free, unofficial, delayed, can break. Nothing was placed. " + M.NOT_ADVICE)
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry, India): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, setup_default="orb"):
        p.add_argument("--setup", choices=list(SETUPS), default=setup_default)
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length from 09:15 (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close beyond (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"])
        p.add_argument("--product", choices=["MIS", "CNC", "NRML"], help="default: config default_product (MIS)")
        p.add_argument("--instrument", choices=["EQ", "FUT", "CE", "PE"], default="EQ",
                       help="what you will trade on this signal (F&O: expiry-day cutoff, fno_ban)")
        p.add_argument("--exchange", choices=["NSE", "BSE"], help="default: config exchange (NSE)")
        p.add_argument("--series", default="EQ", help="NSE series (EQ, BE, BZ ...): trade-for-trade is refused")
        p.add_argument("--band", type=int, help="the stock's circuit band in percent (2, 5, 10, 20)")
        p.add_argument("--prev-close", type=float, help="the previous close, if Yahoo's is missing")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-28 09:50"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default=M.CONFIG_FILE)

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p, setup_default=None)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except (Err, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
