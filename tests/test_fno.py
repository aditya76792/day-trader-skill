"""trader-fno: option chain readings, Black-Scholes, payoffs and option-buying plans.

The chain fixture is real: NSE's NIFTY option chain (expiry 03-Nov-2026) at
15:40 IST on 28-Oct-2026, strikes 22400-23000, spot 22698.85."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

F = load("trader-fno", "fno")
M = F.M
CHAIN = os.path.join(FIX, "trader-fno", "nifty-chain-2026-11-03.json")
DT = datetime.datetime


def chain():
    return F.normalise_chain(M.read_json(CHAIN), "NIFTY")


class Args(object):
    def __init__(self, **kw):
        self.__dict__.update(dict(view="bullish", delta=0.5, stop_pct=30.0, target_r=2.0, product="MIS"), **kw)


class Chain(unittest.TestCase):
    def test_normalise(self):
        c = chain()
        self.assertEqual(c["expiry"], "2026-11-03")
        self.assertEqual(c["lot_size"], 65)
        self.assertEqual(c["underlying_value"], 22698.85)
        self.assertEqual(len(c["strikes"]), 13)
        atm = [s for s in c["strikes"] if s["strike"] == 22700][0]
        self.assertEqual(atm["CE"]["ltp"], 181.15)
        self.assertEqual(atm["PE"]["oi"], 78462)

    def test_summary_numbers(self):
        s = F.summary(chain(), DT(2026, 10, 28, 15, 40))
        self.assertEqual(s["atm"], 22700)
        self.assertEqual(s["max_pain"], 22700)
        self.assertEqual(s["call_wall"], 23000)
        self.assertEqual(s["put_wall"], 22500)
        self.assertEqual(s["atm_straddle"], 358.45)
        self.assertEqual(s["implied_move_pct"], 1.58)
        self.assertAlmostEqual(s["days_to_expiry"], 6.0, places=1)
        self.assertEqual(s["skew"], 2.13)
        self.assertTrue(0.8 < s["pcr_oi"] < 1.0)

    def test_zero_iv_means_unknown(self):
        side = F._side({"lastPrice": 5, "impliedVolatility": 0})
        self.assertIsNone(side["iv"])

    def test_not_a_chain(self):
        with self.assertRaises(F.FnoError):
            F.normalise_chain({"data": []}, "NIFTY")
        with self.assertRaises(F.FnoError):
            F.normalise_chain(M.read_json(CHAIN), "NIFTY", "2026-11-09")

    def test_buildup(self):
        self.assertEqual(F.buildup(5, 100), "long build-up")
        self.assertEqual(F.buildup(-5, 100), "short build-up")
        self.assertEqual(F.buildup(5, -100), "short covering")
        self.assertEqual(F.buildup(-5, -100), "long unwinding")
        self.assertIsNone(F.buildup(None, 1))

    def test_chain_url(self):
        self.assertEqual(F.chain_url("NIFTY", "2026-11-03"),
                         "/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=03-Nov-2026")
        self.assertIn("type=Equity&symbol=M%26M", F.chain_url("M&M", "2026-11-23"))


class BlackScholes(unittest.TestCase):
    def test_iv_matches_nse_within_a_few_tenths(self):
        t = F.years_to_expiry(DT(2026, 10, 28, 15, 40), "2026-11-03")
        iv = F.implied_vol(181.15, 22698.85, 22700, t, 0.065, "CE")
        self.assertAlmostEqual(iv * 100, 14.63, delta=0.3)     # NSE showed 14.63
        ivp = F.implied_vol(99.8, 22698.85, 22500, t, 0.065, "PE")
        self.assertAlmostEqual(ivp * 100, 16.5, delta=0.6)     # NSE showed 16.50

    def test_put_call_parity_and_greeks(self):
        s, k, t, v, r = 22700.0, 22700.0, 6 / 365.0, 0.15, 0.065
        c = F.bs_price(s, k, t, v, r, "CE")
        p = F.bs_price(s, k, t, v, r, "PE")
        self.assertAlmostEqual(c - p, s - k * F.math.exp(-r * t), places=6)
        g = F.greeks(s, k, t, v, r, "CE")
        self.assertTrue(0.5 < g["delta"] < 0.56)
        self.assertLess(g["theta_per_day"], 0)
        self.assertLess(F.greeks(s, k, t, v, r, "PE")["delta"], 0)

    def test_below_intrinsic_has_no_iv(self):
        self.assertIsNone(F.implied_vol(10, 22700, 22500, 0.01, 0.065, "CE"))

    def test_expiry_clock(self):
        self.assertAlmostEqual(F.years_to_expiry(DT(2026, 11, 3, 15, 30), "2026-11-03") * 365 * 86400, 60, places=3)


class Payoff(unittest.TestCase):
    def test_bull_call_spread(self):
        legs = [F.parse_leg("BUY CE 22700 181.15"), F.parse_leg("SELL CE 22900 @ 93.5")]
        r = F.payoff(legs, 22698.85, 65)
        self.assertAlmostEqual(r["max_loss"], -5697.25, places=2)
        self.assertAlmostEqual(r["max_profit"], 7302.75, places=2)
        self.assertEqual(r["breakevens"], [22787.65])

    def test_naked_short_put_loses_up_to_the_whole_strike(self):
        r = F.payoff([F.parse_leg("SELL PE 22500 99.8")], 22698.85, 65)
        self.assertAlmostEqual(r["max_loss"], -(22500 - 99.8) * 65, places=2)
        self.assertAlmostEqual(r["max_profit"], 6487.0, places=2)
        r = F.payoff([F.parse_leg("SELL CE 22900 93.5")], 22698.85, 65)
        self.assertEqual(r["max_loss"], "unlimited")

    def test_long_put_profit_is_capped_and_far_strikes_count(self):
        r = F.payoff([F.parse_leg("BUY PE 25000 100")], 25000, 65)
        self.assertAlmostEqual(r["max_profit"], 24900 * 65, places=2)
        r = F.payoff([F.parse_leg("BUY PE 80 2"), F.parse_leg("SELL PE 70 1")], 100, 65)
        self.assertAlmostEqual(r["max_profit"], 585.0, places=2)
        self.assertEqual(r["breakevens"], [79.0])
        with self.assertRaises(F.FnoError):
            F.payoff([F.parse_leg("BUY PE 80 2")], 0, 65)

    def test_long_straddle_two_breakevens(self):
        r = F.payoff([F.parse_leg("BUY CE 22700 181.15"), F.parse_leg("BUY PE 22700 177.3")], 22698.85, 65)
        self.assertEqual(r["max_profit"], "unlimited")
        self.assertEqual(len(r["breakevens"]), 2)

    def test_bad_legs(self):
        for bad in ("BUY CE 22700", "HOLD CE 1 2", "BUY XX 1 2", "BUY FUT"):
            with self.assertRaises(F.FnoError):
                F.parse_leg(bad)
        self.assertEqual(F.parse_leg("buy fut 22750")["price"], 22750.0)

    def test_charges_on_every_leg(self):
        legs = [F.parse_leg("BUY CE 22700 181.15"), F.parse_leg("SELL CE 22900 93.5")]
        self.assertGreater(F.legs_charges(legs, 65, "NSE", "zerodha", {}), 80)


class Plan(unittest.TestCase):
    def test_bullish_plan_atm_call(self):
        cfg = dict(M.DEFAULT_CONFIG)
        p = F.build_plan(chain(), Args(), cfg, DT(2026, 10, 29, 10, 5))
        self.assertEqual(p["instrument"]["type"], "CE")
        self.assertEqual(p["instrument"]["strike"], 22700)
        self.assertEqual(p["instrument"]["tradingsymbol"], "NIFTY26N0322700CE")
        self.assertEqual(p["entry"], 181.55)                  # the ask
        self.assertEqual(p["stop"], 127.05)                   # 30% under, rounded to 0.05
        self.assertEqual(p["target"], 290.55)
        self.assertEqual(p["flat_by"], "15:10")
        self.assertLess(p["net_reward_risk"], 2.0)

    def test_bearish_plan_buys_a_put(self):
        p = F.build_plan(chain(), Args(view="bearish"), dict(M.DEFAULT_CONFIG), DT(2026, 10, 29, 10, 5))
        self.assertEqual(p["instrument"]["type"], "PE")

    def test_expiry_day_cutoff_and_warnings(self):
        cfg = dict(M.DEFAULT_CONFIG)
        with self.assertRaises(F.FnoError):
            F.build_plan(chain(), Args(), cfg, DT(2026, 11, 3, 13, 30))
        p = F.build_plan(chain(), Args(), cfg, DT(2026, 11, 3, 10, 0))
        self.assertTrue(any("EXPIRY DAY" in n for n in p["notes"]))

    def test_option_buying_off(self):
        cfg = dict(M.DEFAULT_CONFIG, allow_option_buying=False)
        with self.assertRaises(F.FnoError):
            F.build_plan(chain(), Args(), cfg, DT(2026, 10, 29, 10, 5))

    def test_stock_option_physical_settlement_note(self):
        w = F.expiry_warnings("RELIANCE", "2026-11-23", DT(2026, 11, 23, 10, 0), M.DEFAULT_CONFIG)
        self.assertTrue(any("PHYSICALLY SETTLED" in x for x in w))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = F.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_chain_writes_contract_file(self):
        code, out, _ = self.run_cli(["chain", "NIFTY", "--offline", CHAIN, "--now", "2026-10-28 15:40"])
        self.assertEqual(code, 0)
        self.assertIn("max pain 22700", out)
        self.assertIn("Not financial advice", out)
        doc = M.read_json(os.path.join("trader", "fno", "chain-NIFTY-2026-11-03.json"))
        self.assertEqual(doc["summary"]["atm"], 22700)

    def test_plan_write_feeds_the_pipeline(self):
        code, out, _ = self.run_cli(["plan", "NIFTY", "--view", "bullish", "--offline", CHAIN,
                                     "--now", "2026-10-29 10:05", "--write"])
        self.assertEqual(code, 0)
        plan = M.read_json(os.path.join("trader", "plan.json"))
        self.assertEqual(plan["created_by"], "trader-fno")
        self.assertEqual(plan["instrument"]["lot_size"], 65)

    def test_greeks_and_payoff_json(self):
        code, out, _ = self.run_cli(["greeks", "--spot", "22698.85", "--strike", "22700", "--type", "CE",
                                     "--premium", "181.15", "--expiry", "2026-11-03", "--now", "2026-10-28 15:40",
                                     "--json"])
        self.assertEqual(code, 0)
        self.assertAlmostEqual(json.loads(out)["iv_pct"], 14.6, delta=0.3)
        code, out, _ = self.run_cli(["payoff", "--spot", "22698.85", "--symbol", "NIFTY",
                                     "--leg", "SELL PE 22500 99.8"])
        self.assertIn("far more than the premium", out)
        self.assertIn("OFF", out)
        code, out, _ = self.run_cli(["payoff", "--spot", "22698.85", "--symbol", "NIFTY",
                                     "--leg", "SELL CE 22900 93.5"])
        self.assertIn("UNLIMITED", out)

    def test_errors_are_one_line(self):
        code, _, err = self.run_cli(["chain", "NIFTY", "--offline", "missing.json"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))
        code, _, err = self.run_cli(["plan", "NIFTY", "--view", "sideways", "--offline", CHAIN])
        self.assertEqual(code, 1)

    def test_expiries(self):
        code, out, _ = self.run_cli(["expiries", "NIFTY", "--now", "2026-11-03"])
        self.assertIn("<- today", out)


if __name__ == "__main__":
    unittest.main()
