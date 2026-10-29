#!/usr/bin/env python3
"""Insider and large-deal tracker for trader-insider (India edition). Standard library only.

Reads two kinds of PUBLIC disclosure that NSE publishes:

  1. SEBI PIT disclosures: trades by promoters, the promoter group, directors,
     key managerial personnel (KMP) and designated employees of a listed
     company, which they must disclose (SEBI Prohibition of Insider Trading
     Regulations 2015, reg 7(2)) within 2 trading days when their trades in a
     calendar quarter add up to more than Rs 10 lakh. Pledges of promoter
     shares (creation, release, invocation by the lender) show up here too.
  2. Bulk, block and short deals: a bulk deal is a client's total trades in a
     stock on one day of more than 0.5% of its listed shares, reported by the
     broker the same day; a block deal is a single large trade done in a
     separate window (08:45-09:00 or 14:05-14:20 IST); short deals are
     institutional short sales reported for the day.

    python3 insider.py insider                              # PIT disclosures, last 7 days
    python3 insider.py insider --symbol SAMPLECO --category promoter
    python3 insider.py insider --from 2026-10-01 --to 2026-10-28 --kind market
    python3 insider.py deals                                # today's bulk / block / short deals
    python3 insider.py deals --client "mutual fund" --kind block
    python3 insider.py new                                  # only what is new since the last check
    python3 insider.py watch                                # your symbols and names (config insider_watch)

Sources (unofficial; NSE blocks many scripts and data-centre IPs):
  https://www.nseindia.com/api/corporates-pit?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
  https://www.nseindia.com/api/snapshot-capital-market-largedeal
--offline FILE reads a saved answer instead (insider, deals); new and watch take
--offline-pit FILE and --offline-deals FILE.

These are public disclosures, not inside information. They arrive after the
trade (up to 2 trading days for PIT, end of day for bulk deals). Following
them is not a strategy. Not financial advice.
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

PIT_PATH = "/api/corporates-pit?index=equities&from_date=%s&to_date=%s"
PIT_REFERER = "/companies-listing/corporate-filings-insider-trading"
DEALS_PATH = "/api/snapshot-capital-market-largedeal"
DEALS_REFERER = "/market-data/large-deals"
PIT_THRESHOLD_INR = 1000000.0     # Rs 10 lakh in a calendar quarter (reg 7(2))
DISCLOSE_TRADING_DAYS = 2
BULK_PCT = 0.5
DEFAULT_DAYS = 7
FIRST_RUN_DAYS = 3
MAX_SEEN = 20000
STATE_FILE = "trader/state/insider.json"
HIGHLIGHT = ("promoter_market_buy", "promoter_market_sell", "pledge_created", "pledge_invoked", "pledge_released")
LABELS = {"promoter_market_buy": "PROMOTER MARKET BUY", "promoter_market_sell": "PROMOTER MARKET SELL",
          "pledge_created": "PLEDGE CREATED", "pledge_invoked": "PLEDGE INVOKED (the lender enforced it)",
          "pledge_released": "pledge released", "market_buy": "market buy", "market_sell": "market sale",
          "other": "not a market trade"}
CATEGORIES = {"promoter": "promoter", "promoters": "promoter", "promoter group": "promoter",
              "director": "director", "directors": "director", "kmp": "kmp", "key managerial personnel": "kmp",
              "employee": "employee", "designated employee": "employee", "designated employees": "employee",
              "other": "other"}
NOT_ADVICE = ("Public disclosures, not inside information: they arrive after the trade. Following them is not a "
              "strategy. Not financial advice.")


class InsiderError(M.MarketError):
    pass


def _new_net():
    return M.Net()


NET_FACTORY = _new_net


# ---------------------------------------------------------------- parsing helpers

def num(v):
    """'1,20,000' / 120000 / '-' -> float or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "")
    if not t or t in ("-", "NA", "N.A.", "Nil"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_dt(text):
    """'28-Oct-2026 18:12' / '28-Oct-2026' / '2026-10-28' / '28-10-2026' -> datetime or None."""
    t = str(text or "").strip()
    if not t or t == "-":
        return None
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y",
                "%d-%m-%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def parse_day(text, what="date"):
    """A user date: YYYY-MM-DD or DD-MM-YYYY."""
    d = parse_dt(text)
    if d is None:
        raise InsiderError("%s wants YYYY-MM-DD or DD-MM-YYYY, got %r" % (what, text))
    return d.date()


def nse_date(d):
    return d.strftime("%d-%m-%Y")


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def cr(v):
    return None if v is None else round(v / 1e7, 2)


def size(v):
    return "value n/a" if v is None else M.crore(v)


def qty_words(q):
    if q is None:
        return "qty n/a"
    return M.inr(q, paise=False).replace("Rs ", "")


def category_of(text):
    t = str(text or "").lower()
    if "promoter" in t:
        return "promoter"
    if "director" in t:
        return "director"
    if "kmp" in t or "key managerial" in t:
        return "kmp"
    if "employee" in t:
        return "employee"
    return "other"


def classify(txn, mode, category):
    t = ("%s %s" % (txn or "", mode or "")).lower()
    if "invoc" in t:
        return "pledge_invoked"
    if "pledge" in t or "encumb" in t:
        if re.search(r"revo|release|revok", t):
            return "pledge_released"
        return "pledge_created"
    if re.search(r"revo|release", t) and "pledge" in str(mode or "").lower():
        return "pledge_released"
    side = None
    if re.search(r"\bbuy\b|purchase|acqui", t):
        side = "buy"
    elif re.search(r"\bsell\b|sale|dispos", t):
        side = "sell"
    m = str(mode or "").lower()
    market = "market" in m and "off" not in m
    if not side or not market:
        return "other"
    if category_of(category) == "promoter":
        return "promoter_market_%s" % side
    return "market_%s" % side


def side_of(txn, mode):
    t = ("%s %s" % (txn or "", mode or "")).lower()
    if re.search(r"\bbuy\b|purchase|acqui", t):
        return "BUY"
    if re.search(r"\bsell\b|sale|dispos", t):
        return "SELL"
    return (str(txn or mode or "?").strip() or "?").upper()


# ---------------------------------------------------------------- PIT disclosures

def parse_pit(doc):
    """NSE corporates-pit answer ({"acqNameList": [...], "data": [...]}, or a bare list) -> rows."""
    rows = doc.get("data") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise InsiderError("NSE's insider-trading answer is not the shape this tool knows (no \"data\" list)")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        txn, mode = r.get("tdpTransactionType") or r.get("transactionType"), r.get("acqMode")
        cat = r.get("personCategory") or ""
        disclosed = parse_dt(r.get("date"))                 # when NSE published it
        intimated = parse_dt(r.get("intimDt"))              # when the insider told the company
        frm, to = parse_dt(r.get("acqfromDt")), parse_dt(r.get("acqtoDt"))
        buy_q, sell_q = num(r.get("buyQuantity")), num(r.get("sellquantity") or r.get("sellQuantity"))
        buy_v, sell_v = num(r.get("buyValue")), num(r.get("sellValue"))
        if not txn:
            txn = "Buy" if (buy_q or 0) > 0 else ("Sell" if (sell_q or 0) > 0 else "")
        side = side_of(txn, mode)
        qty = num(r.get("secAcq"))
        if qty is None:
            qty = buy_q if side == "BUY" else sell_q if side == "SELL" else None
        val = num(r.get("secVal"))
        if val is None:
            val = buy_v if side == "BUY" else sell_v if side == "SELL" else None
        row = {"source": "pit", "symbol": sym, "company": r.get("company") or r.get("companyName") or sym,
               "person": str(r.get("acqName") or "").strip() or "?", "category": cat or "?",
               "category_key": category_of(cat), "security": r.get("secType") or "",
               "qty": qty, "value": val, "value_cr": cr(val), "txn": txn or "", "mode": mode or "",
               "side": side, "kind": classify(txn, mode, cat),
               "disclosed": disclosed.strftime("%Y-%m-%d %H:%M") if disclosed else None,
               "intimated": intimated.date().isoformat() if intimated else None,
               "traded_from": frm.date().isoformat() if frm else None,
               "traded_to": to.date().isoformat() if to else None,
               "shares_before": num(r.get("befAcqSharesNo")), "shares_after": num(r.get("afterAcqSharesNo")),
               "holding_before_pct": num(r.get("befAcqSharesPer")),
               "holding_after_pct": num(r.get("afterAcqSharesPer")),
               "regulation": r.get("anex") if str(r.get("anex") or "-").strip() not in ("-", "") else None,
               "exchange": r.get("exchange") or "NSE"}
        if row["kind"].startswith("pledge"):
            row["side"] = {"pledge_created": "PLEDGED", "pledge_invoked": "PLEDGE INVOKED ON",
                           "pledge_released": "PLEDGE RELEASED ON"}[row["kind"]]
        # reg 7(2)(a): the insider tells the company within 2 trading days of the trade (intimDt);
        # reg 7(2)(b): the company tells the exchange within 2 trading days of hearing (date).
        told = intimated or disclosed
        if to and told:
            days = M.trading_days_between(to.date(), told.date())
            row["trading_days_to_disclose"] = days
            row["late"] = days > DISCLOSE_TRADING_DAYS
        if intimated and disclosed:
            cdays = M.trading_days_between(intimated.date(), disclosed.date())
            row["trading_days_company"] = cdays
            row["company_late"] = cdays > DISCLOSE_TRADING_DAYS
        row["id"] = "pit-" + _id(sym, row["person"], row["txn"], row["mode"], qty, val, row["traded_from"],
                                 row["traded_to"], row["disclosed"])
        out.append(row)
    out.sort(key=lambda x: (x["disclosed"] or "", x["symbol"]), reverse=True)
    return out


def select_pit(rows, symbols=None, categories=None, kind=None, start=None, end=None, min_cr=None):
    syms = {s.upper() for s in symbols or []}
    cats = set()
    for c in categories or []:
        key = CATEGORIES.get(c.strip().lower())
        if not key:
            raise InsiderError("unknown category %r (promoter, director, kmp, employee, other)" % c)
        cats.add(key)
    out = []
    for r in rows:
        if syms and r["symbol"] not in syms:
            continue
        if cats and r["category_key"] not in cats:
            continue
        if kind == "market" and r["kind"] not in ("promoter_market_buy", "promoter_market_sell", "market_buy",
                                                    "market_sell"):
            continue
        if kind == "pledge" and not r["kind"].startswith("pledge"):
            continue
        if kind == "highlight" and r["kind"] not in HIGHLIGHT:
            continue
        if r["disclosed"] and start and r["disclosed"][:10] < start.isoformat():
            continue
        if r["disclosed"] and end and r["disclosed"][:10] > end.isoformat():
            continue
        if min_cr and (r["value"] is None or r["value"] < min_cr * 1e7):
            continue
        out.append(r)
    return out


def describe_pit(r, watch=None):
    traded = ""
    if r["traded_from"] and r["traded_to"]:
        a, b = r["traded_from"], r["traded_to"]
        traded = "traded %s" % (a if a == b else "%s to %s" % (a, b))
        if r.get("trading_days_to_disclose") is not None:
            n = r["trading_days_to_disclose"]
            traded += ", %s %d trading day%s later%s" % (
                "told the company" if r.get("intimated") else "disclosed", n, "" if n == 1 else "s",
                " (LATE: over %d, reg 7(2)(a))" % DISCLOSE_TRADING_DAYS if r.get("late") else "")
        if r.get("company_late"):
            traded += ", the company took %d trading days to tell NSE (LATE: over %d, reg 7(2)(b))" % (
                r["trading_days_company"], DISCLOSE_TRADING_DAYS)
    hold = ""
    if r["holding_before_pct"] is not None and r["holding_after_pct"] is not None:
        hold = "holding %.2f%% -> %.2f%%" % (r["holding_before_pct"], r["holding_after_pct"])
    elif r.get("shares_before") is not None and r.get("shares_after") is not None:
        hold = "shares held %s -> %s" % (qty_words(r["shares_before"]), qty_words(r["shares_after"]))
    bits = ["%s %s %s" % (r["side"], qty_words(r["qty"]), r["security"] or "securities"), size(r["value"]),
            r["mode"] or r["txn"] or "mode n/a"]
    line = "%s  %-10s %s (%s): %s" % ((r["disclosed"] or "date n/a"), r["symbol"], r["person"], r["category"],
                                      ", ".join(b for b in bits if b))
    if traded:
        line += "; " + traded
    if hold:
        line += "; " + hold
    flags = []
    if r["kind"] in HIGHLIGHT:
        flags.append(LABELS[r["kind"]])
    elif r["kind"] == "other" and r["side"] in ("BUY", "SELL"):
        flags.append("off-market / not a market trade")
    if watch and watch_hit(r, watch):
        flags.append("on your watch list")
    if flags:
        line += "   <- " + ", ".join(flags)
    return line


# ---------------------------------------------------------------- bulk / block / short deals

DEAL_KEYS = (("BULK_DEALS_DATA", "bulk"), ("BLOCK_DEALS_DATA", "block"), ("SHORT_DEALS_DATA", "short"))


def parse_deals(doc):
    """NSE snapshot-capital-market-largedeal -> (as_on date or None, rows)."""
    if not isinstance(doc, dict) or not any(k in doc for k, _ in DEAL_KEYS):
        raise InsiderError("NSE's large-deal answer is not the shape this tool knows (no BULK_DEALS_DATA)")
    as_on = parse_dt(doc.get("as_on_date"))
    out = []
    for key, kind in DEAL_KEYS:
        for r in doc.get(key) or []:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            qty, price = num(r.get("qty")), num(r.get("watp") or r.get("tradePrice"))
            d = parse_dt(r.get("date")) or as_on
            side = str(r.get("buySell") or ("SHORT" if kind == "short" else "?")).strip().upper()
            client = str(r.get("clientName") or "").strip() or None
            row = {"source": "deals", "kind": kind, "symbol": sym, "name": r.get("name") or sym,
                   "client": client, "side": side, "qty": qty, "price": price,
                   "value": qty * price if qty is not None and price is not None else None,
                   "date": d.date().isoformat() if d else None,
                   "remarks": None if str(r.get("remarks") or "-").strip() in ("-", "") else r.get("remarks")}
            row["value_cr"] = cr(row["value"])
            row["id"] = "deal-" + _id(kind, sym, client, side, qty, price, row["date"])
            out.append(row)
    return (as_on.date() if as_on else None), out


def select_deals(rows, symbols=None, client=None, kind=None, min_cr=None):
    syms = {s.upper() for s in symbols or []}
    out = []
    for r in rows:
        if syms and r["symbol"] not in syms:
            continue
        if client and client.lower() not in (r["client"] or "").lower():
            continue
        if kind and r["kind"] != kind:
            continue
        if min_cr and (r["value"] is None or r["value"] < min_cr * 1e7):
            continue
        out.append(r)
    return out


def describe_deal(r, watch=None):
    what = "%s %s" % (r["side"], qty_words(r["qty"]))
    if r["price"] is not None:
        what += " @ %s" % M.inr(r["price"])
    if r["value"] is not None:
        what += " = %s" % size(r["value"])
    line = "%-5s %-10s %s: %s" % (r["kind"].upper(), r["symbol"], r["name"], what)
    if r["client"]:
        line += "  client: %s" % r["client"]
    if r["remarks"]:
        line += "  (%s)" % r["remarks"]
    flags = []
    if watch:
        name = client_watch_hit(r, watch)
        if name:
            flags.append("watched client: %s" % name)
        elif r["symbol"] in watch["symbols"]:
            flags.append("on your watch list")
    if flags:
        line += "   <- " + ", ".join(flags)
    return line


# ---------------------------------------------------------------- watch lists

def watch_from(cfg, portfolio=None, symbols=None, names=None):
    iw = cfg.get("insider_watch") or {}
    if not isinstance(iw, dict):
        raise InsiderError("insider_watch in the config must be {\"symbols\": [...], \"names\": [...]}")
    syms = {str(s).upper() for s in (iw.get("symbols") or []) + list(symbols or [])}
    people = [str(n).strip() for n in (iw.get("names") or []) + list(names or []) if str(n).strip()]
    clients = [str(n).strip() for n in (cfg.get("watch_clients") or []) + (iw.get("clients") or [])
               if str(n).strip()]
    if portfolio:
        pf = M.read_json(portfolio, "portfolio", required=False, default={}) or {}
        pos = pf.get("positions") if isinstance(pf, dict) else None
        if isinstance(pos, dict):
            syms.update(str(k).split("|")[0].upper() for k in pos)
    return {"symbols": syms, "names": people, "clients": clients}


def watch_hit(r, watch):
    if r["symbol"] in watch["symbols"]:
        return True
    person = (r.get("person") or "").lower()
    return any(n.lower() in person for n in watch["names"])


def client_watch_hit(r, watch):
    client = (r.get("client") or "").lower()
    for n in watch["clients"] + watch["names"]:
        if n.lower() in client:
            return n
    return None


def deal_watch_hit(r, watch):
    return r["symbol"] in watch["symbols"] or bool(client_watch_hit(r, watch))


# ---------------------------------------------------------------- fetching

def load_pit(start, end, offline=None, net=None):
    if offline:
        doc = M.read_json(offline, "saved insider-trading file")
    else:
        net = net or NET_FACTORY()
        doc = net.nse_json(PIT_PATH % (nse_date(start), nse_date(end)), referer=PIT_REFERER)
    return parse_pit(doc)


def load_deals(offline=None, net=None):
    if offline:
        doc = M.read_json(offline, "saved large-deals file")
    else:
        net = net or NET_FACTORY()
        doc = net.nse_json(DEALS_PATH, referer=DEALS_REFERER)
    return parse_deals(doc)


def date_range(a, today):
    end = parse_day(a.to, "--to") if a.to else today
    start = parse_day(a.frm, "--from") if a.frm else end - datetime.timedelta(days=a.days)
    if start > end:
        raise InsiderError("--from %s is after --to %s" % (start, end))
    return start, end


# ---------------------------------------------------------------- output pieces

PIT_TIMING = ("Timing: insiders (promoters, directors, KMP, designated employees) must disclose within 2 trading "
              "days of trades worth over Rs 10 lakh in a calendar quarter (SEBI PIT Regulations, reg 7(2)); the "
              "company then tells the exchange within 2 trading days. You read them days after the trade.")
DEALS_TIMING = ("Timing: a bulk deal is a client's trades in one stock on one day above %.1f%% of its listed "
                "shares, reported the same day after the close; block deals are single large trades in the block "
                "windows %s IST; short deals are the day's reported short sales." % (
                    BULK_PCT, " and ".join("%s-%s" % (M.hm(a), M.hm(b)) for a, b in M.BLOCK_WINDOWS)))


def pit_summary(rows):
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    order = ("promoter_market_buy", "promoter_market_sell", "pledge_created", "pledge_invoked", "pledge_released",
             "market_buy", "market_sell", "other")
    return ", ".join("%d %s" % (counts[k], LABELS[k].lower()) for k in order if counts.get(k))


def _print_json(doc):
    print(json.dumps(doc, indent=2, default=str))


# ---------------------------------------------------------------- commands

def cmd_insider(a):
    today = M.parse_now(a.now).date()
    start, end = date_range(a, today)
    cfg = M.load_config(a.config)
    watch = watch_from(cfg)
    rows = load_pit(start, end, a.offline)
    picked = select_pit(rows, a.symbol, a.category, a.kind, start, end, a.min_cr)
    picked = picked[:a.limit]
    if a.json:
        _print_json({"from": start.isoformat(), "to": end.isoformat(), "rows": picked})
        return 0 if picked else 1
    print("SEBI PIT disclosures on NSE, %s to %s: %d row(s)%s." % (
        start.isoformat(), end.isoformat(), len(picked), (" (" + pit_summary(picked) + ")") if picked else ""))
    hi = [r for r in picked if r["kind"] in HIGHLIGHT]
    if hi:
        print("")
        print("Promoter market trades and pledges:")
        for r in hi:
            print("  " + describe_pit(r, watch))
    rest = [r for r in picked if r["kind"] not in HIGHLIGHT]
    if rest:
        print("")
        print("Other disclosures:")
        for r in rest:
            print("  " + describe_pit(r, watch))
    if not picked:
        print("Nothing matched.")
    print("")
    print(PIT_TIMING)
    print(NOT_ADVICE)
    return 0 if picked else 1


def cmd_deals(a):
    today = M.parse_now(a.now).date()
    cfg = M.load_config(a.config)
    watch = watch_from(cfg)
    as_on, rows = load_deals(a.offline)
    picked = select_deals(rows, a.symbol, a.client, a.kind, a.min_cr)
    if a.watched_only:
        picked = [r for r in picked if deal_watch_hit(r, watch)]
    if a.json:
        _print_json({"as_on": as_on.isoformat() if as_on else None, "rows": picked})
        return 0 if picked else 1
    counts = {k: len([r for r in picked if r["kind"] == k]) for _, k in DEAL_KEYS}
    print("NSE large deals%s: %d bulk, %d block, %d short." % (
        " for %s" % as_on.isoformat() if as_on else "", counts["bulk"], counts["block"], counts["short"]))
    if as_on and as_on != today:
        print("note: NSE's file is for %s, not today (%s): it shows the last trading day until today's is out."
              % (as_on.isoformat(), today.isoformat()))
    for _, kind in DEAL_KEYS:
        for r in [x for x in picked if x["kind"] == kind]:
            print("  " + describe_deal(r, watch))
    if not picked:
        print("Nothing matched.")
    if not watch["clients"]:
        print("Tip: list the institutions you follow in \"watch_clients\" in trader/config.json and they are "
              "flagged here.")
    print("")
    print(DEALS_TIMING)
    print(NOT_ADVICE)
    return 0 if picked else 1


def _collect(a, today, start, sources):
    """Rows from the sources asked for; a failing source is reported, not fatal, unless all fail."""
    pit, deals, as_on, errors = [], [], None, []
    if "pit" in sources:
        try:
            pit = load_pit(start, today, a.offline_pit)
        except M.MarketError as e:
            errors.append("insider disclosures not read: %s" % e)
    if "deals" in sources:
        try:
            as_on, deals = load_deals(a.offline_deals)
        except M.MarketError as e:
            errors.append("large deals not read: %s" % e)
    if errors and len(errors) == len(sources):
        raise InsiderError("; ".join(errors))
    return pit, deals, as_on, errors


def cmd_new(a):
    now = M.parse_now(a.now)
    today = now.date()
    cfg = M.load_config(a.config)
    watch = watch_from(cfg, a.portfolio)
    state = M.read_json(a.state, "insider state", required=False, default=None) or {"seen": [], "last_check": None}
    seen = set(state.get("seen") or [])
    last = parse_dt((state.get("last_check") or "")[:16].replace("T", " "))
    start = last.date() if last else today - datetime.timedelta(days=FIRST_RUN_DAYS)
    sources = ("pit", "deals") if a.source == "all" else (a.source,)
    pit, deals, as_on, errors = _collect(a, today, start, sources)
    pit = select_pit(pit, a.symbol, a.category, None, start, today)
    deals = select_deals(deals, a.symbol, a.client, None, a.min_cr)
    new_pit = [r for r in pit if r["id"] not in seen]
    new_deals = [r for r in deals if r["id"] not in seen]
    shown_pit = new_pit if a.all else [r for r in new_pit if r["kind"] in HIGHLIGHT or watch_hit(r, watch)]
    shown_deals = new_deals
    if a.json:
        _print_json({"since": start.isoformat(), "pit": shown_pit, "deals": shown_deals, "errors": errors,
                     "hidden_pit": len(new_pit) - len(shown_pit)})
    else:
        print("New since %s: %d insider disclosure(s) shown of %d new, %d large deal(s)." % (
            state.get("last_check") or "the last %d days (first run)" % FIRST_RUN_DAYS, len(shown_pit),
            len(new_pit), len(shown_deals)))
        for r in shown_pit:
            print("NEW " + describe_pit(r, watch))
        for r in shown_deals:
            print("NEW " + describe_deal(r, watch))
        if len(new_pit) > len(shown_pit):
            print("(%d other new disclosure(s) without a promoter market trade, pledge or watch-list name: "
                  "--all shows them)" % (len(new_pit) - len(shown_pit)))
        if not shown_pit and not shown_deals:
            print("Nothing new worth showing.")
        for e in errors:
            print("partial: " + e)
        print(NOT_ADVICE)
    if not a.dry_run:
        ids = list(state.get("seen") or [])
        known = set(ids)
        for r in pit + deals:
            if r["id"] not in known:
                ids.append(r["id"])
                known.add(r["id"])
        state["seen"] = ids[-MAX_SEEN:]
        state["last_check"] = now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"
        M.write_json(a.state, state)
    return 0 if (shown_pit or shown_deals) else 1


def cmd_watch(a):
    today = M.parse_now(a.now).date()
    cfg = M.load_config(a.config)
    watch = watch_from(cfg, a.portfolio, a.symbol, a.name)
    if not watch["symbols"] and not watch["names"] and not watch["clients"]:
        raise InsiderError("nothing to watch: add \"insider_watch\": {\"symbols\": [...], \"names\": [...]} "
                           "(and \"watch_clients\") to trader/config.json, or pass --symbol / --name")
    start = today - datetime.timedelta(days=a.days)
    sources = ("pit", "deals") if a.source == "all" else (a.source,)
    pit, deals, as_on, errors = _collect(a, today, start, sources)
    pit_hits = [r for r in select_pit(pit, start=start, end=today) if watch_hit(r, watch)]
    deal_hits = [r for r in deals if deal_watch_hit(r, watch)]
    if a.json:
        _print_json({"watch": {k: sorted(v) if isinstance(v, set) else v for k, v in watch.items()},
                     "pit": pit_hits, "deals": deal_hits, "errors": errors})
        return 0 if (pit_hits or deal_hits) else 1
    print("Watching %d symbol(s), %d name(s), %d client(s). Insider disclosures since %s, large deals%s." % (
        len(watch["symbols"]), len(watch["names"]), len(watch["clients"]), start.isoformat(),
        " for " + as_on.isoformat() if as_on else ""))
    for r in pit_hits:
        print("  " + describe_pit(r, watch))
    for r in deal_hits:
        print("  " + describe_deal(r, watch))
    if not pit_hits and not deal_hits:
        print("Nothing on your watch list.")
    for e in errors:
        print("partial: " + e)
    print(NOT_ADVICE)
    return 0 if (pit_hits or deal_hits) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Insider (SEBI PIT) disclosures and bulk/block deals on NSE "
                                             "(trader-insider). Public disclosures, not inside information.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--now", help="pretend it is this IST time, 'YYYY-MM-DD HH:MM'")
        p.add_argument("--config", default=M.CONFIG_FILE)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("insider", help="SEBI PIT disclosures for a date range")
    common(p)
    p.add_argument("--from", dest="frm", help="YYYY-MM-DD or DD-MM-YYYY (default: --days before --to)")
    p.add_argument("--to", help="default today")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--symbol", action="append", help="repeat for more")
    p.add_argument("--category", action="append", help="promoter, director, kmp, employee, other")
    p.add_argument("--kind", choices=["market", "pledge", "highlight"])
    p.add_argument("--min-cr", type=float, help="only rows worth at least this many Rs crore")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offline", metavar="FILE", help="a saved corporates-pit JSON answer")
    p = sub.add_parser("deals", help="today's bulk, block and short deals")
    common(p)
    p.add_argument("--symbol", action="append")
    p.add_argument("--client", help="part of a client name, e.g. \"mutual fund\"")
    p.add_argument("--kind", choices=["bulk", "block", "short"])
    p.add_argument("--min-cr", type=float)
    p.add_argument("--watched-only", action="store_true", help="only watched symbols and clients")
    p.add_argument("--offline", metavar="FILE", help="a saved snapshot-capital-market-largedeal JSON answer")
    for name, hlp in (("new", "only what is new since the last check"),
                      ("watch", "your symbols, people and clients (config insider_watch)")):
        p = sub.add_parser(name, help=hlp)
        common(p)
        p.add_argument("--source", choices=["all", "pit", "deals"], default="all")
        p.add_argument("--symbol", action="append")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--offline-pit", metavar="FILE")
        p.add_argument("--offline-deals", metavar="FILE")
        if name == "new":
            p.add_argument("--category", action="append")
            p.add_argument("--client")
            p.add_argument("--min-cr", type=float)
            p.add_argument("--all", action="store_true", help="every new disclosure, not just the highlights")
            p.add_argument("--state", default=STATE_FILE)
            p.add_argument("--dry-run", action="store_true", help="do not update the state file")
        else:
            p.add_argument("--name", action="append", help="a person or client name to watch")
            p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"insider": cmd_insider, "deals": cmd_deals, "new": cmd_new, "watch": cmd_watch}[a.cmd](a)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
