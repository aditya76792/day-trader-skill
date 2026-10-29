"""trader-news (India): NSE/BSE announcements, Google News headlines, catalyst tags, moves.
Offline only: small handwritten NSE/BSE/Google News samples plus synthetic Yahoo charts."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import _india as I
from _load import FIX, load

news = load("trader-news", "news")
M = news.M
NDIR = os.path.join(FIX, "trader-news")
DAY = datetime.date(2026, 10, 28)
UTC = datetime.timezone.utc
NOW = "2026-10-28 22:00"           # IST, the evening the results came out


def raw(name):
    with open(os.path.join(NDIR, name), "rb") as fh:
        return fh.read()


def jraw(name):
    return json.loads(raw(name).decode("utf-8"))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def chart(sym, prev_close, open_, closes, vol=10000):
    t = I.session_times(DAY)
    bars, px = [], open_
    for i, c in enumerate(closes):
        bars.append((t[i], px, max(px, c) + 1, min(px, c) - 1, c, vol))
        px = c
    return I.chart_doc(sym, bars, prev_close=prev_close)


class Feeds(unittest.TestCase):
    def test_nse_announcements(self):
        items = news.parse_nse(jraw("nse-RELIANCE.json"), "RELIANCE")
        self.assertEqual(len(items), 4)
        first = items[0]
        self.assertEqual(first["id"], "nse:104512345")
        self.assertEqual((first["source"], first["kind"], first["ticker"]), ("NSE filing", "filing", "RELIANCE"))
        self.assertTrue(first["title"].startswith("Outcome of Board Meeting: Reliance Industries Limited has informed"))
        self.assertTrue(first["link"].endswith("_BMOutcome.pdf"))
        # 21:33:50 IST is 16:03:50 UTC
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 28, 16, 3, 50, tzinfo=UTC))
        self.assertEqual(items[3]["link"], "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE")
        tcs = news.parse_nse(jraw("nse-TCS.json"), "TCS")          # the {"data": [...]} shape
        self.assertEqual(tcs[0]["ticker"], "TCS")
        self.assertTrue(tcs[0]["id"].startswith("nse:"))
        with self.assertRaises(news.NewsError):
            news.parse_nse({"error": "blocked"}, "X")

    def test_bse_announcements(self):
        items = news.parse_bse(jraw("bse-500325.json"), "RELIANCE")
        self.assertEqual(len(items), 2)                               # the junk row is skipped
        a, b = items
        self.assertEqual(a["id"], "bse:6f1c2d3e-0001-4a5b-9c8d-111111111111")
        self.assertEqual(a["title"], "ICRA has reaffirmed the long-term credit rating of the Company")
        self.assertEqual(a["link"], "https://www.bseindia.com/xml-data/corpfiling/AttachLive/6f1c2d3e-0001.pdf")
        self.assertEqual(a["time"], datetime.datetime(2026, 10, 28, 13, 50, 11, tzinfo=UTC))
        self.assertEqual(b["title"], "Reliance Industries Ltd - 500325 - Allotment Of Equity Shares On Preferential "
                                     "Basis")
        self.assertEqual(b["category"], "Corp. Action")
        self.assertEqual(news.parse_bse({"Table1": [{"ROWCNT": 0}]}, "X"), [])
        self.assertEqual(len(news.parse_bse(jraw("bse-500325.json")["Table"], "RELIANCE")), 2)

    def test_google_news(self):
        items = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["title"], "Reliance Industries Q2 results: Net profit rises 9% to Rs 19,300 crore")
        self.assertEqual(items[0]["source"], "The Economic Times")
        self.assertEqual(items[0]["time"], datetime.datetime(2026, 10, 28, 15, 45, tzinfo=UTC))
        self.assertEqual(items[0]["id"], "gn:CBMiAAA1")
        with self.assertRaises(news.NewsError):
            news.parse_gnews(b"<rss><channel>", "X")

    def test_times(self):
        self.assertEqual(news.parse_time("28-Oct-2026 09:15:00").hour, 3)       # IST -> UTC
        self.assertEqual(news.parse_time("2026-10-28T09:15:00.437").minute, 45)
        self.assertEqual(news.parse_time("Wed, 28 Oct 2026 10:30:00 GMT").hour, 10)
        self.assertEqual(news.parse_time("2026-10-28T10:30:00+05:30").hour, 5)
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.ist_text(news.parse_time("Wed, 28 Oct 2026 10:30:00 GMT")), "2026-10-28 16:00 IST")

    def test_urls_and_names(self):
        self.assertEqual(news.gnews_url("Reliance Industries", 2),
                         "https://news.google.com/rss/search?q=%22Reliance+Industries%22+when:2d&hl=en-IN&gl=IN"
                         "&ceid=IN:en")
        self.assertEqual(news.short_name("Reliance Industries Limited"), "Reliance Industries")
        self.assertEqual(news.short_name("Bajaj Auto Ltd."), "Bajaj Auto")
        self.assertEqual(news.NSE_ANN % "M%26M", "/api/corporate-announcements?index=equities&symbol=M%26M")


class Catalysts(unittest.TestCase):
    CASES = [
        ("Audited Financial Results for the quarter ended September 30, 2026", "results", True),
        ("Q2 results: net profit rises 12%", "results", True),
        ("Intimation of Board Meeting to consider results", "board meeting", False),
        ("Board recommends final dividend; record date fixed", "dividend", False),
        ("Board approves bonus issue in the ratio 1:1", "bonus", True),
        ("Sub-division of equity shares (stock split) from Rs 10 to Rs 2", "split", True),
        ("Board approves buyback of shares at Rs 4,500", "buyback", True),
        ("Rights issue opens on November 3", "rights issue", True),
        ("Company launches QIP to raise Rs 5,000 crore", "fund raise", True),
        ("Preferential allotment of warrants to promoters", "fund raise", True),
        ("Bulk deal: Goldman Sachs buys 1% stake", "block/bulk deal", True),
        ("Disclosure of creation of pledge / encumbrance by promoter", "promoter pledge", True),
        ("SEBI imposes penalty on the company and its MD", "SEBI action", True),
        ("Resignation of Statutory Auditor", "auditor resignation", True),
        ("CRISIL reaffirms credit rating at AA+", "credit rating", False),
        ("Scheme of amalgamation and demerger approved by NCLT", "merger/acquisition", True),
        ("Company to acquire 51% stake in XYZ", "merger/acquisition", True),
        ("Company bags order worth Rs 1,200 crore from NHAI", "order win", False),
        ("Resignation of Mr. A. Kumar as Chief Financial Officer", "management change", True),
        ("Appointment of Managing Director and CEO", "management change", True),
        ("NCLT admits insolvency petition against the company", "insolvency", True),
        ("Stock enters F&O ban period as MWPL crosses 95%", "F&O ban", True),
        ("Stock moved to ASM framework stage 2", "surveillance", True),
        ("Shares hit upper circuit for third day", "circuit", False),
    ]

    def test_every_rule(self):
        for text, want, high in self.CASES:
            tags = news.catalysts(text)
            self.assertIn(want, tags, text)
            self.assertEqual(want in news.HIGH, high, want)

    def test_tag_touches_and_importance(self):
        items = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        names = {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy Services Limited"}
        news.tag(items, names)
        mc = [i for i in items if i["source"] == "Moneycontrol"][0]
        self.assertEqual(mc["touches"], ["RELIANCE", "TCS"])
        self.assertFalse(mc["important"])
        self.assertTrue(items[0]["important"])                        # results
        nse = news.tag(news.parse_nse(jraw("nse-RELIANCE.json"), "RELIANCE"), names)
        self.assertEqual(nse[0]["tags"], ["results", "board meeting", "dividend"])
        self.assertEqual(nse[1]["tags"], ["management change"])
        self.assertTrue(nse[1]["important"])
        self.assertEqual(nse[2]["tags"], ["credit rating"])
        self.assertFalse(nse[2]["important"])

    def test_symbol_mentions_are_whole_words(self):
        pats = news.mention_patterns({"TCS": "TCS", "M&M": "Mahindra & Mahindra Limited"})
        self.assertTrue(pats["TCS"].search("TCS shares rise"))
        self.assertFalse(pats["TCS"].search("ETCS rollout"))
        self.assertTrue(pats["M&M"].search("M&M and Tata Motors"))
        self.assertTrue(pats["M&M"].search("Mahindra & Mahindra posts record SUV sales"))

    def test_dedupe_merges_the_same_story(self):
        items = news.tag(news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE"), {"RELIANCE": "Reliance Industries"})
        self.assertEqual(len(news.dedupe(items)), 4)                  # ET ran the same headline twice


class Watch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def p(self, name, doc):
        return I.write(os.path.join(self.d, name), doc)

    def test_symbols_from_the_trader_files(self):
        wl = self.p("wl.json", {"hits": [{"symbol": "SBIN"}, {"symbol": "RELIANCE"}]})
        plan = self.p("plan.json", {"symbol": "NIFTY", "instrument": {"type": "CE", "underlying": "NIFTY"}})
        pf = self.p("pf.json", {"cash_inr": 1000, "positions": {
            "TCS|CNC": {"qty": 5, "product": "CNC"},
            "INFY26NOV1500CE|NRML": {"qty": 400, "instrument": {"type": "CE", "underlying": "INFY"}},
            "BANKNIFTY26NOVFUT|NRML": {"qty": 30, "instrument": {"type": "FUT", "underlying": "BANKNIFTY"}}}})
        self.assertEqual(news.watch_from_files(pf, wl, plan), ["SBIN", "RELIANCE", "TCS", "INFY"])
        plan2 = self.p("plan2.json", {"symbol": "HDFCBANK", "instrument": {"type": "EQ"}})
        self.assertEqual(news.watch_from_files("nope", "nope", plan2), ["HDFCBANK"])

    def test_names(self):
        uni = os.path.join(self.d, "universe.txt")
        self.p("universe.json", {"names": {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy"}})
        names = news.company_names(["RELIANCE", "TCS", "SBIN"], {"names": {"tcs": "TCS"}}, uni)
        self.assertEqual(names, {"RELIANCE": "Reliance Industries Limited", "TCS": "TCS", "SBIN": "SBIN"})


class CLI(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.off = os.path.join(self.d, "feeds")
        shutil.copytree(NDIR, self.off)
        I.write(os.path.join(self.off, "chart-RELIANCE.json"), chart("RELIANCE", 1400.0, 1440.0, [1445, 1448, 1450]))
        I.write(os.path.join(self.off, "chart-TCS.json"), chart("TCS", 3000.0, 3005.0, [3010, 3012]))
        I.write(os.path.join(self.off, "chart-NIFTY.json"), chart("NIFTY", 24000.0, 24010.0, [24050, 24100]))
        self.cfg = I.write(os.path.join(self.d, "config.json"), {
            "bse_codes": {"RELIANCE": "500325"},
            "names": {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy Services Limited"}})
        self.state = os.path.join(self.d, "state", "news.json")
        self._make_net = news.make_net

    def tearDown(self):
        news.make_net = self._make_net
        shutil.rmtree(self.d, ignore_errors=True)

    def base(self, *extra):
        return ["--offline", self.off, "--now", NOW, "--config", self.cfg, "--state", self.state] + list(extra)

    def test_first_check_then_nothing_new(self):
        code, text, _ = run(self.base("--tickers", "RELIANCE", "TCS"))
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("RELIANCE    1,450.00 +3.57% vs yesterday's close 1,400.00"))
        self.assertIn("MOVING", lines[0])
        self.assertIn("GAP UP +2.86%", lines[0])
        self.assertNotIn("MOVING", lines[1])                          # TCS +0.40%
        self.assertTrue(lines[2].startswith("NIFTY 50    24,100.00 +0.42%"))
        self.assertIn("! 2026-10-28 21:33 IST  RELIANCE    NSE filing", text)
        self.assertIn("2026-10-28 19:20 IST  RELIANCE    BSE filing", text)
        self.assertIn("tags: results, board meeting, dividend", text)
        self.assertIn("RELIANCE,TCS Moneycontrol", text)
        self.assertNotIn("demerge new energy", text)                  # Oct 25: outside 48 hours
        self.assertNotIn("closure of trading window", text)           # Oct 20
        self.assertIn("Not financial advice", text)
        with open(self.state) as fh:
            st = json.load(fh)
        self.assertEqual(st["last_check"], "2026-10-28T16:30:00+00:00")
        code, text, _ = run(self.base("--tickers", "RELIANCE", "TCS", "--no-moves"))
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", text)
        code, text, _ = run(self.base("--tickers", "RELIANCE", "--no-moves", "--all", "--hours", "100"))
        self.assertEqual(code, 1)                                     # all seen; the Oct 25 story is new
        self.assertIn("demerge new energy", text)

    def test_json_and_filters(self):
        code, text, _ = run(self.base("--tickers", "RELIANCE", "--json", "--dry-run", "--no-google"))
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["checked"], "2026-10-28T22:00:00+05:30")
        self.assertEqual({i["source"] for i in out["items"]}, {"NSE filing", "BSE filing"})
        self.assertEqual(out["items"][0]["time"], "2026-10-28T21:33:50+05:30")
        rel = [m for m in out["moves"] if m["ticker"] == "RELIANCE"][0]
        self.assertEqual((rel["moving"], rel["gap"], rel["gap_pct"]), (True, "gap-up", 2.86))
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.base("--tickers", "RELIANCE", "--json", "--dry-run", "--no-filings", "--hours", "8"))
        out = json.loads(text)
        self.assertEqual([i["source"] for i in out["items"]], ["The Economic Times", "Moneycontrol"])

    def test_watchlist_from_the_scanner(self):
        wl = I.write(os.path.join(self.d, "watchlist.json"), {"created_by": "trader-scanner",
                                                               "hits": [{"symbol": "TCS", "setup": "orb"}]})
        code, text, _ = run(self.base("--watchlist", wl, "--portfolio", "nope", "--plan", "nope", "--no-moves"))
        self.assertEqual(code, 0)
        self.assertIn("TCS         NSE filing", text)

    def test_network_path_and_partial_results(self):
        class FakeNet(object):
            def __init__(s):
                s.calls = []

            def nse_json(s, path, referer="/"):
                s.calls.append(("nse", path))
                if "TCS" in path:
                    raise M.NetError("NSE did not answer /api/corporate-announcements (HTTP 403)")
                return jraw("nse-RELIANCE.json")

            def bse_json(s, url):
                s.calls.append(("bse", url))
                return jraw("bse-500325.json")

            def get(s, url, binary=False, **kw):
                s.calls.append(("get", url))
                return raw("gnews-RELIANCE.xml") if "Reliance" in url else b"<rss><channel></channel></rss>"

            def chart(s, sym, rng, interval):
                s.calls.append(("chart", sym))
                raise M.NetError("Yahoo Finance: HTTP 429")
        net = FakeNet()
        news.make_net = lambda: net
        code, text, _ = run(["--tickers", "RELIANCE,TCS", "--now", NOW, "--config", self.cfg, "--state", self.state,
                             "--hours", "24"])
        self.assertEqual(code, 0)
        kinds = [c[0] for c in net.calls]
        self.assertEqual(kinds.count("bse"), 1)                       # only RELIANCE has a BSE code
        bse_url = [c[1] for c in net.calls if c[0] == "bse"][0]
        self.assertIn("strPrevDate=20261027&strScrip=500325&strSearch=P&strToDate=20261028", bse_url)
        gets = [c[1] for c in net.calls if c[0] == "get"]
        self.assertIn("q=%22Reliance+Industries%22+when:1d", gets[0])
        self.assertIn("q=%22Tata+Consultancy+Services%22+when:1d", gets[1])
        self.assertEqual([c[1] for c in net.calls if c[0] == "chart"], ["RELIANCE", "TCS", "NIFTY"])
        self.assertIn("note: NSE filings for TCS: NSE did not answer", text)
        self.assertIn("note: price for NIFTY: Yahoo Finance: HTTP 429", text)

    def test_errors(self):
        code, _, err = run(self.base("--watchlist", "nope", "--portfolio", "nope", "--plan", "nope"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: no symbols"))
        code, _, err = run(self.base("--tickers", "RELIANCE", "--now", "yesterday"))
        self.assertEqual(code, 1)
        self.assertIn("--now wants", err)
        code, _, err = run(["--tickers", "RELIANCE", "--offline", os.path.join(self.d, "none"), "--now", NOW])
        self.assertIn("no offline folder", err)
        code, _, err = run(self.base("--tickers", "BAD!"))
        self.assertIn("not an NSE symbol", err)
        bad = os.path.join(self.d, "badfeeds")
        os.makedirs(bad)
        with open(os.path.join(bad, "nse-RELIANCE.json"), "w") as fh:
            fh.write("<html>Access Denied</html>")
        code, text, err = run(["--tickers", "RELIANCE", "--offline", bad, "--now", NOW, "--config", self.cfg,
                               "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("note: NSE filings for RELIANCE", text)
        self.assertNotIn("Traceback", err)

    def test_no_us_leftovers(self):
        with open(os.path.join(os.path.dirname(FIX), "..", "trader-news", "news.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("SEC EDGAR", "sec_user_agent", "8-K", "New York", "region=US", chr(0x2014)):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
