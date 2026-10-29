#!/usr/bin/env python3
"""Indian market core for the Day Trader skill (India edition). Standard library only.

Every other trader-* tool imports this file. It holds what is specific to the
Indian market, in one place, so a rule that changes (an STT rate, a lot size,
a holiday, an expiry weekday) is changed once:

    python3 market.py clock                         # is NSE open? which session?
    python3 market.py calendar --year 2026          # NSE/BSE trading holidays
    python3 market.py expiry NIFTY                  # next weekly / monthly expiries
    python3 market.py lots                          # F&O lot sizes and strike steps
    python3 market.py costs --segment intraday --buy 1158 --sell 1170 --qty 50
    python3 market.py costs --segment options --buy 120 --sell 150 --qty 65 --broker upstox
    python3 market.py tax --kind intraday --profit 25000
    python3 market.py bands --price 1158 --band 10  # circuit limits
    python3 market.py tick --price 1158.37          # round to the exchange tick
    python3 market.py symbol RELIANCE --exchange BSE

Facts and where they came from (checked 2026-10-28; see docs/INDIA_RULES.md):
  * Hours: pre-open 09:00-09:08 order entry, 09:08-09:15 matching/buffer,
    normal market 09:15-15:30 IST, closing session 15:40-16:00 (NSE).
  * Holidays 2026: NSE holiday master (/api/holiday-master?type=trading),
    the CM and FO lists are identical. BSE follows the same list in 2026.
  * Expiries: one weekly index per exchange (SEBI, Nov 2024). NSE NIFTY weekly
    on Tuesday, BSE SENSEX weekly on Thursday (since Sept 2025). Monthly:
    last Tuesday (NSE: BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, stocks),
    last Thursday (BSE: SENSEX, BANKEX). A holiday moves the expiry to the
    previous trading day. Checked against NSE's own NIFTY expiry list.
  * Lot sizes: NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120,
    NIFTYNXT50 25, SENSEX 20, BANKEX 30 (Oct-Dec 2026 contracts). Stock lots
    change every six months: refresh with `lots --refresh` (fo_mktlots.csv).
  * STT (Budget 2026, from 1 Apr 2026): delivery 0.1% buy and sell, intraday
    0.025% sell, futures 0.05% sell, options 0.15% of premium on sell,
    exercised options 0.15% of intrinsic value (buyer).
  * Exchange transaction charges (NSE from 1 Mar 2026): equity 0.00307%,
    futures 0.00183%, options 0.03553% of premium. BSE: equity 0.00375%
    (A/B group), futures nil, options 0.0325% of premium.
  * SEBI turnover fee Rs 10 per crore. IPFT Rs 0.01 per crore (NSE).
  * Stamp duty (buy side only): delivery 0.015%, intraday 0.003%,
    futures 0.002%, options 0.003%.
  * GST 18% on brokerage + exchange charges + SEBI fee.

These are statutory numbers that governments and exchanges change. When a
number here is out of date, override it in trader/config.json ("charges")
or fix it here; the tests pin them so a change is deliberate.
"""

import argparse
import calendar as _calendar
import datetime
import gzip
import http.cookiejar
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

VERSION = "india-1.0"
VERIFIED_ON = "2026-10-28"
UA = "Mozilla/5.0 (compatible; day-trader-skill-india/1.0; +https://github.com/aditya76792/day-trader-skill)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
NOT_ADVICE = ("Not financial advice. Most retail intraday and F&O traders in India lose money "
              "(SEBI's own studies: about 9 in 10 F&O traders lost money in FY22-FY24).")


class MarketError(Exception):
    """A problem worth showing the user in one plain line."""


# ================================================================ time (IST, no DST)

def now_ist():
    return datetime.datetime.now(IST).replace(tzinfo=None)


def parse_now(text):
    """'2026-10-28 10:15' (IST) or '2026-10-28T04:45:00Z' (UTC) -> naive IST datetime."""
    if not text:
        return now_ist()
    t = text.strip()
    try:
        if t.endswith("Z"):
            utc = datetime.datetime.strptime(t[:-1].replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S"
                                             if len(t) > 17 else "%Y-%m-%d %H:%M")
            return (utc.replace(tzinfo=datetime.timezone.utc).astimezone(IST)).replace(tzinfo=None)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(t, fmt)
            except ValueError:
                continue
    except ValueError:
        pass
    raise MarketError("--now wants 'YYYY-MM-DD HH:MM' in India time (IST), got %r" % text)


def ist_from_ts(ts):
    """Unix seconds -> naive IST datetime."""
    return (EPOCH + datetime.timedelta(seconds=int(ts))).astimezone(IST).replace(tzinfo=None)


def ts_from_ist(dt):
    return int((dt.replace(tzinfo=IST) - EPOCH).total_seconds())


def utc_iso(dt_ist):
    return (dt_ist.replace(tzinfo=IST).astimezone(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_hhmm(text, what="time"):
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(text or ""))
    if not m:
        raise MarketError("%s wants HH:MM (24-hour, IST), got %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


# ================================================================ calendar

# NSE holiday master, CM and FO segments (identical in 2026), read 2026-10-28.
# Weekend entries are kept for reference; they do not change trading days.
HOLIDAYS = {
    2026: {
        datetime.date(2026, 1, 15): "Municipal Corporation Election - Maharashtra",
        datetime.date(2026, 1, 26): "Republic Day",
        datetime.date(2026, 2, 15): "Mahashivratri",
        datetime.date(2026, 3, 3): "Holi",
        datetime.date(2026, 3, 21): "Id-Ul-Fitr (Ramadan Eid)",
        datetime.date(2026, 3, 26): "Shri Ram Navami",
        datetime.date(2026, 3, 31): "Shri Mahavir Jayanti",
        datetime.date(2026, 4, 3): "Good Friday",
        datetime.date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        datetime.date(2026, 5, 1): "Maharashtra Day",
        datetime.date(2026, 5, 28): "Bakri Id",
        datetime.date(2026, 6, 26): "Muharram",
        datetime.date(2026, 8, 15): "Independence Day",
        datetime.date(2026, 9, 14): "Ganesh Chaturthi",
        datetime.date(2026, 10, 2): "Mahatma Gandhi Jayanti",
        datetime.date(2026, 10, 20): "Dussehra",
        datetime.date(2026, 11, 8): "Diwali Laxmi Pujan (Muhurat trading, timings by circular)",
        datetime.date(2026, 11, 10): "Diwali-Balipratipada",
        datetime.date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
        datetime.date(2026, 12, 25): "Christmas",
    },
}
# Muhurat trading: a one-hour evening session on Diwali. NSE announces the
# time by circular; None means "not announced yet".
SPECIAL_SESSIONS = {datetime.date(2026, 11, 8): {"name": "Muhurat trading", "start": None, "end": None}}
HOLIDAY_FILE = "trader/holidays.json"   # optional: a saved NSE holiday-master answer for later years

PRE_OPEN_START = datetime.time(9, 0)
PRE_OPEN_ORDER_END = datetime.time(9, 8)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
CLOSING_SESSION = (datetime.time(15, 40), datetime.time(16, 0))
SESSION_MINUTES = 375                   # 09:15 to 15:30
BLOCK_WINDOWS = ((datetime.time(8, 45), datetime.time(9, 0)), (datetime.time(14, 5), datetime.time(14, 20)))

_extra_loaded = {}


def _load_extra_holidays(path=HOLIDAY_FILE):
    """A saved NSE /api/holiday-master?type=trading answer (any year) extends HOLIDAYS."""
    if path in _extra_loaded:
        return _extra_loaded[path]
    extra = {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = (doc.get("FO") or doc.get("CM") or []) if isinstance(doc, dict) else doc
        for r in rows:
            d = datetime.datetime.strptime(r["tradingDate"], "%d-%b-%Y").date()
            extra[d] = r.get("description", "holiday")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    _extra_loaded[path] = extra
    return extra


def holidays_for(year):
    out = dict(HOLIDAYS.get(year, {}))
    out.update({d: n for d, n in _load_extra_holidays().items() if d.year == year})
    return out


def calendar_known(year):
    return year in HOLIDAYS or any(d.year == year for d in _load_extra_holidays())


def trading_day(d, exchange="NSE"):
    """Mon-Fri and not an exchange holiday. BSE follows NSE's 2026 list."""
    if isinstance(d, datetime.datetime):
        d = d.date()
    if d.weekday() >= 5:
        return False
    return d not in holidays_for(d.year)


def holiday_name(d):
    return holidays_for(d.year).get(d)


def prev_trading_day(d, exchange="NSE"):
    d = d - datetime.timedelta(days=1)
    while not trading_day(d, exchange):
        d -= datetime.timedelta(days=1)
    return d


def next_trading_day(d, exchange="NSE"):
    d = d + datetime.timedelta(days=1)
    while not trading_day(d, exchange):
        d += datetime.timedelta(days=1)
    return d


def trading_days_between(a, b, exchange="NSE"):
    """Trading days in (a, b]."""
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if trading_day(d, exchange):
            n += 1
    return n


def market_status(now):
    """Where the clock is in the NSE equity day. now: naive IST datetime."""
    d, t = now.date(), now.time()
    warn = None if calendar_known(d.year) else (
        "No %d holiday list loaded: weekends only. Save NSE's holiday master to %s." % (d.year, HOLIDAY_FILE))
    base = {"date": d.isoformat(), "time": hm(t), "open": False, "warning": warn}
    if d.weekday() >= 5:
        sp = SPECIAL_SESSIONS.get(d)
        base.update(phase="weekend", label="weekend" + (" (%s today)" % sp["name"] if sp else ""))
        return base
    name = holiday_name(d)
    if name:
        base.update(phase="holiday", label="exchange holiday: %s" % name)
        return base
    if t < PRE_OPEN_START:
        phase, label = "pre-market", "pre-market (pre-open starts 09:00)"
    elif t < PRE_OPEN_ORDER_END:
        phase, label = "pre-open", "pre-open order entry (09:00-09:08): orders collect, nothing trades"
    elif t < OPEN:
        phase, label = "pre-open-matching", "pre-open matching/buffer (09:08-09:15): equilibrium price set"
    elif t < CLOSE:
        phase, label = "open", "normal market (09:15-15:30)"
        base["open"] = True
    elif CLOSING_SESSION[0] <= t < CLOSING_SESSION[1]:
        phase, label = "closing-session", "closing session (15:40-16:00): trades only at the closing price"
    else:
        phase, label = "closed", "closed for the day"
    base.update(phase=phase, label=label)
    return base


def session_minute(now):
    """Minutes since 09:15 (negative before)."""
    return (now.hour * 60 + now.minute) - (OPEN.hour * 60 + OPEN.minute)


# ================================================================ instruments

INDEXES = {
    # name: exchange, weekly?, lot, strike step, Yahoo symbol, NSE option-chain symbol
    "NIFTY": {"exchange": "NSE", "weekly": True, "lot": 65, "step": 50, "yahoo": "^NSEI", "label": "NIFTY 50"},
    "BANKNIFTY": {"exchange": "NSE", "weekly": False, "lot": 30, "step": 100, "yahoo": "^NSEBANK",
                  "label": "NIFTY BANK"},
    "FINNIFTY": {"exchange": "NSE", "weekly": False, "lot": 60, "step": 50, "yahoo": "NIFTY_FIN_SERVICE.NS",
                 "label": "NIFTY FIN SERVICE"},
    "MIDCPNIFTY": {"exchange": "NSE", "weekly": False, "lot": 120, "step": 25, "yahoo": "NIFTY_MID_SELECT.NS",
                   "label": "NIFTY MID SELECT"},
    "NIFTYNXT50": {"exchange": "NSE", "weekly": False, "lot": 25, "step": 100, "yahoo": "^NSMIDCP",
                   "label": "NIFTY NEXT 50"},
    "SENSEX": {"exchange": "BSE", "weekly": True, "lot": 20, "step": 100, "yahoo": "^BSESN", "label": "SENSEX"},
    "BANKEX": {"exchange": "BSE", "weekly": False, "lot": 30, "step": 100, "yahoo": "BSE-BANK.BO", "label": "BANKEX"},
}
INDIA_VIX = "^INDIAVIX"
# A few stock lots (Oct/Nov/Dec 2026 contracts). The full list changes twice a
# year; `market.py lots --refresh` saves NSE's fo_mktlots.csv to trader/lots.json.
STOCK_LOTS = {"RELIANCE": 500, "TCS": 225, "HDFCBANK": 650, "INFY": 400, "SBIN": 750, "ICICIBANK": 700,
              "TMPV": 1600}
LOTS_FILE = "trader/lots.json"
LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
EXPIRY_WEEKDAY = {"NSE": 1, "BSE": 3}    # Tuesday, Thursday


def lots_table(path=LOTS_FILE):
    table = {k: v["lot"] for k, v in INDEXES.items()}
    table.update(STOCK_LOTS)
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        table.update({k.upper(): int(v) for k, v in (saved.get("lots") or {}).items()})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return table


def lot_size(symbol, path=LOTS_FILE):
    return lots_table(path).get(symbol.upper())


def parse_mktlots(text):
    """NSE fo_mktlots.csv -> {SYMBOL: lot for the nearest month}. Columns:
    UNDERLYING, SYMBOL, then one column per contract month (OCT-26, NOV-26 ...)."""
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 3 or not cells[1] or cells[1].upper() == "SYMBOL":
            continue
        for c in cells[2:]:
            if c.isdigit():
                out[cells[1].upper()] = int(c)
                break
    return out


def strike_step(symbol, price=None):
    s = symbol.upper()
    if s in INDEXES:
        return INDEXES[s]["step"]
    if price is None:
        return None
    # stock option strike steps scale with price (NSE sets them per stock; this is a guide only)
    for lim, step in ((100, 1), (250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50), (10000, 100)):
        if price < lim:
            return step
    return 250


def is_index(symbol):
    return symbol.upper() in INDEXES


def exchange_of(symbol, default="NSE"):
    s = symbol.upper()
    return INDEXES[s]["exchange"] if s in INDEXES else default


def _last_weekday(year, month, weekday):
    last = _calendar.monthrange(year, month)[1]
    d = datetime.date(year, month, last)
    while d.weekday() != weekday:
        d -= datetime.timedelta(days=1)
    return d


def _shift_back(d, exchange):
    while not trading_day(d, exchange):
        d -= datetime.timedelta(days=1)
    return d


def monthly_expiry(year, month, exchange="NSE"):
    return _shift_back(_last_weekday(year, month, EXPIRY_WEEKDAY[exchange]), exchange)


def expiries(symbol, start, weeks=5, months=3):
    """Upcoming expiries on or after `start` -> list of {date, kind}. Index
    weeklies only where SEBI still allows them (NIFTY, SENSEX)."""
    s = symbol.upper()
    exch = exchange_of(s)
    weekly = s in INDEXES and INDEXES[s]["weekly"]
    out = {}
    y, m = start.year, start.month
    while len([d for d, k in out.items() if k == "monthly"]) < months:
        d = monthly_expiry(y, m, exch)
        if d >= start:
            out[d] = "monthly"
        m += 1
        if m == 13:
            y, m = y + 1, 1
    if weekly:
        d = start + datetime.timedelta(days=(EXPIRY_WEEKDAY[exch] - start.weekday()) % 7)
        n = 0
        while n < weeks:
            e = _shift_back(d, exch)
            if e >= start:
                out.setdefault(e, "weekly")
                n += 1
            d += datetime.timedelta(days=7)
    return [{"date": d.isoformat(), "kind": k, "weekday": d.strftime("%A")} for d, k in sorted(out.items())]


def next_expiry(symbol, today):
    e = expiries(symbol, today, weeks=1, months=1)
    return datetime.date.fromisoformat(e[0]["date"]) if e else None


def is_expiry_day(symbol, d):
    return any(x["date"] == d.isoformat() for x in expiries(symbol, d, weeks=1, months=1))


_MONTH_CODE = {10: "O", 11: "N", 12: "D"}


def nse_option_symbol(underlying, expiry, strike, opt_type, monthly=None):
    """Exchange-style trading symbol (the Kite/NSE format):
    monthly  NIFTY26NOV22700CE    weekly  NIFTY26N0322700CE (YY, month code 1-9/O/N/D, DD).
    Brokers differ (Upstox and Dhan use instrument keys / security ids); the
    ticket always carries the plain fields too."""
    if isinstance(expiry, str):
        expiry = datetime.date.fromisoformat(expiry)
    exch = exchange_of(underlying)
    if monthly is None:
        monthly = expiry == monthly_expiry(expiry.year, expiry.month, exch)
    k = ("%d" % strike) if float(strike).is_integer() else ("%g" % strike)
    yy = expiry.strftime("%y")
    if monthly:
        return "%s%s%s%s%s" % (underlying.upper(), yy, expiry.strftime("%b").upper(), k, opt_type.upper())
    mc = _MONTH_CODE.get(expiry.month, str(expiry.month))
    return "%s%s%s%02d%s%s" % (underlying.upper(), yy, mc, expiry.day, k, opt_type.upper())


def futures_symbol(underlying, expiry):
    """Monthly futures trading symbol: NIFTY26NOVFUT (Kite/NSE format)."""
    if isinstance(expiry, str):
        expiry = datetime.date.fromisoformat(expiry)
    return "%s%s%sFUT" % (underlying.upper(), expiry.strftime("%y"), expiry.strftime("%b").upper())


def nse_date(d):
    """2026-11-03 -> '03-Nov-2026' (the format NSE's APIs use)."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return d.strftime("%d-%b-%Y")


def parse_nse_date(text):
    """'03-Nov-2026' / '03-11-2026' / '2026-11-03' -> date."""
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(text.strip()[:11], fmt).date()
        except ValueError:
            continue
    raise MarketError("unreadable date %r" % text)


def iso_ist(dt):
    """naive IST datetime -> '2026-10-28T10:15:00+05:30'."""
    return dt.replace(microsecond=0).isoformat() + "+05:30"


# ================================================================ symbols

def yahoo_symbol(symbol, exchange="NSE"):
    """RELIANCE -> RELIANCE.NS (BSE: RELIANCE.BO). Indexes map to Yahoo's codes.
    Yahoo does not know BSE scrip codes (500325.BO answers 404): use the ticker."""
    s = symbol.strip().upper()
    if s in INDEXES:
        return INDEXES[s]["yahoo"]
    if s in ("INDIAVIX", "VIX", "INDIA VIX"):
        return INDIA_VIX
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return s
    return s + (".BO" if exchange.upper() == "BSE" else ".NS")


def plain_symbol(ysym):
    s = ysym.upper()
    for k, v in INDEXES.items():
        if v["yahoo"].upper() == s:
            return k
    if s == INDIA_VIX:
        return "INDIAVIX"
    return re.sub(r"\.(NS|BO)$", "", s)


SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")


def valid_symbol(s):
    return bool(SYMBOL_RE.match((s or "").upper()))


# ================================================================ ticks, bands, circuits

def tick_size(price, segment="equity"):
    """Price step. Options premium and futures: 0.05. Equity (NSE price-linked
    tick sizes, revised 2025): below Rs 250 -> 0.01, else 0.05 up to 1,000,
    0.10 up to 5,000, 0.50 up to 10,000, 1.00 up to 20,000, 5.00 above.
    NSE reviews these; a wrong tick is a rejected order, not a bad fill,
    and the broker shows the right one. Override with "tick" in a plan."""
    if segment in ("options", "futures"):
        return 0.05
    p = abs(float(price))
    if p < 250:
        return 0.01
    if p < 1000:
        return 0.05
    if p < 5000:
        return 0.10
    if p < 10000:
        return 0.50
    if p < 20000:
        return 1.00
    return 5.00


def round_tick(price, tick=None, mode="nearest", segment="equity"):
    """Round to the tick. mode: nearest | up | down."""
    t = tick or tick_size(price, segment)
    q = float(price) / t
    if mode == "up":
        n = math.ceil(q - 1e-9)
    elif mode == "down":
        n = math.floor(q + 1e-9)
    else:
        n = math.floor(q + 0.5)
    return round(n * t, 2)


PRICE_BANDS = (2, 5, 10, 20)   # % circuit filters for stocks outside F&O


def band_limits(prev_close, band_pct):
    """Upper/lower circuit for a fixed price band (stocks NOT in F&O).
    F&O stocks have no fixed band: they have dynamic price bands (10% to start,
    widened in 5% steps by the exchange). Use band_pct=None for those."""
    if band_pct is None:
        return None
    up = round_tick(prev_close * (1 + band_pct / 100.0), mode="down")
    lo = round_tick(prev_close * (1 - band_pct / 100.0), mode="up")
    return {"band_pct": band_pct, "upper": up, "lower": lo}


def near_circuit(price, limits, within_pct=2.0):
    """'upper' / 'lower' when price is within within_pct% of a circuit limit."""
    if not limits:
        return None
    if price >= limits["upper"] * (1 - within_pct / 100.0):
        return "upper"
    if price <= limits["lower"] * (1 + within_pct / 100.0):
        return "lower"
    return None


def index_circuit_halt(move_pct, t):
    """Market-wide circuit breaker (NIFTY 50 or SENSEX, whichever first).
    -> (level, halt description). Halt rules by time of day (SEBI MWCB)."""
    a = abs(move_pct)
    hhmm = t.hour * 60 + t.minute
    if a >= 20:
        return 20, "trading halted for the rest of the day"
    if a >= 15:
        if hhmm < 13 * 60:
            return 15, "1 hour 45 minutes halt, then a 15-minute pre-open call auction"
        if hhmm < 14 * 60:
            return 15, "45 minutes halt, then a 15-minute pre-open call auction"
        return 15, "trading halted for the rest of the day"
    if a >= 10:
        if hhmm < 13 * 60:
            return 10, "45 minutes halt, then a 15-minute pre-open call auction"
        if hhmm < 14 * 60 + 30:
            return 10, "15 minutes halt, then a 15-minute pre-open call auction"
        return 10, "no halt (after 2:30 PM)"
    return 0, None


# ================================================================ charges

SEGMENTS = ("delivery", "intraday", "futures", "options")
STT = {  # rate, side ("both" / "sell" / "buy")
    "delivery": (0.001, "both"),
    "intraday": (0.00025, "sell"),
    "futures": (0.0005, "sell"),
    "options": (0.0015, "sell"),      # on premium
}
STT_OPTION_EXERCISE = 0.0015          # on intrinsic value, paid by the buyer of an exercised option
EXCHANGE_TXN = {
    "NSE": {"delivery": 0.0000307, "intraday": 0.0000307, "futures": 0.0000183, "options": 0.0003553},
    "BSE": {"delivery": 0.0000375, "intraday": 0.0000375, "futures": 0.0, "options": 0.000325},
}
SEBI_FEE = 10.0 / 1e7                 # Rs 10 per crore
IPFT = {"NSE": 0.01 / 1e7, "BSE": 0.0}
STAMP_BUY = {"delivery": 0.00015, "intraday": 0.00003, "futures": 0.00002, "options": 0.00003}
GST = 0.18

# Brokerage per executed order. ("flat", rs) | ("pct_cap", pct, cap) | ("pct_cap_min", pct, cap, floor)
BROKERS = {
    "zerodha": {"delivery": ("flat", 0.0), "intraday": ("pct_cap", 0.0003, 20.0),
                "futures": ("pct_cap", 0.0003, 20.0), "options": ("flat", 20.0), "dp": 13.0,
                "auto_square_off": {"equity": "15:25", "fno": "15:26"}, "square_off_fee": 50.0},
    "upstox": {"delivery": ("flat", 20.0), "intraday": ("pct_cap", 0.001, 20.0),
               "futures": ("pct_cap", 0.0005, 20.0), "options": ("flat", 20.0), "dp": 20.0,
               "auto_square_off": {"equity": "15:20", "fno": "15:20"}, "square_off_fee": 50.0},
    "angelone": {"delivery": ("pct_cap_min", 0.001, 20.0, 5.0), "intraday": ("pct_cap", 0.001, 20.0),
                 "futures": ("flat", 20.0), "options": ("flat", 20.0), "dp": 20.0,
                 "auto_square_off": {"equity": "15:15", "fno": "15:15"}, "square_off_fee": 50.0},
    "flat20": {"delivery": ("flat", 20.0), "intraday": ("flat", 20.0), "futures": ("flat", 20.0),
               "options": ("flat", 20.0), "dp": 20.0,
               "auto_square_off": {"equity": "15:15", "fno": "15:15"}, "square_off_fee": 50.0},
}
BROKER_NOTES = {
    "zerodha": "zerodha.com/charges, read 2026-10-28 (auto square-off: CAS stocks 15:12, others 15:25, F&O 15:26)",
    "upstox": "upstox.com/brokerage-charges, read 2026-10-28 (square-off time: check your app)",
    "angelone": "angelone.in charges, read 2026-10-28 (first-30-days free offer ignored; square-off: check app)",
    "flat20": "a generic Rs 20 per order discount broker",
}


def _r2(x):
    return round(x + 1e-12, 2)


def brokerage(segment, turnover, broker="zerodha", profile=None):
    rule = (profile or BROKERS[broker])[segment]
    kind = rule[0]
    if kind == "flat":
        return rule[1] if turnover > 0 else 0.0
    if kind == "pct_cap":
        return min(turnover * rule[1], rule[2])
    if kind == "pct_cap_min":
        return max(min(turnover * rule[1], rule[2]), rule[3]) if turnover > 0 else 0.0
    raise MarketError("unknown brokerage rule %r" % (rule,))


def broker_profile(cfg=None):
    cfg = cfg or {}
    name = (cfg.get("broker") or "zerodha").lower()
    prof = dict(BROKERS.get(name) or BROKERS["flat20"])
    custom = cfg.get("brokerage")
    if isinstance(custom, dict):
        for seg, rule in custom.items():
            if seg in SEGMENTS and isinstance(rule, list):
                prof[seg] = tuple(rule)
            elif seg == "dp":
                prof["dp"] = float(rule)
    return name if name in BROKERS else "custom", prof


def leg_charges(segment, side, price, qty, exchange="NSE", broker="zerodha", cfg=None):
    """Statutory charges + brokerage for ONE executed order. price is the
    premium for options. -> dict of rupee components, each rounded to paisa."""
    if segment not in SEGMENTS:
        raise MarketError("segment must be one of %s" % ", ".join(SEGMENTS))
    side = side.lower()
    _, prof = broker_profile(dict(cfg or {}, broker=broker))
    turnover = float(price) * float(qty)
    over = (cfg or {}).get("charges") or {}
    stt_rate, stt_side = STT[segment]
    stt_rate = over.get("stt", {}).get(segment, stt_rate) if isinstance(over.get("stt"), dict) else stt_rate
    stt = turnover * stt_rate if stt_side in ("both", side) else 0.0
    txn = turnover * EXCHANGE_TXN[exchange][segment]
    sebi = turnover * SEBI_FEE
    ipft = turnover * IPFT.get(exchange, 0.0)
    stamp = turnover * STAMP_BUY[segment] if side == "buy" else 0.0
    brk = brokerage(segment, turnover, profile=prof)
    gst = (brk + txn + sebi) * GST
    parts = {"turnover": _r2(turnover), "brokerage": _r2(brk), "stt": _r2(stt), "exchange_txn": _r2(txn),
             "sebi": _r2(sebi), "ipft": _r2(ipft), "stamp": _r2(stamp), "gst": _r2(gst)}
    parts["total"] = _r2(sum(v for k, v in parts.items() if k != "turnover"))
    return parts


def round_trip(segment, buy_price, sell_price, qty, exchange="NSE", broker="zerodha", cfg=None):
    """Both legs plus DP charges (delivery sell) -> totals, net P&L and the
    breakeven move per unit."""
    b = leg_charges(segment, "buy", buy_price, qty, exchange, broker, cfg)
    s = leg_charges(segment, "sell", sell_price, qty, exchange, broker, cfg)
    _, prof = broker_profile(dict(cfg or {}, broker=broker))
    dp = _r2(prof.get("dp", 0.0) * (1 + GST)) if segment == "delivery" and qty > 0 else 0.0
    total = _r2(b["total"] + s["total"] + dp)
    gross = _r2((float(sell_price) - float(buy_price)) * float(qty))
    keys = ("brokerage", "stt", "exchange_txn", "sebi", "ipft", "stamp", "gst")
    return {"segment": segment, "exchange": exchange, "broker": broker, "qty": qty,
            "buy": b, "sell": s, "dp": dp,
            "by_type": {k: _r2(b[k] + s[k]) for k in keys},
            "total": total, "gross_pnl": gross, "net_pnl": _r2(gross - total),
            "breakeven_per_unit": _r2(total / float(qty)) if qty else None,
            "cost_pct_of_turnover": _r2(100.0 * total / max(b["turnover"] + s["turnover"], 1e-9))}


def segment_for(product, instrument="EQ"):
    """Broker product + instrument -> charge segment."""
    inst = (instrument or "EQ").upper()
    if inst in ("CE", "PE", "OPT", "OPTION", "OPTIONS"):
        return "options"
    if inst in ("FUT", "FUTURE", "FUTURES"):
        return "futures"
    return "intraday" if (product or "MIS").upper() == "MIS" else "delivery"


def exercise_stt(intrinsic_value_per_unit, qty):
    """STT on an option exercised (ITM at expiry), charged on intrinsic value."""
    return _r2(intrinsic_value_per_unit * qty * STT_OPTION_EXERCISE)


# ================================================================ tax (a guide, not advice)

TAX = {
    "stcg_equity": 0.20,       # listed equity held <= 12 months, STT paid (from 23 Jul 2024)
    "ltcg_equity": 0.125,      # held > 12 months, above the yearly exemption
    "ltcg_exemption": 125000.0,
    "cess": 0.04,
    "new_regime_slabs": [(400000, 0.0), (800000, 0.05), (1200000, 0.10), (1600000, 0.15),
                         (2000000, 0.20), (2400000, 0.25), (None, 0.30)],
}
TAX_KINDS = {
    "intraday": "speculative business income: taxed at your slab rate; losses set off only against "
                "speculative income, carried forward 4 years",
    "fno": "non-speculative business income: taxed at your slab rate; losses set off against other "
           "non-salary income, carried forward 8 years; ITR-3; tax audit may apply above the turnover limit",
    "stcg": "short-term capital gain on listed shares (held 12 months or less): 20% plus cess",
    "ltcg": "long-term capital gain (held over 12 months): 12.5% plus cess on gains above Rs 1.25 lakh a year",
}


def slab_tax(income, slabs=None):
    slabs = slabs or TAX["new_regime_slabs"]
    tax, lo = 0.0, 0.0
    for hi, rate in slabs:
        top = income if hi is None else min(income, hi)
        if top > lo:
            tax += (top - lo) * rate
        if hi is None or income <= hi:
            break
        lo = hi
    return tax


def tax_estimate(kind, profit, other_income=0.0, ltcg_used=0.0):
    """Rough tax on one kind of trading profit. Returns the rule in words and,
    where it is a flat rate, the number. Slab-rate income is shown as the
    extra tax on top of other_income under the new regime."""
    kind = kind.lower()
    if kind not in TAX_KINDS:
        raise MarketError("kind must be one of %s" % ", ".join(TAX_KINDS))
    out = {"kind": kind, "profit": _r2(profit), "rule": TAX_KINDS[kind], "tax": None}
    if profit <= 0:
        out["note"] = "a loss: no tax; record it to carry forward (file the return on time)"
        return out
    if kind == "stcg":
        out["tax"] = _r2(profit * TAX["stcg_equity"] * (1 + TAX["cess"]))
    elif kind == "ltcg":
        taxable = max(0.0, profit - max(0.0, TAX["ltcg_exemption"] - ltcg_used))
        out["taxable"] = _r2(taxable)
        out["tax"] = _r2(taxable * TAX["ltcg_equity"] * (1 + TAX["cess"]))
    else:
        extra = slab_tax(other_income + profit) - slab_tax(other_income)
        out["tax"] = _r2(extra * (1 + TAX["cess"]))
        out["note"] = ("new-regime slabs FY2026-27, before rebate and surcharge; with other income %s"
                       % inr(other_income))
    return out


# ================================================================ money formatting

def inr(x, paise=True):
    """Indian digit grouping: Rs 1,23,45,678.90"""
    if x is None:
        return "n/a"
    neg = float(x) < 0
    cents = int(round(abs(float(x)) * 100))
    whole, frac = divmod(cents, 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if neg and cents else "") + "Rs " + s + ((".%02d" % frac) if paise else "")


def crore(x):
    return "Rs %.2f cr" % (float(x) / 1e7)


# ================================================================ config

DEFAULT_CONFIG = {
    "capital_inr": 100000,
    "broker": "zerodha",
    "exchange": "NSE",
    "paper": True,
    "cash_buffer_pct": 10,
    "max_position_pct": 50,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 15,
    "min_reward_risk": 1.5,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:10",
    "default_product": "MIS",
    "use_intraday_leverage": False,
    "intraday_margin_pct": 20,
    "allow_short": False,
    "allow_swing": True,
    "allow_futures": False,
    "allow_option_buying": True,
    "allow_option_selling": False,
    "max_premium_per_trade_inr": 5000,
    "max_lots_per_trade": 2,
    "futures_margin_pct": 15,
    "sl_limit_buffer_pct": 2.0,
    "expiry_day_last_entry": "13:30",
    "avoid_circuit_within_pct": 2.0,
    "min_avg_turnover_cr": 5,
    "block_series": ["BE", "BZ", "BT", "IL", "IT"],
    "blocked": [],
    "asm_gsm": [],
    "fno_ban": [],
    "risk_free_rate": 0.065,
    "slippage_pct": 0.05,
    "tax_regime": "new",
}
CONFIG_FILE = "trader/config.json"


def load_config(path=CONFIG_FILE, required=False):
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            raise MarketError("%s is not a JSON object" % path)
        cfg.update(user)
    except FileNotFoundError:
        if required:
            raise MarketError("%s not found: copy trader-budget/config.example.json there" % path)
    except ValueError as e:
        raise MarketError("%s is not valid JSON: %s" % (path, e))
    return cfg


# ================================================================ files

def read_json(path, what="file", required=True, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if required:
            raise MarketError("%s not found (%s)" % (what, path))
        return default
    except ValueError as e:
        raise MarketError("%s (%s) is not valid JSON: %s" % (what, path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_text_maybe_gz(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            raw = z.read(z.namelist()[0])
    return raw.decode("utf-8", "replace")


# ================================================================ network

class NetError(MarketError):
    pass


class Net(object):
    """Polite HTTP with retries. One object per run; it keeps NSE's cookies.

    Yahoo: query2 then query1, one backoff round after a 429.
    NSE: www.nseindia.com/api/* needs the cookies a normal page visit sets,
    so the first call visits the home page. NSE blocks some networks and most
    data centres; when it does, save the JSON from your browser and pass
    --offline FILE (every tool accepts it).
    BSE: api.bseindia.com wants a Referer from www.bseindia.com."""

    def __init__(self, pause=0.3, timeout=20, sleep=time.sleep, opener=None):
        self.pause = pause
        self.timeout = timeout
        self.sleep = sleep
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._nse_ready = False
        self._last = 0.0
        self.calls = 0

    def _polite(self):
        gap = time.time() - self._last
        if self.pause and gap < self.pause:
            self.sleep(self.pause - gap)
        self._last = time.time()

    def get(self, url, headers=None, binary=False, tries=3, backoff=3.0):
        last = "no answer"
        for attempt in range(tries):
            self._polite()
            h = {"User-Agent": UA, "Accept": "*/*"}
            h.update(headers or {})
            req = urllib.request.Request(url, headers=h)
            try:
                self.calls += 1
                with self.opener.open(req, timeout=self.timeout) as r:
                    data = r.read()
                    if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                        try:
                            data = gzip.decompress(data)
                        except OSError:
                            pass
                    return data if binary else data.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                body = b""
                try:
                    body = e.read()
                except Exception:
                    pass
                finally:
                    e.close()
                if e.code == 429 or e.code >= 500:
                    if attempt < tries - 1:
                        self.sleep(backoff * (attempt + 1))
                    continue
                if body.strip().startswith(b"{"):
                    return body if binary else body.decode("utf-8", "replace")
                raise NetError("%s answered %s" % (urllib.parse.urlsplit(url).netloc, last))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                if attempt < tries - 1:
                    self.sleep(backoff)
                continue
        raise NetError("%s: %s" % (urllib.parse.urlsplit(url).netloc, last))

    # ---- Yahoo
    def yahoo(self, path):
        last = None
        for host in ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com"):
            try:
                return self.get(host + path, tries=2)
            except NetError as e:
                last = e
        raise NetError("Yahoo Finance: %s" % last)

    def chart(self, symbol, rng="5d", interval="5m", exchange="NSE"):
        ys = yahoo_symbol(symbol, exchange)
        path = "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ys, safe="^.-_"), rng, interval)
        return json.loads(self.yahoo(path))

    def spark(self, symbols, rng="3mo", interval="1d", exchange="NSE"):
        """{SYMBOL: series} for up to any number of symbols (20 per request)."""
        out = {}
        syms = list(symbols)
        for i in range(0, len(syms), 20):
            chunk = syms[i:i + 20]
            ys = ",".join(urllib.parse.quote(yahoo_symbol(s, exchange), safe="^.-_") for s in chunk)
            text = self.yahoo("/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (ys, rng, interval))
            out.update(parse_spark(text))
        return out

    # ---- NSE
    NSE = "https://www.nseindia.com"

    def _nse_headers(self, referer="/"):
        return {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-IN,en;q=0.9", "Referer": self.NSE + referer}

    def nse_bootstrap(self, referer="/option-chain"):
        self.get(self.NSE + "/", headers={"User-Agent": BROWSER_UA, "Accept": "text/html"}, tries=2)
        self.get(self.NSE + referer, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"}, tries=1)
        self._nse_ready = True

    def nse_json(self, path, referer="/option-chain"):
        for attempt in (0, 1):
            if not self._nse_ready or attempt:
                try:
                    self.nse_bootstrap(referer)
                except NetError:
                    pass
            try:
                text = self.get(self.NSE + path, headers=self._nse_headers(referer), tries=2)
                return json.loads(text)
            except (NetError, ValueError) as e:
                err = e
        raise NetError("NSE did not answer %s (%s). NSE often blocks scripts and data-centre IPs: "
                       "open the URL in your browser, save the JSON, and pass --offline FILE"
                       % (path.split("?")[0], err))

    def nse_archive(self, url, binary=False):
        return self.get(url, headers={"User-Agent": BROWSER_UA, "Referer": self.NSE + "/"}, binary=binary)

    # ---- BSE
    def bse_json(self, url):
        text = self.get(url, headers={"User-Agent": BROWSER_UA, "Referer": "https://www.bseindia.com/",
                                      "Origin": "https://www.bseindia.com", "Accept": "application/json"})
        try:
            return json.loads(text)
        except ValueError:
            raise NetError("BSE answered something that is not JSON for %s" % url.split("?")[0])


# ================================================================ Yahoo parsing

def series_from_result(res):
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return (list(v) + [None] * (n - len(v))) if isinstance(v, list) else [None] * n
    return {"t": list(t), "o": col("open"), "h": col("high"), "l": col("low"), "c": col("close"),
            "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc):
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, (str, bytes)) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise MarketError("Yahoo's spark answer is not the shape this tool knows")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        out[plain_symbol(ysym)] = series_from_result(resp[0])
    if not out and sp.get("error"):
        e = sp["error"]
        raise MarketError("Yahoo spark: %s" % (e.get("description") or e.get("code") or e))
    return out


def parse_chart(text_or_doc, sym=""):
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, (str, bytes)) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise MarketError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise MarketError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def bars_from_series(series, interval_sec=300, now=None, complete_only=True):
    """series -> list of bars {t, dt (IST), o, h, l, c, v}. Drops null closes,
    the live tick (off the bar grid) and, when now is given, the bar still forming."""
    out = []
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None or ts is None:
            continue
        dt = ist_from_ts(ts)
        if interval_sec < 86400 and (dt.minute * 60 + dt.second) % interval_sec and interval_sec < 3600:
            continue
        if now is not None and complete_only and interval_sec < 86400:
            if dt + datetime.timedelta(seconds=interval_sec) > now:
                continue
        o, h, lo = series["o"][i], series["h"][i], series["l"][i]
        out.append({"t": ts, "dt": dt, "o": o if o is not None else c, "h": h if h is not None else c,
                    "l": lo if lo is not None else c, "c": c, "v": series["v"][i] or 0})
    return out


def by_session(bars):
    days = {}
    for b in bars:
        days.setdefault(b["dt"].date(), []).append(b)
    return dict(sorted(days.items()))


def bar_slot(dt, interval_min=5):
    """Index of a bar in the session: 09:15 -> 0, 09:20 -> 1 ..."""
    return ((dt.hour * 60 + dt.minute) - (OPEN.hour * 60 + OPEN.minute)) // interval_min


def bars_per_session(interval_min=5):
    return SESSION_MINUTES // interval_min


# ================================================================ indicators (shared)

def sma(xs, n):
    return sum(xs[-n:]) / float(n) if len(xs) >= n else None


def ema_series(xs, n):
    if not xs:
        return []
    k = 2.0 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    """Wilder RSI of the last close."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def atr(bars, n=14):
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, lo, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


def vwap(bars):
    pv = vv = 0.0
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += typical * (b["v"] or 0)
        vv += b["v"] or 0
    return pv / vv if vv else None


def gap_pct(open_price, prev_close):
    if not prev_close:
        return None
    return 100.0 * (open_price - prev_close) / prev_close


def classify_gap(gp, small=0.5, large=2.0):
    if gp is None:
        return None
    if gp >= large:
        return "gap-up"
    if gp <= -large:
        return "gap-down"
    if gp >= small:
        return "small gap-up"
    if gp <= -small:
        return "small gap-down"
    return "flat open"


def run_cli(main, argv=None):
    """Run a tool's main(); bad data in a trader/ file becomes one plain error line, not a traceback."""
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("stopped.", file=sys.stderr)
        return 130
    except (TypeError, KeyError, AttributeError, IndexError, OverflowError, ValueError, ZeroDivisionError) as e:
        print("error: unexpected data (%s: %s). A file in trader/ or a number in trader/config.json may be "
              "malformed; fix or regenerate it." % (type(e).__name__, e), file=sys.stderr)
        return 1


# ================================================================ CLI

def _print(doc, as_json):
    if as_json:
        print(json.dumps(doc, indent=2, default=str))
        return True
    return False


def cmd_clock(a):
    now = parse_now(a.now)
    st = market_status(now)
    if _print(st, a.json):
        return 0
    print("%s %s IST  %s" % (st["date"], st["time"], st["label"]))
    nxt = next_trading_day(now.date()) if not st["open"] else None
    if nxt:
        print("next trading day: %s (%s)" % (nxt.isoformat(), nxt.strftime("%A")))
    for idx in ("NIFTY", "SENSEX"):
        e = next_expiry(idx, now.date())
        if e:
            print("next %s expiry: %s (%s)%s" % (idx, e.isoformat(), e.strftime("%A"),
                                                 "  <- TODAY" if e == now.date() else ""))
    if st.get("warning"):
        print("note: " + st["warning"])
    return 0


def cmd_calendar(a):
    y = a.year or now_ist().year
    hol = holidays_for(y)
    if not hol:
        print("No %d holiday list in this tool yet. Save NSE's list from "
              "https://www.nseindia.com/api/holiday-master?type=trading (open it in your browser after "
              "visiting nseindia.com) to %s." % (y, HOLIDAY_FILE))
        return 1
    rows = [{"date": d.isoformat(), "weekday": d.strftime("%A"), "name": n, "weekend": d.weekday() >= 5}
            for d, n in sorted(hol.items())]
    if _print(rows, a.json):
        return 0
    print("NSE/BSE trading holidays %d (equity and F&O):" % y)
    for r in rows:
        print("  %s %-9s %s%s" % (r["date"], r["weekday"], r["name"], "  (weekend)" if r["weekend"] else ""))
    for d, sp in sorted(SPECIAL_SESSIONS.items()):
        if d.year == y:
            print("  %s special session: %s (%s)" % (d.isoformat(), sp["name"],
                                                     "timing by NSE circular" if not sp["start"] else
                                                     "%s-%s" % (sp["start"], sp["end"])))
    return 0


def cmd_expiry(a):
    start = parse_now(a.now).date()
    sym = a.symbol.upper()
    rows = expiries(sym, start, weeks=a.weeks, months=a.months)
    if _print(rows, a.json):
        return 0
    lot = lot_size(sym)
    print("%s (%s)%s" % (sym, exchange_of(sym), ", lot %d" % lot if lot else ""))
    for r in rows:
        print("  %s %-9s %s" % (r["date"], r["weekday"], r["kind"]))
    if sym in INDEXES and not INDEXES[sym]["weekly"]:
        print("  (monthly only: SEBI allows one weekly index per exchange: NIFTY on NSE, SENSEX on BSE)")
    return 0


def cmd_lots(a):
    if a.refresh:
        net = Net()
        text = net.nse_archive(LOTS_URL)
        lots = parse_mktlots(text)
        if not lots:
            raise MarketError("fo_mktlots.csv had no rows this tool could read")
        write_json(LOTS_FILE, {"source": LOTS_URL, "saved_at": now_ist().isoformat(timespec="minutes"),
                               "lots": lots})
        print("saved %d lot sizes to %s" % (len(lots), LOTS_FILE))
        return 0
    table = lots_table()
    if a.symbol:
        s = a.symbol.upper()
        print("%s lot %s, strike step %s" % (s, table.get(s, "unknown (refresh with --refresh)"),
                                             strike_step(s) or "by price"))
        return 0
    if _print(table, a.json):
        return 0
    print("Index F&O (Oct-Dec 2026 contracts):")
    for k, v in INDEXES.items():
        print("  %-11s %-3s lot %4d  strike step %-4s weekly: %s" % (k, v["exchange"], v["lot"], v["step"],
                                                                    "yes" if v["weekly"] else "no"))
    print("Stocks known: %d (run `lots --refresh` for NSE's full list)" % (len(table) - len(INDEXES)))
    return 0


def cmd_costs(a):
    cfg = load_config(a.config)
    broker = a.broker or cfg.get("broker", "zerodha")
    rt = round_trip(a.segment, a.buy, a.sell, a.qty, a.exchange, broker, cfg)
    if _print(rt, a.json):
        return 0
    print("%s %s, %s x %s, buy %s sell %s (broker: %s)" % (a.exchange, a.segment, a.qty,
                                                           "1", a.buy, a.sell, broker))
    for k, v in rt["by_type"].items():
        print("  %-13s %12s" % (k, inr(v)))
    if rt["dp"]:
        print("  %-13s %12s" % ("dp charges", inr(rt["dp"])))
    print("  %-13s %12s" % ("TOTAL", inr(rt["total"])))
    print("gross %s  ->  net %s   (breakeven move %s per unit, %s%% of turnover)"
          % (inr(rt["gross_pnl"]), inr(rt["net_pnl"]), inr(rt["breakeven_per_unit"]),
             rt["cost_pct_of_turnover"]))
    return 0


def cmd_tax(a):
    t = tax_estimate(a.kind, a.profit, a.other_income, a.ltcg_used)
    if _print(t, a.json):
        return 0
    print("%s: %s" % (t["kind"], t["rule"]))
    if t["tax"] is not None:
        print("estimated tax on %s: %s%s" % (inr(t["profit"]), inr(t["tax"]),
                                              "  (" + t["note"] + ")" if t.get("note") else ""))
    elif t.get("note"):
        print(t["note"])
    print("A rough guide, not tax advice. Rules change every Budget; ask a CA before you file.")
    return 0


def cmd_bands(a):
    lim = band_limits(a.price, a.band)
    if not lim:
        print("F&O stocks have no fixed circuit: dynamic price bands (10%% to start, widened in steps).")
        return 0
    print("prev close %s, %d%% band: upper %s, lower %s" % (a.price, a.band, lim["upper"], lim["lower"]))
    return 0


def cmd_tick(a):
    t = tick_size(a.price, a.segment)
    print("tick %s -> %s (nearest), %s (up), %s (down)" % (t, round_tick(a.price, t), round_tick(a.price, t, "up"),
                                                           round_tick(a.price, t, "down")))
    return 0


def cmd_symbol(a):
    print(yahoo_symbol(a.symbol, a.exchange))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="market.py", description="Indian market rules: hours, holidays, expiries, "
                                "lot sizes, charges, tax, circuits.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--config", default=CONFIG_FILE)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("clock")
    s.add_argument("--now")
    s.set_defaults(fn=cmd_clock)
    s = sub.add_parser("calendar")
    s.add_argument("--year", type=int)
    s.set_defaults(fn=cmd_calendar)
    s = sub.add_parser("expiry")
    s.add_argument("symbol")
    s.add_argument("--now")
    s.add_argument("--weeks", type=int, default=5)
    s.add_argument("--months", type=int, default=3)
    s.set_defaults(fn=cmd_expiry)
    s = sub.add_parser("lots")
    s.add_argument("symbol", nargs="?")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(fn=cmd_lots)
    s = sub.add_parser("costs")
    s.add_argument("--segment", required=True, choices=SEGMENTS)
    s.add_argument("--buy", type=float, required=True)
    s.add_argument("--sell", type=float, required=True)
    s.add_argument("--qty", type=float, required=True)
    s.add_argument("--exchange", default="NSE", choices=("NSE", "BSE"))
    s.add_argument("--broker", choices=sorted(BROKERS))
    s.set_defaults(fn=cmd_costs)
    s = sub.add_parser("tax")
    s.add_argument("--kind", required=True, choices=sorted(TAX_KINDS))
    s.add_argument("--profit", type=float, required=True)
    s.add_argument("--other-income", type=float, default=0.0)
    s.add_argument("--ltcg-used", type=float, default=0.0)
    s.set_defaults(fn=cmd_tax)
    s = sub.add_parser("bands")
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--band", type=int, choices=PRICE_BANDS)
    s.set_defaults(fn=cmd_bands)
    s = sub.add_parser("tick")
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--segment", default="equity", choices=("equity", "futures", "options"))
    s.set_defaults(fn=cmd_tick)
    s = sub.add_parser("symbol")
    s.add_argument("symbol")
    s.add_argument("--exchange", default="NSE", choices=("NSE", "BSE"))
    s.set_defaults(fn=cmd_symbol)
    for name in ("clock", "calendar", "expiry", "lots", "costs", "tax", "bands", "tick", "symbol"):
        sub.choices[name].add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sub.choices[name].add_argument("--config", default=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if not getattr(a, "cmd", None):
        p.print_help()
        return 2
    try:
        return a.fn(a)
    except MarketError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_cli(main))
