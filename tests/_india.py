"""Shared offline fixtures for the India edition tests.

Real Yahoo answers for NSE symbols have this exact shape (checked against
RELIANCE.NS, ^NSEI and TCS.NS on 2026-10-28: currency INR, exchangeName NSI,
gmtoffset 19800, 5-minute bars from 09:15 IST, 75 bars a session). The
builders below produce that shape from small, readable bar lists, so every
test is deterministic and offline.
"""

import datetime
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "trader-market"))
import market as M  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def ts(day, hhmm):
    """IST date + 'HH:MM' -> unix seconds."""
    h, m = map(int, hhmm.split(":"))
    return M.ts_from_ist(datetime.datetime(day.year, day.month, day.day, h, m))


def session_times(day, interval_min=5):
    start = datetime.datetime(day.year, day.month, day.day, 9, 15)
    return [start + datetime.timedelta(minutes=interval_min * i) for i in range(M.SESSION_MINUTES // interval_min)]


def chart_doc(symbol, bars, prev_close=None, interval="5m", exchange="NSE", price=None):
    """bars: list of (datetime IST, o, h, l, c, v). -> Yahoo v8 chart JSON (dict)."""
    t = [M.ts_from_ist(b[0]) for b in bars]
    q = {"open": [b[1] for b in bars], "high": [b[2] for b in bars], "low": [b[3] for b in bars],
         "close": [b[4] for b in bars], "volume": [b[5] for b in bars]}
    ys = M.yahoo_symbol(symbol, exchange)
    meta = {"currency": "INR", "symbol": ys, "exchangeName": "NSI" if exchange == "NSE" else "BSE",
            "fullExchangeName": exchange, "instrumentType": "INDEX" if ys.startswith("^") else "EQUITY",
            "gmtoffset": 19800, "timezone": "IST", "exchangeTimezoneName": "Asia/Kolkata",
            "regularMarketPrice": price if price is not None else (bars[-1][4] if bars else None),
            "chartPreviousClose": prev_close, "previousClose": prev_close,
            "regularMarketVolume": sum(b[5] or 0 for b in bars), "dataGranularity": interval}
    return {"chart": {"result": [{"meta": meta, "timestamp": t, "indicators": {"quote": [q]}}], "error": None}}


def spark_doc(series_by_symbol, interval="1d", exchange="NSE"):
    """{SYMBOL: [(datetime, close, volume), ...]} -> Yahoo v7 spark JSON (dict)."""
    result = []
    for sym, rows in series_by_symbol.items():
        ys = M.yahoo_symbol(sym, exchange)
        meta = {"currency": "INR", "symbol": ys, "exchangeName": "NSI", "gmtoffset": 19800,
                "regularMarketPrice": rows[-1][1], "regularMarketVolume": rows[-1][2],
                "chartPreviousClose": rows[-2][1] if len(rows) > 1 else rows[-1][1], "dataGranularity": interval}
        result.append({"symbol": ys, "response": [{
            "meta": meta, "timestamp": [M.ts_from_ist(r[0]) for r in rows],
            "indicators": {"quote": [{"close": [r[1] for r in rows], "volume": [r[2] for r in rows]}]}}]})
    return {"spark": {"result": result, "error": None}}


def walk_session(day, start, seed, drift=0.0, vol=0.0015, volume=20000, interval_min=5):
    """A deterministic random-walk session of 5-minute bars."""
    rnd = random.Random(seed)
    out, px = [], start
    for dt in session_times(day, interval_min):
        o = px
        c = max(0.05, o * (1 + drift + rnd.gauss(0, vol)))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append((dt, round(o, 2), round(h, 2), round(lo, 2), round(c, 2), volume + rnd.randint(0, volume)))
        px = c
    return out


def trading_days_before(day, n):
    out, d = [], day
    while len(out) < n:
        d = M.prev_trading_day(d)
        out.append(d)
    return list(reversed(out))


def history(day, n_sessions=60, start=1000.0, seed=7, include_today_until=None):
    """n full sessions before `day` (+ today's bars up to 'HH:MM' if asked)."""
    bars, px = [], start
    for i, d in enumerate(trading_days_before(day, n_sessions)):
        s = walk_session(d, px, seed + i)
        bars += s
        px = s[-1][4]
    if include_today_until:
        cut = datetime.datetime.combine(day, M.parse_hhmm(include_today_until))
        bars += [b for b in walk_session(day, px, seed + 999) if b[0] < cut]
    return bars


def write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path
