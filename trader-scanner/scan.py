#!/usr/bin/env python3
"""Chart Scanner for trader-scanner (India edition): scan NSE charts for setups. Standard library only.

    python3 scan.py universe                          # NSE EQUITY_L.csv (series EQ) -> trader/universe.txt
    python3 scan.py universe --index nifty500         # or nifty50 / nifty100 / nifty200 / fno
    python3 scan.py daily                             # pre-market / swing setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,TCS,SBIN # or --watchlist FILE, or --limit 200
    python3 scan.py gap                               # after 09:20 IST: opening gaps, go or fill, circuits
    python3 scan.py orb                               # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15 --short
    python3 scan.py orb --offline day.json --now "2026-10-28 10:15"

universe  NSE's list of equities (EQUITY_L.csv) keeping series EQ. BE and BZ
          are trade-for-trade (delivery only, no intraday): out unless
          --include-t2t. Or an index constituent list (--index nifty50 ...
          nifty500), or the F&O stocks (--index fno: the lot table that
          market.py keeps, refreshed from fo_mktlots.csv). Writes one symbol a
          line to trader/universe.txt and the names, series and ISINs to
          trader/universe.json (trader-news reads the names).

daily     Daily bars, 6 months. Stage 1: Yahoo spark closes for every symbol
          (20 a request; spark has closes only). Setups from closes:
          breakout_20d, near_52w_high, bull_flag, reclaim_ma20, ema_pullback,
          oversold, oversold_bounce. Stage 2: a v8 daily chart (open, high,
          low, close, volume) for each candidate, most traded first, at most
          --max-confirm: real average turnover, then nr7 and volume_surge,
          which need highs, lows and volumes (--deep checks every liquid chart,
          not only candidates). Liquidity in rupees: average daily turnover
          (close x volume, 20 sessions) >= --min-turnover-cr crore and the last
          close >= --min-price (20: penny stocks out). Today's daily bar is
          left out until the 15:30 close.

gap       From 09:20 IST (the first 5-minute bar is complete): each chart's
          open against the previous close (gap %), classed gap-up / gap-down
          (2 %+) or small; then gap-and-go (the first --hold-minutes, 15, close
          at or beyond the open and fill under half the gap) or gap-fill
          (trading back toward the previous close), or gap filled. Flags a
          price near its circuit limit when the band is known (config
          "bands" or --bands FILE, NSE's sec_list.csv); F&O stocks have no
          fixed band.

orb       Opening range = the first --or-minutes (15) from 09:15 IST on
          5-minute bars (75 bars a session). Stage 1: spark closes for every
          symbol; stage 2: the v8 chart (real highs and lows) for the
          candidates, most traded first, at most --max-confirm. A hit is the
          first completed bar to CLOSE above the range high (and, with --short,
          below the range low: intraday MIS only). Each hit carries gap_pct
          and turnover.

Every scan skips the config's blocked, asm_gsm and fno_ban lists and says why.
NSE holidays come from market.py. There are no bars before 09:15 (pre-open
09:00-09:15 is an auction, not trading). Times are India time (IST).

Data: Yahoo Finance (.NS symbols) and NSE's public files. Both are free,
unofficial and delayed, and they break without notice. --offline PATH reads
saved files instead: a spark answer, a v8 chart, a bundle {"RELIANCE": <v8>},
or a folder of <SYMBOL>.json files (universe: the saved CSV).

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3
BAR_MIN = 5
MAX_CONFIRM = 200
MIN_DAILY_BARS = 30
EMA_BARS = 60
NEAR_HIGH_PCT = 3.0
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_OUT = "trader/watchlist.json"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
DATA_NOTE = "Yahoo/NSE data is free, unofficial and delayed."

EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
INDEX_URLS = {
    "nifty50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
}
T2T_SERIES = ("BE", "BZ")
SERIES_WHY = {"BE": "trade-for-trade (BE)", "BZ": "trade-for-trade (BZ)", "SM": "SME platform (SM)",
              "ST": "SME platform (ST)", "GB": "gold bond (GB)", "IL": "institutional only (IL)",
              "IT": "institutional only (IT)", "BT": "trade-for-trade (BT)"}
CONTEXT = (("NIFTY", "NIFTY 50"), ("BANKNIFTY", "NIFTY BANK"), ("INDIAVIX", "India VIX"))

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("near_52w_high", "within 3% of the 52-week high"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("ema_pullback", "pullback to the 20 EMA in an uptrend (swing)"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
    ("nr7", "NR7 (narrowest range of 7 days)"),
    ("volume_surge", "volume over 2x its 20-day average"),
]
LABEL = dict(SETUPS)
CLOSE_SETUPS = ("breakout_20d", "near_52w_high", "bull_flag", "reclaim_ma20", "ema_pullback", "oversold",
                "oversold_bounce")


class ScanError(M.MarketError):
    pass


# ---------------------------------------------------------------- small helpers

def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def iso_ist(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


def crore(x):
    return None if x is None else x / 1e7


def r2(x):
    return None if x is None else round(x, 2)


def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def make_net(a):
    """One network object per run (tests replace this)."""
    return M.Net(pause=getattr(a, "pause", PAUSE))


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def num(x):
    return "{:,.2f}".format(x)


# ---------------------------------------------------------------- offline files

def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    if isinstance(doc, dict) and "spark" in doc:
        data.update(M.parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = ""
        try:
            sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
        except (AttributeError, KeyError, TypeError, IndexError):
            sym = ""
        sym = M.plain_symbol(sym or name_hint or "")
        if not sym:
            raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        try:
            data[sym] = M.parse_chart(doc, sym)
        except M.MarketError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % (name_hint or "?"))


def load_offline(path):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(M.read_text_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            _absorb(doc, _stem(f) if isinstance(doc, dict) and "chart" in doc else "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(M.read_text_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


def split_context(data):
    """Index and VIX charts are context, not scan targets."""
    ctx = {k: data.pop(k) for k in list(data) if k in M.INDEXES or k == "INDIAVIX"}
    return ctx


# ---------------------------------------------------------------- universe

def _csv_rows(text):
    rdr = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = [r for r in rdr if any(c.strip() for c in r)]
    if not rows:
        return [], []
    hdr = [h.strip().upper() for h in rows[0]]
    return hdr, [dict(zip(hdr, [c.strip() for c in r])) for r in rows[1:]]


def parse_equity_list(text, include_t2t=False):
    """NSE EQUITY_L.csv -> (kept [{symbol, name, series, isin}], counts {reason: n}).
    Columns: SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,
    MARKET LOT, ISIN NUMBER, FACE VALUE."""
    hdr, rows = _csv_rows(text)
    if "SYMBOL" not in hdr or "SERIES" not in hdr:
        raise ScanError("not NSE's EQUITY_L.csv (header %s)" % ",".join(hdr)[:80])
    keep_series = ("EQ",) + (T2T_SERIES if include_t2t else ())
    kept, counts = {}, {"rows": len(rows)}
    for r in rows:
        sym = (r.get("SYMBOL") or "").upper()
        series = (r.get("SERIES") or "").upper()
        if not M.valid_symbol(sym):
            why = "not a plain NSE symbol"
        elif series not in keep_series:
            why = "series %s" % series if series not in SERIES_WHY else SERIES_WHY[series]
        else:
            why = "keep"
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            kept[sym] = {"symbol": sym, "name": " ".join((r.get("NAME OF COMPANY") or "").split()),
                         "series": series, "isin": r.get("ISIN NUMBER") or ""}
    return [kept[k] for k in sorted(kept)], counts


def parse_index_list(text, include_t2t=False):
    """NSE index constituents CSV (Company Name, Industry, Symbol, Series, ISIN Code)."""
    hdr, rows = _csv_rows(text)
    if "SYMBOL" not in hdr or "COMPANY NAME" not in hdr:
        raise ScanError("not an NSE index constituent list (header %s)" % ",".join(hdr)[:80])
    kept, counts = {}, {"rows": len(rows)}
    for r in rows:
        sym = (r.get("SYMBOL") or "").upper()
        series = (r.get("SERIES") or "EQ").upper()
        if not M.valid_symbol(sym):
            why = "not a plain NSE symbol"
        elif series in T2T_SERIES and not include_t2t:
            why = SERIES_WHY[series]
        else:
            why = "keep"
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            kept[sym] = {"symbol": sym, "name": " ".join((r.get("COMPANY NAME") or "").split()),
                         "series": series, "isin": r.get("ISIN CODE") or "", "industry": r.get("INDUSTRY") or ""}
    return [kept[k] for k in sorted(kept)], counts


def fno_list(text=None):
    """F&O stocks: from a saved fo_mktlots.csv, else market.py's lot table (indexes removed)."""
    lots = M.parse_mktlots(text) if text is not None else M.lots_table()
    syms = sorted(s for s in lots if s not in M.INDEXES and M.valid_symbol(s))
    if not syms:
        raise ScanError("no F&O stocks found: run `market.py lots --refresh` or pass --offline fo_mktlots.csv")
    return [{"symbol": s, "name": "", "series": "EQ", "isin": "", "lot": lots[s]} for s in syms], {"rows": len(syms)}


def universe_meta_path(out):
    return os.path.splitext(out)[0] + ".json"


def load_universe_meta(path=DEFAULT_UNIVERSE):
    doc = M.read_json(universe_meta_path(path), "universe", required=False, default=None)
    return doc if isinstance(doc, dict) else {}


# ---------------------------------------------------------------- symbols and config

def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.split("#")[0].strip()
        if ln:
            out.append(re.split(r"[|\s]", ln)[0].strip().upper())
    return out


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        s = M.plain_symbol(s)
        if not M.valid_symbol(s):
            raise ValueError("%r is not an NSE symbol" % s)
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def _upper_list(x):
    return [str(s).upper() for s in (x or []) if s]


def skip_reasons(cfg, mode, universe_meta=None):
    """{SYMBOL: why it is skipped}: the config's blocked / ASM-GSM / F&O-ban lists,
    and trade-for-trade stocks for the intraday scans (no MIS in BE/BZ)."""
    out = {}
    for s in _upper_list(cfg.get("fno_ban")):
        out[s] = "in the F&O ban period (MWPL over 95%): skipped"
    for s in _upper_list(cfg.get("asm_gsm")):
        out[s] = "under ASM/GSM surveillance: skipped"
    for s in _upper_list(cfg.get("blocked")):
        out[s] = "on your blocked list"
    if mode in ("orb", "gap"):
        series = (universe_meta or {}).get("series") or {}
        block = set(_upper_list(cfg.get("block_series"))) | set(T2T_SERIES)
        for s, ser in series.items():
            if str(ser).upper() in block and s.upper() not in out:
                out[s.upper()] = "series %s: trade-for-trade, delivery only, no intraday" % ser
    return out


def fno_symbols():
    return set(s for s in M.lots_table() if s not in M.INDEXES)


def load_bands(path):
    """Price bands from NSE's sec_list.csv (Symbol, Series, Security Name, Band, ...)
    or a JSON {SYMBOL: pct}. 'No Band' -> None (F&O stocks)."""
    if not path:
        return {}
    if not os.path.exists(path):
        raise ScanError("no bands file at %s" % path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if text.lstrip().startswith("{"):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        doc = doc.get("bands", doc) if isinstance(doc, dict) else {}
        return {str(k).upper(): (float(v) if v not in (None, "", "No Band") else None) for k, v in doc.items()}
    hdr, rows = _csv_rows(text)
    if "SYMBOL" not in hdr or not any("BAND" in h for h in hdr):
        raise ScanError("%s is not NSE's sec_list.csv (want Symbol and Band columns)" % path)
    band_col = next(h for h in hdr if "BAND" in h)
    out = {}
    for r in rows:
        sym = (r.get("SYMBOL") or "").upper()
        if r.get("SERIES") and r["SERIES"].upper() not in ("EQ", "BE", "BZ", "SM", "ST"):
            continue
        v = (r.get(band_col) or "").strip()
        try:
            out.setdefault(sym, float(v))
        except ValueError:
            out.setdefault(sym, None)
    return out


# ---------------------------------------------------------------- fetching

def fetch_spark(net, symbols, rng, interval, a=None):
    """Batches of 20; a failed batch is reported, the rest go on. -> (data, failed)."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        try:
            got = net.spark(batch, rng, interval)
        except (M.MarketError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def fetch_context(net, offline_ctx=None):
    """{NIFTY|BANKNIFTY|INDIAVIX: series}; a failure leaves it out."""
    if offline_ctx is not None:
        return offline_ctx
    out = {}
    for sym, _ in CONTEXT:
        try:
            out[sym] = M.parse_chart(net.chart(sym, "5d", "5m"), sym)
        except (M.MarketError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- series helpers

def has_hl(series):
    return any(x is not None for x in (series.get("h") or [])) and any(x is not None for x in (series.get("l") or []))


def has_volume(series):
    return any(x for x in (series.get("v") or []))


def prev_close_of(series, day):
    """Yesterday's official close when Yahoo gives it, else the previous session's last 5-minute close.
    (NSE's official close is the volume-weighted price of the last 30 minutes, so the
    last bar can differ a little.) -> (price, basis)."""
    meta = series.get("meta") or {}
    best, latest = None, None
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None or ts is None:
            continue
        dt = M.ist_from_ts(ts)
        latest = dt.date() if latest is None or dt.date() > latest else latest
        if dt.date() < day and dt.time() < M.CLOSE and (best is None or dt >= best[0]):
            best = (dt, float(c))
    # Yahoo's previousClose belongs to the chart's latest session: only use it for that day
    if latest is None or latest == day:
        for k in ("regularMarketPreviousClose", "previousClose"):
            if meta.get(k):
                return float(meta[k]), "official"
    if best:
        return best[1], "last 5-minute close"
    if (latest is None or latest == day) and meta.get("chartPreviousClose") and meta.get("range") in (None, "1d"):
        return float(meta["chartPreviousClose"]), "official"
    return None, None


def prior_turnover_cr(series, day, n=20):
    """Average daily turnover (crore) of the sessions before `day` inside an intraday series."""
    if not has_volume(series):
        return None
    per = {}
    for i, ts in enumerate(series["t"]):
        c, v = series["c"][i], (series.get("v") or [None] * len(series["t"]))[i]
        if c is None or not v or ts is None:
            continue
        d = M.ist_from_ts(ts).date()
        if d < day:
            per[d] = per.get(d, 0.0) + float(c) * float(v)
    vals = [per[d] for d in sorted(per)][-n:]
    return crore(sum(vals) / len(vals)) if vals else None


def context_lines(ctx, now):
    """Index context at the clock: [{symbol, name, price, prev_close, pct}]."""
    out = []
    for sym, name in CONTEXT:
        s = ctx.get(sym)
        if not s:
            continue
        last = None
        for i, ts in enumerate(s["t"]):
            c = s["c"][i]
            if c is None or ts is None:
                continue
            dt = M.ist_from_ts(ts)
            if dt + datetime.timedelta(minutes=BAR_MIN) <= now or dt.date() < now.date():
                if last is None or dt >= last[0]:
                    last = (dt, float(c))
        if last is None:
            continue
        pc, _ = prev_close_of(s, last[0].date())
        out.append({"symbol": sym, "name": name, "price": r2(last[1]), "prev_close": r2(pc),
                    "pct": r2(M.gap_pct(last[1], pc)) if pc else None, "at": last[0].strftime("%Y-%m-%d %H:%M")})
    return out


def print_context(ctx_rows):
    if not ctx_rows:
        return
    parts = []
    for c in ctx_rows:
        parts.append("%s %s%s" % (c["name"], num(c["price"]),
                                  (" (%+.2f%%)" % c["pct"]) if c["pct"] is not None else ""))
    print("Market: " + ", ".join(parts) + ".")


# ---------------------------------------------------------------- daily setups

def daily_bars(series, now):
    """COMPLETE daily bars [{d, o, h, l, c, v}]: null closes dropped, anything after the
    clock dropped, today's bar dropped until the 15:30 close."""
    today = now.date()
    today_done = (not M.trading_day(today)) or now.time() >= M.CLOSE
    n = len(series["t"])
    o, h, lo, v = (series.get(k) or [None] * n for k in ("o", "h", "l", "v"))
    out = {}
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None or ts is None:
            continue
        d = M.ist_from_ts(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = {"d": d, "c": float(c), "o": o[i], "h": h[i], "l": lo[i], "v": v[i]}
    return [out[k] for k in sorted(out)]


def avg_turnover_cr(bars, n=20):
    vals = [b["c"] * b["v"] for b in bars[-n:] if b.get("v")]
    return crore(sum(vals) / len(vals)) if vals else None


def find_setups(bars, high52=None):
    """bars (oldest first, >= 30) -> (setup keys, numbers). nr7 needs highs and lows,
    volume_surge needs volumes; both are skipped on closes-only data."""
    c = [b["c"] for b in bars]
    last = c[-1]
    hl = all(b["h"] is not None and b["l"] is not None for b in bars[-7:])
    vol = all(b.get("v") for b in bars[-21:])
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = M.rsi(c), M.rsi(c[:-1])
    window_hi = max(max(b["h"] for b in bars) if hl and all(b["h"] is not None for b in bars) else 0.0, max(c))
    hi52 = max(float(high52), window_hi) if high52 else window_hi
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if last >= hi52 * (1 - NEAR_HIGH_PCT / 100.0):
        tags.append("near_52w_high")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    e20 = e50 = None
    if len(c) >= EMA_BARS:
        es20, es50 = M.ema_series(c, 20), M.ema_series(c, 50)
        e20, e50 = es20[-1], es50[-1]
        touched = (min(b["l"] for b in bars[-3:]) <= e20 * 1.005) if hl else (min(c[-3:]) <= e20 * 1.01)
        if e20 > e50 and e50 > es50[-6] and last > e50 and abs(last / e20 - 1) <= 0.015 and touched:
            tags.append("ema_pullback")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    if hl:
        rngs = [b["h"] - b["l"] for b in bars[-7:]]
        if rngs[-1] < min(rngs[:-1]):
            tags.append("nr7")
    vavg = None
    if vol:
        vavg = sum(b["v"] for b in bars[-21:-1]) / 20.0
        if vavg and bars[-1]["v"] > 2 * vavg:
            tags.append("volume_surge")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20, "ema20": e20, "ema50": e50,
                  "high52": hi52, "high52_basis": "Yahoo 52-week high" if high52 else "%d sessions" % len(bars),
                  "run_pct": run * 100, "range5_pct": rng5 * 100, "ohlc": hl, "volume": vol,
                  "vol_ratio": (bars[-1]["v"] / vavg) if vavg else None}


def _liquidity(series, bars):
    """-> (turnover crore, basis)."""
    if has_volume(series):
        return avg_turnover_cr(bars), "20-day average"
    meta = series.get("meta") or {}
    vol, px = meta.get("regularMarketVolume"), meta.get("regularMarketPrice") or (bars[-1]["c"] if bars else None)
    if vol and px:
        return crore(float(vol) * float(px)), "last session (Yahoo meta)"
    return None, None


def scan_daily(data, symbols, now, min_price, min_turn, confirm=None, max_confirm=None, deep=False, skip=None):
    """Pure apart from confirm(sym) -> a v8 daily series. Returns (hits, counts, last_date, failed)."""
    skip = skip or {}
    counts = {"requested": len(symbols), "fetched": 0, "skipped": 0, "illiquid": 0, "short": 0, "scanned": 0,
              "ohlc_checked": 0, "unconfirmed": 0}
    rows, dates, failed = [], {}, {}
    for sym in symbols:
        if sym in skip:
            counts["skipped"] += 1
            continue
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_bars(s, now)
        if not bars:
            counts["short"] += 1
            continue
        turn, basis = _liquidity(s, bars)
        if bars[-1]["c"] < min_price or (min_turn > 0 and (turn is None or turn < min_turn)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        tags, nums = find_setups(bars, (s.get("meta") or {}).get("fiftyTwoWeekHigh"))
        rows.append({"sym": sym, "series": s, "bars": bars, "turn": turn, "basis": basis, "tags": tags,
                     "nums": nums})
    # stage 2: v8 daily charts (OHLCV) for charts that only had closes
    need = [r for r in rows if not r["nums"]["ohlc"] and (deep or r["tags"])]
    if confirm is not None and need:
        need.sort(key=lambda r: -(r["turn"] or 0))
        if max_confirm and len(need) > max_confirm:
            counts["unconfirmed"] = len(need) - max_confirm
            need = need[:max_confirm]
        for r in need:
            try:
                s2 = confirm(r["sym"])
            except (M.MarketError, ValueError) as e:
                failed[r["sym"]] = str(e)
                continue
            bars2 = daily_bars(s2, now)
            if len(bars2) < MIN_DAILY_BARS:
                failed[r["sym"]] = "the v8 daily chart is too short"
                continue
            r["bars"] = bars2
            turn2, basis2 = _liquidity(s2, bars2)
            if turn2 is not None:
                r["turn"], r["basis"] = turn2, basis2
            hi52 = (s2.get("meta") or {}).get("fiftyTwoWeekHigh") or (r["series"].get("meta") or {}).get(
                "fiftyTwoWeekHigh")
            r["tags"], r["nums"] = find_setups(bars2, hi52)
            if min_turn > 0 and (r["turn"] is None or r["turn"] < min_turn):
                r["drop"] = True
    hits = []
    for r in rows:
        if r.get("drop"):
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if r["nums"]["ohlc"]:
            counts["ohlc_checked"] += 1
        d_last = r["bars"][-1]["d"]
        dates[d_last] = dates.get(d_last, 0) + 1
        if not r["tags"]:
            continue
        n = r["nums"]
        hits.append({"symbol": r["sym"], "setup": r["tags"][0], "setups": r["tags"],
                     "price": round(r["bars"][-1]["c"], 2), "close_date": d_last.isoformat(),
                     "rsi": round(n["rsi"], 1) if n["rsi"] is not None else None,
                     "turnover_cr": r2(r["turn"]), "turnover_basis": r["basis"],
                     "high52": r2(n["high52"]), "ohlc": n["ohlc"],
                     "note": ", ".join(LABEL[t] for t in r["tags"])})
    hits.sort(key=lambda h: (-(h["turnover_cr"] or 0), h["symbol"]))
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date, failed


# ---------------------------------------------------------------- intraday bars

def slot(day, k):
    return datetime.datetime.combine(day, M.OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` (09:15 to 15:25 starts) complete at `cutoff`, on the grid, with a close."""
    out = {}
    n = len(series["t"])
    o, hs, ls, v = (series.get(k) or [None] * n for k in ("o", "h", "l", "v"))
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None or ts is None:
            continue
        dt = M.ist_from_ts(ts)
        if dt.date() != day or dt.second or dt.minute % BAR_MIN:
            continue
        if dt.time() < M.OPEN or dt.time() >= M.CLOSE:
            continue        # pre-open (no bars before 09:15) or the closing session
        if dt + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue        # still forming
        out[dt] = {"start": dt, "c": float(c), "o": None if o[i] is None else float(o[i]),
                   "h": None if hs[i] is None else float(hs[i]), "l": None if ls[i] is None else float(ls[i]),
                   "v": v[i] or 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_min):
    """Every 5-minute slot from 09:15 to the cutoff is there (at most 75), and at least n_min."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    want = min(want, M.bars_per_session(BAR_MIN))
    if want < n_min or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def today_turnover_cr(bars):
    vals = [b["c"] * b["v"] for b in bars if b["v"]]
    return crore(sum(vals)) if vals else None


def traded_value(series, bars):
    """Ranking proxy: rupees traded so far (or price x Yahoo's day volume when spark has no volumes)."""
    tv = sum(b["c"] * b["v"] for b in bars if b["v"])
    if tv:
        return tv
    vol = (series.get("meta") or {}).get("regularMarketVolume") or 0
    return bars[-1]["c"] * float(vol) if bars else 0.0


def intraday_window(now, at, explicit_day, first_close, until=None, what="orb", explain=""):
    """-> (day, cutoff) or raise ScanError in plain words."""
    day = explicit_day or now.date()
    if not M.trading_day(day):
        why = "weekend" if day.weekday() >= 5 else ("NSE holiday: %s" % M.holiday_name(day))
        raise ScanError("No session on %s (%s): NSE is closed. Run %s on a trading day, or replay a past "
                        "day with --offline." % (day_label(day), why, what))
    close_dt = datetime.datetime.combine(day, M.CLOSE)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (M.hm(at), M.hm(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the 15:30 close." % M.hm(at))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            if what == "orb":
                raise ScanError("NSE closed at 3:30 PM on %s. Replay the session with --at 10:15." % day_label(day))
            cutoff = close_dt
    if cutoff < datetime.datetime.combine(day, M.OPEN):
        raise ScanError("It is %s IST: %s. Trading starts 09:15 (pre-open 09:00-09:15 is an auction, no "
                        "bars). %s" % (M.ampm(cutoff), M.market_status(cutoff)["label"], explain))
    if cutoff < first_close:
        raise ScanError("It is %s IST: %s" % (M.ampm(cutoff), explain))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (M.ampm(cutoff), M.hm(until), M.ampm(first_close)))
    return day, cutoff


def orb_window(now, at, or_minutes, until, explicit_day=None):
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    day = explicit_day or now.date()
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    explain = ("the %d-minute opening range runs 9:15 AM to %s and the first bar after it closes at %s. "
               "Run orb again after %s." % (or_minutes, M.ampm(range_end), M.ampm(first_close), M.ampm(first_close)))
    return intraday_window(now, at, explicit_day, first_close, until, "orb", explain)


# ---------------------------------------------------------------- opening-range breakout

def orb_candidate(bars, n_range, short=False):
    """Stage 1, closes only (necessary condition): a close after the range at or beyond
    the range's best close."""
    top = max(b["c"] for b in bars[:n_range])
    bot = min(b["c"] for b in bars[:n_range])
    if any(b["c"] >= top for b in bars[n_range:]):
        return True
    return short and any(b["c"] <= bot for b in bars[n_range:])


def orb_hits(bars, n_range, short=False):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high
    (and below the range low with short). -> list of hit dicts (0, 1 or 2)."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return []
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    out = []
    for side in ("buy", "sell") if short else ("buy",):
        for k in range(n_range, len(bars)):
            b = bars[k]
            if (side == "buy" and b["c"] > oh) or (side == "sell" and b["c"] < ol):
                closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
                lvl = oh if side == "buy" else ol
                out.append({"side": side, "price": round(b["c"], 2), "at": M.hm(closes_at),
                            "bar_start": M.hm(b["start"]), "bar_index": k, "level": round(lvl, 2),
                            "or_high": round(oh, 2), "or_low": round(ol, 2),
                            "pct_over": round((b["c"] / lvl - 1) * 100, 2), "_t": closes_at})
                break
    return out


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_turn=0.0, confirm=None,
             max_confirm=None, short=False, skip=None):
    """stage1 = {SYM: series} (spark closes or v8). confirm(sym) -> v8 series with highs/lows.
    Returns (hits, counts, failed)."""
    skip = skip or {}
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "skipped": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        if sym in skip:
            counts["skipped"] += 1
            continue
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range + 1):
            counts["unusable"] += 1
            continue
        turn = prior_turnover_cr(s, day)
        if bars[-1]["c"] < min_price or (min_turn > 0 and turn is not None and turn < min_turn):
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range, short):
            cands.append((sym, s, bars, traded_value(s, bars)))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[3])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, s, bars, _ in cands:
        full, src = bars, s
        if confirm is not None and not has_hl(s):
            try:
                src = confirm(sym)
            except (M.MarketError, ValueError) as e:
                failed[sym] = str(e)
                continue
            full = session_bars(src, day, cutoff)
            if not usable(full, day, cutoff, n_range + 1):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        turn = prior_turnover_cr(src, day)
        if min_turn > 0 and turn is not None and turn < min_turn:
            counts["illiquid"] += 1
            counts["scanned"] -= 1
            continue
        counts["confirmed"] += 1
        pc, basis = prev_close_of(src, day)
        gp = M.gap_pct(full[0]["o"], pc) if (pc and full[0]["o"] is not None) else None
        for h in orb_hits(full, n_range, short):
            hits.append(dict({"symbol": sym, "setup": "orb", "setups": ["orb"],
                              "opening_range": {"high": h["or_high"], "low": h["or_low"], "minutes": or_minutes},
                              "gap_pct": r2(gp), "prev_close": r2(pc),
                              "turnover_cr": r2(turn if turn is not None else today_turnover_cr(full)),
                              "turnover_basis": "average of earlier sessions" if turn is not None else "today so far",
                              "turnover_today_cr": r2(today_turnover_cr(full)),
                              "note": "closed %s the %d-minute range %s at %s" % (
                                  "above" if h["side"] == "buy" else "below", or_minutes,
                                  "high" if h["side"] == "buy" else "low", M.ampm(h["_t"]))}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"], x["side"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- gaps

def gap_window(now, at, explicit_day=None):
    day = explicit_day or now.date()
    first_close = slot(day, 1)
    explain = "the first 5-minute bar (9:15 AM) closes at 9:20 AM. Run gap again after 9:20 AM."
    return intraday_window(now, at, explicit_day, first_close, None, "gap", explain)


def classify_open(bars, prev_close, hold_minutes=15):
    """Opening gap and what the first minutes did with it. bars need opens, highs, lows."""
    o = bars[0]["o"]
    gp = M.gap_pct(o, prev_close)
    kind = M.classify_gap(gp)
    n_hold = hold_minutes // BAR_MIN
    up = o >= prev_close
    size = abs(o - prev_close)

    def filled(bs):
        if not size:
            return 0.0
        if up:
            return max(0.0, (o - min(b["l"] for b in bs)) / size * 100)
        return max(0.0, (max(b["h"] for b in bs) - o) / size * 100)
    fill_all = filled(bars)
    window = bars[:n_hold]
    if fill_all >= 100:
        status = "gap filled"
    elif len(bars) < n_hold:
        status = "too early"
    else:
        wc = window[-1]["c"]
        holds = (wc >= o if up else wc <= o) and filled(window) < 50
        status = "gap-and-go" if holds else "gap-fill"
    return {"gap_pct": r2(gp), "gap": kind, "status": status, "filled_pct": round(min(fill_all, 999.0), 1),
            "first": {"high": round(max(b["h"] for b in window), 2), "low": round(min(b["l"] for b in window), 2),
                      "minutes": hold_minutes, "close": round(window[-1]["c"], 2)}}


GAP_WORDS = {"gap-and-go": "held beyond the open for the first %d minutes",
             "gap-fill": "trading back toward yesterday's close",
             "gap filled": "traded all the way back to yesterday's close",
             "too early": "the first %d minutes are not done yet"}


def scan_gap(stage1, symbols, day, cutoff, hold_minutes=15, min_gap=1.0, bands=None, fno=None, within_pct=2.0,
             min_price=0.0, min_turn=0.0, confirm=None, max_confirm=None, skip=None):
    """Gaps at the open. Stage 1 (spark closes, no opens) uses the first 5-minute close as a
    stand-in for the open to pick candidates (|gap| >= half of min_gap); stage 2 reads the
    real open from the v8 chart. Returns (hits, counts, failed)."""
    skip, bands, fno = skip or {}, bands or {}, fno or set()
    counts = {"requested": len(symbols), "fetched": 0, "skipped": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        if sym in skip:
            counts["skipped"] += 1
            continue
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        pc, _ = prev_close_of(s, day)
        if not usable(bars, day, cutoff, 1) or not pc:
            counts["unusable"] += 1
            continue
        turn = prior_turnover_cr(s, day)
        if bars[-1]["c"] < min_price or (min_turn > 0 and turn is not None and turn < min_turn):
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        approx = bars[0]["o"] if bars[0]["o"] is not None else bars[0]["c"]
        if abs(M.gap_pct(approx, pc)) >= (min_gap if bars[0]["o"] is not None else min_gap / 2.0):
            cands.append((sym, s, bars, traded_value(s, bars)))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[3])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, s, bars, _ in cands:
        src, full = s, bars
        if not has_hl(s) or bars[0]["o"] is None:
            if confirm is None:
                failed[sym] = "no opens, highs or lows in the data"
                continue
            try:
                src = confirm(sym)
            except (M.MarketError, ValueError) as e:
                failed[sym] = str(e)
                continue
            full = session_bars(src, day, cutoff)
            if not usable(full, day, cutoff, 1) or full[0]["o"] is None:
                failed[sym] = "the v8 chart has a missing bar"
                continue
        pc, basis = prev_close_of(src, day)
        if not pc:
            failed[sym] = "no previous close"
            continue
        counts["confirmed"] += 1
        g = classify_open(full, pc, hold_minutes)
        if abs(g["gap_pct"]) < min_gap:
            continue
        price = full[-1]["c"]
        band = None if sym in fno else bands.get(sym)
        lim = M.band_limits(pc, band) if band else None
        near = M.near_circuit(price, lim, within_pct) if lim else None
        circuit = {"band_pct": band, "upper": lim["upper"] if lim else None, "lower": lim["lower"] if lim else None,
                   "near": near,
                   "basis": "F&O stock: no fixed band (dynamic price bands)" if sym in fno else
                   ("band %s%%" % _n(band) if band else "band unknown (add it to config bands or --bands)")}
        turn = prior_turnover_cr(src, day)
        words = GAP_WORDS[g["status"]] % hold_minutes if "%d" in GAP_WORDS[g["status"]] else GAP_WORDS[g["status"]]
        note = "%s %+.2f%%, %s" % (g["gap"], g["gap_pct"], words)
        if near:
            note += "; within %s%% of the %s circuit (%s)" % (_n(within_pct), near, num(lim[near]))
        hits.append({"symbol": sym, "setup": "gap", "setups": ["gap", g["status"].replace(" ", "_")],
                     "side": "buy" if g["gap_pct"] > 0 else "sell", "price": round(price, 2),
                     "open": round(full[0]["o"], 2), "prev_close": round(pc, 2), "prev_close_basis": basis,
                     "gap_pct": g["gap_pct"], "gap": g["gap"], "status": g["status"], "filled_pct": g["filled_pct"],
                     "opening_range": {"high": g["first"]["high"], "low": g["first"]["low"],
                                       "minutes": hold_minutes},
                     "circuit": circuit, "turnover_cr": r2(turn if turn is not None else today_turnover_cr(full)),
                     "turnover_today_cr": r2(today_turnover_cr(full)), "note": note})
    hits.sort(key=lambda h: (-abs(h["gap_pct"]), h["symbol"]))
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- commands

def _config(a):
    return M.load_config(getattr(a, "config", None) or M.CONFIG_FILE)


def _min_turn(a, cfg):
    return float(a.min_turnover_cr if a.min_turnover_cr is not None else cfg.get("min_avg_turnover_cr", 5) or 0)


def cmd_universe(a):
    t0 = time.time()
    kind = a.index or "equity"
    url = EQUITY_URL if kind == "equity" else INDEX_URLS.get(kind)
    text = None
    if a.offline:
        if not os.path.isfile(a.offline):
            raise ScanError("no offline file at %s" % a.offline)
        text = M.read_text_maybe_gz(a.offline)
    elif kind != "fno":
        text = make_net(a).nse_archive(url)
    if kind == "fno":
        rows, counts = fno_list(text)
        source = a.offline or "market.py lot table (%s)" % M.LOTS_FILE
    elif kind == "equity":
        rows, counts = parse_equity_list(text, a.include_t2t)
        source = a.offline or url
    else:
        rows, counts = parse_index_list(text, a.include_t2t)
        source = a.offline or url
    now = M.parse_now(a.now)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write("# trader-scanner universe: %s, %d symbols, %s IST\n" % (kind, len(rows),
                                                                             now.strftime("%Y-%m-%d %H:%M")))
            for r in rows:
                fh.write(r["symbol"] + "\n")
        M.write_json(universe_meta_path(a.out), {
            "created_by": "trader-scanner", "created_at": iso_ist(now), "kind": kind, "source": source,
            "count": len(rows), "names": {r["symbol"]: r["name"] for r in rows if r.get("name")},
            "series": {r["symbol"]: r["series"] for r in rows if r.get("series") and r["series"] != "EQ"},
            "isin": {r["symbol"]: r["isin"] for r in rows if r.get("isin")},
            "industry": {r["symbol"]: r["industry"] for r in rows if r.get("industry")}})
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"kind": kind, "count": len(rows), "rows": counts.get("rows", 0), "dropped": dropped,
                          "out": None if a.no_write else a.out, "source": source,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
        return 0 if rows else 1
    what = {"equity": "NSE equity list (EQUITY_L.csv)", "fno": "F&O stock list"}.get(
        kind, "NSE %s constituents" % kind.upper().replace("NIFTY", "NIFTY "))
    print("%s: %s rows." % (what, "{:,}".format(counts.get("rows", 0))))
    print("Kept %s symbols (series EQ%s)." % ("{:,}".format(len(rows)),
                                            " plus BE/BZ trade-for-trade" if a.include_t2t else ""))
    if dropped:
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                      for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
    if a.include_t2t:
        print("Trade-for-trade (BE/BZ) stocks settle by delivery only: no intraday (MIS), every trade is a delivery.")
    if not a.no_write:
        print("Wrote %s (one symbol a line) and %s (names, series, ISINs)." % (a.out, universe_meta_path(a.out)))
    return 0 if rows else 1


def _offline_inputs(a):
    data, failed = load_offline(a.offline)
    ctx = split_context(data)
    for k in list(failed):
        if k in M.INDEXES or k == "INDIAVIX":
            failed.pop(k)
    return data, failed, ctx


def _base_doc(a, mode, now, day, as_of, symbols, counts, nfailed, source, filters, hits, ctx_rows, skip):
    doc = {"created_by": "trader-scanner", "created_at": iso_ist(now), "scan": mode, "mode": mode,
           "date": day.isoformat() if day else None, "as_of": as_of, "universe": len(symbols),
           "scanned": counts["scanned"], "failed": nfailed, "unconfirmed": counts.get("unconfirmed", 0),
           "source": source, "filters": filters, "context": ctx_rows,
           "skipped": {s: skip[s] for s in symbols if s in skip}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    return doc


def _print_skipped(doc):
    sk = doc.get("skipped") or {}
    if sk:
        print("Skipped %d: %s." % (len(sk), "; ".join("%s (%s)" % (s, w) for s, w in sorted(sk.items())[:8])
                                   + (" ..." if len(sk) > 8 else "")))


def cmd_daily(a):
    t0 = time.time()
    now = M.parse_now(a.now)
    cfg = _config(a)
    min_turn = _min_turn(a, cfg)
    offline_syms, ctx = None, None
    if a.offline:
        data, failed, ctx = _offline_inputs(a)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    skip = skip_reasons(cfg, "daily", load_universe_meta(a.universe or DEFAULT_UNIVERSE))
    confirm = None
    net = None
    if not a.offline:
        net = make_net(a)
        want = [s for s in symbols if s not in skip]
        progress(a, "Fetching 6 months of daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(want)), (len(want) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(net, want, "6mo", "1d", a)

        def confirm(sym):
            return M.parse_chart(net.chart(sym, "6mo", "1d"), sym)
    hits, counts, last_date, failed2 = scan_daily(data, symbols, now, a.min_price, min_turn, confirm,
                                                  a.max_confirm, a.deep, skip)
    failed = dict(failed)
    failed.update(failed2)
    if not a.no_index:
        ctx = fetch_context(net, ctx) if (net or ctx is not None) else {}
    ctx_rows = context_lines(ctx or {}, now)
    nfailed = len([s for s in symbols if s in failed and s not in skip])
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = _base_doc(a, "daily", now, last_date, as_of, symbols, counts, nfailed, "yahoo",
                    {"min_price": a.min_price, "min_turnover_cr": min_turn}, hits, ctx_rows, skip)
    if not a.no_write:
        M.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Daily scan, %s IST, closes through %s (Yahoo daily bars; %s)." % (
        now.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing", DATA_NOTE))
    print_context(ctx_rows)
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (Rs %s+ and average turnover Rs %s crore+)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols) - counts["skipped"]),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["scanned"] + counts["short"]), _n(a.min_price), _n(min_turn),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    _print_skipped(doc)
    print("Highs, lows and volumes (NR7, volume surge, exact turnover) checked on %s charts%s."
          % ("{:,}".format(counts["ohlc_checked"]),
             "; the others had closes only" if counts["ohlc_checked"] < counts["scanned"] else ""))
    if counts["unconfirmed"]:
        print("PARTIAL: %s candidates were not checked with highs and lows (only the %s most traded are; "
              "--max-confirm 0 checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (average daily turnover):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-11s %14s  RSI %5s  %8s cr  %s" % (h["symbol"], M.inr(h["price"]),
                                                    "%.1f" % h["rsi"] if h["rsi"] is not None else "n/a",
                                                    "%.1f" % h["turnover_cr"] if h["turnover_cr"] else "n/a",
                                                    h["note"]))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (scan daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


def _intraday_setup(a, now):
    """Shared by orb and gap: offline data, explicit day, clock."""
    data = failed = ctx = None
    explicit_day = offline_syms = None
    at = M.parse_hhmm(a.at, "--at") if a.at else None
    if a.offline:
        data, failed, ctx = _offline_inputs(a)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [M.ist_from_ts(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\" (IST)")
    return data, failed, ctx, explicit_day, offline_syms, at


def _live_range(day):
    age = (M.now_ist().date() - day).days
    if age <= 4:
        return "5d"
    if age <= 25:
        return "1mo"
    raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())


def cmd_orb(a):
    t0 = time.time()
    now = M.parse_now(a.now)
    cfg = _config(a)
    min_turn = _min_turn(a, cfg)
    until = M.parse_hhmm(a.until or cfg.get("no_entries_after") or "15:30", "--until")
    data, failed, ctx, explicit_day, offline_syms, at = _intraday_setup(a, now)
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    skip = skip_reasons(cfg, "orb", load_universe_meta(a.universe or DEFAULT_UNIVERSE))
    confirm = net = None
    if not a.offline:
        net = make_net(a)
        rng = _live_range(day)
        want = [s for s in symbols if s not in skip]
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(want)), (len(want) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(net, want, rng, "%dm" % BAR_MIN, a)

        def confirm(sym):
            return M.parse_chart(net.chart(sym, rng, "%dm" % BAR_MIN), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, min_turn, confirm,
                                     a.max_confirm, a.short, skip)
    failed = dict(failed or {})
    failed.update(failed2)
    if not a.no_index:
        ctx = fetch_context(net, ctx) if (net or ctx is not None) else {}
    ctx_rows = context_lines(ctx or {}, cutoff)
    nfailed = len([s for s in symbols if s in failed and s not in skip])
    doc = _base_doc(a, "orb", now, day, "%s %s IST" % (day.isoformat(), M.hm(cutoff)), symbols, counts, nfailed,
                    "yahoo", {"min_price": a.min_price, "min_turnover_cr": min_turn}, hits, ctx_rows, skip)
    doc.update({"or_minutes": a.or_minutes, "interval": "%dm" % BAR_MIN, "short": bool(a.short)})
    if not a.no_write:
        M.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 AM to %s), 5-minute bars, Yahoo (%s)"
          % (day_label(day), M.ampm(cutoff), a.or_minutes, M.ampm(slot(day, a.or_minutes // BAR_MIN)), DATA_NOTE))
    print_context(ctx_rows)
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols) - counts["skipped"]),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the Rs %s / Rs %s crore floor)" % ("{:,}".format(counts["illiquid"]), _n(a.min_price),
                                                             _n(min_turn))) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    _print_skipped(doc)
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if a.short:
        print("Shorts are intraday only (MIS), squared off the same day%s."
              % ("" if cfg.get("allow_short") else "; allow_short is off in your config, so trader-risk will block them"))
    if hits:
        print("%d breakout%s by %s:" % (len(hits), "" if len(hits) == 1 else "s", M.ampm(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-11s %-4s %14s  range %s to %s  %+.2f%%  gap %s  turnover %s cr" % (
                M.ampm(M.parse_hhmm(h["at"])), h["symbol"], "long" if h["side"] == "buy" else "short",
                M.inr(h["price"]), num(h["or_low"]), num(h["or_high"]), h["pct_over"],
                ("%+.2f%%" % h["gap_pct"]) if h["gap_pct"] is not None else "n/a",
                ("%.1f" % h["turnover_cr"]) if h["turnover_cr"] is not None else "n/a"))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed %s its opening range by %s." % ("outside" if a.short else "above", M.ampm(cutoff)))
    if not a.no_write:
        print("Wrote %s (scan orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


def cmd_gap(a):
    t0 = time.time()
    now = M.parse_now(a.now)
    cfg = _config(a)
    min_turn = _min_turn(a, cfg)
    if a.hold_minutes < BAR_MIN or a.hold_minutes % BAR_MIN or a.hold_minutes > 60:
        raise ScanError("--hold-minutes wants a multiple of 5 from 5 to 60 (15 is the classic)")
    data, failed, ctx, explicit_day, offline_syms, at = _intraday_setup(a, now)
    day, cutoff = gap_window(now, at, explicit_day)
    symbols = read_symbols(a, offline_syms)
    skip = skip_reasons(cfg, "gap", load_universe_meta(a.universe or DEFAULT_UNIVERSE))
    bands = {str(k).upper(): v for k, v in (cfg.get("bands") or {}).items()}
    bands.update(load_bands(a.bands))
    within = float(cfg.get("avoid_circuit_within_pct", 2.0) or 2.0)
    confirm = net = None
    if not a.offline:
        net = make_net(a)
        rng = _live_range(day)
        want = [s for s in symbols if s not in skip]
        progress(a, "Stage 1: 5-minute closes for %s symbols..." % "{:,}".format(len(want)))
        data, failed = fetch_spark(net, want, rng, "%dm" % BAR_MIN, a)

        def confirm(sym):
            return M.parse_chart(net.chart(sym, rng, "%dm" % BAR_MIN), sym)
    hits, counts, failed2 = scan_gap(data, symbols, day, cutoff, a.hold_minutes, a.min_gap, bands, fno_symbols(),
                                     within, a.min_price, min_turn, confirm, a.max_confirm, skip)
    failed = dict(failed or {})
    failed.update(failed2)
    if not a.no_index:
        ctx = fetch_context(net, ctx) if (net or ctx is not None) else {}
    ctx_rows = context_lines(ctx or {}, cutoff)
    nfailed = len([s for s in symbols if s in failed and s not in skip])
    doc = _base_doc(a, "gap", now, day, "%s %s IST" % (day.isoformat(), M.hm(cutoff)), symbols, counts, nfailed,
                    "yahoo", {"min_price": a.min_price, "min_turnover_cr": min_turn, "min_gap_pct": a.min_gap},
                    hits, ctx_rows, skip)
    doc.update({"hold_minutes": a.hold_minutes, "interval": "%dm" % BAR_MIN})
    if not a.no_write:
        M.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Gap scan, %s %s IST: open vs yesterday's close, first %d minutes, 5-minute bars, Yahoo (%s)"
          % (day_label(day), M.ampm(cutoff), a.hold_minutes, DATA_NOTE))
    print_context(ctx_rows)
    print("Fetched %s of %s charts%s; %s usable; %s candidates, %s checked with the real open."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols) - counts["skipped"]),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]), "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    _print_skipped(doc)
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (--max-confirm 0 checks all)."
              % "{:,}".format(counts["unconfirmed"]))
    if hits:
        print("%d chart%s opened %s%% or more away from yesterday's close (biggest first):"
              % (len(hits), "" if len(hits) == 1 else "s", _n(a.min_gap)))
        for h in hits[:a.show]:
            c = h["circuit"]
            print("  %-11s %+6.2f%%  open %s  now %s  %-11s filled %5.1f%%%s" % (
                h["symbol"], h["gap_pct"], num(h["open"]), num(h["price"]), h["status"], h["filled_pct"],
                ("  NEAR %s CIRCUIT %s" % (c["near"].upper(), num(c[c["near"]]))) if c["near"] else ""))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart opened %s%% or more away from yesterday's close." % _n(a.min_gap))
    print("A stock at its circuit limit can freeze: no sellers at the upper circuit, no buyers at the lower.")
    if not a.no_write:
        print("Wrote %s (scan gap, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner, India): NSE daily setups, gaps "
                                 "and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="NSE equity / index / F&O list -> trader/universe.txt")
    p.add_argument("--index", choices=sorted(INDEX_URLS) + ["fno"], help="an index's constituents instead of "
                   "every EQ stock")
    p.add_argument("--include-t2t", action="store_true", help="keep BE/BZ trade-for-trade stocks (no intraday)")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="FILE", help="a saved EQUITY_L.csv / index CSV / fo_mktlots.csv")
    p.add_argument("--now", help='pin the clock ("YYYY-MM-DD HH:MM" IST)')
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--config", default=M.CONFIG_FILE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (default %s)" % _n(min_price))
        p.add_argument("--min-turnover-cr", type=float, default=None,
                       help="average daily turnover floor in crore (default: config min_avg_turnover_cr, 5)")
        p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                       help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo file or folder instead of the network")
        p.add_argument("--now", help='pin the clock ("2026-10-28 10:15", IST)')
        p.add_argument("--out", default=DEFAULT_OUT)
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--no-index", action="store_true", help="skip the NIFTY / BANKNIFTY / India VIX line")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--config", default=M.CONFIG_FILE)
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market / swing setups from daily bars")
    common(p, 20.0)
    p.add_argument("--deep", action="store_true", help="fetch highs/lows/volumes for every liquid chart "
                   "(NR7 and volume surge everywhere), not only candidates")
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 20.0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--short", action="store_true", help="also breaks below the range low (MIS only)")
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after)")
    p = sub.add_parser("gap", help="opening gaps: gap-and-go or gap-fill, near a circuit")
    common(p, 20.0)
    p.add_argument("--min-gap", type=float, default=1.0, help="smallest gap to list, percent (1.0)")
    p.add_argument("--hold-minutes", type=int, default=15, help="minutes a gap must hold (15)")
    p.add_argument("--bands", metavar="FILE", help="NSE sec_list.csv (price bands) or JSON {SYMBOL: pct}")
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb, "gap": cmd_gap}[a.cmd](a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
