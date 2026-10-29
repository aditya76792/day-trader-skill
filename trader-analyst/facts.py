#!/usr/bin/env python3
"""Earnings reader for trader-analyst (India edition). Standard library only.

Reads the quarterly results a listed company files with NSE (SEBI LODR
regulation 33), lays out the last quarters in Rs crore and says what changed:
revenue from operations, operating margin (EBITDA, approximate), net profit,
net margin and EPS, each against the same quarter a year earlier (YoY) and
the quarter before (QoQ).

    python3 facts.py TCS
    python3 facts.py TCS INFY --quarters 8
    python3 facts.py INFY --standalone            # standalone instead of consolidated
    python3 facts.py TCS --json
    python3 facts.py SAMPLECO --from-file my-quarters.csv   # your own numbers (Rs crore)

Sources (unofficial; NSE blocks many scripts and data-centre IPs):
  1. SEBI's "Integrated Filing - Financials" on NSE (all results since early 2025):
     https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=SYMBOL&type=Integrated%20Filing-%20Financials&page=1&size=20
     -> {"data": [...], "size", "page", "totalCount"}; each row: qe_Date (quarter
     end), consolidated ("Consolidated" / "Non-Consolidated"), audited,
     broadcast_Date, revised_Date, type_Sub ("Original" / "Revised") and the
     URL of the filing's XBRL instance.
  2. The older results listing, for quarters before 2025 (its newest rows stop
     around Dec 2024, and it is not sorted):
     https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=SYMBOL&period=Quarterly
  3. The XBRL instance of each result (SEBI's Ind AS taxonomy, prefix
     in-capmkt). Facts are read by their LOCAL element name, so a changed
     namespace does not matter, and contexts are picked by their DATES, never
     by their ids: the three-month, undimensioned duration ending on the
     quarter end. An integrated filing has only the quarter and the year to
     date (no year-ago or previous-quarter column), so YoY and QoQ come from
     the earlier quarters' own filings (--filings, default quarters + 1).
     Old-format filings that do carry comparison columns still use them.
  4. --from-file FILE: a CSV or JSON of quarters you typed from the results
     PDF: quarter_end, revenue, net_profit (Rs crore), eps (Rs); optional
     other_income, finance_costs, depreciation, pbt, tax (Rs crore).

Units: XBRL money facts are FULL RUPEES (unitRef INR; decimals "-7" means
rounded to the crore). The "LevelOfRounding" text fact ("Crores") describes the
rounding, not a unit: values are never multiplied or divided by it. They are
shown in Rs crore (divided by 1e7). EPS (unitRef INRPerShare) is in rupees.
--scale is a manual override for a filing you know is tagged in other units.

Net profit is the profit attributable to owners of the parent when the filing
has it (consolidated results; EPS is based on it), else profit for the period.
EBITDA here is approximate: profit before tax + finance costs + depreciation
- other income (exceptional items stay in). Banks and NBFCs report interest
income as revenue; their margins mean something else.

This is unofficial parsing of exchange filings. Tags can be mis-mapped and
companies tag differently. Verify against the results PDF before relying on
a number. Numbers only: this tool never says whether a stock is a buy.

--offline DIR reads DIR/integrated-<SYMBOL>.json and DIR/results-<SYMBOL>.json
(either may be missing) and each XBRL file by its file name from DIR.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

INTEGRATED_PATH = ("/api/integrated-filing-results?index=equities&symbol=%s"
                   "&type=Integrated%%20Filing-%%20Financials&page=1&size=%d")
INTEGRATED_REFERER = "/companies-listing/corporate-integrated-filing"
LISTING_PATH = "/api/corporates-financial-results?index=equities&symbol=%s&period=Quarterly"
LISTING_REFERER = "/companies-listing/corporate-filings-financial-results"
CRORE = 1e7
DUE_DAYS = 45          # SEBI LODR reg 33: quarterly results within 45 days of the quarter end
DUE_DAYS_Q4 = 60       # the March quarter (with the audited annual results): 60 days
DISCLAIMER = ("Unofficial parsing of exchange filings: XBRL tags can be mis-mapped and a revised filing can "
              "change a number. Verify against the results PDF on nseindia.com before relying on a number.")

TAGS = {
    "revenue": ["RevenueFromOperations", "RevenueFromOperationsNet", "TotalRevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income", "TotalIncome"],
    "expenses": ["Expenses", "ExpensesTotal", "TotalExpenses"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense", "DepreciationAndAmortisationExpense"],
    "exceptional": ["ExceptionalItemsBeforeTax", "ExceptionalItems"],
    "pbt": ["ProfitBeforeTax", "ProfitLossBeforeTax", "ProfitLossBeforeTaxFromContinuingOperations"],
    "tax": ["TaxExpense", "TotalTaxExpense"],
    "net_profit": ["ProfitLossForPeriod", "ProfitLossForThePeriod", "ProfitLossForPeriodFromContinuingOperations"],
    "net_profit_owners": ["ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAttributableToOwnersOfParent"],
    "eps_basic": ["BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShare",
                  "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsPerShare",
                    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"],
    "paid_up": ["PaidUpValueOfEquityShareCapital"],
    "face_value": ["FaceValueOfEquityShareCapital"],
}
NOT_MONEY = ("eps_basic", "eps_diluted", "face_value")
TEXT_TAGS = {
    "name": ["NameOfTheCompany", "NameOfCompany"],
    "symbol": ["Symbol", "NSESymbol"],
    "basis": ["NatureOfReportStandaloneConsolidated"],
    "audited": ["WhetherResultsAreAuditedOrUnaudited"],
    "rounding": ["LevelOfRounding", "LevelOfRoundingUsedInFinancialStatements"],
    "currency": ["DescriptionOfPresentationCurrency"],
    "period_end": ["DateOfEndOfReportingPeriod"],
    "reporting_quarter": ["ReportingQuarter"],
    "period_type": ["TypeOfReportingPeriod"],
}
DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y", "%d %b %Y", "%d-%B-%Y", "%d/%m/%Y")


class FactsError(M.MarketError):
    pass


def _new_net():
    return M.Net()


NET_FACTORY = _new_net


# ---------------------------------------------------------------- small helpers

def parse_datetime(text):
    """'08-Oct-2026 22:07:33', '30-SEP-2026', '2026-09-30' -> datetime or None."""
    t = str(text or "").strip()
    if not t or t in ("-", "null", "None"):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def parse_date(text):
    """Dates as NSE writes them ('30-Sep-2026', '17-Oct-2026 19:42', '2026-09-30') -> date or None."""
    t = str(text or "").strip()
    if not t or t == "-":
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2}-[A-Za-z]{3}-\d{4})", t)
    if m:
        return parse_date(m.group(1))
    return None


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def cr(v):
    return None if v is None else round(v / CRORE, 2)


def money(v):
    """Rupees -> 'Rs 73,188.00 cr' (Indian digit grouping)."""
    return "n/a" if v is None else M.inr(v / CRORE) + " cr"


def quarter_label(end):
    """Indian fiscal year (April-March): 2026-09-30 -> 'Q2 FY27'."""
    fy = end.year + 1 if end.month >= 4 else end.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(end.month)
    return ("Q%d FY%02d" % (q, fy % 100)) if q else "FY%02d" % (fy % 100)


def results_due(end):
    """Last day for the results of the quarter ending `end` (SEBI LODR reg 33)."""
    return end + datetime.timedelta(days=DUE_DAYS_Q4 if end.month == 3 else DUE_DAYS)


def next_quarter_end(end):
    m, y = end.month + 3, end.year
    if m > 12:
        m, y = m - 12, y + 1
    first_after = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    return first_after - datetime.timedelta(days=1)


# ---------------------------------------------------------------- the listing

def parse_listing(doc, symbol, source="integrated"):
    """An NSE results listing -> filings, tolerant to missing fields. Reads both the
    integrated-filing answer ({"data": [...]} with qe_Date, broadcast_Date, type_Sub ...)
    and the older corporates-financial-results list (toDate, filingDate ...)."""
    rows = doc
    if isinstance(doc, dict):
        rows = doc.get("data") or doc.get("rows") or doc.get("results") or []
    if not isinstance(rows, list):
        raise FactsError("NSE's results listing is not the shape this tool knows")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or symbol).upper()
        if sym != symbol.upper():
            continue
        typ = str(r.get("type") or "")
        if typ and "financ" not in typ.lower() and "integrated" in typ.lower():
            continue                                  # governance / other integrated filings
        to = parse_date(r.get("qe_Date") or r.get("toDate") or r.get("to_date") or r.get("periodEnded")
                        or r.get("period_end"))
        frm = parse_date(r.get("fromDate") or r.get("from_date"))
        if not to:
            continue
        if frm and (to - frm).days > 100:
            continue                                  # half-year / annual rows: not a quarter
        flag = str(r.get("consolidated") or r.get("consolidatedFlag") or "")
        if re.search(r"non|standalone", flag, re.I):
            basis = "standalone"
        elif re.search(r"consol", flag, re.I):
            basis = "consolidated"
        else:
            basis = "unknown"
        xbrl = str(r.get("xbrl") or r.get("xbrlFile") or r.get("xbrlUrl") or "").strip()
        if not xbrl.lower().endswith(".xml"):
            xbrl = None
        revised = str(r.get("type_Sub") or "").lower().startswith("revis")
        when = None
        for k in ("revised_Date", "broadcast_Date", "creation_Date", "filingDate", "broadCastDate", "broadcastDate"):
            when = parse_datetime(r.get(k))
            if when:
                filed_text = str(r.get(k)).strip()
                break
        else:
            filed_text = None
        out.append({"symbol": sym, "company": r.get("cmName") or r.get("companyName") or r.get("company"),
                    "from": frm.isoformat() if frm else None, "to": to.isoformat(), "basis": basis,
                    "audited": r.get("audited"), "period": r.get("relatingTo") or r.get("period"),
                    "filed": filed_text, "filed_key": (when.isoformat() if when else to.isoformat()),
                    "revised": revised, "bank": str(r.get("bank") or "").upper() == "Y",
                    "listing": source, "xbrl": xbrl})
    out.sort(key=lambda e: (e["to"], e["filed_key"]), reverse=True)      # the old list is not sorted
    return out


def choose_filings(entries, prefer="consolidated", quarters=6):
    """One filing per quarter and ONE basis for the whole table: the preferred basis
    (consolidated unless --standalone) when the company files it, else the other.
    A revision (the later filing) wins. Bases are never mixed in one table.
    -> (filings newest first, basis, note or None)."""
    bases = sorted({e["basis"] for e in entries})
    other = "standalone" if prefer == "consolidated" else "consolidated"
    basis = prefer if prefer in bases else (other if other in bases else bases[0])
    note = None
    if basis != prefer:
        note = "the company has no %s results in the listing, so these are %s" % (prefer, basis)
    by_q = {}
    for e in entries:
        if e["basis"] == basis:
            by_q.setdefault(e["to"], []).append(e)
    chosen = []
    for to in sorted(by_q, reverse=True)[:quarters]:
        es = sorted(by_q[to], key=lambda e: (e["filed_key"], e["revised"]), reverse=True)  # a revision wins
        es.sort(key=lambda e: not e["xbrl"])
        chosen.append(dict(es[0], bases=bases))
    return chosen, basis, note


# ---------------------------------------------------------------- XBRL

def parse_xbrl(data):
    """XBRL instance -> {"contexts": {id: {start, end, instant, dims}}, "units": {id: measure},
    "facts": {local name: [(contextRef, text, unitRef, decimals)]}}."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FactsError("the XBRL file is not valid XML: %s" % e)
    contexts, units, facts = {}, {}, {}
    for el in root.iter():
        name = _local(el.tag)
        if name == "context":
            c = {"start": None, "end": None, "instant": None, "dims": False}
            for sub in el.iter():
                n = _local(sub.tag)
                if n == "startDate":
                    c["start"] = (sub.text or "").strip()[:10]
                elif n == "endDate":
                    c["end"] = (sub.text or "").strip()[:10]
                elif n == "instant":
                    c["instant"] = (sub.text or "").strip()[:10]
                elif n in ("segment", "scenario") and len(list(sub)):
                    c["dims"] = True
            contexts[el.get("id")] = c
        elif name == "unit":
            units[el.get("id")] = " ".join((m.text or "").strip() for m in el.iter() if _local(m.tag) == "measure")
        elif el.get("contextRef") is not None:
            nil = any(_local(k) == "nil" and v == "true" for k, v in el.attrib.items())
            if nil:
                continue
            facts.setdefault(name, []).append((el.get("contextRef"), (el.text or "").strip(), el.get("unitRef"),
                                               el.get("decimals")))
    if not contexts:
        raise FactsError("the XBRL file has no contexts")
    return {"contexts": contexts, "units": units, "facts": facts}


def _duration_groups(x):
    """{(start, end): [context ids]} for undimensioned contexts lasting about three months.
    Picked by dates, never by id: a Q4 filing's OneD can be the quarter or the year."""
    groups = {}
    for cid, c in x["contexts"].items():
        if c["dims"] or not c["start"] or not c["end"]:
            continue
        try:
            days = (datetime.date.fromisoformat(c["end"]) - datetime.date.fromisoformat(c["start"])).days
        except ValueError:
            continue
        if 80 <= days <= 100:
            groups.setdefault((c["start"], c["end"]), []).append(cid)
    return groups


def _value(x, key, ctx_ids, scale=None):
    for tag in TAGS[key]:
        for cref, text, unit, _dec in x["facts"].get(tag, []):
            if cref not in ctx_ids:
                continue
            try:
                v = float(text.replace(",", ""))
            except ValueError:
                continue
            measure = x["units"].get(unit)
            if key not in NOT_MONEY and scale:
                v *= scale                            # manual --scale only: values are full rupees
            return v, tag, measure
    return None, None, None


def text_fact(x, key):
    for tag in TEXT_TAGS[key]:
        for _c, text, _u, _d in x["facts"].get(tag, []):
            if text:
                return text
    return None


def quarter_values(x, ctx_ids, scale=None):
    row, used = {}, {}
    for key in TAGS:
        v, tag, measure = _value(x, key, set(ctx_ids), scale)
        row[key] = v
        if tag:
            used[key] = tag
        if key == "revenue" and measure:
            row["unit"] = measure
    return row, used


def filing_quarters(x, to_date=None, scale=None):
    """The quarter columns of a results filing: current (the three-month period ending
    to_date), and, only in old-format filings that carry them, the quarter before and the
    same quarter a year earlier. Integrated filings have just the quarter (plus the year to
    date, which is ignored), so preceding / year_ago are None there."""
    groups = _duration_groups(x)
    if not groups:
        raise FactsError("no three-month periods in the XBRL file")
    ends = sorted({e for _, e in groups})
    cur_end = to_date if to_date in ends else ends[-1]
    if to_date and to_date not in ends:
        raise FactsError("the XBRL file has no quarter ending %s (it has %s)" % (to_date, ", ".join(ends)))

    def pick(end):
        keys = [k for k in groups if k[1] == end]
        return (keys[0], groups[keys[0]]) if keys else (None, None)

    def near(target, slack=10):
        best = None
        for e in ends:
            gap = abs((datetime.date.fromisoformat(e) - target).days)
            if gap <= slack and e != cur_end and (best is None or gap < best[0]):
                best = (gap, e)
        return best[1] if best else None

    out = {}
    key, ids = pick(cur_end)
    ce = datetime.date.fromisoformat(cur_end)
    cs = datetime.date.fromisoformat(key[0])
    for label, end in (("current", cur_end),
                       ("preceding", near(cs - datetime.timedelta(days=1), slack=5)),
                       ("year_ago", near(ce - datetime.timedelta(days=365)))):
        if not end:
            out[label] = None
            continue
        k, ids = pick(end)
        vals, used = quarter_values(x, ids, scale)
        if all(vals[m] is None for m in ("revenue", "net_profit", "pbt")):
            out[label] = None
            continue
        out[label] = dict(vals, start=k[0], end=k[1], tags=used)
    return out


# ---------------------------------------------------------------- derived numbers

def derive(v):
    """EBITDA (approx.), margins, EPS (diluted, else basic), tax rate."""
    if v is None:
        return None
    v = dict(v)
    v["net_profit_total"] = v.get("net_profit")
    if v.get("net_profit_owners") is not None:
        v["net_profit"] = v["net_profit_owners"]      # what EPS is based on (consolidated)
    rev, pbt = v.get("revenue"), v.get("pbt")
    ebitda = None
    if pbt is not None:
        ebitda = pbt + (v.get("finance_costs") or 0) + (v.get("depreciation") or 0) - (v.get("other_income") or 0)
    eps = v.get("eps_diluted") if v.get("eps_diluted") is not None else v.get("eps_basic")
    tax_rate = margin(v.get("tax"), pbt) if pbt and pbt > 0 else None
    return dict(v, ebitda=ebitda, operating_margin=margin(ebitda, rev), net_margin=margin(v.get("net_profit"), rev),
                eps=round(eps, 2) if eps is not None else None, tax_rate=tax_rate)


def _near_row(rows, end, days):
    target = datetime.date.fromisoformat(end) - datetime.timedelta(days=days)
    best = None
    for e in rows:
        gap = abs((datetime.date.fromisoformat(e) - target).days)
        if gap <= 10 and (best is None or gap < best[0]):
            best = (gap, e)
    return rows[best[1]] if best else None


def growth(row, rows):
    """YoY and QoQ: from the filing's own comparison columns when there, else from the table."""
    ya = row.get("_year_ago") or _near_row(rows, row["end"], 365)
    pq = row.get("_preceding") or _near_row(rows, row["end"], 91)
    out = {}
    for k in ("revenue", "net_profit", "eps"):
        out["%s_yoy" % k] = pct(row.get(k), (ya or {}).get(k))
        out["%s_qoq" % k] = pct(row.get(k), (pq or {}).get(k))
    out["operating_margin_prev"] = (pq or {}).get("operating_margin")
    out["operating_margin_year_ago"] = (ya or {}).get("operating_margin")
    out["net_margin_prev"] = (pq or {}).get("net_margin")
    out["growth_from"] = "same filing" if row.get("_year_ago") else ("table" if ya else None)
    return out


def assemble(filings, quarters=6):
    """[{filing, quarters}] (newest first) -> rows, newest first. A quarter's own filing wins;
    comparison columns of later filings fill quarters with no filing of their own ("c")."""
    rows = {}
    for f in filings:
        cur = f["quarters"]["current"]
        if cur and cur["end"] not in rows:
            r = derive(cur)
            r.update(source="filing", filing=f["filing"],
                     _year_ago=derive(f["quarters"].get("year_ago")),
                     _preceding=derive(f["quarters"].get("preceding")))
            rows[cur["end"]] = r
    for f in filings:
        for label in ("preceding", "year_ago"):
            q = f["quarters"].get(label)
            if q and q["end"] not in rows:
                r = derive(q)
                r.update(source="comparison", filing=f["filing"], _year_ago=None, _preceding=None)
                rows[q["end"]] = r
    out = []
    for end in sorted(rows, reverse=True)[:quarters]:
        r = rows[end]
        r.update(growth(r, rows))
        out.append(r)
    return out, rows


def ttm(rows_by_end):
    ends = sorted(rows_by_end)[-4:]
    if len(ends) < 4:
        return {}
    if (datetime.date.fromisoformat(ends[-1]) - datetime.date.fromisoformat(ends[0])).days > 300:
        return {}
    out = {}
    for k in ("revenue", "net_profit", "eps"):
        vals = [rows_by_end[e].get(k) for e in ends]
        if all(v is not None for v in vals):
            out[k] = round(sum(vals), 2) if k == "eps" else sum(vals)
    out["end"] = ends[-1]
    return out


# ---------------------------------------------------------------- what changed

def changes(result):
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    out = []
    f = cur.get("filing") or {}
    bits = [result.get("basis") or f.get("basis")]
    if f.get("audited"):
        bits.append(str(f["audited"]).lower())
    if f.get("filed"):
        bits.append("filed %s" % f["filed"])
    head = "Quarter ending %s (%s%s)" % (cur["end"], quarter_label(datetime.date.fromisoformat(cur["end"])),
                                         ", " + ", ".join(b for b in bits if b) if any(bits) else "")
    line = "%s: revenue %s" % (head, money(cur.get("revenue")))
    g = []
    if cur.get("revenue_yoy") is not None:
        g.append("%+.1f%% vs a year earlier" % cur["revenue_yoy"])
    if cur.get("revenue_qoq") is not None:
        g.append("%+.1f%% vs the quarter before" % cur["revenue_qoq"])
    if g:
        line += " (%s)" % ", ".join(g)
    line += ", net profit %s" % money(cur.get("net_profit"))
    if cur.get("net_profit_yoy") is not None:
        line += " (%+.1f%% YoY)" % cur["net_profit_yoy"]
    if cur.get("eps") is not None:
        line += ", EPS Rs %.2f" % cur["eps"]
        if cur.get("eps_yoy") is not None:
            line += " (%+.1f%% YoY)" % cur["eps_yoy"]
    out.append(line + ".")
    if cur.get("operating_margin") is not None:
        s = "Operating margin (EBITDA, approx.) %.1f%%" % cur["operating_margin"]
        d = []
        if cur.get("operating_margin_prev") is not None:
            d.append("%+.1f points vs the quarter before" % (cur["operating_margin"] - cur["operating_margin_prev"]))
        if cur.get("operating_margin_year_ago") is not None:
            d.append("%+.1f points vs a year earlier" % (cur["operating_margin"] - cur["operating_margin_year_ago"]))
        out.append(s + (" (%s)" % ", ".join(d) if d else "") + ".")
    if cur.get("net_margin") is not None:
        s = "Net margin %.1f%%" % cur["net_margin"]
        if cur.get("net_margin_prev") is not None:
            s += " (%+.1f points vs the quarter before)" % (cur["net_margin"] - cur["net_margin_prev"])
        out.append(s + ".")
    oi, pbt = cur.get("other_income"), cur.get("pbt")
    if oi and pbt and pbt > 0 and oi / pbt >= 0.2:
        out.append("Other income is %s, %.0f%% of profit before tax: part of the profit is treasury or one-off "
                   "income, not the business." % (money(oi), 100.0 * oi / pbt))
    if cur.get("exceptional"):
        out.append("Exceptional items of %s are inside profit before tax: read the notes to the results before "
                   "reading the profit as growth." % money(cur["exceptional"]))
    if cur.get("tax_rate") is not None and not 15 <= cur["tax_rate"] <= 35:
        out.append("Effective tax rate %.1f%%, outside the usual 25%% or so: a tax credit or charge moved net "
                   "profit." % cur["tax_rate"])
    if cur.get("net_profit") is not None and cur.get("ebitda") is not None and cur["net_profit"] > cur["ebitda"]:
        out.append("Net profit is above EBITDA: gains outside operations are inside it. Check the PDF.")
    if cur.get("revenue_qoq") is not None:
        out.append("Many Indian businesses are seasonal (festive quarter, monsoon, March year-end): read YoY first.")
    return out


def stale_note(result, today, entries=None):
    """Plain words when the newest quarter is older than it should be, or a newer filing was not read."""
    rows = result["rows"]
    notes = []
    if not rows:
        return notes
    latest = datetime.date.fromisoformat(rows[0]["end"])
    nxt = next_quarter_end(latest)
    due = results_due(nxt)
    if today > due:
        notes.append("The newest results here are for the quarter ending %s. Results for the quarter ending %s "
                     "were due by %s (SEBI LODR reg 33: 45 days, 60 for the March quarter). Check NSE's corporate "
                     "announcements: the listing may lag, or the company filed late." % (latest, nxt, due))
    elif today >= nxt:
        notes.append("Next results (quarter ending %s) are due by %s. A position held into the board meeting "
                     "date is exposed to them: check /trader-news for the date." % (nxt, due))
    if (today - latest).days > 0 and rows[0].get("source") == "file":
        notes.append("These numbers came from your file, not from an exchange filing.")
    for e in entries or []:
        if e["to"] > rows[0]["end"]:
            notes.append("A newer filing (quarter ending %s, %s, filed %s) could not be read%s: open its PDF on "
                         "nseindia.com." % (e["to"], e["basis"], e.get("filed") or "?",
                                            " (" + e["error"] + ")" if e.get("error") else ""))
            break
    return notes


# ---------------------------------------------------------------- sources

class Source(object):
    def __init__(self, offline=None, net=None):
        self.offline, self._net = offline, net

    @property
    def net(self):
        if self._net is None:
            self._net = NET_FACTORY()
        return self._net

    def _get(self, kind, symbol, size=20):
        if self.offline:
            name = "%s-%s.json" % ("integrated" if kind == "integrated" else "results", symbol)
            return M.read_json(os.path.join(self.offline, name), "saved %s listing" % kind)
        if kind == "integrated":
            return self.net.nse_json(INTEGRATED_PATH % (symbol, size), referer=INTEGRATED_REFERER)
        return self.net.nse_json(LISTING_PATH % symbol, referer=LISTING_REFERER)

    def listings(self, symbol, need, prefer="consolidated"):
        """Integrated filings first; the old results listing when they do not cover `need`
        quarters of the wanted basis (results before 2025) or cannot be read.
        -> (entries newest first, notes). Raises only when both sources fail."""
        entries, notes, errors = [], [], []
        try:
            entries = parse_listing(self._get("integrated", symbol), symbol, "integrated")
        except M.MarketError as e:
            errors.append("integrated filings: %s" % e)
        have = {e["to"] for e in entries if e["basis"] == prefer} or {e["to"] for e in entries}
        if len(have) < need:
            try:
                old = parse_listing(self._get("old", symbol), symbol, "old")
                urls = {e["xbrl"] for e in entries if e["xbrl"]}
                entries += [e for e in old if not (e["xbrl"] and e["xbrl"] in urls)]
            except M.MarketError as e:
                errors.append("older results listing: %s" % e)
        if not entries:
            raise FactsError("; ".join(errors) if errors else
                             "NSE's listings have no quarterly results for %s (check the symbol)" % symbol)
        if errors:
            notes.append("partial: " + "; ".join(errors))
        entries.sort(key=lambda e: (e["to"], e["filed_key"]), reverse=True)
        return entries, notes

    def xbrl(self, url):
        if self.offline:
            path = os.path.join(self.offline, url.rstrip("/").rsplit("/", 1)[-1])
            if not os.path.isfile(path):
                raise FactsError("offline: no XBRL file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        return self.net.nse_archive(url, binary=True)


def from_file(path):
    """Your own quarters (Rs crore, EPS in Rs) -> rows like the XBRL ones."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as e:
        raise FactsError("cannot read %s: %s" % (path, e))
    if path.lower().endswith(".json"):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise FactsError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("quarters") if isinstance(doc, dict) else doc
    else:
        items = list(csv.DictReader(io.StringIO(text)))
    if not isinstance(items, list) or not items:
        raise FactsError("%s has no quarters (columns: quarter_end, revenue, net_profit, eps)" % path)
    rows = {}
    for it in items:
        it = {str(k).strip().lower(): v for k, v in it.items() if k}
        end = parse_date(it.get("quarter_end"))
        if not end:
            raise FactsError("%s: quarter_end %r is not a date" % (path, it.get("quarter_end")))

        def num(k, scale=CRORE):
            v = it.get(k)
            if v in (None, "", "-"):
                return None
            try:
                return float(str(v).replace(",", "")) * scale
            except ValueError:
                raise FactsError("%s: %s %r is not a number" % (path, k, v))
        v = {k: None for k in TAGS}
        for k in ("revenue", "net_profit", "other_income", "finance_costs", "depreciation", "pbt", "tax",
                  "exceptional"):
            v[k] = num(k)
        v["eps_diluted"] = num("eps", 1.0)
        v.update(start=None, end=end.isoformat(), tags={})
        rows[end.isoformat()] = v
    return rows


# ---------------------------------------------------------------- one company

def analyse_symbol(symbol, src, quarters=5, prefer="consolidated", scale=None, filings_n=None):
    """Fetch the last `filings_n` quarterly filings (default quarters + 1) of one basis.
    Integrated filings have no comparison columns, so growth needs the older filings."""
    filings_n = filings_n or quarters + 1
    entries, list_notes = src.listings(symbol, filings_n, prefer)
    chosen, basis, basis_note = choose_filings(entries, prefer, filings_n)
    filings, failed = [], []
    meta = {"name": None, "rounding": None, "unit": None}
    for e in chosen:
        if not e["xbrl"]:
            failed.append(dict(e, error="no XBRL file in the listing"))
            continue
        try:
            x = parse_xbrl(src.xbrl(e["xbrl"]))
            fq = filing_quarters(x, e["to"], scale)
        except M.MarketError as err:
            failed.append(dict(e, error=str(err)))
            continue
        meta["name"] = meta["name"] or text_fact(x, "name")
        meta["rounding"] = meta["rounding"] or text_fact(x, "rounding")
        cur = fq.get("current") or {}
        meta["unit"] = meta["unit"] or cur.get("unit")
        end_fact = text_fact(x, "period_end")
        if end_fact and parse_date(end_fact) and parse_date(end_fact).isoformat() != e["to"]:
            failed.append(dict(e, error="the XBRL says the period ends %s, the listing %s" % (end_fact, e["to"])))
            continue
        filings.append({"filing": e, "quarters": fq})
    if not filings:
        raise FactsError("none of %d XBRL files could be read (%s)" % (len(chosen), failed[0]["error"]
                                                                       if failed else "none listed"))
    rows, by_end = assemble(filings, quarters)
    return {"symbol": symbol, "name": chosen[0].get("company") or meta["name"] or symbol, "basis": basis,
            "bases_filed": chosen[0].get("bases", [basis]), "rounding": meta["rounding"], "unit": meta["unit"],
            "rows": rows, "ttm": ttm(by_end), "failed": failed, "read": len(filings), "tried": len(chosen),
            "fallback": basis_note, "listing_notes": list_notes, "bank": any(e.get("bank") for e in chosen),
            "listings_used": sorted({f["filing"]["listing"] for f in filings}),
            "source": "NSE financial results (XBRL)"}


def analyse_file(symbol, path, quarters=5):
    by_end = {e: derive(v) for e, v in from_file(path).items()}
    for e, r in by_end.items():
        r.update(source="file", filing=None, _year_ago=None, _preceding=None)
    rows = []
    for end in sorted(by_end, reverse=True)[:quarters]:
        r = by_end[end]
        r.update(growth(r, by_end))
        rows.append(r)
    return {"symbol": symbol, "name": symbol, "basis": None, "bases_filed": [], "rounding": None, "unit": None,
            "rows": rows,
            "ttm": ttm(by_end), "failed": [], "read": 0, "tried": 0, "fallback": None, "listing_notes": [],
            "bank": False, "listings_used": [],
            "source": "your file %s" % os.path.basename(path)}


# ---------------------------------------------------------------- output

def _c(v):
    return "-" if v is None else M.inr(v / CRORE).replace("Rs ", "")


def _p(v):
    return "-" if v is None else ("0.0%" if round(v, 1) == 0 else "%+.1f%%" % v)


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(result, today):
    basis = result["basis"]
    head = "%s (%s), NSE. Rs crore except EPS (Rs).%s Source: %s." % (
        result["name"], result["symbol"],
        (" %s%s." % (basis.capitalize(), (" (also filed: %s)" % ", ".join(b for b in result["bases_filed"]
                                                                          if b != basis))
                     if len(result["bases_filed"]) > 1 else "")) if basis else "",
        result["source"])
    out = [head, ""]
    out.append("%-11s %-8s %10s %8s %8s %7s %7s %10s %8s %8s %8s" % (
        "quarter end", "quarter", "revenue", "rev yoy", "rev qoq", "ebitda", "net", "net profit", "np yoy",
        "EPS", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-8s %10s %8s %8s %7s %7s %10s %8s %8s %8s%s" % (
            r["end"], quarter_label(datetime.date.fromisoformat(r["end"])), _c(r.get("revenue")),
            _p(r.get("revenue_yoy")), _p(r.get("revenue_qoq")), _pm(r.get("operating_margin")),
            _pm(r.get("net_margin")), _c(r.get("net_profit")), _p(r.get("net_profit_yoy")),
            "-" if r.get("eps") is None else "%.2f" % r["eps"], _p(r.get("eps_yoy")),
            "  c" if r.get("source") == "comparison" else ""))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters (to %s): revenue %s, net profit %s, EPS %s." % (
            t["end"], money(t.get("revenue")), money(t.get("net_profit")),
            "Rs %.2f" % t["eps"] if t.get("eps") is not None else "n/a"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    filing = (result["rows"][0].get("filing") or {}) if result["rows"] else {}
    if filing.get("xbrl"):
        out.append("")
        out.append("Latest filing: quarter ending %s, %s, filed %s  %s" % (
            filing["to"], filing["basis"], filing.get("filed") or "?", filing["xbrl"]))
    notes = []
    if any(r.get("source") == "comparison" for r in result["rows"]):
        notes.append("Rows marked c come from the comparison columns of a later filing (possibly restated).")
    if result.get("fallback"):
        notes.append(result["fallback"][:1].upper() + result["fallback"][1:] + ".")
    if result.get("rounding") or result.get("unit"):
        notes.append("XBRL money values are full rupees (unit %s), shown here in crore%s." % (
            result.get("unit") or "INR", "; the filing's level of rounding is %s" % result["rounding"]
            if result.get("rounding") else ""))
    if "old" in (result.get("listings_used") or []):
        notes.append("Filings before 2025 (used here for the table or for growth) come from NSE's older "
                     "results listing; later ones from SEBI's integrated filings.")
    if any(r.get("net_profit_owners") is not None for r in result["rows"]):
        notes.append("Net profit is the profit attributable to owners of the parent (what EPS is based on).")
    if result.get("bank"):
        notes.append("A bank: revenue is mostly interest income, so EBITDA and margins mean something else here.")
    notes += result.get("listing_notes") or []
    if result["tried"] and result["read"] < result["tried"]:
        notes.append("partial: %d of %d XBRL files read." % (result["read"], result["tried"]))
    notes += stale_note(result, today, result.get("failed"))
    for n in notes:
        out.append("NOTE: " + n)
    out.append("")
    out.append(DISCLAIMER)
    return "\n".join(out) + "\n"


def metrics_row(result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"symbol": result["symbol"], "name": result["name"], "exchange": "NSE", "basis": result["basis"],
            "period_end": cur.get("end"),
            "quarter": quarter_label(datetime.date.fromisoformat(cur["end"])) if cur else None,
            "revenue_cr": cr(cur.get("revenue")), "net_profit_cr": cr(cur.get("net_profit")),
            "eps": cur.get("eps"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "revenue_growth_qoq": cur.get("revenue_qoq"),
            "net_profit_growth_yoy": cur.get("net_profit_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "operating_margin": cur.get("operating_margin"), "net_margin": cur.get("net_margin"),
            "revenue_ttm_cr": cr(t.get("revenue")), "net_profit_ttm_cr": cr(t.get("net_profit")),
            "eps_ttm": t.get("eps"), "source": result["source"]}


def _public(result):
    """JSON-safe copy without the private comparison rows."""
    doc = dict(result)
    doc["rows"] = []
    for r in result["rows"]:
        row = {k: v for k, v in r.items() if not k.startswith("_")}
        for k in ("revenue", "other_income", "total_income", "expenses", "finance_costs", "depreciation",
                  "exceptional", "pbt", "tax", "net_profit", "net_profit_owners", "net_profit_total", "ebitda",
                  "paid_up"):
            if k in row:
                row[k + "_cr"] = cr(row.pop(k))
        doc["rows"].append(row)
    doc["ttm"] = {k: (cr(v) if k in ("revenue", "net_profit") else v) for k, v in result["ttm"].items()}
    doc["changes"] = changes(result)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. RELIANCE TCS")
    ap.add_argument("--quarters", type=int, default=5, help="rows to show (default 5)")
    ap.add_argument("--filings", type=int, help="quarterly filings to read (default quarters + 1; "
                                                 "quarters + 4 gives YoY on every row)")
    ap.add_argument("--standalone", action="store_true", help="standalone results instead of consolidated")
    ap.add_argument("--from-file", metavar="FILE", help="CSV/JSON of your own quarters (one symbol)")
    ap.add_argument("--scale", choices=["lakh", "crore"],
                    help="manual override only: multiply XBRL money values (normally full rupees, leave it off)")
    ap.add_argument("--metrics", default="trader/metrics.json", metavar="FILE",
                    help="merge one row per symbol into this JSON (default trader/metrics.json)")
    ap.add_argument("--no-write", action="store_true", help="do not write the metrics file")
    ap.add_argument("--config", default=M.CONFIG_FILE)
    ap.add_argument("--offline", metavar="DIR",
                    help="integrated-<SYMBOL>.json, results-<SYMBOL>.json and XBRL files in DIR")
    ap.add_argument("--now", help="today's date for the stale-data note, 'YYYY-MM-DD HH:MM' IST")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.symbols:
        ap.print_help()
        return 2
    try:
        today = M.parse_now(a.now).date()
        if a.from_file and len(a.symbols) != 1:
            raise FactsError("--from-file takes one symbol")
        for s in a.symbols:
            if not M.valid_symbol(s):
                raise FactsError("%r is not an NSE symbol" % s)
    except M.MarketError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    scale = {None: None, "lakh": 1e5, "crore": CRORE}[a.scale]
    prefer = "standalone" if a.standalone else "consolidated"
    src = Source(a.offline)
    results, failed = [], 0
    for s in a.symbols:
        sym = s.upper()
        try:
            if a.from_file:
                res = analyse_file(sym, a.from_file, a.quarters)
            else:
                res = analyse_symbol(sym, src, a.quarters, prefer, scale, a.filings)
        except (M.MarketError, ValueError) as e:
            print("error: %s: %s" % (sym, e), file=sys.stderr)
            failed += 1
            continue
        results.append(res)
        if not a.json:
            sys.stdout.write(render(res, today))
            sys.stdout.write("\n")
    if a.json and results:
        print(json.dumps([_public(r) for r in results], indent=2, default=str))
    if results and not a.no_write and a.metrics:
        try:
            old = M.read_json(a.metrics, "metrics file", required=False, default={}) or {}
            existing = {r.get("symbol"): r for r in old.get("companies", []) if isinstance(r, dict)}
            for r in results:
                existing[r["symbol"]] = metrics_row(r)
            M.write_json(a.metrics, {"created_by": "trader-analyst", "as_of": today.isoformat(),
                                     "note": DISCLAIMER,
                                     "companies": sorted(existing.values(), key=lambda r: r.get("symbol") or "")})
        except (M.MarketError, OSError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        if not a.json:
            print("wrote %s (%d companies)" % (a.metrics, len(existing)))
    if failed and results:
        print("partial: %d of %d symbols read." % (len(results), len(a.symbols)), file=sys.stderr)
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
