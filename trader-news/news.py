#!/usr/bin/env python3
"""News monitor for trader-news (India edition). Standard library only.

Watches exchange filings and media headlines for the NSE stocks you watch or
hold and prints only what is new since the last check, newest first, in India
time (IST), with how far each stock has moved today, its opening gap, a NIFTY
line for context, and a tag on every India-relevant catalyst.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE TCS SBIN
    python3 news.py --tickers RELIANCE --all --hours 72
    python3 news.py --no-google                      # exchange filings only
    python3 news.py --no-moves                       # skip the price lines

Your symbols, when --tickers is not given: the hits in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json (trader-broker).

Sources, one request each per symbol:
  1. NSE corporate announcements (the company's own filings to NSE):
     https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=SYMBOL
  2. BSE corporate announcements, when trader/config.json maps the symbol to
     its BSE scrip code ("bse_codes": {"RELIANCE": "500325"}):
     https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?...&strScrip=500325...
  3. Google News RSS search for the company name (media headlines):
     https://news.google.com/rss/search?q=%22Reliance+Industries%22+when:2d&hl=en-IN&gl=IN&ceid=IN:en
     Google's feed terms allow personal, non-commercial use only.
  4. The move: Yahoo Finance's chart (SYMBOL.NS, unofficial, delayed): the
     last price against yesterday's close, and today's open against it (gap).
Yahoo's own headline RSS answers an empty feed for NSE symbols, so it is not used.

Company names (for Google News and to spot mentions): config "names", else
trader/universe.json (written by `scan.py universe`), else the symbol.

State: trader/state/news.json remembers what you have seen (30 days), so an
item shows once. Offline: --offline DIR reads nse-<SYMBOL>.json,
bse-<CODE>.json, gnews-<SYMBOL>.xml and chart-<SYMBOL>.json (chart-NIFTY.json
for the index) from DIR; a missing file is simply skipped. The tests use it.

All of these are free public endpoints: unofficial, delayed, and they can
change or block scripts without notice. Headlines are leads, not facts: open
the filing or the story before acting. Not financial advice.
"""

import argparse
import datetime as _dt
import email.utils
import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

UTC = _dt.timezone.utc
NSE_ANN = "/api/corporate-announcements?index=equities&symbol=%s"
NSE_REFERER = "/companies-listing/corporate-filings-announcements"
BSE_ANN = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1"
           "&strPrevDate=%s&strScrip=%s&strSearch=P&strToDate=%s&strType=C&subcategory=-1")
BSE_ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/%s"
NSE_COMPANY = "https://www.nseindia.com/get-quotes/equity?symbol=%s"
GNEWS = "https://news.google.com/rss/search?q=%s+when:%dd&hl=en-IN&gl=IN&ceid=IN:en"
KEEP_DAYS = 30
MOVE_PCT = 3.0

# (tag, rule, high impact?) -- matched case-insensitively on title + summary + category
CATALYSTS = [
    ("results", r"\b(financial|quarterly|audited|unaudited|standalone|consolidated) results?\b|\bQ[1-4] ?(FY)?\d{0,4} "
                r"(results|earnings|profit|net)\b|\bearnings\b|\bnet profit\b|\bprofit (rises|falls|jumps|drops|up|down)",
     True),
    ("board meeting", r"\bboard meeting\b|\boutcome of (the )?board\b|\bmeeting of the board\b", False),
    ("dividend", r"\bdividend\b|\brecord date\b|\bex-date\b", False),
    ("bonus", r"\bbonus (issue|shares?)\b|\bbonus\b", True),
    ("split", r"\b(stock|share) split\b|\bsub-?division\b|\bsplit of (equity )?shares\b", True),
    ("buyback", r"\bbuy-?back\b", True),
    ("rights issue", r"\brights issue\b|\brights entitlement\b", True),
    ("fund raise", r"\bQIP\b|\bqualified institutions? placement\b|\bpreferential (issue|allotment)\b"
                   r"|\bpreferential basis\b|\bfund ?rais(e|ing)\b|\bwarrants allotment\b", True),
    ("block/bulk deal", r"\bblock deals?\b|\bbulk deals?\b|\bstake sale\b|\boffer for sale\b|\bOFS\b", True),
    ("promoter pledge", r"\bpledge[ds]?\b|\bencumbrance\b|\binvocation\b", True),
    ("SEBI action", r"\bSEBI\b.{0,80}?\b(order|penalty|penalt|bans?|barred|show[- ]cause|settlement|probe)\b"
                    r"|\bshow[- ]cause notice\b|\bpenalty\b", True),
    ("auditor resignation", r"\bauditors?\b.{0,80}?\bresign|\bresignation of (the )?(statutory |secretarial )?auditor",
     True),
    ("credit rating", r"\bcredit rating\b|\brating (upgrade|downgrade|reaffirm)|\b(CRISIL|ICRA|CARE Ratings|"
                      r"India Ratings|Acuite|Moody'?s|Fitch)\b|\bdowngrade[sd]?\b", False),
    ("merger/acquisition", r"\bmerger\b|\bamalgamation\b|\bde-?merger\b|\bacqui(re|res|red|ring|sition)\b"
                           r"|\bscheme of arrangement\b|\btakeover\b|\bopen offer\b", True),
    ("order win", r"\border (win|wins|inflow|book)\b|\b(bags|wins|secures|receives|bagged|won|secured|received)\b"
                  r".{0,30}?\b(order|contract)s?\b|\bletter of award\b|\bLoA\b", False),
    ("management change", r"\bchange in (directorate|key managerial|management)\b|\b(CEO|MD|CFO|managing director|chief executive|chief financial officer|whole[- ]time "
                          r"director|chairman)\b.{0,80}?\b(resign\w*|appoint\w*|steps? down|cessation|quits?)"
                          r"|\b(resign\w*|appoint\w*|cessation|quits?)\b.{0,80}?\b(CEO|MD|CFO|managing director|"
                          r"chief executive|chief financial officer|whole[- ]time director|chairman)\b", True),
    ("insolvency", r"\binsolvency\b|\bNCLT\b|\bIBC\b|\bCIRP\b|\bbankrupt\w*\b|\bdefault(ed|s)?\b", True),
    ("F&O ban", r"\bF&O ban\b|\bban period\b|\bMWPL\b|\bmarket[- ]wide position limit\b", True),
    ("surveillance", r"\bASM\b|\bGSM\b|\badditional surveillance\b|\bgraded surveillance\b|\bsurveillance measure",
     True),
    ("circuit", r"\b(upper|lower) circuit\b|\bcircuit (limit|filter|breaker)\b|\bprice band\b", False),
]
HIGH = set(t for t, _, h in CATALYSTS if h)
_RX = [(t, re.compile(rx, re.I)) for t, rx, _ in CATALYSTS]


class NewsError(M.MarketError):
    pass


class MissingOffline(NewsError):
    pass


# ---------------------------------------------------------------- time

def ist(dt):
    """Aware datetime -> naive IST wall clock."""
    return dt.astimezone(M.IST).replace(tzinfo=None)


def ist_text(dt):
    return ist(dt).strftime("%Y-%m-%d %H:%M IST")


def parse_time(value):
    """RFC 822 (RSS, usually GMT), ISO 8601 with an offset, or NSE/BSE local text
    ('28-Oct-2026 21:33:50', '2026-10-28T21:30:12.437', both IST) -> aware UTC, or None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return _dt.datetime.strptime(value, fmt).replace(tzinfo=M.IST).astimezone(UTC)
        except ValueError:
            pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(:\d{2})?)(\.\d+)?$", value)
    if m:
        t = m.group(2) if m.group(3) else m.group(2) + ":00"
        return _dt.datetime.strptime(m.group(1) + " " + t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=M.IST).astimezone(UTC)
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError as e:
            raise NewsError("%s is not valid JSON: %s" % (path, e))
    return None


def _sym(x):
    return M.plain_symbol(str(x).split("|")[0].strip())


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """[symbols]: the scanner's hits, the planned trade and your holdings."""
    out = []

    def add(s):
        if s:
            s = _sym(s)
            if M.valid_symbol(s) and s not in out:
                out.append(s)
    wl = load_json(watchlist) or {}
    for h in (wl.get("hits") or []) if isinstance(wl, dict) else []:
        add(h.get("symbol") or h.get("ticker"))
    pl = load_json(plan) or {}
    if isinstance(pl, dict):
        s = (pl.get("instrument") or {}).get("underlying") or pl.get("symbol")
        if s and not M.is_index(str(s)):
            add(s)
    pf = load_json(portfolio) or {}
    pos = pf.get("positions") if isinstance(pf, dict) else None
    if isinstance(pos, dict):
        for k, p in pos.items():
            inst = (p or {}).get("instrument") or {}
            und = inst.get("underlying")
            if und and M.is_index(und):
                continue
            add(und or (p or {}).get("symbol") or k)
    elif isinstance(pos, list):
        for p in pos:
            add(p.get("symbol") or p.get("ticker"))
    return out


def company_names(symbols, cfg, universe="trader/universe.txt"):
    """{SYMBOL: name}: config "names", then the scanner's saved universe, then the symbol."""
    names = {}
    uni = load_json(os.path.splitext(universe)[0] + ".json") or {}
    saved = uni.get("names") or {} if isinstance(uni, dict) else {}
    conf = {str(k).upper(): v for k, v in (cfg.get("names") or {}).items()}
    for s in symbols:
        names[s] = conf.get(s) or saved.get(s) or s
    return names


def short_name(name):
    """'Reliance Industries Limited' -> 'Reliance Industries' (what the press writes)."""
    n = re.sub(r"\s+", " ", name or "").strip()
    n = re.sub(r"(?i)[\s,]+(limited|ltd\.?|ltd)$", "", n)
    return n.strip() or name


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Network through market.Net, or files from --offline DIR."""

    def __init__(self, net=None, offline=None):
        self.net = net
        self.offline = offline

    def _file(self, name, binary=False):
        path = os.path.join(self.offline, name)
        if not os.path.isfile(path):
            raise MissingOffline("offline: no file %s" % path)
        with open(path, "rb") as fh:
            data = fh.read()
        return data if binary else data.decode("utf-8", "replace")

    def _json_file(self, name):
        try:
            return json.loads(self._file(name))
        except ValueError as e:
            raise NewsError("%s is not valid JSON: %s" % (name, e))

    def nse(self, sym):
        if self.offline:
            return self._json_file("nse-%s.json" % sym)
        return self.net.nse_json(NSE_ANN % urllib.parse.quote(sym, safe=""), referer=NSE_REFERER)

    def bse(self, code, start, end):
        if self.offline:
            return self._json_file("bse-%s.json" % code)
        return self.net.bse_json(BSE_ANN % (start.strftime("%Y%m%d"), code, end.strftime("%Y%m%d")))

    def gnews(self, sym, name, days):
        if self.offline:
            return self._file("gnews-%s.xml" % sym, binary=True)
        return self.net.get(gnews_url(name, days), binary=True)

    def chart(self, sym):
        if self.offline:
            return self._json_file("chart-%s.json" % sym)
        return self.net.chart(sym, "1d", "5m")


def make_net():
    """One network object per run (tests replace this)."""
    return M.Net()


def gnews_url(name, days):
    return GNEWS % (urllib.parse.quote_plus('"%s"' % name), max(1, int(days)))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _clean(s, n=400):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 3].rstrip() + "..."


def _hid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def parse_nse(doc, sym):
    """NSE /api/corporate-announcements answer (a list, or {"data": [...]}) -> items.
    Fields: symbol, sm_name, desc, attchmntText, attchmntFile, an_dt, sort_date (IST)."""
    rows = doc.get("data") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise NewsError("NSE announcements for %s are not the shape this tool knows" % sym)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        desc = _clean(r.get("desc") or r.get("subject") or "announcement", 200)
        text = _clean(r.get("attchmntText") or "")
        when = parse_time(r.get("an_dt") or r.get("sort_date") or r.get("dt") or "")
        link = (r.get("attchmntFile") or "").strip() or NSE_COMPANY % urllib.parse.quote(sym)
        rid = r.get("seq_id") or _hid(r.get("symbol") or sym, r.get("an_dt"), desc, r.get("attchmntFile"))
        title = desc if not text else "%s: %s" % (desc, _clean(text, 160))
        out.append({"id": "nse:%s" % rid, "source": "NSE filing", "kind": "filing",
                    "ticker": (r.get("symbol") or sym).upper(), "company": r.get("sm_name") or "",
                    "title": title, "summary": text, "category": desc, "link": link, "time": when})
    return out


def parse_bse(doc, sym):
    """BSE AnnSubCategoryGetData answer {"Table": [...], "Table1": [{"ROWCNT": n}]} -> items.
    Tolerant: NEWSID, HEADLINE/NEWSSUB, NEWS_DT/DT_TM, ATTACHMENTNAME, CATEGORYNAME, SLONGNAME."""
    rows = doc.get("Table", []) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise NewsError("BSE announcements for %s are not the shape this tool knows" % sym)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        head = _clean(r.get("HEADLINE") or r.get("NEWSSUB") or r.get("NEWS_SUB") or "", 240)
        sub = _clean(r.get("NEWSSUB") or r.get("MORE") or "")
        if not head and not sub:
            continue
        att = (r.get("ATTACHMENTNAME") or "").strip()
        when = parse_time(r.get("NEWS_DT") or r.get("DT_TM") or r.get("News_submission_dt") or "")
        nid = r.get("NEWSID") or _hid(r.get("SCRIP_CD"), head, r.get("NEWS_DT"))
        out.append({"id": "bse:%s" % nid, "source": "BSE filing", "kind": "filing", "ticker": sym,
                    "company": r.get("SLONGNAME") or "", "title": head or sub,
                    "summary": sub if sub != head else "", "category": r.get("CATEGORYNAME") or "",
                    "link": BSE_ATTACH % att if att else "https://www.bseindia.com/corporates/ann.html",
                    "time": when})
    return out


def parse_gnews(data, sym):
    """Google News RSS -> items. Titles read 'Headline - Source': the source is split off."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("Google News feed for %s is not valid XML: %s" % (sym, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        src = _text(it, "source")
        if src and title.endswith(" - " + src):
            title = title[:-len(src) - 3].rstrip()
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        desc = re.sub(r"<[^>]+>", " ", _text(it, "description"))
        out.append({"id": "gn:" + guid, "source": src or "Google News", "kind": "headline", "ticker": sym,
                    "title": _clean(title, 240), "summary": _clean(desc) if desc.strip() != title else "",
                    "category": "", "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_move(doc, sym):
    """Yahoo chart (.NS) -> {ticker, price, prev_close, pct, open, gap_pct, gap, time}."""
    try:
        series = M.parse_chart(doc, sym)
    except M.MarketError as e:
        raise NewsError(str(e))
    meta = series["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % sym)
    when = meta.get("regularMarketTime")
    bars = M.bars_from_series(series, 300)
    t_price = M.ist_from_ts(when).date() if when else (bars[-1]["dt"].date() if bars else None)
    today = [b for b in bars if b["dt"].date() == t_price] if t_price else []
    o = today[0]["o"] if today else None
    gp = M.gap_pct(o, float(prev)) if o else None
    return {"ticker": sym, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "open": round(o, 2) if o else None, "gap_pct": round(gp, 2) if gp is not None else None,
            "gap": M.classify_gap(gp) if gp is not None else None,
            "time": _dt.datetime.fromtimestamp(when, UTC) if when else None}


# ---------------------------------------------------------------- tagging

def mention_patterns(names):
    pats = {}
    for t, name in names.items():
        alts = [r"(?<![A-Za-z0-9&])%s(?![A-Za-z0-9&])" % re.escape(t),
                r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t)]
        sn = short_name(name or "")
        if sn and sn.upper() != t and len(sn) >= 4:
            alts.append(r"\b%s\b" % re.escape(sn).replace(r"\ ", r"[\s-]+"))
        pats[t] = re.compile("|".join(alts))
    return pats


def catalysts(text):
    return [t for t, rx in _RX if rx.search(text or "")]


def tag(items, names):
    """Which watched symbols each item touches, its catalyst tags and whether it is high impact."""
    pats = mention_patterns(names)
    for it in items:
        hay = "%s %s %s" % (it["title"], it.get("summary") or "", it.get("category") or "")
        touches = {it["ticker"]} if it["ticker"] in names else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = catalysts(hay)
        it["important"] = bool(set(it["tags"]) & HIGH)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"] if it["kind"] == "headline" else "",
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           ist(it["time"]).date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, names, cfg, now, hours, filings=True, google=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    codes = {str(k).upper(): str(v) for k, v in (cfg.get("bse_codes") or {}).items()}
    start = ist(now - _dt.timedelta(hours=hours)).date()
    for t in watch:
        if filings:
            try:
                items += parse_nse(fetcher.nse(t), t)
            except MissingOffline:
                pass
            except (NewsError, M.MarketError, ValueError) as exc:
                errors.append("NSE filings for %s: %s" % (t, exc))
            if t in codes:
                try:
                    items += parse_bse(fetcher.bse(codes[t], start, ist(now).date()), t)
                except MissingOffline:
                    pass
                except (NewsError, M.MarketError, ValueError) as exc:
                    errors.append("BSE filings for %s (%s): %s" % (t, codes[t], exc))
        if google:
            try:
                items += parse_gnews(fetcher.gnews(t, short_name(names.get(t) or t), math.ceil(hours / 24.0)), t)
            except MissingOffline:
                pass
            except (NewsError, M.MarketError, ValueError) as exc:
                errors.append("Google News for %s: %s" % (t, exc))
    return items


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in list(watch) + ["NIFTY"]:
        try:
            m = parse_move(fetcher.chart(t), t)
            m["index"] = t == "NIFTY"
            out.append(m)
        except MissingOffline:
            pass
        except (NewsError, M.MarketError, ValueError) as exc:
            errors.append("price for %s: %s" % (t, exc))
    return out


def select(items, state, now, hours=48, show_all=False):
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        it["new"] = fingerprint(it) not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now, i["kind"] == "filing"), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flags = []
        if not m.get("index") and abs(m["pct"]) >= move_pct:
            flags.append("MOVING")
        if not m.get("index") and m.get("gap") in ("gap-up", "gap-down"):
            flags.append("GAP %s %+.2f%%" % ("UP" if m["gap"] == "gap-up" else "DOWN", m["gap_pct"]))
        when = (" at %s IST" % ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        label = M.INDEXES["NIFTY"]["label"] if m.get("index") else m["ticker"]
        out.append("%-11s %s %+.2f%% vs yesterday's close %s%s%s  %s" % (
            label, "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]), when,
            ("  open %+.2f%%" % m["gap_pct"]) if m.get("gap_pct") is not None else "", "  ".join(flags)))
    return [ln.rstrip() for ln in out]


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = ist_text(it["time"]) if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-18s %s" % (mark, when, who, it["source"][:18], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Exchange filings are what the company told NSE/BSE; headlines are leads, not facts. "
               "Free public feeds: unofficial, delayed, can break. Not financial advice." % ist_text(now))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = ist(it["time"]).strftime("%Y-%m-%dT%H:%M:%S+05:30") if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="News monitor (trader-news, India): NSE/BSE filings and headlines.")
    ap.add_argument("--tickers", nargs="*", help="NSE symbols (default: watchlist + plan + portfolio)")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--universe", default="trader/universe.txt", help="the scanner's universe (names)")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-filings", action="store_true", help="skip NSE and BSE announcements")
    ap.add_argument("--no-google", action="store_true", help="skip Google News headlines")
    ap.add_argument("--no-moves", action="store_true", help="skip the price lines")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--config", default=M.CONFIG_FILE)
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='pin the clock ("YYYY-MM-DD HH:MM" IST)')
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


def run(args):
    if args.hours <= 0:
        raise ValueError("--hours must be more than 0")
    if args.offline and not os.path.isdir(args.offline):
        raise NewsError("no offline folder at %s" % args.offline)
    cfg = M.load_config(args.config)
    if args.tickers:
        watch = []
        for t in args.tickers:
            for s in re.split(r"[,\s]+", t):
                if not s:
                    continue
                s = M.plain_symbol(s)
                if not M.valid_symbol(s):
                    raise ValueError("%r is not an NSE symbol" % s)
                if s not in watch:
                    watch.append(s)
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        raise NewsError("no symbols: pass --tickers, or run /trader-scanner first (it writes trader/watchlist.json)")
    now = M.parse_now(args.now).replace(tzinfo=M.IST).astimezone(UTC)
    names = company_names(watch, cfg, args.universe)
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(None if args.offline else make_net(), offline=args.offline)
    items = collect(fetcher, watch, names, cfg, now, args.hours, filings=not args.no_filings,
                    google=not args.no_google, errors=errors)
    items = dedupe(tag(items, names))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=ist(m["time"]).strftime("%Y-%m-%dT%H:%M:%S+05:30") if m.get("time") else None,
                   moving=(not m.get("index")) and abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": ist(now).strftime("%Y-%m-%dT%H:%M:%S+05:30"), "symbols": watch,
                          "names": names, "moves": mv, "items": _json_ready(shown), "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        M.write_json(args.state, state)
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
