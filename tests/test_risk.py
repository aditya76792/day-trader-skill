import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
import _india  # noqa: F401
import market as M

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
RDIR = os.path.join(FIX, "trader-risk")
NOW = datetime.datetime(2026, 10, 28, 9, 50)
EXPIRY = datetime.datetime(2026, 11, 3, 10, 5)


def jload(p):
    with open(p) as fh:
        return json.load(fh)


def orders(name):
    return jload(os.path.join(RDIR, name))["orders"]


def rel(**over):
    o = copy.deepcopy(orders("orders-reliance.json")[0])
    o.update(over)
    return o


def ce(**over):
    o = copy.deepcopy(orders("orders-nifty-ce.json")[0])
    o.update(over)
    return o


def cfg(**over):
    c = jload(CFG_PATH)
    c.update(over)
    return c


def one(order, now=NOW, c=None, **kw):
    res = check.check([order], c or cfg(), now=now, **kw)
    return res["checks"][0], res


def has(reasons, prefix):
    return any(r.startswith(prefix) for r in reasons)


class Fingerprint(unittest.TestCase):
    def test_same_function_in_risk_and_broker(self):
        for name in sorted(os.listdir(RDIR)):
            for o in orders(name):
                self.assertEqual(check.fingerprint(o), ticket.fingerprint(o), name)
                self.assertEqual(len(check.fingerprint(o)), 40)
        o = rel()
        self.assertEqual(check.fingerprint(o), check.fingerprint(dict(o, qty="42", price="1171.20", side="buy",
                                                                       why="different notes", exits={})))
        for k, v in (("qty", 43), ("price", 1171.3), ("product", "CNC"), ("side", "SELL"), ("exchange", "BSE"),
                     ("order_type", "MARKET"), ("trigger_price", 1170.0), ("tradingsymbol", "TCS")):
            self.assertNotEqual(check.fingerprint(o), check.fingerprint(dict(o, **{k: v})), k)
            self.assertEqual(check.fingerprint(dict(o, **{k: v})), ticket.fingerprint(dict(o, **{k: v})), k)


class Equity(unittest.TestCase):
    def test_reliance_orb_passes(self):
        r, res = one(rel())
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["kind"], "entry")
        self.assertAlmostEqual(r["max_loss_with_costs"], 336.0 + r["costs_at_stop"], places=2)
        self.assertGreater(r["costs_at_stop"], 40)
        self.assertEqual(res["date"], "2026-10-28")

    def test_loss_at_the_stop_includes_charges(self):
        # 125 x 8 = Rs 1,000 exactly: inside the limit gross, over it with charges
        r, _ = one(rel(qty=125), c=cfg(max_position_pct=100, max_order_inr=500000, capital_inr=500000))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(has(r["reasons"], "loss:"), r["reasons"])
        self.assertIn("charges", [x for x in r["reasons"] if x.startswith("loss:")][0])
        r, _ = one(rel(qty=113), c=cfg(max_position_pct=100, max_order_inr=500000, capital_inr=500000))
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_holidays_weekends_and_session(self):
        r, _ = one(rel(), now=datetime.datetime(2026, 11, 10, 9, 50))
        self.assertTrue(any("Diwali-Balipratipada" in x for x in r["reasons"]), r["reasons"])
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 31, 10, 0))
        self.assertTrue(any("weekend" in x for x in r["reasons"]))
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 28, 9, 10))
        self.assertTrue(has(r["reasons"], "session:"), r["reasons"])
        # an MIS AMO would fill at 09:15, before the entry window: blocked
        r, _ = one(rel(variety="amo"), now=datetime.datetime(2026, 10, 28, 8, 30))
        self.assertTrue(has(r["reasons"], "AMO:"), r["reasons"])
        # a CNC AMO before the open is fine, with a gap warning; also on a holiday (it fills next session)
        for when in (datetime.datetime(2026, 10, 28, 8, 30), datetime.datetime(2026, 11, 10, 18, 0)):
            r, _ = one(rel(variety="amo", product="CNC"), now=when,
                       c=cfg(max_position_pct=100, max_order_inr=500000, capital_inr=500000))
            self.assertFalse(has(r["reasons"], "session:"), r["reasons"])
            self.assertFalse(has(r["reasons"], "calendar:"), r["reasons"])
            self.assertFalse(has(r["reasons"], "AMO:"), r["reasons"])
            self.assertTrue(any("AMO" in w for w in r["warnings"]))
        # an AMO while the market is open is refused
        r, _ = one(rel(variety="amo", product="CNC"), now=datetime.datetime(2026, 10, 28, 11, 0))
        self.assertTrue(has(r["reasons"], "AMO:"), r["reasons"])

    def test_entry_window_and_mis_square_off(self):
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 28, 9, 20))
        self.assertTrue(any("before 09:30" in x for x in r["reasons"]))
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 28, 14, 50))
        self.assertTrue(any("after 14:45" in x for x in r["reasons"]))
        late = cfg(no_entries_after=None)
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 28, 15, 12), c=late)
        self.assertTrue(any("flat_by 15:10" in x for x in r["reasons"]), r["reasons"])
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 28, 15, 12), c=cfg(no_entries_after=None, flat_by=None))
        self.assertFalse(any("auto square-off 15:25" in x for x in r["reasons"]))
        r, _ = one(rel(), now=datetime.datetime(2026, 10, 28, 15, 26), c=cfg(no_entries_after=None, flat_by=None))
        self.assertTrue(any("auto square-off 15:25" in x for x in r["reasons"]), r["reasons"])

    def test_ticks(self):
        r, _ = one(rel(price=1171.25))
        self.assertTrue(has(r["reasons"], "tick:"), r["reasons"])
        o = rel()
        o["exits"] = copy.deepcopy(o["exits"])
        o["exits"]["target"]["price"] = 1187.23
        r, _ = one(o)
        self.assertTrue(any("target 1187.23" in x for x in r["reasons"]))

    def test_shorts_and_products(self):
        short = rel(side="SELL")
        short["exits"] = {"stop_loss": {"side": "BUY", "order_type": "SL-M", "trigger_price": 1179.2},
                          "target": {"side": "BUY", "order_type": "LIMIT", "price": 1147.2}}
        short["plan"] = dict(short["plan"], stop=1179.2, target=1147.2)
        r, _ = one(short)
        self.assertTrue(has(r["reasons"], "short:"))
        r, _ = one(short, c=cfg(allow_short=True))
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        r, _ = one(dict(short, product="CNC"), c=cfg(allow_short=True))
        self.assertTrue(any("no CNC short" in x for x in r["reasons"]))
        r, _ = one(rel(product="CNC"), c=cfg(allow_swing=False, min_reward_risk=1.0))
        self.assertTrue(any("allow_swing" in x for x in r["reasons"]))
        r, _ = one(rel(product="NRML"))
        self.assertTrue(has(r["reasons"], "product:"))

    def test_trade_for_trade_series_and_lists(self):
        r, _ = one(rel(series="BE"))
        self.assertTrue(has(r["reasons"], "series:"))
        r, _ = one(rel(series="BE", product="CNC"), c=cfg(min_reward_risk=1.0))
        self.assertFalse(has(r["reasons"], "series:"))
        r, _ = one(rel(), c=cfg(blocked=["RELIANCE"]))
        self.assertTrue(has(r["reasons"], "blocked:"))
        r, _ = one(rel(), c=cfg(asm_gsm=["RELIANCE"]))
        self.assertTrue(has(r["reasons"], "asm/gsm:"))
        r, _ = one(rel(), c=cfg(fno_ban=["RELIANCE"]))
        self.assertEqual(r["verdict"], "PASS")       # the F&O ban does not stop cash-market trades

    def test_circuit_proximity(self):
        o = rel(circuit={"band_pct": 10, "upper": 1180.0, "lower": 965.0})
        r, _ = one(o)
        self.assertTrue(has(r["reasons"], "circuit:"), r["reasons"])
        o = rel(circuit={"band_pct": 20, "upper": 1400.0, "lower": 950.0})
        r, _ = one(o)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        o = rel(circuit={"band_pct": 2})
        prices = {"RELIANCE": {"price": 1171.0, "time": NOW, "prev_close": 1150.0}}
        r, _ = one(o, prices=prices)
        self.assertTrue(has(r["reasons"], "circuit:"), r["reasons"])

    def test_stale_price_for_market_orders(self):
        o = rel(order_type="MARKET", price=None)
        r, _ = one(o)
        self.assertTrue(has(r["reasons"], "stale price:"))
        fresh = {"RELIANCE": {"price": 1172.0, "time": NOW - datetime.timedelta(minutes=5), "prev_close": None}}
        r, _ = one(o, prices=fresh)
        self.assertFalse(has(r["reasons"], "stale price:"), r["reasons"])
        self.assertEqual(r["entry_ref"], 1172.0)          # the worse of the plan and the fresh price
        old = {"RELIANCE": {"price": 1172.0, "time": NOW - datetime.timedelta(minutes=40), "prev_close": None}}
        r, _ = one(o, prices=old)
        self.assertTrue(has(r["reasons"], "stale price:"))
        r, _ = one(rel(), prices=old)
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(any("older than 15 minutes" in w for w in r["warnings"]))

    def test_reward_risk_after_costs_and_no_stop(self):
        r, _ = one(rel(), c=cfg(min_reward_risk=1.7))
        self.assertTrue(has(r["reasons"], "reward:risk:"))
        o = rel(exits={})
        o["plan"] = {}
        r, _ = one(o)
        self.assertTrue(any(x.startswith("stop: no stop") for x in r["reasons"]))

    def test_money_limits(self):
        r, _ = one(rel(qty=80), c=cfg(min_reward_risk=1.0, max_loss_per_trade_inr=2000))
        self.assertTrue(has(r["reasons"], "position:"))
        self.assertTrue(has(r["reasons"], "size:"))
        r, _ = one(rel(qty=1), c=cfg(min_order_inr=2000))
        self.assertTrue(any("minimum order" in x for x in r["reasons"]))
        pf = {"cash_inr": 20000, "positions": {}}
        r, _ = one(rel(), c=cfg(paper=False), portfolio=pf)
        self.assertTrue(has(r["reasons"], "margin:"))
        r, _ = one(rel(), c=cfg(use_intraday_leverage=True, paper=False), portfolio=pf)
        self.assertFalse(has(r["reasons"], "margin:"), r["reasons"])


class Today(unittest.TestCase):
    def state(self, n, max_loss=500.0, day="2026-10-28"):
        return {"orders": [{"status": "placed", "date": day, "kind": "entry", "tradingsymbol": "TCS%d" % i,
                            "side": "BUY", "max_loss": max_loss} for i in range(n)]}

    def test_trades_per_day(self):
        r, _ = one(rel(), state=self.state(3))
        self.assertTrue(has(r["reasons"], "trades:"))
        r, _ = one(rel(), state=self.state(3, day="2026-10-27"))
        self.assertEqual(r["verdict"], "PASS")

    def test_daily_loss_from_risk_and_realised(self):
        r, _ = one(rel(), state=self.state(2, max_loss=1400.0))
        self.assertTrue(has(r["reasons"], "daily loss:"), r["reasons"])
        ledger = {"cash_inr": 97000, "positions": {}, "realised": [
            {"date": "2026-10-28", "net": -2900.0}, {"date": "2026-10-27", "net": -2000.0}]}
        r, _ = one(rel(), ledger=ledger)
        self.assertTrue(has(r["reasons"], "daily loss:"))
        ledger["realised"][0]["net"] = -3000.0
        r, _ = one(rel(), ledger=ledger)
        self.assertTrue(any("done for the day" in x for x in r["reasons"]))
        live = {"orders": [{"status": "filled", "date": "2026-10-28", "realised_pnl": -2800.0, "paper": False}]}
        r, _ = one(rel(), state=live, c=cfg(paper=False))
        self.assertTrue(has(r["reasons"], "daily loss:"))

    def test_batch_counts_as_if_filled(self):
        a = rel()
        b = rel(tradingsymbol="TCS", symbol="TCS", price=3100.0, qty=10)
        b["exits"] = {"stop_loss": {"side": "SELL", "order_type": "SL-M", "trigger_price": 3080.0},
                      "target": {"side": "SELL", "order_type": "LIMIT", "price": 3150.0}}
        b["plan"] = {"entry": 3100.0, "stop": 3080.0, "target": 3150.0}
        res = check.check([a, b, rel()], cfg(), now=NOW)
        v = [r["verdict"] for r in res["checks"]]
        self.assertEqual(v[:2], ["PASS", "PASS"])
        self.assertTrue(has(res["checks"][2]["reasons"], "duplicate:"))

    def test_exit_is_never_blocked_by_the_day_rules(self):
        ledger = {"cash_inr": 50800, "positions": {"RELIANCE|MIS": {"qty": 42, "product": "MIS"}}, "realised": []}
        out = {"exchange": "NSE", "symbol": "RELIANCE", "tradingsymbol": "RELIANCE", "instrument": {"type": "EQ"},
               "side": "SELL", "qty": 42, "product": "MIS", "order_type": "MARKET", "price": None,
               "trigger_price": None, "validity": "DAY", "variety": "regular", "intent": "exit"}
        r, _ = one(out, now=datetime.datetime(2026, 10, 28, 15, 20), ledger=ledger, state=self.state(3))
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "PASS"), r["reasons"])
        self.assertTrue(any(w.startswith("exit, not blocked") for w in r["warnings"]))
        r, _ = one(dict(out, qty=50), now=datetime.datetime(2026, 10, 28, 15, 20), ledger=ledger)
        self.assertEqual(r["kind"], "entry")
        self.assertEqual(r["verdict"], "BLOCK")


class Fno(unittest.TestCase):
    def test_nifty_call_passes_before_the_expiry_cutoff(self):
        r, _ = one(ce(), now=EXPIRY)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["lots"], 1)

    def test_expiry_day_last_entry(self):
        r, _ = one(ce(), now=datetime.datetime(2026, 11, 3, 13, 45), c=cfg(no_entries_after=None))
        self.assertTrue(has(r["reasons"], "expiry day:"), r["reasons"])
        r, _ = one(ce(), now=datetime.datetime(2026, 11, 3, 13, 25))
        self.assertFalse(has(r["reasons"], "expiry day:"))
        r, _ = one(ce(), now=datetime.datetime(2026, 11, 4, 10, 0))
        self.assertTrue(any("expired" in x for x in r["reasons"]))

    def test_option_selling_blocked_by_default(self):
        o = orders("orders-nifty-pe-sell.json")[0]
        r, _ = one(o, now=EXPIRY)
        self.assertTrue(has(r["reasons"], "options:"), r["reasons"])
        r, _ = one(o, now=EXPIRY, c=cfg(allow_option_selling=True, max_loss_per_trade_inr=5000,
                                       max_position_pct=100, max_order_inr=500000, capital_inr=500000))
        self.assertFalse(has(r["reasons"], "options:"), r["reasons"])

    def test_futures_blocked_by_default(self):
        o = orders("orders-nifty-fut.json")[0]
        r, _ = one(o)
        self.assertTrue(has(r["reasons"], "futures:"))
        big = cfg(allow_futures=True, capital_inr=5000000, max_order_inr=2000000, max_position_pct=100,
                  max_loss_per_trade_inr=5000, max_daily_loss_inr=10000)
        r, _ = one(o, c=big)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any("SPAN" in w for w in r["warnings"]))
        self.assertAlmostEqual(r["margin"], 25850 * 65 * 0.15, places=2)

    def test_lot_multiple_and_max_lots(self):
        r, _ = one(orders("orders-lot-violation.json")[0], now=EXPIRY)
        self.assertTrue(any("not a multiple of the NIFTY lot size 65" in x for x in r["reasons"]), r["reasons"])
        r, _ = one(ce(qty=195, lots=3), now=EXPIRY, c=cfg(max_premium_per_trade_inr=50000,
                                                         max_loss_per_trade_inr=5000))
        self.assertTrue(has(r["reasons"], "lots:"))

    def test_premium_cap(self):
        r, _ = one(ce(qty=130, lots=2), now=EXPIRY)
        self.assertTrue(has(r["reasons"], "premium:"))

    def test_fno_ban_and_sl_m_on_options(self):
        r, _ = one(ce(), now=EXPIRY, c=cfg(fno_ban=["NIFTY"]))
        self.assertTrue(has(r["reasons"], "F&O ban:"))
        o = ce()
        o["exits"] = copy.deepcopy(o["exits"])
        o["exits"]["stop_loss"].update(order_type="SL-M", price=None)
        r, _ = one(o, now=EXPIRY)
        self.assertTrue(any("SL-M" in x for x in r["reasons"]))

    def test_stock_options_warn_physical_settlement(self):
        o = ce(symbol="RELIANCE", tradingsymbol="RELIANCE26NOV1200CE", price=20.0)
        o["instrument"] = {"type": "CE", "underlying": "RELIANCE", "expiry": "2026-11-24", "strike": 1200,
                           "lot_size": 500}
        o.update(qty=500, lots=1)
        o["exits"] = {"stop_loss": {"side": "SELL", "order_type": "SL", "trigger_price": 19.0, "price": 18.6},
                      "target": {"side": "SELL", "order_type": "LIMIT", "price": 24.0}}
        o["plan"] = {"entry": 20.0, "stop": 19.0, "target": 24.0}
        r, _ = one(o, c=cfg(max_premium_per_trade_inr=20000))
        self.assertTrue(any("physically settled" in w for w in r["warnings"]))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader")
        shutil.copy(CFG_PATH, "trader/config.json")
        shutil.copy(os.path.join(RDIR, "orders-reliance.json"), "trader/orders.json")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_pass_writes_risk_json(self):
        code, out, err = self.run_cli(["--now", "2026-10-28 09:50"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASS  #1 BUY 42 RELIANCE MIS", out)
        self.assertIn("Not financial advice", out)
        doc = jload("trader/state/risk.json")
        c = doc["checks"][0]
        for k in ("fingerprint", "verdict", "reasons", "date", "time"):
            self.assertIn(k, c)
        self.assertEqual((c["verdict"], c["date"], c["time"]), ("PASS", "2026-10-28", "09:50"))
        self.assertEqual(c["fingerprint"], ticket.fingerprint(jload("trader/orders.json")["orders"][0]))

    def test_block_exit_1_and_json(self):
        code, out, _ = self.run_cli(["--now", "2026-11-10 09:50", "--json", "--no-write"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["checks"][0]["verdict"], "BLOCK")
        self.assertFalse(os.path.exists("trader/state/risk.json"))

    def test_errors(self):
        code, _, err = self.run_cli(["--orders", "missing.json", "--now", "2026-10-28 09:50"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))
        code, _, err = self.run_cli(["--now", "28/10/2026"])
        self.assertEqual(code, 1)
        with open("trader/config.json", "w") as fh:
            fh.write("{bad json")
        code, _, err = self.run_cli(["--now", "2026-10-28 09:50"])
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)

    def test_prices_file_contract_shape(self):
        with open("trader/prices.json", "w") as fh:
            json.dump({"created_by": "trader-alerts", "source": "yahoo", "at": "2026-10-28T09:48:00+05:30",
                       "quotes": {"RELIANCE": {"price": 1172.5, "time": "2026-10-28T09:48:00+05:30",
                                               "prev_close": 1196.2}}}, fh)
        doc = jload("trader/orders.json")
        doc["orders"][0].update(order_type="MARKET", price=None)
        with open("trader/orders.json", "w") as fh:
            json.dump(doc, fh)
        code, out, _ = self.run_cli(["--now", "2026-10-28 09:50", "--json", "--no-write"])
        c = json.loads(out)["checks"][0]
        self.assertEqual(c["entry_ref"], 1172.5)   # the price ran up: less reward, more risk
        self.assertEqual(c["verdict"], "BLOCK")
        self.assertTrue(c["reasons"][0].startswith("reward:risk: 1.28"), c["reasons"])


if __name__ == "__main__":
    unittest.main()
