import datetime
import glob
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
import _india
import market as M

paper = load("trader-broker", "paper")
ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
size = load("trader-budget", "size")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
DAY = datetime.date(2026, 10, 28)


def jload(p):
    with open(p) as fh:
        return json.load(fh)


def call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    PLAN = "plan-reliance-orb.json"
    NOW = "2026-10-28 09:50"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader")
        shutil.copy(CFG_PATH, "trader/config.json")
        shutil.copy(os.path.join(FIX, "trader-budget", self.PLAN), "trader/plan.json")
        self.assertEqual(call(size.main, ["--write", "--now", self.NOW])[0], 0)
        self.assertEqual(call(check.main, ["--now", self.NOW])[0], 0)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def approve(self, reply="yes"):
        code, out, err = call(ticket.main, ["approve", "--index", "1", "--reply", reply, "--now", self.NOW])
        self.assertEqual(code, 0, out + err)

    def p(self, *argv):
        return call(paper.main, list(argv))

    def ledger(self):
        return jload("trader/paper/ledger.json")

    def open_orders(self):
        return [o for o in self.ledger()["orders"] if o["status"] == "open"]


class PlaceAndExits(Base):
    def test_place_needs_your_yes(self):
        code, _, err = self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        self.assertEqual(code, 1)
        self.assertIn("no yes from you today", err)
        self.assertFalse(os.path.exists("trader/paper/ledger.json"))

    def test_entry_fills_and_the_oco_pair_goes_in(self):
        self.approve()
        code, out, err = self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        self.assertEqual(code, 0, out + err)
        led = self.ledger()
        pos = led["positions"]["RELIANCE|MIS"]
        self.assertEqual((pos["qty"], pos["avg_price"]), (42, 1170.5))   # a marketable limit fills at the market
        roles = sorted(o["role"] for o in self.open_orders())
        self.assertEqual(roles, ["stop_loss", "target"])
        fee = led["fills"][0]["total_charges"]
        self.assertAlmostEqual(led["cash_inr"], 100000 - 42 * 1170.5 - fee, places=2)
        st = jload("trader/state/broker.json")["orders"]
        placed = [r for r in st if r["status"] == "placed"][0]
        self.assertTrue(placed["broker_order_id"].startswith("PAPER-"))
        self.assertTrue(placed["paper"])
        code, out, _ = call(ticket.main, ["show", "--now", "2026-10-28 09:53"])
        self.assertIn("DONE: placed", out)
        code, _, err = self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:53")
        self.assertEqual(code, 1)

    def test_stop_loss_fills_and_cancels_the_target(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        code, out, _ = self.p("check", "--price", "1175", "--now", "2026-10-28 10:00")
        self.assertIn("nothing filled", out)
        code, out, _ = self.p("check", "--price", "1160", "--now", "2026-10-28 10:30")
        self.assertIn("stop_loss", out)
        led = self.ledger()
        self.assertEqual(led["positions"], {})
        self.assertEqual(self.open_orders(), [])
        sl = [o for o in led["orders"] if o.get("role") == "stop_loss"][0]
        self.assertEqual(sl["fill_price"], M.round_tick(1160 * (1 - 0.0005), 0.1, "down"))
        tg = [o for o in led["orders"] if o.get("role") == "target"][0]
        self.assertEqual(tg["status"], "cancelled")
        r = led["realised"][0]
        self.assertAlmostEqual(r["gross"], (sl["fill_price"] - 1170.5) * 42, places=2)
        self.assertAlmostEqual(r["net"], r["gross"] - sum(f["total_charges"] for f in led["fills"]), places=1)
        st = jload("trader/state/broker.json")["orders"]
        self.assertAlmostEqual(st[-1]["realised_pnl"], r["net"], places=2)
        # trader-risk now counts the realised loss from the paper ledger
        summ = check.today_summary(jload("trader/state/broker.json"), led, "2026-10-28")
        self.assertAlmostEqual(summ["realised_loss"], -r["net"], places=2)

    def test_target_fills_at_its_limit(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        self.p("check", "--price", "1190", "--now", "2026-10-28 11:00")
        led = self.ledger()
        tg = [o for o in led["orders"] if o.get("role") == "target"][0]
        self.assertEqual((tg["status"], tg["fill_price"]), ("filled", 1187.2))
        self.assertEqual([o for o in led["orders"] if o.get("role") == "stop_loss"][0]["status"], "cancelled")
        self.assertGreater(led["realised"][0]["net"], 0)

    def test_limit_that_is_not_reached_waits(self):
        self.approve()
        code, out, _ = self.p("place", "--index", "1", "--price", "1175", "--now", "2026-10-28 09:52")
        self.assertIn("is open", out)
        self.assertEqual(self.ledger()["positions"], {})
        self.p("check", "--price", "1171", "--now", "2026-10-28 10:05")
        led = self.ledger()
        self.assertEqual(led["positions"]["RELIANCE|MIS"]["avg_price"], 1171.2)
        self.assertEqual(len(self.open_orders()), 2)

    def test_mis_auto_square_off_with_the_broker_fee(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        code, out, _ = self.p("check", "--price", "1175", "--now", "2026-10-28 15:12")
        self.assertIn("after your flat_by 15:10", out)
        self.assertIn("RELIANCE|MIS", self.ledger()["positions"])
        code, out, _ = self.p("check", "--price", "1176", "--now", "2026-10-28 15:26")
        self.assertIn("AUTO SQUARE-OFF", out)
        led = self.ledger()
        self.assertEqual(led["positions"], {})
        self.assertEqual(self.open_orders(), [])
        f = led["fills"][-1]
        self.assertAlmostEqual(f["extra_charges"], 59.0)
        self.assertIn("square-off fee", f["note"])
        note = jload("trader/paper/contract_notes/2026-10-28.json")
        self.assertAlmostEqual(note["charges"]["other"], 59.0)

    def test_market_closed_and_live_mode_refuse(self):
        self.approve()
        code, _, err = self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 16:00")
        self.assertEqual(code, 1)
        self.assertIn("not open", err)
        c = jload("trader/config.json")
        c["paper"] = False
        with open("trader/config.json", "w") as fh:
            json.dump(c, fh)
        code, _, err = self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        self.assertIn("\"paper\" is false", err)

    def test_bars_from_an_offline_chart_stop_first_in_a_wide_bar(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        t = lambda hh, mm: datetime.datetime(2026, 10, 28, hh, mm)   # noqa: E731
        bars = [(t(9, 50), 1170.0, 1172.0, 1169.0, 1171.0, 1000), (t(9, 55), 1171.0, 1175.0, 1168.0, 1170.0, 1000),
                (t(10, 0), 1170.0, 1190.0, 1162.0, 1180.0, 1000), (t(10, 5), 1180.0, 1181.0, 1179.0, 1180.0, 1000)]
        _india.write("chart.json", _india.chart_doc("RELIANCE", bars, prev_close=1196.2))
        code, out, err = self.p("check", "--offline", "chart.json", "--now", "2026-10-28 10:30")
        self.assertEqual(code, 0, err)
        led = self.ledger()
        sl = [o for o in led["orders"] if o.get("role") == "stop_loss"][0]
        self.assertEqual(sl["status"], "filled")
        self.assertEqual(sl["fill_price"], M.round_tick(1163.2 * 0.9995, 0.1, "down"))
        self.assertTrue(sl["filled_at"].startswith("2026-10-28T10:00"))

    def test_contract_note_status_and_reset(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        self.p("check", "--price", "1190", "--now", "2026-10-28 11:00")
        note = jload("trader/paper/contract_notes/2026-10-28.json")
        led = self.ledger()
        self.assertEqual(len(note["trades"]), 2)
        self.assertAlmostEqual(note["charges"]["total"], sum(f["total_charges"] for f in led["fills"]), places=2)
        self.assertIn("SIMULATED", note["note"])
        for k in ("brokerage", "stt", "exchange_txn", "sebi", "stamp", "gst", "dp"):
            self.assertIn(k, note["charges"])
        self.assertAlmostEqual(note["net_amount"], note["realised_net"], places=1)
        code, out, _ = self.p("status", "--json", "--now", "2026-10-28 11:05")
        s = json.loads(out)
        self.assertAlmostEqual(s["cash_inr"], 100000 + s["realised_total"], places=2)
        code, out, _ = self.p("reset", "--capital", "50000", "--now", "2026-10-28 16:00")
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger()["cash_inr"], 50000)
        self.assertEqual(len(glob.glob("trader/paper/ledger-*.json")), 1)

    def test_status_with_an_open_position(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        code, out, _ = self.p("status", "--price", "RELIANCE=1180", "--now", "2026-10-28 10:00")
        self.assertIn("RELIANCE", out)
        self.assertIn("unrealised", out)
        s = json.loads(self.p("status", "--json", "--price", "RELIANCE=1180", "--now", "2026-10-28 10:00")[1])
        self.assertLess(s["positions"][0]["unrealised_net"], (1180 - 1170.5) * 42)
        self.assertEqual(len(s["open_orders"]), 2)

    def test_square_off_ticket_cancels_the_exits(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "1170.5", "--now", "2026-10-28 09:52")
        code, out, err = call(ticket.main, ["flatten", "--tradingsymbol", "RELIANCE", "--now", "2026-10-28 15:05"])
        self.assertEqual(code, 0, err)
        self.assertEqual(call(check.main, ["--now", "2026-10-28 15:05"])[0], 0)
        code, out, err = call(ticket.main, ["approve", "--index", "1", "--reply", "yes", "--now", "2026-10-28 15:05"])
        self.assertEqual(code, 0, out + err)
        code, out, err = self.p("place", "--index", "1", "--price", "1178", "--now", "2026-10-28 15:06")
        self.assertEqual(code, 0, err)
        led = self.ledger()
        self.assertEqual(led["positions"], {})
        self.assertEqual(self.open_orders(), [])
        self.assertIn("replaced by your square-off order", out)


class OptionStopLimit(Base):
    PLAN = "plan-nifty-ce.json"
    NOW = "2026-11-03 10:05"

    def test_needs_a_price_for_options(self):
        self.approve()
        code, _, err = self.p("place", "--index", "1", "--now", "2026-11-03 10:06")
        self.assertEqual(code, 1)
        self.assertIn("Yahoo has no NSE option", err)

    def test_sl_that_gaps_past_its_limit_does_not_fill(self):
        self.approve("yes 1 lot")
        code, out, err = self.p("place", "--index", "1", "--price", "70", "--now", "2026-11-03 10:06")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ledger()["positions"]["NIFTY26N0322700CE|MIS"]["qty"], 65)
        code, out, _ = self.p("check", "--price", "60", "--now", "2026-11-03 10:30")
        self.assertIn("TRIGGERED but NOT FILLED", out)
        self.assertIn("NIFTY26N0322700CE|MIS", self.ledger()["positions"])
        self.p("check", "--price", "63", "--now", "2026-11-03 10:35")
        led = self.ledger()
        self.assertEqual(led["positions"], {})
        sl = [o for o in led["orders"] if o.get("role") == "stop_loss"][0]
        self.assertEqual(sl["fill_price"], 62.7)
        f = led["fills"][-1]
        self.assertEqual(f["segment"], "options")
        self.assertGreater(f["charges"]["stt"], 0)          # STT on the option sell
        self.assertEqual(led["fills"][0]["charges"]["stt"], 0)

    def test_fno_auto_square_off_time(self):
        self.approve()
        self.p("place", "--index", "1", "--price", "70", "--now", "2026-11-03 10:06")
        code, out, _ = self.p("check", "--price", "NIFTY26N0322700CE=72", "--now", "2026-11-03 15:25")
        self.assertNotIn("AUTO SQUARE-OFF", out)
        code, out, _ = self.p("check", "--price", "NIFTY26N0322700CE=72", "--now", "2026-11-03 15:26")
        self.assertIn("AUTO SQUARE-OFF", out)


class Pure(unittest.TestCase):
    def test_price_map(self):
        self.assertEqual(paper.price_map("1171.2"), {None: 1171.2})
        self.assertEqual(paper.price_map("RELIANCE=1190, nifty26n0322700ce=82.5"),
                         {"RELIANCE": 1190.0, "NIFTY26N0322700CE": 82.5})
        with self.assertRaises(ValueError):
            paper.price_map("RELIANCE=abc")

    def test_try_fill_rules(self):
        bar = {"o": 100.0, "h": 101.0, "l": 95.0, "c": 96.0, "dt": None}
        o = {"side": "SELL", "order_type": "SL-M", "trigger_price": 98.0, "instrument": {"type": "EQ"}}
        self.assertEqual(paper.try_fill(o, bar, 0), 98.0)
        gap = dict(bar, o=94.0, h=94.5, l=93.0)
        self.assertEqual(paper.try_fill(o, gap, 0), 94.0)                 # gapped through: the open
        sl = {"side": "SELL", "order_type": "SL", "trigger_price": 98.0, "price": 97.0, "instrument": {"type": "CE"}}
        self.assertIsNone(paper.try_fill(sl, gap, 0))
        self.assertTrue(sl["triggered"])
        lim = {"side": "BUY", "order_type": "LIMIT", "price": 96.0, "instrument": {"type": "EQ"}}
        self.assertEqual(paper.try_fill(lim, bar, 0), 96.0)
        self.assertIsNone(paper.try_fill(lim, dict(bar, l=96.5), 0))
        mkt = {"side": "BUY", "order_type": "MARKET", "instrument": {"type": "EQ"}}
        self.assertEqual(paper.try_fill(mkt, bar, 0.05), M.round_tick(96.0 * 1.0005, 0.01, "up"))


if __name__ == "__main__":
    unittest.main()
