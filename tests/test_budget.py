import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
import _india  # noqa: F401  (puts trader-market on the path)
import market as M

size = load("trader-budget", "size")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN_REL = os.path.join(FIX, "trader-budget", "plan-reliance-orb.json")
PLAN_CE = os.path.join(FIX, "trader-budget", "plan-nifty-ce.json")
PLAN_PE_SELL = os.path.join(FIX, "trader-budget", "plan-nifty-pe-sell.json")
PLAN_FUT = os.path.join(FIX, "trader-budget", "plan-nifty-fut.json")


def jload(p):
    with open(p) as fh:
        return json.load(fh)


def cfg(**over):
    c = jload(CFG_PATH)
    c.update(over)
    return c


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExampleConfig(unittest.TestCase):
    def test_india_keys_and_safe_defaults(self):
        c = jload(CFG_PATH)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "min_reward_risk",
                  "require_stop", "no_entries_after", "flat_by", "capital_inr", "broker"):
            self.assertIn(k, c, k)
        self.assertIs(c["require_stop"], True)
        self.assertIs(c["paper"], True)
        self.assertIs(c["allow_short"], False)
        self.assertIs(c["allow_option_selling"], False)
        self.assertIs(c["allow_futures"], False)
        self.assertEqual((c["capital_inr"], c["max_loss_per_trade_inr"], c["max_daily_loss_inr"], c["flat_by"],
                          c["broker"]), (100000, 1000, 3000, "15:10", "zerodha"))
        for k, v in M.DEFAULT_CONFIG.items():
            self.assertEqual(c[k], v, k)
        for us in ("budget", "max_loss_per_trade_usd", "pdt_guard", "sec_user_agent", "fractional",
                   "allow_options", "allow_margin"):
            self.assertNotIn(us, c, us)

    def test_us_weights_file_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "trader-budget", "targets.example.json")))


class EquitySizing(unittest.TestCase):
    def test_reliance_orb_is_bound_by_the_position_cap(self):
        s = size.size_trade(jload(PLAN_REL), cfg(), today="2026-10-28")
        self.assertIsNone(s["refusal"])
        self.assertEqual(s["qty"], 42)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual(s["entry"], 1171.2)
        self.assertEqual(s["stop"], 1163.2)
        self.assertEqual(s["segment"], "intraday")
        self.assertAlmostEqual(s["max_loss_with_costs"], s["max_loss"] + s["costs_at_stop"], places=2)
        self.assertLess(s["net_reward_risk"], s["gross_reward_risk"])

    def test_charges_inclusive_max_loss(self):
        # without the other caps the max loss decides: 1000 / 8 = 125 shares gross, fewer once charges count
        s = size.size_trade(jload(PLAN_REL), cfg(max_position_pct=100, max_order_inr=500000, cash_buffer_pct=0,
                                                 capital_inr=500000), today="2026-10-28")
        self.assertEqual(s["bound_by"], "max loss")
        q = s["qty"]
        self.assertLess(q, 125)
        self.assertLessEqual(s["max_loss_with_costs"], 1000.0)
        nxt = M.round_trip("intraday", 1171.2, 1163.2, q + 1)["total"] + 8.0 * (q + 1)
        self.assertGreater(nxt, 1000.0)
        self.assertEqual(q, 113)

    def test_prices_snap_to_the_tick(self):
        plan = dict(jload(PLAN_REL), entry=1171.23, stop=1163.27, target=1187.26)
        plan.pop("tick")
        s = size.size_trade(plan, cfg(), today="2026-10-28")
        self.assertEqual(s["tick"], 0.1)
        self.assertEqual(s["entry"], 1171.2)
        self.assertEqual(s["stop"], 1163.2)      # a long stop rounds down (more risk, never less)
        self.assertEqual(s["target"], 1187.2)    # a long target rounds down (less reward)
        cheap = dict(plan, entry=212.337, stop=209.111, target=219.999)
        s = size.size_trade(cheap, cfg(), today="2026-10-28")
        self.assertEqual((s["tick"], s["entry"], s["stop"]), (0.01, 212.34, 209.11))

    def test_intraday_leverage_margin_is_capped_at_5x(self):
        s = size.size_trade(jload(PLAN_REL), cfg(use_intraday_leverage=True, intraday_margin_pct=10),
                            today="2026-10-28")
        self.assertAlmostEqual(s["margin"], round(s["notional"] * 0.20, 2), places=2)
        self.assertIn("5x", s["margin_basis"])
        s = size.size_trade(jload(PLAN_REL), cfg(), today="2026-10-28")
        self.assertEqual(s["margin"], s["notional"])

    def test_cnc_costs_include_dp_and_delivery_stt(self):
        plan = dict(jload(PLAN_REL), product="CNC")
        cnc = size.size_trade(plan, cfg(min_reward_risk=1.0), today="2026-10-28")
        mis = size.size_trade(jload(PLAN_REL), cfg(min_reward_risk=1.0), today="2026-10-28")
        strict = size.size_trade(plan, cfg(), today="2026-10-28")   # the charges eat the reward
        self.assertIn("net reward:risk after charges", strict["refusal"])
        self.assertEqual(cnc["segment"], "delivery")
        self.assertGreater(cnc["costs_at_stop"], mis["costs_at_stop"] + 15)
        o = size.build_order(cnc)
        self.assertIn("GTT OCO", o["exits"]["how"])
        self.assertEqual(o["exits"]["stop_loss"]["order_type"], "SL-M")

    def test_shorts(self):
        plan = dict(jload(PLAN_REL), side="sell", stop=1179.2, target=1155.2)
        s = size.size_trade(plan, cfg(), today="2026-10-28")
        self.assertIn("short selling is off", s["refusal"])
        s = size.size_trade(plan, cfg(allow_short=True), today="2026-10-28")
        self.assertIsNone(s["refusal"])
        o = size.build_order(s)
        self.assertEqual((o["side"], o["exits"]["stop_loss"]["side"]), ("SELL", "BUY"))
        s = size.size_trade(dict(plan, product="CNC"), cfg(allow_short=True), today="2026-10-28")
        self.assertIn("CNC (delivery) short is not possible", s["refusal"])

    def test_net_reward_risk_after_charges_can_refuse(self):
        s = size.size_trade(jload(PLAN_REL), cfg(min_reward_risk=1.7), today="2026-10-28")
        self.assertIn("net reward:risk after charges", s["refusal"])
        self.assertIsNone(size.build_order(s))

    def test_plan_for_another_day_and_bad_plans(self):
        with self.assertRaises(size.SizeError):
            size.size_trade(jload(PLAN_REL), cfg(), today="2026-10-29")
        with self.assertRaises(size.SizeError):
            size.size_trade(dict(jload(PLAN_REL), stop=1180.0), cfg(), today="2026-10-28")
        with self.assertRaises(size.SizeError):
            size.size_trade(dict(jload(PLAN_REL), product="NRML"), cfg(), today="2026-10-28")

    def test_market_order_sizes_at_the_worse_price(self):
        s = size.size_trade(jload(PLAN_REL), cfg(), market=True, price=1174.0, today="2026-10-28")
        self.assertEqual((s["entry"], s["entry_from"], s["order_type"]), (1174.0, "price", "MARKET"))
        s = size.size_trade(jload(PLAN_REL), cfg(), market=True, price=1168.0, today="2026-10-28")
        self.assertEqual(s["entry"], 1171.2)
        o = size.build_order(s)
        self.assertIsNone(o["price"])

    def test_blocked_list(self):
        s = size.size_trade(jload(PLAN_REL), cfg(blocked=["RELIANCE"]), today="2026-10-28")
        self.assertIn("blocked list", s["refusal"])


class FnoSizing(unittest.TestCase):
    def test_nifty_call_in_whole_lots_with_an_sl_stop(self):
        s = size.size_trade(jload(PLAN_CE), cfg(), today="2026-11-03")
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["qty"], s["lots"], s["lot"]), (65, 1, 65))
        self.assertIn(s["bound_by"], ("premium cap", "max loss"))
        # the loss is counted at the SL limit (the worst fill), charges included
        self.assertLessEqual(s["max_loss_with_costs"], 1000)
        self.assertLess(s["sl_price"], 64.0)
        o = size.build_order(s)
        self.assertEqual(o["tradingsymbol"], "NIFTY26N0322700CE")
        self.assertEqual(o["qty"] % 65, 0)
        sl = o["exits"]["stop_loss"]
        self.assertEqual((sl["order_type"], sl["side"], sl["trigger_price"]), ("SL", "SELL", 64.0))
        self.assertLess(sl["price"], sl["trigger_price"])
        self.assertAlmostEqual(sl["price"] / 0.05, round(sl["price"] / 0.05), places=6)
        self.assertEqual(o["costs"]["segment"], "options")
        self.assertEqual(o["margin"]["required"], 65 * 70.0)

    def test_more_premium_allowed_gives_two_lots(self):
        s = size.size_trade(jload(PLAN_CE), cfg(max_premium_per_trade_inr=20000, max_loss_per_trade_inr=1500),
                            today="2026-11-03")
        self.assertEqual((s["qty"], s["lots"]), (130, 2))
        self.assertLessEqual(s["max_loss_with_costs"], 1500)
        s = size.size_trade(jload(PLAN_CE), cfg(max_premium_per_trade_inr=20000, max_lots_per_trade=1,
                                                max_loss_per_trade_inr=1500), today="2026-11-03")
        self.assertEqual((s["qty"], s["bound_by"]), (65, "max lots"))

    def test_refuses_when_one_lot_breaks_the_max_loss(self):
        plan = dict(jload(PLAN_CE), stop=52.0)
        s = size.size_trade(plan, cfg(), today="2026-11-03")
        self.assertEqual(s["qty"], 0)
        self.assertIn("one lot (65 units)", s["refusal"])
        self.assertIsNone(size.build_order(s))

    def test_lot_size_falls_back_to_the_core_table(self):
        plan = jload(PLAN_CE)
        plan["instrument"] = {"type": "CE", "underlying": "NIFTY", "expiry": "2026-11-03", "strike": 22700}
        s = size.size_trade(plan, cfg(), today="2026-11-03")
        self.assertEqual(s["lot"], 65)
        self.assertEqual(s["tradingsymbol"], "NIFTY26N0322700CE")

    def test_option_selling_is_refused_by_default(self):
        s = size.size_trade(jload(PLAN_PE_SELL), cfg(), today="2026-11-03")
        self.assertIn("option selling is off", s["refusal"])
        s = size.size_trade(jload(PLAN_PE_SELL), cfg(allow_option_selling=True, max_loss_per_trade_inr=5000),
                            today="2026-11-03")
        self.assertNotIn("option selling is off", s["refusal"] or "")

    def test_futures_refused_by_default_then_margin_is_an_estimate(self):
        s = size.size_trade(jload(PLAN_FUT), cfg(), today="2026-10-28")
        self.assertIn("futures are off", s["refusal"])
        big = cfg(allow_futures=True, capital_inr=5000000, max_order_inr=2000000, max_position_pct=100,
                  max_loss_per_trade_inr=10000, min_reward_risk=1.2)
        s = size.size_trade(jload(PLAN_FUT), big, today="2026-10-28")
        self.assertIsNone(s["refusal"])
        self.assertEqual(s["qty"] % 65, 0)
        self.assertIn("SPAN", s["margin_basis"])
        self.assertAlmostEqual(s["margin"], round(25850.0 * s["qty"] * 0.15, 2), places=2)
        o = size.build_order(s)
        self.assertIn("SPAN", o["margin"]["note"])
        # max order is on the full value: one lot (~Rs 16.8 lakh) is over a Rs 3 lakh max order
        small = dict(big, max_order_inr=300000)
        self.assertEqual(size.size_trade(jload(PLAN_FUT), small, today="2026-10-28")["qty"], 0)

    def test_fno_ban(self):
        s = size.size_trade(jload(PLAN_CE), cfg(fno_ban=["NIFTY"]), today="2026-11-03")
        self.assertIn("F&O ban", s["refusal"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader")
        shutil.copy(CFG_PATH, "trader/config.json")
        shutil.copy(PLAN_REL, "trader/plan.json")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def test_write_orders_contract(self):
        code, out, err = run(["--write", "--now", "2026-10-28 09:50"])
        self.assertEqual(code, 0, err)
        self.assertIn("42 shares", out)
        self.assertIn("Not financial advice", out)
        doc = jload("trader/orders.json")
        self.assertEqual((doc["created_by"], doc["status"], doc["paper"]), ("trader-budget", "proposed", True))
        o = doc["orders"][0]
        for k in ("exchange", "symbol", "tradingsymbol", "instrument", "side", "qty", "lots", "product",
                  "order_type", "price", "trigger_price", "validity", "exits", "why", "plan", "costs", "margin"):
            self.assertIn(k, o, k)
        self.assertEqual((o["side"], o["qty"], o["product"], o["order_type"], o["price"], o["validity"]),
                         ("BUY", 42, "MIS", "LIMIT", 1171.2, "DAY"))
        self.assertEqual(o["exits"]["stop_loss"]["order_type"], "SL-M")
        self.assertIn("max_loss_with_costs", o["plan"])

    def test_market_with_fresh_and_stale_prices(self):
        with open("trader/prices.json", "w") as fh:
            json.dump({"at": "2026-10-28T09:45:00+05:30",
                       "quotes": {"RELIANCE": {"price": 1174.0, "time": "2026-10-28T09:45:00+05:30"}}}, fh)
        code, out, _ = run(["--market", "--json", "--now", "2026-10-28 09:50"])
        self.assertEqual(json.loads(out)["sizing"]["entry"], 1174.0)
        code, out, _ = run(["--market", "--json", "--now", "2026-10-28 10:20"])
        doc = json.loads(out)
        self.assertEqual(doc["sizing"]["entry"], 1171.2)
        self.assertTrue(any("older than" in n for n in doc["notes"]))

    def test_refusal_and_errors_exit_1(self):
        code, out, _ = run(["--config", "trader/config.json", "--now", "2026-10-28 09:50", "--capital", "1000"])
        self.assertEqual(code, 1)
        self.assertIn("No order", out)
        code, out, err = run(["--plan", "nope.json", "--now", "2026-10-28 09:50"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))
        code, out, err = run(["--now", "2026-10-29 09:50"])
        self.assertEqual(code, 1)
        self.assertIn("the plan is for 2026-10-28", err)
        code, out, err = run(["--now", "yesterday"])
        self.assertEqual(code, 1)
        code, out, err = run(["--limit", "abc", "--now", "2026-10-28 09:50"])
        self.assertEqual(code, 1)

    def test_limit_and_amo(self):
        code, out, _ = run(["--limit", "1170.03", "--amo", "--json", "--now", "2026-10-28 08:30"])
        o = json.loads(out)["orders"][0]
        self.assertEqual((o["price"], o["variety"]), (1170.0, "amo"))


if __name__ == "__main__":
    unittest.main()
