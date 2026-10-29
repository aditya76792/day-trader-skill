"""trader-entry (India): ORB from 09:15, 75 bars, NSE holidays, expiry-day cutoffs,
gap / VWAP / circuit filters, shorts, refusals, watch loop and CLI. Offline only."""

import contextlib
import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest

import _india as I
from _load import ROOT, load

M = I.M
E = load("trader-entry", "entry")

DAY = datetime.date(2026, 10, 28)          # Wednesday, an ordinary session
# the opening range is 998.00 to 1006.00; bar 3 wicks above, bar 4 closes AT the high,
# bar 5 (09:40-09:45) closes above it at 1008.40
ORB_ROWS = {0: (1000, 1004, 998, 1002), 1: (1002, 1006, 1001, 1005), 2: (1005, 1005.5, 1000, 1003),
            3: (1003, 1007, 1002, 1005.5), 4: (1005.5, 1006.5, 1004, 1006.0), 5: (1006, 1010, 1005, 1008.4)}


def rd(path, raw=False):
    with open(path, encoding="utf-8") as fh:
        return fh.read() if raw else json.load(fh)


def at(day, hhmm):
    return datetime.datetime.combine(day, M.parse_hhmm(hhmm))


def day_bars(day, base=1000.0, rows=None, volume=10000, interval=5):
    """A session from 09:15: scripted bars where given, flat bars (h/l +-0.3) elsewhere."""
    out, c = [], base
    for i, t in enumerate(I.session_times(day, interval)):
        if rows and i in rows:
            r = rows[i]
            o, h, lo, c2 = r[:4]
            v = r[4] if len(r) > 4 else volume
        else:
            o, h, lo, c2, v = c, c + 0.3, c - 0.3, c, volume
        out.append((t, o, h, lo, c2, v))
        c = c2
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chart(self, sym, bars, prev_close=1000.0, interval="5m", day=DAY, **meta):
        doc = I.chart_doc(sym, bars, prev_close=prev_close, interval=interval)
        doc["chart"]["result"][0]["meta"].update(meta)
        path = I.write(os.path.join(self.tmp, "%s.json" % sym), doc)
        return E.get_chart(sym, interval, day, day, M.exchange_of(sym), path)

    def ev(self, chart, now, **kw):
        cfg = dict(M.DEFAULT_CONFIG, **kw.pop("cfg", {}))
        return E.evaluate(chart, now, cfg=cfg, **kw)


class OpeningRangeBreakout(Base):
    def test_triggers_on_the_first_close_above_the_range(self):
        r = self.ev(self.chart("RELIANCE", day_bars(DAY, rows=ORB_ROWS)), at(DAY, "09:46"))
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"], {"high": 1006.0, "low": 998.0, "minutes": 15, "bars": 3, "of": 3,
                                              "complete": True, "avg_volume": 10000})
        t = r["trigger"]
        self.assertEqual((t["bar_start"], t["close_time"], t["bar_index"], t["price"]), ("09:40", "09:45", 5, 1008.4))
        self.assertEqual(r["level"], 1006.0)
        self.assertEqual(r["gap_pct"], 0.0)
        self.assertEqual(r["gap_class"], "flat open")
        self.assertIsNotNone(r["vwap"])
        self.assertEqual(r["vwap_side"], "above")

    def test_wick_and_a_close_at_the_high_do_not_trigger(self):
        r = self.ev(self.chart("RELIANCE", day_bars(DAY, rows=ORB_ROWS)), at(DAY, "09:44"))
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Rs 0.00 under the range high Rs 1,006.00", r["reason"])
        self.assertEqual(r["forming"]["bar_start"], "09:40")

    def test_pre_open_and_range_forming(self):
        ch = self.chart("RELIANCE", day_bars(DAY, rows=ORB_ROWS))
        r = self.ev(ch, at(DAY, "09:05"))
        self.assertEqual(r["phase"], "pre-open")
        self.assertIn("9:15 AM", r["reason"])
        self.assertIn("pre-open auction", r["reason"])
        r = self.ev(ch, at(DAY, "09:25"))
        self.assertEqual(r["phase"], "forming")
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertIn("9:30 AM", r["reason"])

    def test_a_session_is_75_five_minute_bars_and_375_one_minute_bars(self):
        r = self.ev(self.chart("TCS", day_bars(DAY, 3000.0)), at(DAY, "15:45"))
        self.assertEqual(r["bars_closed"], 75)
        ch = self.chart("TCS", day_bars(DAY, 3000.0, interval=1), interval="1m")
        r = self.ev(ch, at(DAY, "15:45"), interval="1m")
        self.assertEqual(r["bars_closed"], 375)
        self.assertEqual(r["opening_range"]["of"], 15)

    def test_no_entries_before_0930_and_cutoff_1445(self):
        rows = dict(ORB_ROWS)
        rows[2] = (1005, 1007.2, 1000, 1007)          # range high 1007.20
        rows[3] = (1007, 1008, 1006, 1007.5)          # 09:30 bar closes 09:35: counts (>= 09:30)
        r = self.ev(self.chart("SBIN", day_bars(DAY, rows=rows)), at(DAY, "09:40"))
        self.assertEqual(r["trigger"]["close_time"], "09:35")
        r = self.ev(self.chart("SBIN", day_bars(DAY, rows=rows)), at(DAY, "09:36"),
                    cfg={"no_entries_before": "09:45"})
        self.assertEqual(r["status"], "waiting")
        self.assertIn("no entries before 9:45 AM", r["reason"])
        r = self.ev(self.chart("SBIN", day_bars(DAY, rows={0: (1000, 1004, 998, 1002)})), at(DAY, "14:50"))
        self.assertEqual(r["status"], "expired")
        self.assertIn("2:45 PM", r["reason"])

    def test_volume_confirmation_and_level_setup(self):
        rows = dict(ORB_ROWS)
        rows[5] = (1006, 1010, 1005, 1008.4, 5000)    # half the range's volume
        rows[7] = (1008.4, 1012, 1008, 1011.0, 30000)
        ch = self.chart("INFY", day_bars(DAY, rows=rows))
        r = self.ev(ch, at(DAY, "10:00"), min_volume_x=1.5)
        self.assertEqual(r["trigger"]["bar_index"], 7)
        self.assertEqual(r["trigger"]["volume_x"], 3.0)
        r = self.ev(ch, at(DAY, "10:00"), setup="level", level=1010.0)
        self.assertEqual(r["trigger"]["price"], 1011.0)
        with self.assertRaises(M.MarketError):
            self.ev(ch, at(DAY, "10:00"), setup="level")

    def test_bad_inputs(self):
        ch = self.chart("INFY", day_bars(DAY))
        for kw in ({"interval": "15m"}, {"or_minutes": 7}, {"setup": "cup"}, {"level": 10.0}):
            with self.assertRaises(M.MarketError):
                self.ev(ch, at(DAY, "10:00"), **kw)

    def test_yahoo_lag_keeps_the_bar_pending(self):
        bars = day_bars(DAY, rows=ORB_ROWS)[:6]
        lag = M.ts_from_ist(at(DAY, "09:44"))          # last trade before the 09:40 bar closed
        r = self.ev(self.chart("RELIANCE", bars, regularMarketTime=lag), at(DAY, "09:45"))
        self.assertEqual(r["status"], "waiting")
        self.assertIn("catches up", r["lag_note"])
        r = self.ev(self.chart("RELIANCE", bars, regularMarketTime=lag), at(DAY, "09:46"))
        self.assertEqual(r["status"], "triggered")          # a minute later it counts


class Calendar(Base):
    def test_holiday_weekend_and_muhurat(self):
        hol = datetime.date(2026, 10, 20)
        r = self.ev(self.chart("RELIANCE", day_bars(DAY), day=hol), at(hol, "10:00"))
        self.assertEqual(r["status"], "no-data")
        self.assertIn("Dussehra", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        sun = datetime.date(2026, 11, 8)
        r = self.ev(self.chart("RELIANCE", day_bars(DAY), day=sun), at(sun, "18:00"))
        self.assertEqual(r["phase"], "closed")
        self.assertTrue(any("Muhurat" in w for w in r["warnings"]))
        self.assertEqual(r["next_open"], "2026-11-09 09:15")

    def test_holiday_adjacent_day_takes_the_close_before_the_holiday(self):
        d = datetime.date(2026, 10, 21)                     # the day after Dussehra
        prev = day_bars(datetime.date(2026, 10, 19), 990.0)
        today = day_bars(d, 1000.0, rows={0: (1009.9, 1012, 1008, 1010)})
        r = self.ev(self.chart("RELIANCE", prev + today, prev_close=None, day=d), at(d, "10:00"))
        self.assertEqual(r["prev_close"], 990.0)
        self.assertIn("2026-10-19", r["prev_close_source"])
        self.assertEqual(r["gap_pct"], 2.01)
        self.assertEqual(r["gap_class"], "gap-up")

    def test_expiry_day_cutoff_for_index_and_fno(self):
        exp = datetime.date(2026, 11, 3)                    # NIFTY weekly expiry, a Tuesday
        self.assertTrue(M.is_expiry_day("NIFTY", exp))
        rows = {0: (25000, 25040, 24980, 25020, 0), 1: (25020, 25050, 25000, 25030, 0),
                2: (25030, 25045, 25010, 25035, 0), 58: (25035, 25090, 25030, 25080, 0)}   # 14:05 bar
        bars = day_bars(exp, 25035.0, rows=rows, volume=0)
        r = self.ev(self.chart("NIFTY", bars, prev_close=25000.0, day=exp), at(exp, "14:30"))
        self.assertTrue(r["expiry_day"])
        self.assertEqual(r["no_entries_after"], "13:30")
        self.assertEqual(r["status"], "expired")
        self.assertIn("expiry day", r["reason"])
        self.assertIsNone(r["vwap"])                         # an index has no volume
        self.assertTrue(any("trader-fno" in w for w in r["warnings"]))
        r = self.ev(self.chart("NIFTY", bars, prev_close=25000.0, day=exp), at(exp, "14:30"),
                    cfg={"expiry_day_last_entry": "14:30"})
        self.assertEqual(r["status"], "triggered")
        # a stock in the cash market is not cut off, but hears about the index expiry
        r = self.ev(self.chart("TCS", day_bars(exp, 3000.0), day=exp), at(exp, "14:30"), ctx={"instrument": "EQ"})
        self.assertFalse(r["expiry_day"])
        self.assertEqual(r["no_entries_after"], "14:45")
        self.assertTrue(any("NIFTY weekly expiry" in w for w in r["warnings"]))

    def test_stock_monthly_expiry_moves_before_a_holiday(self):
        d = datetime.date(2026, 11, 23)                     # last Tuesday 24 Nov is Guru Nanak Jayanti
        self.assertTrue(M.is_expiry_day("RELIANCE", d))
        r = self.ev(self.chart("RELIANCE", day_bars(d), day=d), at(d, "11:00"), ctx={"instrument": "FUT"})
        self.assertTrue(r["expiry_day"])
        self.assertEqual(r["no_entries_after"], "13:30")

    def test_chart_range_counts_trading_days(self):
        today = datetime.date(2026, 10, 28)
        self.assertEqual(E.chart_range(today, today, "5m"), "5d")
        self.assertEqual(E.chart_range(datetime.date(2026, 10, 19), today, "5m"), "1mo")
        self.assertEqual(E.chart_range(datetime.date(2026, 8, 20), today, "5m"), "60d")
        with self.assertRaises(M.MarketError):
            E.chart_range(datetime.date(2026, 10, 19), today, "1m")
        with self.assertRaises(M.MarketError):
            E.chart_range(datetime.date(2026, 6, 1), today, "5m")


class Filters(Base):
    def test_gap_and_go(self):
        rows = {0: (1030, 1034, 1028, 1032), 1: (1032, 1036, 1031, 1035), 2: (1035, 1035.5, 1030, 1033),
                5: (1033, 1040, 1032, 1038.5)}
        ch = self.chart("TMPV", day_bars(DAY, 1030.0, rows=rows))
        r = self.ev(ch, at(DAY, "10:00"))
        self.assertEqual(r["gap_pct"], 3.0)
        self.assertEqual(r["gap_class"], "gap-up")
        self.assertTrue(r["gap_and_go"])
        self.assertTrue(any("Extra risk" in w for w in r["warnings"]))
        r = self.ev(ch, at(DAY, "10:00"), setup="gap")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["trigger"]["price"], 1038.5)
        r = self.ev(self.chart("TMPV", day_bars(DAY, rows=ORB_ROWS)), at(DAY, "10:00"), setup="gap")
        self.assertEqual(r["status"], "expired")
        self.assertIn("gap-and-go does not apply", r["reason"])
        filled = dict(rows)
        filled[2] = (1035, 1035.5, 999, 1033)
        r = self.ev(self.chart("TMPV", day_bars(DAY, 1030.0, rows=filled)), at(DAY, "10:00"), setup="gap")
        self.assertIn("gap filled", r["reason"])

    def test_vwap_reclaim(self):
        rows = {0: (1000, 1002, 998, 1001), 1: (1001, 1002, 999, 1000), 2: (1000, 1001, 999, 1000.5),
                3: (1000.5, 1000.6, 995, 996, 30000), 4: (996, 997, 995, 996.5), 6: (996.5, 1003, 996, 1002.5, 40000)}
        ch = self.chart("SBIN", day_bars(DAY, rows=rows))
        r = self.ev(ch, at(DAY, "10:00"), setup="vwap_reclaim")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["trigger"]["bar_index"], 6)
        self.assertEqual(r["trigger"]["vwap_side"], "above")
        self.assertLess(r["level"], 1002.5)
        nifty = self.chart("NIFTY", day_bars(DAY, 25000.0, volume=0), prev_close=25000.0)
        r = self.ev(nifty, at(DAY, "10:00"), setup="vwap_reclaim")
        self.assertEqual(r["status"], "no-data")

    def test_near_the_upper_circuit_a_long_is_blocked(self):
        rows = {0: (1030, 1034, 1028, 1032), 1: (1032, 1036, 1031, 1035), 2: (1035, 1035.5, 1030, 1033),
                5: (1033, 1040, 1032, 1038.5)}
        ch = self.chart("SMALLCO", day_bars(DAY, 1030.0, rows=rows))
        r = self.ev(ch, at(DAY, "10:00"), ctx={"band_pct": 5.0})
        self.assertEqual(r["circuit"], {"band_pct": 5.0, "upper": 1050.0, "lower": 950.0, "near": "upper"})
        self.assertEqual(r["status"], "expired")
        self.assertTrue(r["blocked"])
        self.assertIn("can lock at the upper circuit; you may not be able to exit", r["reason"])
        r = self.ev(ch, at(DAY, "10:00"), ctx={"band_pct": 10.0})
        self.assertEqual(r["status"], "triggered")
        self.assertIsNone(r["circuit"]["near"])

    def test_a_short_below_the_range(self):
        rows = dict(ORB_ROWS)
        rows[5] = (1003, 1004, 996, 997.0)
        r = self.ev(self.chart("INFY", day_bars(DAY, rows=rows)), at(DAY, "10:00"), side="sell")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["level"], 998.0)
        self.assertEqual(r["trigger"]["price"], 997.0)

    def test_side_rules(self):
        cfg = dict(M.DEFAULT_CONFIG)
        with self.assertRaisesRegex(M.MarketError, "allow_short"):
            E.check_side("sell", "MIS", "EQ", cfg, "INFY")
        cfg["allow_short"] = True
        self.assertEqual(E.check_side("short", "MIS", "EQ", cfg, "INFY"), "sell")
        with self.assertRaisesRegex(M.MarketError, "MIS"):
            E.check_side("sell", "CNC", "EQ", cfg, "INFY")
        with self.assertRaisesRegex(M.MarketError, "PE"):
            E.check_side("sell", "MIS", "CE", cfg, "NIFTY")
        self.assertEqual(E.check_side("sell", "MIS", "PE", dict(M.DEFAULT_CONFIG), "NIFTY"), "sell")

    def test_refusals_from_the_config_lists(self):
        cfg = dict(M.DEFAULT_CONFIG, blocked=["YESBANK"], asm_gsm=["PENNY"], fno_ban=["IDEA"])
        self.assertTrue(E.refusals("YESBANK", cfg)[0])
        self.assertIn("ASM/GSM", E.refusals("PENNY", cfg)[0][0])
        self.assertIn("trade-for-trade", E.refusals("XYZ", cfg, series="BE")[0][0])
        self.assertIn("F&O ban", E.refusals("IDEA", cfg, instrument="FUT")[0][0])
        bad, warn = E.refusals("IDEA", cfg, instrument="EQ")
        self.assertEqual(bad, [])
        self.assertTrue(warn)


class Watch(Base):
    def opts(self, **kw):
        o = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None,
             "side": "buy", "cfg": dict(M.DEFAULT_CONFIG), "ctx": {}}
        o.update(kw)
        return o

    def test_replays_bar_by_bar_and_stops_on_the_trigger(self):
        ch = self.chart("RELIANCE", day_bars(DAY, rows=ORB_ROWS))
        clock = E.SimClock(at(DAY, "09:05"))
        lines, fired = [], []
        code, res = E.watch_loop(lambda now: ch, clock.now, clock.sleep, lines.append, self.opts(),
                                 on_trigger=lambda r, n: fired.append(n))
        self.assertEqual(code, 0)
        self.assertIn("watching from the first bar's close at 9:20 AM", lines[0])
        self.assertTrue(any(l.startswith("09:25 bar") and "opening range set" in l for l in lines))
        self.assertTrue(any(l.startswith("09:40 bar") and "TRIGGERED" in l for l in lines))
        self.assertEqual(fired[0].strftime("%H:%M"), "09:45")

    def test_expires_until_closed_day_and_errors(self):
        flat = self.chart("RELIANCE", day_bars(DAY))
        clock = E.SimClock(at(DAY, "14:00"))
        code, res = E.watch_loop(lambda now: flat, clock.now, clock.sleep, [].append, self.opts())
        self.assertEqual((code, res["status"]), (1, "expired"))
        clock = E.SimClock(at(DAY, "10:00"))
        lines = []
        code, _ = E.watch_loop(lambda now: flat, clock.now, clock.sleep, lines.append, self.opts(),
                               until=at(DAY, "10:30"))
        self.assertIn("--until 10:30", lines[-1])
        hol = datetime.date(2026, 10, 20)
        lines = []
        clock = E.SimClock(at(hol, "10:00"))
        self.assertEqual(E.watch_loop(lambda now: flat, clock.now, clock.sleep, lines.append, self.opts())[0], 1)
        self.assertIn("Dussehra", lines[0])

        def boom(now):
            raise M.NetError("Yahoo Finance: HTTP 429")
        lines = []
        clock = E.SimClock(at(DAY, "10:00"))
        self.assertEqual(E.watch_loop(boom, clock.now, clock.sleep, lines.append, self.opts())[0], 1)
        self.assertIn("5 failed reads", lines[-1])


class FakeNet(object):
    def __init__(self, doc):
        self.doc, self.calls = doc, []

    def chart(self, symbol, rng="5d", interval="5m", exchange="NSE"):
        self.calls.append((symbol, rng, interval, exchange))
        return self.doc


class Cli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = E.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        Base.setUp(self)
        self.cfg = os.path.join(self.tmp, "config.json")
        I.write(self.cfg, {"broker": "zerodha"})
        self.file = I.write(os.path.join(self.tmp, "RELIANCE.json"),
                            I.chart_doc("RELIANCE", day_bars(DAY, rows=ORB_ROWS), prev_close=1000.0))

    def test_check_write_is_the_contract_shape(self):
        out = os.path.join(self.tmp, "trader", "entry.json")
        code, text, _ = self.run_cli("check", "RELIANCE", "--offline", self.file, "--now", "2026-10-28 09:46",
                                     "--config", self.cfg, "--write", out)
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:45 AM at Rs 1,008.40", text)
        self.assertIn("Nothing was placed", text)
        doc = rd(out)
        for k in ("created_by", "symbol", "exchange", "setup", "side", "date", "status", "time", "bar_start",
                  "bar_index", "price", "level", "interval", "opening_range", "vwap", "gap_pct", "source",
                  "checked_at", "reason"):
            self.assertIn(k, doc)
        self.assertEqual((doc["time"], doc["bar_start"], doc["bar_index"]), ("09:45", "09:40", 5))
        self.assertEqual(doc["opening_range"], {"high": 1006.0, "low": 998.0, "minutes": 15})
        self.assertTrue(doc["checked_at"].endswith("+05:30"))

    def test_waiting_json_and_exit_codes(self):
        code, text, _ = self.run_cli("check", "RELIANCE", "--offline", self.tmp, "--now", "2026-10-28 09:41",
                                     "--config", self.cfg, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["status"], "waiting")

    def test_errors_are_one_plain_line(self):
        cases = [("check", "RELIANCE", "--offline", self.file, "--side", "sell", "--config", self.cfg),
                 ("check", "TCS", "--offline", self.file, "--config", self.cfg),
                 ("check", "RELIANCE", "--offline", os.path.join(self.tmp, "nope.json"), "--config", self.cfg),
                 ("check", "RELIANCE", "--offline", self.file, "--now", "yesterday", "--config", self.cfg),
                 ("check", "RELIANCE", "--offline", self.file, "--band", "7", "--config", self.cfg)]
        for args in cases:
            code, _, err = self.run_cli(*args)
            self.assertEqual(code, 1, args)
            self.assertTrue(err.startswith("error: "), err)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
        I.write(self.cfg, {"asm_gsm": ["RELIANCE"]})
        code, _, err = self.run_cli("check", "RELIANCE", "--offline", self.file, "--config", self.cfg)
        self.assertIn("refused", err)

    def test_band_from_config_blocks_near_the_circuit(self):
        rows = {0: (1030, 1034, 1028, 1032), 1: (1032, 1036, 1031, 1035), 2: (1035, 1035.5, 1030, 1033),
                5: (1033, 1040, 1032, 1038.5)}
        f = I.write(os.path.join(self.tmp, "SMALLCO.json"),
                    I.chart_doc("SMALLCO", day_bars(DAY, 1030.0, rows=rows), prev_close=1000.0))
        I.write(self.cfg, {"bands": {"SMALLCO": 5}})
        code, text, _ = self.run_cli("check", "SMALLCO", "--offline", f, "--now", "2026-10-28 10:00",
                                     "--config", self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", text)

    def test_watch_replays_the_file_and_writes(self):
        out = os.path.join(self.tmp, "e.json")
        code, text, _ = self.run_cli("watch", "RELIANCE", "--offline", self.file, "--now", "2026-10-28 09:10",
                                     "--config", self.cfg, "--out", out)
        self.assertEqual(code, 0)
        self.assertEqual(rd(out)["status"], "triggered")

    def test_watchlist(self):
        I.write(os.path.join(self.tmp, "TCS.json"), I.chart_doc("TCS", day_bars(DAY, 3000.0), prev_close=3000.0))
        wl = I.write(os.path.join(self.tmp, "watchlist.json"), {"hits": [
            {"symbol": "RELIANCE", "setup": "orb"}, {"symbol": "TCS"}, {"symbol": "JUNK", "series": "BE"}]})
        code, text, _ = self.run_cli("watchlist", "--watchlist", wl, "--offline", self.tmp, "--now",
                                     "2026-10-28 10:00", "--config", self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("1 of 3 triggered", text)
        self.assertRegex(text, r"JUNK\s+error: refused: .*trade-for-trade")

    def test_network_path_uses_market_net(self):
        doc = I.chart_doc("RELIANCE", day_bars(DAY, rows=ORB_ROWS), prev_close=1000.0)
        fake = FakeNet(doc)
        old = E.NET_FACTORY
        E.NET_FACTORY = lambda: fake
        try:
            ch = E.get_chart("RELIANCE", "5m", DAY, DAY, "NSE")
        finally:
            E.NET_FACTORY = old
        self.assertEqual(fake.calls, [("RELIANCE", "5d", "5m", "NSE")])
        self.assertEqual(len(ch["bars"]), 75)
        self.assertEqual(E.norm_symbol("reliance.bo", "NSE"), ("RELIANCE", "BSE"))
        self.assertEqual(E.norm_symbol("^NSEI", "NSE"), ("NIFTY", "NSE"))
        self.assertEqual(E.norm_symbol("M&M", "NSE"), ("M&M", "NSE"))


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        text = rd(os.path.join(ROOT, "trader-entry", "SKILL.md"), raw=True)
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertIn("name: trader-entry", fm)
        self.assertGreater(len(fm), 200)
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("not financial advice", "yes", "09:15", "nse", "unofficial", "expiry", "circuit"):
            self.assertIn(must, low)
        for f in ("SKILL.md", "entry.py"):
            self.assertNotIn(chr(0x2014), rd(os.path.join(ROOT, "trader-entry", f), raw=True))


if __name__ == "__main__":
    unittest.main()
