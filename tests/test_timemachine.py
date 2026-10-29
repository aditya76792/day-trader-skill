"""trader-timemachine (India): 75-bar sessions from 09:15, NSE-holiday-aware pool,
seeded replays, net expectancy after Indian charges and slippage, flat_by exits,
CNC overnight gap risk, history with costs, and the new backtest. Offline only."""

import contextlib
import datetime
import io
import json
import os
import random
import re
import shutil
import tempfile
import unittest

import _india as I
from _load import ROOT, load

M = I.M
T = load("trader-timemachine", "timemachine")
DAY = datetime.date(2026, 10, 28)
NOW = datetime.datetime(2026, 10, 28, 15, 45)
CFG = dict(M.DEFAULT_CONFIG)


def rd(path, raw=False):
    with open(path, encoding="utf-8") as fh:
        return fh.read() if raw else json.load(fh)


def gappy_history(day, n=60, start=1000.0, seed=7, gap_vol=0.008, big_gaps=(), today_until="15:30"):
    """n sessions before `day` with real overnight gaps (big_gaps: {index: pct}), plus today's bars."""
    rnd = random.Random(seed)
    bars, px = [], start
    for i, d in enumerate(I.trading_days_before(day, n)):
        g = dict(big_gaps).get(i, rnd.gauss(0, gap_vol) * 100)
        s = I.walk_session(d, px * (1 + g / 100.0), seed + i)
        bars += s
        px = s[-1][4]
    if today_until:
        cut = datetime.datetime.combine(day, M.parse_hhmm(today_until))
        bars += [b for b in I.walk_session(day, px, seed + 999) if b[0] < cut]
    return bars


def plan_for(bars, k=6, stop_pct=0.6, r=2.0, side="buy", **kw):
    """A plan on today's bar k close."""
    today = [b for b in bars if b[0].date() == DAY]
    e = round(today[k][4], 2)
    risk = round(e * stop_pct / 100.0, 2)
    long = side == "buy"
    raw = {"symbol": "RELIANCE", "side": side, "date": DAY.isoformat(), "entry": e,
           "stop": round(e - risk if long else e + risk, 2), "target": round(e + r * risk if long else e - r * risk, 2),
           "entry_time": T.clock24(T.slot_close(k)), "entry_bar": k, "setup": "orb",
           "opening_range": {"minutes": 15}, "stop_rule": "pct", "product": "MIS", "exchange": "NSE",
           "flat_by": "15:10", "instrument": {"type": "EQ"}}
    raw.update(kw)
    return raw


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = gappy_history(DAY, big_gaps={10: -4.0, 25: -5.0, 40: 3.5})
        cls.doc = I.chart_doc("RELIANCE", cls.bars, prev_close=None)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tm(self, raw=None, replays=2000, seed=19, until=None, cfg=None, doc=None, **kw):
        plan = T.normalize_plan(raw or plan_for(self.bars, **kw))
        return T.time_machine(doc or self.doc, plan, NOW, replays, seed, 2, until, "offline", cfg or CFG)


class Sessions(Base):
    def test_75_bars_from_0915_and_the_pool(self):
        self.assertEqual(T.SESSION_BARS, 75)
        self.assertEqual(T.clock24(T.OPEN_MIN), "09:15")
        _, bars, dropped = T.parse_bars(self.doc, "RELIANCE", "NSE", NOW)
        sessions, short = T.full_sessions(T.split_sessions(bars), DAY.isoformat())
        self.assertEqual(len(sessions), 60)
        self.assertEqual(short, [])
        self.assertTrue(all(len(D) == 75 for _, D in sessions))
        self.assertEqual(sessions[0][1][0]["start"], "09:15")
        self.assertEqual(sessions[0][1][-1]["start"], "15:25")
        pool = T.build_pool(sessions)
        self.assertEqual((len(pool), len(pool[0]), len(pool[1])), (75, 0, 60))

    def test_holidays_and_short_sessions_are_left_out(self):
        self.assertNotIn("2026-10-20", {b[0].date().isoformat() for b in self.bars})     # Dussehra
        extra = I.walk_session(datetime.date(2026, 10, 20), 1000.0, 1)                  # a stray holiday session
        broken = [b for b in self.bars if not (b[0].date() == datetime.date(2026, 10, 14) and b[0].hour == 12)]
        doc = I.chart_doc("RELIANCE", sorted(broken + extra), prev_close=None)
        _, bars, _ = T.parse_bars(doc, "RELIANCE", "NSE", NOW)
        sessions, short = T.full_sessions(T.split_sessions(bars), DAY.isoformat())
        self.assertEqual(len(sessions), 59)
        why = {x["date"]: x["why"] for x in short}
        self.assertIn("not an NSE trading day", why["2026-10-20"])
        self.assertIn("short session", why["2026-10-14"])

    def test_overnight_gaps_only_between_consecutive_trading_days(self):
        _, bars, _ = T.parse_bars(self.doc, "RELIANCE", "NSE", NOW)
        sessions, _ = T.full_sessions(T.split_sessions(bars), DAY.isoformat())
        gaps = T.overnight_gaps(sessions)
        self.assertEqual(len(gaps), 59)
        self.assertAlmostEqual(min(gaps), 0.95, places=2)
        # 2026-10-19 -> 2026-10-21 skips the Dussehra holiday and still counts as consecutive
        self.assertEqual(M.next_trading_day(datetime.date(2026, 10, 19)), datetime.date(2026, 10, 21))
        no_hol = [s for s in sessions if s[0] != "2026-10-21"]
        self.assertEqual(len(T.overnight_gaps(no_hol)), 57)

    def test_in_progress_bar_is_dropped_and_today_not_in_the_pool(self):
        doc = I.chart_doc("RELIANCE", gappy_history(DAY, today_until="10:00"), prev_close=None)
        _, bars, dropped = T.parse_bars(doc, "RELIANCE", "NSE", datetime.datetime(2026, 10, 28, 9, 57))
        self.assertEqual(dropped["not_complete"], 1)                 # the 09:55 bar is still forming
        self.assertEqual(bars[-1]["start"], "09:50")
        sessions, _ = T.full_sessions(T.split_sessions(bars), DAY.isoformat())
        self.assertNotIn(DAY.isoformat(), [d for d, _ in sessions])


class Replays(Base):
    def test_same_seed_same_numbers_other_seed_other_numbers(self):
        a, b, c = self.tm(seed=19), self.tm(seed=19), self.tm(seed=7)
        self.assertEqual(a["mc"], b["mc"])
        self.assertEqual(a["costs"], b["costs"])
        self.assertNotEqual(a["mc"]["p_target"], c["mc"]["p_target"])
        m = a["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=3)
        self.assertEqual(m["bands"][0], [a["plan"]["entry"]] * 5)

    def test_flat_by_ends_the_replay(self):
        d = self.tm()
        self.assertEqual(d["mc"]["until"], "15:10")
        self.assertEqual(d["mc"]["bars_left"], 70 - 6)                # the 15:05 bar closes at 15:10
        d = self.tm(until="15:30")
        self.assertEqual(d["mc"]["bars_left"], 74 - 6)
        with self.assertRaises(M.MarketError):
            self.tm(until="15:35")
        with self.assertRaises(M.MarketError):
            self.tm(until="09:45")

    def test_costs_make_the_net_worse_than_the_gross(self):
        d = self.tm(replays=3000)
        co = d["costs"]
        self.assertEqual(co["segment"], "intraday")
        self.assertGreater(co["costs_per_share"], 0)
        self.assertLess(co["net_expectancy_r"], d["mc"]["avg_R"])
        self.assertEqual(d["net_expectancy_r"], co["net_expectancy_r"])
        self.assertEqual(d["target_first"], d["mc"]["p_target"])
        self.assertTrue(any("After charges and slippage" in n for n in d["verdict_notes"]))
        # with no slippage a trade to the target nets exactly the round trip of market.py
        plan = d["plan"]
        net = T.net_of_costs(["target"], [plan["target"]], dict(plan, exchange="NSE"), 50, "intraday", CFG, 0.0)
        rt = M.round_trip("intraday", plan["entry"], plan["target"], 50, "NSE", "zerodha", CFG)
        self.assertEqual(net["net_expectancy_inr"], rt["net_pnl"])

    def test_cnc_swing_draws_overnight_gaps(self):
        raw = plan_for(self.bars, stop_pct=1.5, r=4.0, product="CNC", flat_by=None)
        d = self.tm(raw=raw, replays=4000)
        g = d["gap_risk"]
        self.assertEqual(d["mc"]["until"], "15:30")
        self.assertEqual(d["costs"]["segment"], "delivery")
        self.assertEqual(g["gaps_in_data"], 59)
        self.assertGreater(g["held_overnight"], 0)
        self.assertGreater(g["gap_through_stop"], 0)                  # the -4% and -5% gaps go through a 1.5% stop
        self.assertEqual(g["p_gap_through_stop"], round(g["gap_through_stop"] / 4000.0, 4))
        self.assertGreaterEqual(g["historical_gaps_past_stop_distance"], 2 / 59.0)   # at least the -4% and -5%
        self.assertEqual(g["worst_gap_pct"], -5.0)
        self.assertIn("Overnight (CNC)", "\n".join(T.report(d)))
        self.assertIsNone(self.tm()["gap_risk"])

    def test_a_sell_plan_mirrors(self):
        d = self.tm(side="sell")
        self.assertEqual(d["plan"]["side"], "sell")
        self.assertGreater(d["mc"]["p_target"] + d["mc"]["p_stop"], 0)

    def test_history_with_costs(self):
        raw = plan_for(self.bars, stop_rule="or_mid")
        today = [b for b in self.bars if b[0].date() == DAY]
        oh, ol = max(b[2] for b in today[:3]), min(b[3] for b in today[:3])
        raw["stop"] = M.round_tick((oh + ol) / 2, mode="down")
        if raw["stop"] >= raw["entry"]:
            raw["stop"] = round(raw["entry"] - 3, 2)
            raw["stop_rule"] = "pct"
        raw["target"] = round(raw["entry"] + 2 * (raw["entry"] - raw["stop"]), 2)
        d = self.tm(raw=raw)
        h = d["history"]
        self.assertEqual(h["sessions"], 60)
        self.assertGreater(h["triggered"], 0)
        for t in h["trades"]:
            self.assertLess(t["net"], t["gross"])
            self.assertGreaterEqual(t["time"], "09:30")
        self.assertIn("History: the same rule (orb", "\n".join(T.report(d)))

    def test_level_setup_skips_history_and_options_are_refused(self):
        d = self.tm(raw=plan_for(self.bars, setup="level"))
        self.assertIn("skipped", d["history"]["note"])
        with self.assertRaisesRegex(M.MarketError, "option premium"):
            T.normalize_plan(plan_for(self.bars, instrument={"type": "CE", "underlying": "NIFTY"}))


class PlanChecks(Base):
    def test_plan_checks(self):
        good = plan_for(self.bars)
        bad = [dict(good, stop=good["entry"] + 1), dict(good, side="hold"), dict(good, interval="1m"),
               dict(good, entry_time="09:52", entry_bar=None), dict(good, entry_time=None, entry_bar=None),
               dict(good, entry_time="15:30", entry_bar=None), dict(good, symbol="not a symbol"),
               dict(good, date="28/10/2026"), dict(good, entry_bar=9)]
        for raw in bad:
            with self.assertRaises(M.MarketError):
                T.normalize_plan(raw)
        p = T.normalize_plan(dict(good, entry_time="9:50 AM"))
        self.assertEqual((p["entry_time"], p["entry_bar"]), ("09:50", 6))

    def test_data_problems(self):
        with self.assertRaisesRegex(M.MarketError, "not in the data"):
            self.tm(raw=dict(plan_for(self.bars), date="2026-10-29"))
        with self.assertRaisesRegex(M.MarketError, "not an NSE trading day"):
            self.tm(raw=dict(plan_for(self.bars), date="2026-10-20"))
        few = I.chart_doc("RELIANCE", gappy_history(DAY, n=10), prev_close=None)
        with self.assertRaisesRegex(M.MarketError, "at least 20"):
            self.tm(doc=few)
        with self.assertRaisesRegex(M.MarketError, "plan is for RELIANCE"):
            self.tm(doc=I.chart_doc("TCS", self.bars))


class Verdict(unittest.TestCase):
    def mc(self, pt, ps, r=1.0, avg=0.0):
        return {"p_target": pt, "p_stop": ps, "p_neither": round(1 - pt - ps, 4), "r_at_target": r, "avg_R": avg,
                "until": "15:10"}

    def test_thresholds(self):
        self.assertTrue(T.verdict(self.mc(0.30, 0.28)).startswith("Close to a coin flip"))
        self.assertTrue(T.verdict(self.mc(0.40, 0.30)).startswith("Leans toward the target"))
        self.assertTrue(T.verdict(self.mc(0.20, 0.60)).startswith("Strongly toward the stop"))
        self.assertTrue(T.verdict(self.mc(0.24, 0.50, r=2.0)).startswith("Close to break-even"))
        self.assertIn("No replay", T.verdict(self.mc(0.0, 0.0)))
        notes = T.verdict_notes(self.mc(0.30, 0.20, avg=0.05),
                                {"net_expectancy_r": -0.12, "net_expectancy_inr": -150.0})
        self.assertTrue(any("hit neither" in n for n in notes))
        self.assertTrue(any("turns negative" in n for n in notes))


def scripted(day, kind, base=1000.0, gap=0.0):
    """A full session: the 998-1006 opening range, a close above at 09:45 (bar 5), then a win or a loss."""
    o = base * (1 + gap / 100.0)
    sh = o - base
    rows = {0: (1000, 1004, 998, 1002), 1: (1002, 1006, 1001, 1005), 2: (1005, 1005.5, 1000, 1003),
            3: (1003, 1005, 1002, 1004), 4: (1004, 1005.9, 1003, 1005), 5: (1006, 1010, 1005, 1008.4)}
    if kind == "win":
        rows[8] = (1008.4, 1022, 1008, 1020)
    else:
        rows[7] = (1008.4, 1008.5, 1001, 1001.5)
    out, c = [], base
    for i, t in enumerate(I.session_times(day)):
        if i in rows:
            r = rows[i]
            row = (t, r[0] + sh, r[1] + sh, r[2] + sh, r[3] + sh, 10000)
        else:
            row = (t, c, c + 0.2, c - 0.2, c, 10000)
        out.append(row)
        c = row[4]
    return out


class Backtest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.days = I.trading_days_before(DAY, 30)
        bars = []
        for i, d in enumerate(self.days):
            bars += scripted(d, "win" if i % 3 == 0 else "loss")
        I.write(os.path.join(self.tmp, "RELIANCE.json"), I.chart_doc("RELIANCE", bars))
        self.rule = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exact_trades_charges_and_drawdown(self):
        doc = T.backtest(["RELIANCE"], self.rule, "buy", CFG, NOW, offline=self.tmp)
        s = doc["per_symbol"]["RELIANCE"]
        self.assertEqual((s["sessions"], s["trades"], s["target"], s["stop"]), (30, 30, 10, 20))
        win = T.trade_pnl(1008.4, 1021.2, 79, True, "intraday", "NSE", CFG, 0.05)
        loss = T.trade_pnl(1008.4, 1002.0, 79, True, "intraday", "NSE", CFG, 0.05)
        t0 = doc["trades"][0]
        self.assertEqual((t0["time"], t0["entry"], t0["stop"], t0["target"], t0["qty"]), ("09:45", 1008.4, 1002.0,
                                                                                         1021.2, 79))
        self.assertEqual((t0["net"], t0["charges"]), (win["net"], win["charges"]))
        self.assertEqual(s["total_charges"], round(10 * win["charges"] + 20 * loss["charges"], 2))
        self.assertEqual(s["net_pnl"], round(10 * win["net"] + 20 * loss["net"], 2))
        self.assertEqual(s["win_rate"], round(10 / 30.0, 4))
        eq = peak = dd = 0.0
        for t in doc["trades"]:
            eq += t["net"]
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        self.assertEqual(s["max_drawdown"], round(dd, 2))
        self.assertEqual(s["charges_pct_of_gross_profit"],
                         round(100 * s["total_charges"] / (10 * win["gross"]), 1))
        self.assertNotIn("2026-10-20", [t["date"] for t in doc["trades"]])
        self.assertIn("small sample", doc["note"])

    def test_gap_rule_and_short_side(self):
        bars = []
        for i, d in enumerate(self.days):
            bars += scripted(d, "win", gap=5.0 if i % 2 else 0.0)     # 1,050 open over a 1,020 close: +2.9%
        I.write(os.path.join(self.tmp, "TMPV.json"), I.chart_doc("TMPV", bars))
        doc = T.backtest(["TMPV"], dict(self.rule, setup="gap"), "buy", CFG, NOW, offline=self.tmp)
        s = doc["per_symbol"]["TMPV"]
        self.assertGreater(s["trades"], 5)
        self.assertIn("no gap", s["skipped"])
        doc = T.backtest(["RELIANCE"], dict(self.rule, stop_rule="or_high"), "sell", CFG, NOW, offline=self.tmp)
        self.assertEqual(doc["per_symbol"]["RELIANCE"]["trades"], 0)       # never closes under the range low

    def test_index_is_costed_as_futures_lots(self):
        bars = []
        for i, d in enumerate(self.days):
            bars += [(b[0], b[1] * 25, b[2] * 25, b[3] * 25, b[4] * 25, 0) for b in scripted(d, "win")]
        I.write(os.path.join(self.tmp, "NIFTY.json"), I.chart_doc("NIFTY", bars))
        doc = T.backtest(["NIFTY"], self.rule, "buy", CFG, NOW, offline=self.tmp)
        self.assertEqual(doc["per_symbol"]["NIFTY"]["trades"], 0)
        self.assertIn("risk per lot above max loss", doc["per_symbol"]["NIFTY"]["skipped"])
        doc = T.backtest(["NIFTY"], self.rule, "buy", dict(CFG, max_loss_per_trade_inr=50000), NOW, offline=self.tmp)
        s = doc["per_symbol"]["NIFTY"]
        self.assertEqual((s["segment"], s["trades"]), ("futures", 30))
        self.assertEqual(doc["trades"][0]["qty"] % 65, 0)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = T.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_cli_writes_backtest_json_and_reports_partial(self):
        out = os.path.join(self.tmp, "trader", "backtest.json")
        cfg = I.write(os.path.join(self.tmp, "config.json"), {})
        code, text, err = self.run_cli("backtest", "RELIANCE", "--offline", self.tmp, "--now", "2026-10-28 15:45",
                                       "--config", cfg, "--write", out)
        self.assertEqual(code, 0, err)
        doc = rd(out)
        for k in ("trades", "win_rate", "avg_net_pnl", "expectancy_r", "max_drawdown", "total_charges",
                  "charges_pct_of_gross_profit"):
            self.assertIn(k, doc["total"])
        self.assertIn("Past results do not predict future results", text)
        self.assertIn("small sample", text)
        code, text, err = self.run_cli("backtest", "RELIANCE", "TCS", "--offline", self.tmp, "--now",
                                       "2026-10-28 15:45", "--config", cfg)
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL", text)
        code, _, err = self.run_cli("backtest", "TCS", "--offline", self.tmp, "--config", cfg)
        self.assertTrue(err.startswith("error: no symbol could be backtested"))
        code, _, err = self.run_cli("backtest", "RELIANCE", "--rule", "squeeze", "--config", cfg)
        self.assertEqual(code, 1)


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
            code = T.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def files(self):
        data = I.write(os.path.join(self.tmp, "RELIANCE-60d5m.json"), self.doc)
        plan = I.write(os.path.join(self.tmp, "plan.json"), plan_for(self.bars))
        cfg = I.write(os.path.join(self.tmp, "config.json"), {"broker": "zerodha"})
        return data, plan, cfg

    def test_run_writes_the_contract(self):
        data, plan, cfg = self.files()
        out = os.path.join(self.tmp, "trader", "timemachine.json")
        code, text, err = self.run_cli("run", "--plan", plan, "--offline", self.tmp, "--now", "2026-10-28 15:45",
                                       "--config", cfg, "--replays", "1000", "--write", out)
        self.assertEqual(code, 0, err)
        doc = rd(out)
        for k in ("created_by", "symbol", "plan", "replays", "seed", "target_first", "stop_first", "neither",
                  "net_expectancy_r", "history", "verdict", "costs_per_share"):
            self.assertIn(k, doc)
        self.assertIn("seed 19", text)
        self.assertIn("After costs", text)
        self.assertIn("Not financial advice", text)
        code, text, _ = self.run_cli("chart", "--from", out)
        self.assertEqual(code, 0)
        self.assertIn("replay paths from the 09:50 IST entry", text)
        code, text, _ = self.run_cli("run", "--plan", plan, "--offline", data, "--now", "2026-10-28 15:45",
                                     "--config", cfg, "--replays", "500", "--json")
        self.assertNotIn("sample_paths", json.loads(text)["mc"])

    def test_flags_match_the_file(self):
        data, plan, cfg = self.files()
        p = plan_for(self.bars)
        args = ["--offline", data, "--now", "2026-10-28 15:45", "--config", cfg, "--replays", "500", "--json"]
        _, a, _ = self.run_cli("run", "--plan", plan, *args)
        _, b, err = self.run_cli("run", "--symbol", "RELIANCE", "--date", p["date"], "--entry", str(p["entry"]),
                                 "--stop", str(p["stop"]), "--target", str(p["target"]), "--entry-time",
                                 p["entry_time"], "--flat-by", "15:10", "--stop-rule", "pct", *args)
        self.assertEqual(json.loads(a)["mc"]["p_target"], json.loads(b)["mc"]["p_target"], err)

    def test_errors_are_one_plain_line(self):
        data, plan, cfg = self.files()
        for args in (("run", "--plan", os.path.join(self.tmp, "none.json"), "--config", cfg),
                     ("run", "--plan", plan, "--offline", os.path.join(self.tmp, "none"), "--config", cfg),
                     ("run", "--symbol", "RELIANCE", "--entry", "100", "--config", cfg),
                     ("run", "--plan", plan, "--offline", data, "--now", "someday", "--config", cfg),
                     ("chart", "--from", plan)):
            code, _, err = self.run_cli(*args)
            self.assertEqual(code, 1, args)
            self.assertTrue(err.startswith("error: "), err)
            self.assertEqual(len(err.strip().splitlines()), 1)

    def test_network_path(self):
        fake = FakeNet(self.doc)
        old = T.NET_FACTORY
        T.NET_FACTORY = lambda: fake
        try:
            text, source = T.fetch("RELIANCE", "NSE")
        finally:
            T.NET_FACTORY = old
        self.assertEqual((fake.calls, source), ([("RELIANCE", "60d", "5m", "NSE")], "yahoo"))


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        text = rd(os.path.join(ROOT, "trader-timemachine", "SKILL.md"), raw=True)
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertIn("name: trader-timemachine", fm)
        self.assertGreater(len(fm), 200)
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("not financial advice", "yes", "backtest", "75", "past results do not predict", "unofficial",
                     "slippage"):
            self.assertIn(must, low)
        for f in ("SKILL.md", "timemachine.py"):
            self.assertNotIn(chr(0x2014), rd(os.path.join(ROOT, "trader-timemachine", f), raw=True))


if __name__ == "__main__":
    unittest.main()
