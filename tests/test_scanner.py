"""trader-scanner (India): universe from NSE files, daily setups, gaps, opening-range breakouts.
Offline only: synthetic Yahoo answers from tests/_india.py and small NSE CSV samples."""

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

scan = load("trader-scanner", "scan")
M = scan.M
SDIR = os.path.join(FIX, "trader-scanner")
D = datetime.date
DT = datetime.datetime
DAY = D(2026, 10, 28)          # a Wednesday, a normal NSE session
PREV = D(2026, 10, 27)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def read(name):
    with open(os.path.join(SDIR, name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- builders

def flat(n, px):
    return [(px, px + 1, px - 1, px)] * n


def session(day, ohlc, vol=50000):
    times = I.session_times(day)
    return [(times[i], o, h, lo, c, vol) for i, (o, h, lo, c) in enumerate(ohlc)]


def breakout_today(open_=1000.0):
    """Range 09:15-09:25 high 1010 low 990; 09:30 closes 1005; 09:35 closes 1012 (the break)."""
    o = open_
    rng = [(o, 1010, 990, 1000), (1000, 1008, 995, 1002), (1002, 1006, 996, 1001)]
    after = [(1001, 1007, 1000, 1005), (1005, 1015, 1004, 1012)]
    return rng + after + flat(70, 1012)


def breakdown_today():
    rng = [(1000, 1010, 990, 1000), (1000, 1008, 995, 1002), (1002, 1006, 996, 1001)]
    return rng + [(1001, 1002, 985, 986)] + flat(71, 986)


def intraday_doc(sym, today_ohlc, prev_close=1000.0, vol=50000, prev_px=1000.0, official=True):
    bars = session(PREV, flat(75, prev_px), vol) + session(DAY, today_ohlc, vol)
    return I.chart_doc(sym, bars, prev_close=prev_close if official else None)


def daily_doc(sym, closes, end=PREV, vol=100000, ohlc=None, vols=None, high52=None):
    days = I.trading_days_before(M.next_trading_day(end), len(closes))
    bars = []
    for i, (d, c) in enumerate(zip(days, closes)):
        o, h, lo = (ohlc[i] if ohlc else (c, c + 5, c - 5))
        bars.append((DT(d.year, d.month, d.day, 9, 15), o, h, lo, c, vols[i] if vols else vol))
    doc = I.chart_doc(sym, bars, interval="1d")
    if high52:
        doc["chart"]["result"][0]["meta"]["fiftyTwoWeekHigh"] = high52
    return doc


def closes_only(doc):
    """A v8 doc -> the spark-like series (closes and volumes, no opens/highs/lows)."""
    s = M.parse_chart(doc)
    n = len(s["t"])
    s["o"], s["h"], s["l"] = [None] * n, [None] * n, [None] * n
    return s


BREAKOUT = [1000 + (10 if i % 2 else 0) for i in range(59)] + [1050]
OVERSOLD = [1500 - 8 * i for i in range(60)]
FLAG = [1000 + (5 if i % 2 else 0) for i in range(45)] + [1000 + 16 * i for i in range(1, 11)] + [1160, 1165, 1158,
                                                                                                1162, 1163]
QUIET = [1000 + (3 if i % 2 else 0) for i in range(60)]


class FakeNet(object):
    """Stands in for market.Net: spark from v8 docs (closes only), charts, NSE archives."""

    def __init__(self, charts=None, texts=None, fail_spark=()):
        self.charts = charts or {}
        self.texts = texts or {}
        self.fail_spark = set(fail_spark)
        self.spark_calls, self.chart_calls, self.archive_calls = [], [], []

    def spark(self, syms, rng, interval):
        self.spark_calls.append((list(syms), rng, interval))
        if self.fail_spark & set(syms):
            raise M.NetError("Yahoo Finance: HTTP 429")
        return {s: closes_only(self.charts[s]) for s in syms if s in self.charts}

    def chart(self, sym, rng, interval):
        self.chart_calls.append((sym, rng, interval))
        if sym not in self.charts:
            raise M.NetError("Yahoo Finance: HTTP 404")
        return self.charts[sym]

    def nse_archive(self, url, binary=False):
        self.archive_calls.append(url)
        if url not in self.texts:
            raise M.NetError("nsearchives.nseindia.com answered HTTP 403")
        return self.texts[url]


class Workdir(unittest.TestCase):
    """Each test runs in an empty folder (trader/ is relative)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.d)
        self._make_net, self._range = scan.make_net, scan._live_range

    def tearDown(self):
        scan.make_net, scan._live_range = self._make_net, self._range
        os.chdir(self.old)
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, name, doc):
        return I.write(os.path.join(self.d, name), doc)

    def cfg(self, doc):
        return self.write("config.json", doc)

    def wl(self):
        with open(os.path.join(self.d, "trader", "watchlist.json"), encoding="utf-8") as fh:
            return json.load(fh)


# ---------------------------------------------------------------- universe

class Universe(Workdir):
    def test_equity_list_keeps_series_eq(self):
        rows, counts = scan.parse_equity_list(read("EQUITY_L.csv"))
        syms = [r["symbol"] for r in rows]
        self.assertEqual(syms, ["20MICRONS", "BAJAJ-AUTO", "GOLDBEES", "HDFCBANK", "M&M", "RELIANCE", "SBIN", "TCS"])
        self.assertEqual(counts["rows"], 13)
        self.assertEqual(counts["trade-for-trade (BE)"], 1)
        self.assertEqual(counts["trade-for-trade (BZ)"], 1)
        self.assertEqual(counts["SME platform (SM)"], 1)
        self.assertEqual(counts["gold bond (GB)"], 1)
        self.assertEqual(counts["not a plain NSE symbol"], 1)
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(by["M&M"]["name"], "Mahindra & Mahindra Limited")
        self.assertEqual(by["RELIANCE"]["isin"], "INE002A01018")

    def test_trade_for_trade_only_on_request(self):
        rows, _ = scan.parse_equity_list(read("EQUITY_L.csv"), include_t2t=True)
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(by["ABCLTD"]["series"], "BE")
        self.assertEqual(by["ABCLTD"]["name"], "ABC Infra, Projects Limited")   # a quoted comma
        self.assertEqual(by["XYZPOWER"]["series"], "BZ")
        self.assertNotIn("SMECO", by)

    def test_index_and_fno_lists(self):
        rows, counts = scan.parse_index_list(read("ind_nifty50list.csv"))
        self.assertEqual([r["symbol"] for r in rows], ["HDFCBANK", "M&M", "RELIANCE", "SBIN", "TCS"])
        self.assertEqual(rows[0]["industry"], "Financial Services")
        rows, _ = scan.fno_list(read("fo_mktlots.csv"))
        self.assertEqual([(r["symbol"], r["lot"]) for r in rows], [("RELIANCE", 500), ("SBIN", 750), ("TCS", 225)])
        rows, _ = scan.fno_list()                       # market.py's own table: indexes are not stocks
        syms = [r["symbol"] for r in rows]
        self.assertIn("RELIANCE", syms)
        self.assertNotIn("NIFTY", syms)

    def test_not_an_nse_file(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_equity_list("Symbol|Security Name\nAAPL|Apple\n")
        with self.assertRaises(scan.ScanError):
            scan.parse_index_list(read("EQUITY_L.csv"))

    def test_cli_writes_universe_and_names(self):
        code, text, _ = run(["universe", "--offline", os.path.join(SDIR, "EQUITY_L.csv"), "--include-t2t",
                             "--now", "2026-10-28 08:00"])
        self.assertEqual(code, 0)
        self.assertIn("Kept 10 symbols (series EQ plus BE/BZ trade-for-trade)", text)
        self.assertIn("no intraday (MIS)", text)
        with open("trader/universe.txt", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertTrue(lines[0].startswith("# trader-scanner universe: equity, 10 symbols, 2026-10-28 08:00 IST"))
        self.assertEqual(lines[1:4], ["20MICRONS", "ABCLTD", "BAJAJ-AUTO"])
        self.assertEqual(scan.symbols_from_file("trader/universe.txt")[:2], ["20MICRONS", "ABCLTD"])
        meta = scan.load_universe_meta()
        self.assertEqual(meta["names"]["RELIANCE"], "Reliance Industries Limited")
        self.assertEqual(meta["series"], {"ABCLTD": "BE", "XYZPOWER": "BZ"})
        self.assertEqual(meta["created_at"], "2026-10-28T08:00:00+05:30")

    def test_cli_index_over_the_network(self):
        net = FakeNet(texts={scan.INDEX_URLS["nifty50"]: read("ind_nifty50list.csv")})
        scan.make_net = lambda a: net
        code, text, _ = run(["universe", "--index", "nifty50", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["kind"], out["count"]), ("nifty50", 5))
        self.assertEqual(net.archive_calls, [scan.INDEX_URLS["nifty50"]])
        code, _, err = run(["universe", "--index", "nifty500"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: ") and "HTTP 403" in err)
        code, _, err = run(["universe", "--offline", "nope.csv"])
        self.assertEqual(code, 1)
        self.assertIn("no offline file", err)

    def test_fno_universe(self):
        code, text, _ = run(["universe", "--index", "fno", "--offline", os.path.join(SDIR, "fo_mktlots.csv"),
                             "--no-write", "--json"])
        self.assertEqual(json.loads(text)["count"], 3)


# ---------------------------------------------------------------- daily

class Daily(Workdir):
    NOW = M.parse_now("2026-10-28 08:30")

    def series(self, closes, **kw):
        return M.parse_chart(daily_doc("X", closes, **kw))

    def test_close_setups(self):
        def tags(closes):
            bars = scan.daily_bars(self.series(closes), self.NOW)
            return scan.find_setups(bars)[0]
        self.assertIn("breakout_20d", tags(BREAKOUT))
        self.assertEqual(tags(OVERSOLD), ["oversold"])
        self.assertEqual(tags(OVERSOLD + [OVERSOLD[-1] + 5]), ["oversold", "oversold_bounce"])
        self.assertIn("bull_flag", tags(FLAG))
        self.assertIn("reclaim_ma20", tags(QUIET[:50] + [990, 1004]))
        up = [1000 + 5 * i for i in range(70)]
        self.assertIn("ema_pullback", tags(up + [up[-1] - 12 * j for j in range(1, 5)]))
        self.assertNotIn("ema_pullback", tags(up))                  # no pullback yet
        self.assertNotIn("ema_pullback", tags(up[:40] + [1195, 1180, 1170]))   # under 60 bars: no EMA 50

    def test_near_52_week_high_uses_yahoo_meta(self):
        s = self.series(QUIET)
        self.assertIn("near_52w_high", scan.find_setups(scan.daily_bars(s, self.NOW))[0])
        t, n = scan.find_setups(scan.daily_bars(s, self.NOW), high52=1200)
        self.assertNotIn("near_52w_high", t)
        self.assertEqual(n["high52_basis"], "Yahoo 52-week high")

    def test_nr7_and_volume_surge_need_ohlcv(self):
        closes = QUIET
        ohlc = [(c, c + 10, c - 10) for c in closes[:-1]] + [(closes[-1], closes[-1] + 2, closes[-1] - 2)]
        vols = [100000] * 59 + [250000]
        s = self.series(closes, ohlc=ohlc, vols=vols)
        tags, nums = scan.find_setups(scan.daily_bars(s, self.NOW))
        self.assertIn("nr7", tags)
        self.assertIn("volume_surge", tags)
        self.assertAlmostEqual(nums["vol_ratio"], 2.5)
        tags, _ = scan.find_setups(scan.daily_bars(closes_only(daily_doc("X", closes, ohlc=ohlc)), self.NOW))
        self.assertNotIn("nr7", tags)                                 # closes only: cannot know the range

    def test_todays_bar_waits_for_the_1530_close(self):
        s = self.series(QUIET, end=DAY)
        self.assertEqual(scan.daily_bars(s, M.parse_now("2026-10-28 15:29"))[-1]["d"], PREV)
        self.assertEqual(scan.daily_bars(s, M.parse_now("2026-10-28 15:30"))[-1]["d"], DAY)
        self.assertEqual(scan.daily_bars(s, M.parse_now("2026-10-27 12:00"))[-1]["d"], D(2026, 10, 26))
        days = [b["d"] for b in scan.daily_bars(s, M.parse_now("2026-10-29 08:00"))]
        self.assertNotIn(D(2026, 10, 20), days)                       # Dussehra: no session

    def test_liquidity_in_rupees(self):
        data = {"BIG": self.series(BREAKOUT, vol=100000),           # ~Rs 10 crore a day
                "THIN": self.series(BREAKOUT, vol=1000),            # ~Rs 0.1 crore a day
                "PENNY": self.series([x / 100.0 for x in BREAKOUT], vol=10 ** 9)}
        hits, counts, last, _ = scan.scan_daily(data, ["BIG", "THIN", "PENNY"], self.NOW, 20, 5)
        self.assertEqual([h["symbol"] for h in hits], ["BIG"])
        self.assertEqual(counts["illiquid"], 2)
        self.assertEqual(last, PREV)
        self.assertAlmostEqual(hits[0]["turnover_cr"], 10.1, places=1)
        self.assertEqual(hits[0]["turnover_basis"], "20-day average")

    def test_stage_two_fetches_ohlcv_for_candidates_only(self):
        docs = {"A": daily_doc("A", BREAKOUT, vol=200000), "B": daily_doc("B", OVERSOLD, vol=300000),
                "C": daily_doc("C", QUIET[:-1] + [1001.5], vol=100000)}
        stage1 = {k: closes_only(v) for k, v in docs.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return M.parse_chart(docs[sym])
        hits, counts, _, _ = scan.scan_daily(stage1, ["A", "B", "C"], self.NOW, 20, 5, confirm, 1)
        self.assertEqual(asked, ["B"])                                # the most traded candidate first, cap 1
        self.assertEqual((counts["unconfirmed"], counts["ohlc_checked"]), (2, 1))
        asked[:] = []
        hits, counts, _, _ = scan.scan_daily(stage1, ["A", "B", "C"], self.NOW, 20, 5, confirm, 0, deep=True)
        self.assertEqual(sorted(asked), ["A", "B", "C"])
        self.assertEqual(counts["ohlc_checked"], 3)

    def test_cli_daily_with_skips_and_context(self):
        bundle = {"RELIANCE": daily_doc("RELIANCE", BREAKOUT), "TCS": daily_doc("TCS", OVERSOLD),
                  "SBIN": daily_doc("SBIN", FLAG), "YESBANK": daily_doc("YESBANK", BREAKOUT),
                  "NIFTY": I.chart_doc("NIFTY", session(D(2026, 10, 26), flat(75, 24000)) +
                                       session(PREV, flat(75, 24100)))}
        self.write("d.json", bundle)
        self.cfg({"asm_gsm": ["YESBANK"]})
        code, text, _ = run(["daily", "--offline", "d.json", "--now", "2026-10-28 08:30", "--config", "config.json"])
        self.assertEqual(code, 0)
        self.assertIn("closes through 2026-10-27", text)
        self.assertIn("Market: NIFTY 50 24,100.00 (+0.42%)", text)
        self.assertIn("Skipped 1: YESBANK (under ASM/GSM surveillance: skipped)", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.wl()
        self.assertEqual((doc["created_by"], doc["scan"], doc["date"], doc["universe"], doc["scanned"]),
                         ("trader-scanner", "daily", "2026-10-27", 4, 3))
        self.assertEqual(doc["created_at"], "2026-10-28T08:30:00+05:30")
        self.assertEqual(sorted(h["symbol"] for h in doc["hits"]), ["RELIANCE", "SBIN", "TCS"])
        self.assertEqual(doc["context"][0]["symbol"], "NIFTY")
        self.assertEqual(doc["skipped"], {"YESBANK": "under ASM/GSM surveillance: skipped"})

    def test_cli_daily_network_partial(self):
        charts = {"S%02d" % i: daily_doc("S%02d" % i, BREAKOUT) for i in range(25)}
        net = FakeNet(charts, fail_spark=["S24"])
        scan.make_net = lambda a: net
        code, text, _ = run(["daily", "--symbols", ",".join(sorted(charts)), "--now", "2026-10-28 08:30",
                             "--no-write", "--no-index"])
        self.assertEqual(code, 0)
        self.assertEqual([len(c[0]) for c in net.spark_calls], [20, 5])
        self.assertTrue(all(c[1:] == ("6mo", "1d") for c in net.spark_calls))
        self.assertIn("Fetched 20 of 25 charts (5 failed or had no data)", text)
        self.assertEqual(len(net.chart_calls), 20)                    # stage 2: candidates' OHLCV


# ---------------------------------------------------------------- orb

class Orb(Workdir):
    def test_session_shape(self):
        self.assertEqual(M.bars_per_session(5), 75)
        s = M.parse_chart(intraday_doc("X", breakout_today()))
        bars = scan.session_bars(s, DAY, DT(2026, 10, 28, 15, 30))
        self.assertEqual(len(bars), 75)
        self.assertEqual(bars[0]["start"], DT(2026, 10, 28, 9, 15))
        self.assertEqual(bars[-1]["start"], DT(2026, 10, 28, 15, 25))
        self.assertTrue(scan.usable(bars, DAY, DT(2026, 10, 28, 15, 30), 4))
        self.assertEqual(len(scan.session_bars(s, DAY, DT(2026, 10, 28, 9, 39))), 4)   # 09:35 bar still forming

    def test_long_and_short_hits(self):
        s = M.parse_chart(intraday_doc("X", breakout_today()))
        bars = scan.session_bars(s, DAY, DT(2026, 10, 28, 10, 15))
        h = scan.orb_hits(bars, 3)
        self.assertEqual(len(h), 1)
        self.assertEqual({k: h[0][k] for k in ("side", "price", "at", "bar_start", "bar_index", "or_high", "or_low")},
                         {"side": "buy", "price": 1012, "at": "09:40", "bar_start": "09:35", "bar_index": 4,
                          "or_high": 1010, "or_low": 990})
        s = M.parse_chart(intraday_doc("X", breakdown_today()))
        bars = scan.session_bars(s, DAY, DT(2026, 10, 28, 10, 15))
        self.assertEqual(scan.orb_hits(bars, 3), [])
        h = scan.orb_hits(bars, 3, short=True)
        self.assertEqual((h[0]["side"], h[0]["at"], h[0]["level"]), ("sell", "09:35", 990))
        self.assertTrue(scan.orb_candidate(bars, 3, short=True))
        self.assertFalse(scan.orb_candidate(bars, 3))

    def test_windows_in_plain_words(self):
        def err(now, at=None, minutes=15, until=None):
            with self.assertRaises(scan.ScanError) as c:
                scan.orb_window(M.parse_now(now), at, minutes, until)
            return str(c.exception)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err("2026-10-31 10:15"))
        self.assertIn("NSE holiday: Dussehra", err("2026-10-20 10:15"))
        self.assertIn("NSE holiday: Diwali-Balipratipada", err("2026-11-10 10:15"))
        self.assertIn("pre-open", err("2026-10-28 09:05"))
        self.assertIn("first bar after it closes at 9:35 AM", err("2026-10-28 09:34"))
        self.assertIn("closes at 9:50 AM", err("2026-10-28 09:40", minutes=30))
        self.assertIn("expired", err("2026-10-28 15:00", until=datetime.time(14, 45)))
        self.assertIn("closed at 3:30 PM", err("2026-10-28 15:45"))
        self.assertIn("later than now", err("2026-10-28 10:00", at=datetime.time(10, 30)))
        self.assertIn("multiple of 5", err("2026-10-28 10:15", minutes=7))
        day, cutoff = scan.orb_window(M.parse_now("2026-10-28 09:35"), None, 15, None)
        self.assertEqual(cutoff, DT(2026, 10, 28, 9, 35))

    def test_scan_stage_two_and_cap(self):
        docs = {"BIG": intraday_doc("BIG", breakout_today(), vol=90000),
                "MID": intraday_doc("MID", breakout_today(), vol=60000),
                "LOW": intraday_doc("LOW", breakout_today(), vol=30000),
                "NONE": intraday_doc("NONE", flat(3, 1000) + flat(72, 995))}
        stage1 = {k: closes_only(v) for k, v in docs.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return M.parse_chart(docs[sym])
        cutoff = DT(2026, 10, 28, 10, 15)
        hits, counts, failed = scan.scan_orb(stage1, sorted(docs), DAY, cutoff, 15, 20, 5, confirm, 2)
        self.assertEqual(asked, ["BIG", "MID"])                       # most traded first
        self.assertEqual((counts["candidates"], counts["confirmed"], counts["unconfirmed"]), (3, 2, 1))
        self.assertEqual([h["symbol"] for h in hits], ["BIG", "MID"])
        self.assertEqual(hits[0]["opening_range"], {"high": 1010, "low": 990, "minutes": 15})
        self.assertAlmostEqual(hits[0]["turnover_cr"], 675.0)         # 75 x Rs 1000 x 90,000 / 1e7

    def test_missing_bar_and_turnover_floor(self):
        doc = intraday_doc("GAPPY", breakout_today())
        q = doc["chart"]["result"][0]["indicators"]["quote"][0]
        q["close"][75 + 6] = None                                     # the 09:45 bar has no trade
        thin = intraday_doc("THIN", breakout_today(), vol=100)
        data = {"GAPPY": M.parse_chart(doc), "THIN": M.parse_chart(thin)}
        hits, counts, _ = scan.scan_orb(data, ["GAPPY", "THIN"], DAY, DT(2026, 10, 28, 10, 15), 15, 20, 5)
        self.assertEqual((counts["unusable"], counts["illiquid"], len(hits)), (1, 1, 0))

    def test_gap_pct_with_each_hit(self):
        data = {"UP": M.parse_chart(intraday_doc("UP", breakout_today(open_=1000.0), prev_close=980.0))}
        hits, _, _ = scan.scan_orb(data, ["UP"], DAY, DT(2026, 10, 28, 10, 15), 15)
        self.assertEqual(hits[0]["gap_pct"], 2.04)
        self.assertEqual(hits[0]["prev_close"], 980.0)
        # no official close in the chart: the previous session's last 5-minute close stands in
        data = {"UP": M.parse_chart(intraday_doc("UP", breakout_today(), official=False, prev_px=990.0))}
        hits, _, _ = scan.scan_orb(data, ["UP"], DAY, DT(2026, 10, 28, 10, 15), 15)
        self.assertEqual(hits[0]["prev_close"], 990.0)

    def test_cli_orb_contract_and_skips(self):
        bundle = {"RELIANCE": intraday_doc("RELIANCE", breakout_today()),
                  "SBIN": intraday_doc("SBIN", breakdown_today()),
                  "IDEA": intraday_doc("IDEA", breakout_today()),
                  "ABCLTD": intraday_doc("ABCLTD", breakout_today()),
                  "NIFTY": intraday_doc("NIFTY", flat(75, 24100), prev_close=24000, prev_px=24000),
                  "BANKNIFTY": intraday_doc("BANKNIFTY", flat(75, 51000), prev_close=51500, prev_px=51500),
                  "INDIAVIX": intraday_doc("INDIAVIX", flat(75, 13.2), prev_close=13.0, prev_px=13.0)}
        self.write("b.json", bundle)
        self.cfg({"fno_ban": ["IDEA"], "no_entries_after": "14:45"})
        os.makedirs("trader")
        self.write("trader/universe.json", {"series": {"ABCLTD": "BE"}})
        code, text, _ = run(["orb", "--offline", "b.json", "--now", "2026-10-28 10:15", "--short",
                             "--config", "config.json"])
        self.assertEqual(code, 0)
        self.assertIn("Market: NIFTY 50 24,100.00 (+0.42%), NIFTY BANK 51,000.00 (-0.97%), India VIX 13.20 (+1.54%)",
                      text)
        self.assertIn("IDEA (in the F&O ban period", text)
        self.assertIn("ABCLTD (series BE: trade-for-trade, delivery only, no intraday)", text)
        self.assertIn("allow_short is off", text)
        self.assertIn("9:35 AM  SBIN        short", text)
        self.assertIn("9:40 AM  RELIANCE    long", text)
        doc = self.wl()
        self.assertEqual((doc["scan"], doc["date"], doc["as_of"], doc["universe"], doc["scanned"]),
                         ("orb", "2026-10-28", "2026-10-28 10:15 IST", 4, 2))
        rel = [h for h in doc["hits"] if h["symbol"] == "RELIANCE"][0]
        for k in ("symbol", "setup", "price", "opening_range", "gap_pct", "turnover_cr", "note"):
            self.assertIn(k, rel)
        self.assertEqual((rel["setup"], rel["side"], rel["price"], rel["at"]), ("orb", "buy", 1012, "09:40"))
        self.assertEqual([c["symbol"] for c in doc["context"]], ["NIFTY", "BANKNIFTY", "INDIAVIX"])

    def test_cli_errors_and_exit_codes(self):
        self.write("b.json", {"RELIANCE": intraday_doc("RELIANCE", breakout_today())})
        code, _, err = run(["orb", "--offline", "b.json", "--now", "2026-10-20 10:15"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: No session on Tue 2026-10-20 (NSE holiday: Dussehra)"))
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists("trader/watchlist.json"))
        code, _, err = run(["orb", "--offline", "b.json"])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", "b.json", "--at", "09:35", "--no-index"])
        self.assertEqual(code, 1)                                     # replay at 09:35: no break yet
        self.assertIn("No chart closed above its opening range by 9:35 AM", text)
        self.assertEqual(self.wl()["hits"], [])
        code, text, _ = run(["orb", "--offline", "b.json", "--at", "10:15", "--json", "--no-write"])
        self.assertEqual((code, json.loads(text)["hits"][0]["symbol"]), (0, "RELIANCE"))
        code, _, err = run(["orb", "--offline", "b.json", "--now", "2026-10-28 10:15", "--symbols", "BAD!"])
        self.assertIn("not an NSE symbol", err)
        code, _, err = run(["orb", "--universe", "nope.txt", "--now", "2026-10-28 10:15"])
        self.assertIn("scan.py universe", err)
        code, _, err = run(["orb", "--offline", "missing", "--now", "2026-10-28 10:15"])
        self.assertIn("no offline file or folder", err)
        code, _, err = run(["orb", "--offline", "b.json", "--now", "28/10/2026"])
        self.assertIn("--now wants", err)

    def test_cli_orb_network_two_stages(self):
        charts = {"A%02d" % i: intraday_doc("A%02d" % i, breakout_today(), vol=40000 + 1000 * i) for i in range(22)}
        charts["NIFTY"] = intraday_doc("NIFTY", flat(75, 24100), prev_close=24000, prev_px=24000)
        net = FakeNet(charts)
        scan.make_net = lambda a: net
        scan._live_range = lambda d: "5d"
        syms = ",".join("A%02d" % i for i in range(22))
        code, text, _ = run(["orb", "--symbols", syms, "--now", "2026-10-28 10:15", "--max-confirm", "5",
                             "--no-write"])
        self.assertEqual(code, 0)
        self.assertEqual([len(c[0]) for c in net.spark_calls], [20, 2])
        self.assertTrue(all(c[1:] == ("5d", "5m") for c in net.spark_calls))
        stocks = [c[0] for c in net.chart_calls if c[0].startswith("A")]
        self.assertEqual(stocks, ["A21", "A20", "A19", "A18", "A17"])
        self.assertIn("PARTIAL: 17 more candidates were not checked", text)
        self.assertIn("Market: NIFTY 50", text)


# ---------------------------------------------------------------- gap

class Gap(Workdir):
    def bars(self, ohlc, prev_close=1000.0):
        s = M.parse_chart(intraday_doc("X", ohlc + flat(75 - len(ohlc), ohlc[-1][3]), prev_close=prev_close))
        return s

    def test_classify_open(self):
        go = [(1030, 1035, 1028, 1033), (1033, 1040, 1031, 1038), (1038, 1042, 1034, 1040)]
        g = scan.classify_open(scan.session_bars(self.bars(go), DAY, DT(2026, 10, 28, 9, 30)), 1000.0)
        self.assertEqual((g["gap_pct"], g["gap"], g["status"]), (3.0, "gap-up", "gap-and-go"))
        self.assertAlmostEqual(g["filled_pct"], 6.7)
        fade = [(1030, 1031, 1012, 1015), (1015, 1018, 1008, 1010), (1010, 1012, 1005, 1008)]
        g = scan.classify_open(scan.session_bars(self.bars(fade), DAY, DT(2026, 10, 28, 9, 30)), 1000.0)
        self.assertEqual(g["status"], "gap-fill")
        filled = fade + [(1008, 1009, 999, 1002)]
        g = scan.classify_open(scan.session_bars(self.bars(filled), DAY, DT(2026, 10, 28, 9, 35)), 1000.0)
        self.assertEqual(g["status"], "gap filled")
        g = scan.classify_open(scan.session_bars(self.bars(go), DAY, DT(2026, 10, 28, 9, 20)), 1000.0)
        self.assertEqual(g["status"], "too early")
        down = [(970, 972, 960, 962), (962, 965, 955, 958), (958, 966, 950, 952)]
        g = scan.classify_open(scan.session_bars(self.bars(down), DAY, DT(2026, 10, 28, 9, 30)), 1000.0)
        self.assertEqual((g["gap"], g["status"]), ("gap-down", "gap-and-go"))
        small = [(1008, 1010, 1006, 1009)]
        g = scan.classify_open(scan.session_bars(self.bars(small), DAY, DT(2026, 10, 28, 9, 30)), 1000.0)
        self.assertEqual(g["gap"], "small gap-up")

    def test_bands_file(self):
        bands = scan.load_bands(os.path.join(SDIR, "sec_list.csv"))
        self.assertEqual(bands["SMALLCO"], 5.0)
        self.assertEqual(bands["20MICRONS"], 20.0)
        self.assertIsNone(bands["RELIANCE"])                          # F&O: no fixed band
        self.write("b.json", {"SMALLCO": 10})
        self.assertEqual(scan.load_bands(os.path.join(self.d, "b.json")), {"SMALLCO": 10.0})
        with self.assertRaises(scan.ScanError):
            scan.load_bands(os.path.join(SDIR, "EQUITY_L.csv"))

    def test_scan_gap_circuits_and_skips(self):
        near_upper = [(103, 104.8, 102.9, 104.6), (104.6, 104.9, 104.2, 104.7), (104.7, 104.9, 104.5, 104.8)]
        rel_go = [(1030, 1035, 1028, 1033), (1033, 1040, 1031, 1038), (1038, 1042, 1034, 1040)]
        docs = {"SMALLCO": intraday_doc("SMALLCO", near_upper + flat(72, 104.8), prev_close=100.0, prev_px=100.0),
                "RELIANCE": intraday_doc("RELIANCE", rel_go + flat(72, 1040)),
                "QUIET": intraday_doc("QUIET", flat(75, 1002)),
                "IDEA": intraday_doc("IDEA", rel_go + flat(72, 1040))}
        data = {k: M.parse_chart(v) for k, v in docs.items()}
        skip = scan.skip_reasons({"fno_ban": ["IDEA"]}, "gap")
        hits, counts, _ = scan.scan_gap(data, sorted(docs), DAY, DT(2026, 10, 28, 9, 45), 15, 1.0,
                                        {"SMALLCO": 5, "RELIANCE": 10}, {"RELIANCE"}, 2.0, 20, 0, skip=skip)
        self.assertEqual([h["symbol"] for h in hits], ["RELIANCE", "SMALLCO"])   # 3.0% then 3.0%: by name
        sm = hits[1]
        self.assertEqual(sm["circuit"], {"band_pct": 5, "upper": 105.0, "lower": 95.0, "near": "upper",
                                         "basis": "band 5%"})
        self.assertIn("within 2% of the upper circuit (105.00)", sm["note"])
        rel = hits[0]
        self.assertIsNone(rel["circuit"]["band_pct"])
        self.assertIn("F&O stock", rel["circuit"]["basis"])
        self.assertEqual((rel["status"], rel["setups"]), ("gap-and-go", ["gap", "gap-and-go"]))
        self.assertEqual(counts["skipped"], 1)

    def test_spark_stage_uses_first_close_then_the_real_open(self):
        rel = intraday_doc("RELIANCE", [(1030, 1035, 1010, 1012)] + flat(74, 1012))   # opened +3%, closed +1.2%
        flatdoc = intraday_doc("FLAT", flat(75, 1000))
        stage1 = {"RELIANCE": closes_only(rel), "FLAT": closes_only(flatdoc)}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return M.parse_chart({"RELIANCE": rel, "FLAT": flatdoc}[sym])
        hits, counts, _ = scan.scan_gap(stage1, ["RELIANCE", "FLAT"], DAY, DT(2026, 10, 28, 9, 45), 15, 2.0,
                                        confirm=confirm, max_confirm=10)
        self.assertEqual(asked, ["RELIANCE"])
        self.assertEqual((hits[0]["open"], hits[0]["gap_pct"]), (1030, 3.0))

    def test_cli_gap(self):
        go = [(1030, 1035, 1028, 1033), (1033, 1040, 1031, 1038), (1038, 1042, 1034, 1040)]
        self.write("b.json", {"TCS": intraday_doc("TCS", go + flat(72, 1040)),
                              "SBIN": intraday_doc("SBIN", flat(75, 1000))})
        self.cfg({"bands": {"TCS": 20}})
        code, text, _ = run(["gap", "--offline", "b.json", "--now", "2026-10-28 09:50", "--config", "config.json",
                             "--no-index"])
        self.assertEqual(code, 0)
        self.assertIn("1 chart opened 1% or more away from yesterday's close", text)
        self.assertIn("TCS          +3.00%", text)
        self.assertIn("gap-and-go", text)
        doc = self.wl()
        self.assertEqual((doc["scan"], doc["hits"][0]["setup"], doc["hits"][0]["gap"]), ("gap", "gap", "gap-up"))
        code, _, err = run(["gap", "--offline", "b.json", "--now", "2026-10-28 09:17"])
        self.assertEqual(code, 1)
        self.assertIn("closes at 9:20 AM", err)
        code, _, err = run(["gap", "--offline", "b.json", "--now", "2026-10-28 09:50", "--hold-minutes", "7"])
        self.assertIn("--hold-minutes", err)
        code, _, err = run(["gap", "--offline", "b.json", "--now", "2026-10-28 09:50", "--bands", "nope.csv"])
        self.assertIn("no bands file", err)


class Notes(unittest.TestCase):
    def test_the_note_and_no_us_leftovers(self):
        self.assertEqual(scan.NOTE, "A scan is a list of charts that match a rule, not a list of stocks to buy.")
        with open(os.path.join(os.path.dirname(FIX), "..", "trader-scanner", "scan.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("NYSE", "Nasdaq", "New York", chr(0x2014)):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
