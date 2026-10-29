"""trader-exit (India): stops and targets on the NSE tick, Indian round-trip charges,
flat_by vs broker auto square-off, CNC swing plans, option and futures plans,
expiry-day physical settlement, circuit caps. Offline only."""

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
X = load("trader-exit", "exit")
DAY = datetime.date(2026, 10, 28)


def rd(path, raw=False):
    with open(path, encoding="utf-8") as fh:
        return fh.read() if raw else json.load(fh)


class Math(unittest.TestCase):
    def test_or_mid_on_the_tick_and_2r(self):
        c = X.compute(1008.4, "or_mid", None, "r", 2.0, "buy", "equity", 1006.0, 998.05)
        # (1006 + 998.05) / 2 = 1002.025 -> rounded DOWN to the 0.10 tick (prices 1,000-5,000)
        self.assertEqual((c["entry"], c["stop"], c["risk"], c["target"], c["reward_risk"]),
                         (1008.4, 1002.0, 6.4, 1021.2, 2.0))
        self.assertIn("rounded down to the tick", c["stop_detail"])

    def test_ticks_by_price_band_and_safe_directions(self):
        c = X.compute(120.37, "pct", 1.0, "r", 2.0)                 # under Rs 250: 0.01 tick
        self.assertEqual((c["stop"], c["risk"], c["target"]), (119.16, 1.21, 122.79))
        c = X.compute(500.0, "price", 497.37, "r", 1.5)             # 250-1000: 0.05 tick
        self.assertEqual(c["stop"], 497.35)                          # away from the entry
        self.assertEqual(c["target"], 503.95)                        # 503.975 toward the entry
        c = X.compute(1000.0, "pct", 1.0, "r", 2.0, side="sell")
        self.assertEqual((c["stop"], c["target"]), (1010.0, 980.0))
        c = X.compute(1000.03, "pct", 1.0, "r", 2.0, side="sell")    # a short's entry rounds down
        self.assertEqual(c["entry"], 1000.0)
        c = X.compute(120.03, "price", 90, "r", 2.0, segment="options")   # options: 0.05
        self.assertEqual((c["entry"], c["stop"], c["target"]), (120.05, 90.0, 180.15))

    def test_other_stops_and_targets(self):
        self.assertEqual(X.compute(1008.4, "or_low", None, "r", 1.0, or_high=1006, or_low=998)["stop"], 998.0)
        c = X.compute(1008.4, "atr", 1.5, "r", 2.0, atr=2.0, atr_bars=7)
        self.assertEqual(c["stop"], 1005.4)
        self.assertIn("fewer than 14", c["stop_detail"])
        self.assertEqual(X.compute(1008.4, "vwap", None, "r", 2.0, vwap=1004.23)["stop"], 1004.1)
        c = X.compute(1008.4, "prev_day_low", None, "prev_day_high", None, prev_high=1030.27, prev_low=995.5)
        self.assertEqual((c["stop"], c["target"]), (995.45, 1030.2))     # one 0.05 tick under 995.50
        self.assertEqual(X.compute(1008.4, "pct", 1, "pct", 3)["target"], 1038.6)

    def test_target_capped_at_the_upper_circuit(self):
        lim = M.band_limits(1000.0, 5)
        c = X.compute(1040.0, "price", 1030.0, "r", 2.0, limits=lim)
        self.assertEqual(c["target"], 1050.0)
        self.assertEqual(c["capped"], {"from": 1060.0, "to": 1050.0})
        self.assertIn("capped at the upper circuit", c["target_label"])
        self.assertEqual(c["reward_risk"], 1.0)

    def test_refusals_say_why(self):
        for args, msg in (((1000.0, "price", 1001, "r", 2.0), "at or above the entry"),
                          ((1000.0, "price", 990, "price", 999), "at or below the entry"),
                          ((1000.0, "price", 999.9, "r", 2.0), "too tight"),
                          ((1000.0, "pct", 1, "r", 2.0, "sell", "equity", None, None, None, None, None, None, None,
                            M.band_limits(1000, 2)), None)):
            if msg is None:
                c = X.compute(*args)
                self.assertEqual(c["target"], 980.0)                   # the 2% lower circuit is 980
                continue
            with self.assertRaisesRegex(M.MarketError, msg):
                X.compute(*args)
        with self.assertRaisesRegex(M.MarketError, "or-high"):
            X.parse_stop("or-low", long=False)
        with self.assertRaisesRegex(M.MarketError, "prev-day-high"):
            X.parse_target("prev-day-low", long=True)
        self.assertEqual(X.parse_stop("1163.2"), ("price", 1163.2))
        self.assertEqual(X.parse_target("1.5R"), ("r", 1.5))

    def test_costs_and_net_reward_risk(self):
        cfg = dict(M.DEFAULT_CONFIG)
        q, basis = X.ref_quantity(1008.4, 6.4, cfg)
        self.assertEqual(q, 79)                         # 1,000 / 6.4 = 156, capped by 80,000 / 1,008.4 = 79
        co = X.plan_costs(1008.4, 1002.0, 1021.2, "buy", "intraday", q, "NSE", cfg)
        rt = M.round_trip("intraday", 1008.4, 1021.2, 79, "NSE", "zerodha", cfg)
        self.assertEqual(co["at_target"], rt["total"])
        self.assertAlmostEqual(co["net_reward_risk"],
                               round((12.8 - rt["total"] / 79) / (6.4 + co["at_stop"] / 79), 2), places=2)
        self.assertLess(co["net_reward_risk"], 2.0)
        self.assertEqual(X.ref_quantity(120, 30, cfg, lot=65), (65, "1 lot of 65"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = I.write(os.path.join(self.tmp, "config.json"), {"broker": "zerodha"})
        self.entry = I.write(os.path.join(self.tmp, "entry.json"), {
            "created_by": "trader-entry", "symbol": "RELIANCE", "exchange": "NSE", "setup": "orb", "side": "buy",
            "date": "2026-10-28", "status": "triggered", "time": "09:45", "bar_start": "09:40", "bar_index": 5,
            "price": 1008.4, "level": 1006.0, "interval": "5m",
            "opening_range": {"high": 1006.0, "low": 998.05, "minutes": 15}, "vwap": 1004.23, "gap_pct": 0.0,
            "prev_close": 1000.0, "product": "MIS", "source": "yahoo", "checked_at": "2026-10-28T09:46:00+05:30"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = X.main(list(args) + ["--config", self.cfg] if args[0] == "plan" else list(args))
        return code, out.getvalue(), err.getvalue()

    def plan(self, *extra):
        out = os.path.join(self.tmp, "trader", "plan.json")
        code, text, err = self.run_cli("plan", "--from", self.entry, "--now", "2026-10-28 09:46", "--write", out,
                                       *extra)
        self.assertEqual(code, 0, err)
        return rd(out), text

    def test_entry_to_plan_is_the_contract_shape(self):
        p, text = self.plan()
        for k in ("created_by", "symbol", "exchange", "instrument", "product", "side", "date", "setup", "entry",
                  "entry_time", "entry_bar", "interval", "opening_range", "stop", "stop_rule", "stop_label",
                  "target", "target_r", "target_label", "risk_per_share", "reward_risk", "tick", "flat_by",
                  "costs_per_share", "net_reward_risk", "circuit"):
            self.assertIn(k, p)
        self.assertEqual((p["entry"], p["stop"], p["target"], p["risk_per_share"]), (1008.4, 1002.0, 1021.2, 6.4))
        self.assertEqual((p["entry_time"], p["entry_bar"], p["tick"], p["flat_by"]), ("09:45", 5, 0.1, "15:10"))
        self.assertEqual(p["instrument"], {"type": "EQ"})
        self.assertEqual(p["circuit"], {"band_pct": None, "upper": None, "lower": None})
        self.assertGreater(p["costs_per_share"], 0)
        self.assertIn("Rs 1,008.40", text)
        self.assertIn("Not financial advice", text)

    def test_vwap_stop_from_the_entry_and_circuit_from_config(self):
        I.write(self.cfg, {"bands": {"RELIANCE": 2}})
        p, text = self.plan("--stop", "vwap", "--target", "3R")
        self.assertEqual(p["stop"], 1004.1)
        self.assertEqual(p["circuit"], {"band_pct": 2.0, "upper": 1020.0, "lower": 980.0})
        self.assertEqual(p["target"], 1020.0)
        self.assertIn("capped", p["target_label"])

    def test_atr_vwap_and_prev_day_from_the_bars(self):
        prev = I.walk_session(datetime.date(2026, 10, 27), 1000.0, 5)
        today = I.walk_session(DAY, 1003.0, 6)
        f = I.write(os.path.join(self.tmp, "RELIANCE.json"), I.chart_doc("RELIANCE", prev + today, 1000.0))
        high = max(b[2] for b in prev)
        p, _ = self.plan("--stop", "atr:1.5", "--target", "prev-day-high", "--offline", f, "--entry",
                         "%.1f" % (high - 5))
        self.assertEqual(p["stop_rule"], "atr")
        self.assertIn("atr", p)
        self.assertEqual(p["prev_day"]["date"], "2026-10-27")
        self.assertEqual(p["prev_day"]["high"], round(high, 2))
        self.assertEqual(p["target"], M.round_tick(high, mode="down"))
        code, text, err = self.run_cli("plan", "--symbol", "RELIANCE", "--entry", "%.1f" % (high + 30), "--stop",
                                       "vwap", "--offline", f, "--date", "2026-10-28", "--entry-bar", "10",
                                       "--now", "2026-10-28 15:00", "--json")
        self.assertEqual(code, 0, err)
        sub = [b for b in I.walk_session(DAY, 1003.0, 6)[:11]]
        vw = sum((b[2] + b[3] + b[4]) / 3.0 * b[5] for b in sub) / sum(b[5] for b in sub)
        self.assertAlmostEqual(json.loads(text)["vwap"], round(vw, 2), places=2)
        code, _, err = self.run_cli("plan", "--from", self.entry, "--stop", "vwap", "--vwap", "0")
        self.assertIn("--vwap", err)

    def test_flat_by_never_after_the_broker_square_off(self):
        I.write(self.cfg, {"flat_by": "15:28"})
        p, text = self.plan()
        self.assertEqual(p["flat_by"], "15:20")
        self.assertEqual(p["auto_square_off"], "15:25")
        self.assertIn("auto square-off", text)
        code, _, err = self.run_cli("plan", "--from", self.entry, "--now", "2026-10-28 09:46", "--flat-by", "15:25")
        self.assertEqual(code, 1)
        self.assertIn("auto square-off at 15:25", err)
        I.write(self.cfg, {"broker": "angelone"})
        code, _, err = self.run_cli("plan", "--from", self.entry, "--now", "2026-10-28 09:46", "--flat-by", "15:15")
        self.assertIn("15:15", err)
        code, _, err = self.run_cli("plan", "--from", self.entry, "--now", "2026-10-28 09:46", "--time", "15:12")
        self.assertIn("no time left", err)

    def test_cnc_swing_has_no_flat_by_but_valid_until(self):
        out = os.path.join(self.tmp, "p.json")
        code, text, err = self.run_cli("plan", "--symbol", "TCS", "--entry", "3120.5", "--stop", "pct:2",
                                       "--product", "CNC", "--date", "2026-11-06", "--now", "2026-11-06 10:00",
                                       "--write", out)
        self.assertEqual(code, 0, err)
        p = rd(out)
        self.assertIsNone(p["flat_by"])
        self.assertEqual(p["valid_until"], "2026-11-16")      # 9 Nov, 11 Nov (10th a holiday), 12, 13, 16
        self.assertEqual(p["costs"]["segment"], "delivery")
        self.assertGreater(p["costs"]["dp"], 0)
        self.assertIn("gap straight through the stop", text)

    def test_index_option_plan(self):
        out = os.path.join(self.tmp, "o.json")
        code, text, err = self.run_cli("plan", "--instrument", "CE", "--underlying", "NIFTY", "--expiry",
                                       "2026-11-03", "--strike", "22700", "--entry", "120", "--stop", "90",
                                       "--now", "2026-11-03 10:00", "--write", out)
        self.assertEqual(code, 0, err)
        p = rd(out)
        self.assertEqual(p["instrument"], {"type": "CE", "underlying": "NIFTY", "expiry": "2026-11-03",
                                           "strike": 22700, "lot_size": 65, "tradingsymbol": "NIFTY26N0322700CE"})
        self.assertEqual((p["tick"], p["target"], p["costs"]["ref_qty"], p["costs"]["segment"]),
                         (0.05, 180.0, 65, "options"))
        self.assertEqual(p["auto_square_off"], "15:26")
        self.assertIn("theta", text)
        self.assertIn("settle in cash", text)
        self.assertFalse(p["flat_by_mandatory"])

    def test_stock_option_on_expiry_is_physically_settled(self):
        out = os.path.join(self.tmp, "o.json")
        code, text, err = self.run_cli("plan", "--instrument", "CE", "--underlying", "RELIANCE", "--expiry",
                                       "2026-11-23", "--strike", "1200", "--entry", "0.3", "--stop", "0.2",
                                       "--product", "NRML", "--now", "2026-11-23 10:00", "--write", out)
        self.assertEqual(code, 0, err)
        p = rd(out)
        self.assertTrue(p["flat_by_mandatory"])
        self.assertEqual(p["flat_by"], "15:10")
        self.assertEqual(p["instrument"]["tradingsymbol"], "RELIANCE26NOV1200CE")
        self.assertIn("PHYSICALLY SETTLED", text)
        self.assertIn("charges eat", text)                         # a cheap option: costs bite

    def test_futures_plan_and_chain_ltp(self):
        # NIFTY's November monthly expiry: the last Tuesday (24 Nov) is a holiday, so Monday 23 Nov
        chain = I.write(os.path.join(self.tmp, "chain.json"), {"symbol": "NIFTY", "expiry": "2026-11-23",
                                                               "strikes": [{"strike": 22700, "CE": {"ltp": 150.3}}]})
        out = os.path.join(self.tmp, "o.json")
        code, _, err = self.run_cli("plan", "--instrument", "CE", "--underlying", "NIFTY", "--expiry", "2026-11-23",
                                    "--strike", "22700", "--chain", chain, "--stop", "pct:30",
                                    "--now", "2026-10-28 10:00", "--write", out)
        self.assertEqual(code, 0, err)
        self.assertEqual(rd(out)["entry"], 150.3)
        self.assertEqual(rd(out)["instrument"]["tradingsymbol"], "NIFTY26NOV22700CE")
        code, text, err = self.run_cli("plan", "--instrument", "FUT", "--underlying", "BANKNIFTY", "--expiry",
                                       "2026-11-23", "--entry", "52000", "--stop", "51800", "--now",
                                       "2026-10-28 10:00", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual(p["instrument"]["tradingsymbol"], "BANKNIFTY26NOVFUT")
        self.assertEqual((p["instrument"]["lot_size"], p["costs"]["segment"]), (30, "futures"))
        self.assertTrue(any("allow_futures" in w for w in p["warnings"]))
        code, text, err = self.run_cli("plan", "--instrument", "FUT", "--underlying", "BANKNIFTY", "--expiry",
                                       "2026-11-24", "--entry", "52000", "--stop", "51800", "--now",
                                       "2026-10-28 10:00", "--json")
        self.assertTrue(any("not an expiry" in w for w in json.loads(text)["warnings"]))

    def test_refusals_from_the_cli(self):
        cases = [(("--from", os.path.join(self.tmp, "nope.json")), "not found"),
                 (("--symbol", "NIFTY", "--entry", "25000", "--stop", "pct:1"), "index"),
                 (("--symbol", "INFY", "--entry", "1500", "--side", "sell", "--stop", "pct:1"), "allow_short"),
                 (("--symbol", "INFY", "--entry", "1500", "--stop", "pct:1", "--date", "2026-10-20"), "Dussehra"),
                 (("--symbol", "INFY", "--entry", "1500", "--stop", "pct:1", "--product", "NRML"), "NRML"),
                 (("--instrument", "CE", "--underlying", "NIFTY", "--expiry", "2026-11-03", "--strike", "22700",
                   "--entry", "100"), "premium stop"),
                 (("--instrument", "FUT", "--underlying", "NIFTY", "--expiry", "2026-10-27", "--entry", "25000",
                   "--stop", "24900", "--product", "CNC"), "not CNC"),
                 (("--instrument", "FUT", "--underlying", "NIFTY", "--expiry", "2026-10-27", "--entry", "25000",
                   "--stop", "24900"), "expired"),
                 (("--symbol", "INFY", "--entry", "1500", "--setup", "level"), "give --stop")]
        for args, msg in cases:
            code, _, err = self.run_cli("plan", "--now", "2026-10-28 10:00", *args)
            self.assertEqual(code, 1, args)
            self.assertTrue(err.startswith("error: "), err)
            self.assertIn(msg, err, args)
        I.write(self.entry, {"symbol": "RELIANCE", "status": "waiting", "price": None})
        code, _, err = self.run_cli("plan", "--from", self.entry)
        self.assertIn("not triggered", err)

    def test_short_plan_when_allowed(self):
        I.write(self.cfg, {"allow_short": True})
        code, text, err = self.run_cli("plan", "--symbol", "INFY", "--entry", "1500", "--side", "sell", "--stop",
                                       "pct:1", "--now", "2026-10-28 10:00", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["side"], p["stop"], p["target"]), ("sell", 1515.0, 1470.0))

    def test_show(self):
        code, text, _ = self.run_cli("show", "--plan", os.path.join(self.tmp, "none.json"))
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)
        p, _ = self.plan()
        code, text, _ = self.run_cli("show", "--plan", os.path.join(self.tmp, "trader", "plan.json"),
                                     "--now", "2026-10-29 09:00")
        self.assertEqual(code, 0)
        self.assertIn("not today", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        text = rd(os.path.join(ROOT, "trader-exit", "SKILL.md"), raw=True)
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertIn("name: trader-exit", fm)
        self.assertGreater(len(fm), 200)
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("not financial advice", "yes", "tick", "square-off", "physically settled", "stt", "unofficial"):
            self.assertIn(must, low)
        for f in ("SKILL.md", "exit.py"):
            self.assertNotIn(chr(0x2014), rd(os.path.join(ROOT, "trader-exit", f), raw=True))


if __name__ == "__main__":
    unittest.main()
