import copy
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
import _india  # noqa: F401
import market as M

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
size = load("trader-budget", "size")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
RDIR = os.path.join(FIX, "trader-risk")
NOW = "2026-10-28 09:50"


def jload(p):
    with open(p) as fh:
        return json.load(fh)


def rel(**over):
    o = copy.deepcopy(jload(os.path.join(RDIR, "orders-reliance.json"))["orders"][0])
    o.update(over)
    return o


def ce(**over):
    o = copy.deepcopy(jload(os.path.join(RDIR, "orders-nifty-ce.json"))["orders"][0])
    o.update(over)
    return o


def call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=("RELIANCE", "TCS", "INFY")):
        return ticket.parse_yes(reply, order or rel(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 42 RELIANCE",
                  "yes 42", "yes at 1171.20", "yes trigger 1163.2", "yes max loss 388.08", "yes target 1,187.20"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "haan", "ha", "go", "do it", "yeah", "yep", "absolutely",
                  "send it", "yes to all", "yes, all of them", "yes to the rest", "yes both", "yes TCS",
                  "yes 50", "yes but make it 50", "yes?", "no", "not yes", "yes, wait", "yes if it dips",
                  "yes 42 but at 1170", "yes 1,000", "yes, stop 1163.2"):
            self.assertFalse(self.yes(r), r)

    def test_option_tradingsymbols_are_not_read_as_amounts(self):
        o = ce()
        for r in ("yes", "yes NIFTY26N0322700CE", "yes 1 lot", "yes 65 NIFTY26N0322700CE", "yes 70",
                  "yes trigger 64 limit 62.70"):
            self.assertTrue(ticket.parse_yes(r, o, ["NIFTY26N0322700CE", "NIFTY26N0322500PE"])[0], r)
        for r in ("yes NIFTY26N0322500PE", "yes 2 lots", "yes 130"):
            self.assertFalse(ticket.parse_yes(r, o, ["NIFTY26N0322700CE", "NIFTY26N0322500PE"])[0], r)


class Mappings(unittest.TestCase):
    def test_kite_upstox_dhan_equity(self):
        m = ticket.mappings(rel())
        self.assertIn("check your broker's API docs", m["note"])
        k = m["kite"]["entry"]
        self.assertEqual(k, {"variety": "regular", "tradingsymbol": "RELIANCE", "exchange": "NSE",
                             "transaction_type": "BUY", "order_type": "LIMIT", "quantity": 42, "product": "MIS",
                             "validity": "DAY", "price": 1171.2})
        self.assertEqual(m["kite"]["stop-loss"]["order_type"], "SL-M")
        self.assertEqual(m["kite"]["stop-loss"]["trigger_price"], 1163.2)
        self.assertNotIn("price", m["kite"]["stop-loss"])
        self.assertEqual(m["upstox"]["entry"]["product"], "I")
        self.assertTrue(m["upstox"]["entry"]["instrument_token"].startswith("NSE_EQ|"))
        self.assertEqual(m["dhan"]["entry"]["productType"], "INTRADAY")
        self.assertEqual(m["dhan"]["stop-loss"]["orderType"], "STOP_LOSS_MARKET")

    def test_fno_and_carry_products(self):
        m = ticket.mappings(ce(product="NRML"))
        self.assertEqual(m["kite"]["entry"]["exchange"], "NFO")
        self.assertEqual(m["kite"]["stop-loss"]["order_type"], "SL")
        self.assertEqual((m["kite"]["stop-loss"]["trigger_price"], m["kite"]["stop-loss"]["price"]), (64.0, 62.7))
        self.assertEqual(m["upstox"]["entry"]["product"], "D")
        self.assertTrue(m["upstox"]["entry"]["instrument_token"].startswith("NSE_FO|"))
        self.assertEqual((m["dhan"]["entry"]["productType"], m["dhan"]["entry"]["exchangeSegment"]),
                         ("MARGIN", "NSE_FNO"))
        cnc = ticket.mappings(rel(product="CNC", variety="amo"))
        self.assertEqual((cnc["kite"]["entry"]["variety"], cnc["upstox"]["entry"]["is_amo"],
                          cnc["dhan"]["entry"]["amoTime"]), ("amo", True, "OPEN"))
        self.assertEqual(ticket.mappings(rel(exchange="BSE", product="CNC"))["dhan"]["entry"]["exchangeSegment"],
                         "BSE_EQ")


class Book(unittest.TestCase):
    def test_round_trip_charges_and_realised(self):
        book = ticket.new_book(100000)
        c = M.load_config(os.path.join(ROOT, "nope.json"))
        a = ticket.book_fill(book, c, "RELIANCE", "buy", 42, 1171.2, "MIS")
        self.assertIsNone(a["realised"])
        b = ticket.book_fill(book, c, "RELIANCE", "sell", 42, 1187.2, "MIS")
        rt = M.round_trip("intraday", 1171.2, 1187.2, 42)
        self.assertAlmostEqual(a["total_charges"] + b["total_charges"], rt["total"], delta=0.02)
        self.assertAlmostEqual(b["realised"]["net"], rt["net_pnl"], delta=0.02)
        self.assertEqual(book["positions"], {})
        self.assertAlmostEqual(book["cash_inr"], 100000 + rt["net_pnl"], delta=0.02)

    def test_cnc_sell_pays_dp_and_shorts_work(self):
        c = M.load_config(os.path.join(ROOT, "nope.json"))
        book = ticket.new_book(100000)
        ticket.book_fill(book, c, "TCS", "buy", 10, 3100, "CNC")
        s = ticket.book_fill(book, c, "TCS", "sell", 10, 3150, "CNC")
        self.assertAlmostEqual(s["charges"]["dp"], round(13.0 * 1.18, 2))
        book = ticket.new_book(100000)
        ticket.book_fill(book, c, "SBIN", "sell", 100, 800, "MIS")
        self.assertEqual(book["positions"]["SBIN|MIS"]["qty"], -100)
        r = ticket.book_fill(book, c, "SBIN", "buy", 100, 790, "MIS")["realised"]
        self.assertEqual((r["side"], r["gross"]), ("short", 1000.0))


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader")
        shutil.copy(CFG_PATH, "trader/config.json")
        shutil.copy(os.path.join(FIX, "trader-budget", "plan-reliance-orb.json"), "trader/plan.json")
        self.assertEqual(call(size.main, ["--write", "--now", NOW])[0], 0)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def risk(self, now=NOW):
        return call(check.main, ["--now", now])

    def t(self, *argv):
        return call(ticket.main, list(argv))

    def test_not_ready_without_a_check_then_ready(self):
        code, out, _ = self.t("show", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: trader-risk has not checked", out)
        self.risk()
        code, out, _ = self.t("show", "--now", NOW)
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS", out)
        self.assertIn("PAPER (built-in paper broker)", out)
        self.assertIn("Zerodha Kite Connect", out)
        self.assertIn("Upstox", out)
        self.assertIn("Dhan", out)
        self.assertIn("check your broker's API docs", out)
        self.assertIn("charges included", out)
        self.assertIn("Not financial advice", out)

    def test_yesterdays_check_and_changed_orders_do_not_count(self):
        self.risk()
        code, out, _ = self.t("show", "--now", "2026-10-29 09:50")
        self.assertIn("not today", out)
        doc = jload("trader/orders.json")
        doc["orders"][0]["exits"]["stop_loss"]["trigger_price"] = 1150.0
        with open("trader/orders.json", "w") as fh:
            json.dump(doc, fh)
        code, out, _ = self.t("show", "--now", NOW)
        self.assertIn("the stop changed since the risk check", out)
        doc["orders"][0]["qty"] = 43
        with open("trader/orders.json", "w") as fh:
            json.dump(doc, fh)
        code, out, _ = self.t("show", "--now", NOW)
        self.assertIn("was not in the last risk check", out)

    def test_blocked_is_shown_and_cannot_be_approved(self):
        self.risk("2026-11-10 09:50")
        code, out, _ = self.t("show", "--now", "2026-11-10 09:50")
        self.assertIn("BLOCKED by trader-risk", out)
        code, _, err = self.t("approve", "--index", "1", "--reply", "yes", "--now", "2026-11-10 09:50")
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_approve_flow(self):
        self.risk()
        code, out, _ = self.t("approve", "--index", "1", "--reply", "sure", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", out)
        self.assertFalse(os.path.exists("trader/state/broker.json"))
        code, out, _ = self.t("approve", "--index", "1", "--reply", "yes 42 RELIANCE", "--now", NOW)
        self.assertEqual(code, 0, out)
        self.assertIn("paper.py place --index 1", out)
        rec = jload("trader/state/broker.json")["orders"][-1]
        self.assertEqual((rec["status"], rec["reply"], rec["kind"], rec["paper"]),
                         ("approved", "yes 42 RELIANCE", "entry", True))
        self.assertGreater(rec["max_loss"], 336.0)
        code, _, err = self.t("approve", "--index", "1", "--reply", "yes", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("APPROVED by you", err)

    def test_live_mode_result_and_fill(self):
        c = jload("trader/config.json")
        c["paper"] = False
        with open("trader/config.json", "w") as fh:
            json.dump(c, fh)
        self.risk()
        code, out, _ = self.t("show", "--now", NOW)
        self.assertIn("LIVE ACCOUNT, REAL MONEY", out)
        code, _, err = self.t("result", "--index", "1", "--status", "placed", "--now", NOW)
        self.assertEqual(code, 1)
        code, out, _ = self.t("approve", "--index", "1", "--reply", "yes", "--now", NOW)
        self.assertIn("Needs approval", out)
        self.assertIn('"transaction_type": "BUY"', out)
        code, out, _ = self.t("result", "--index", "1", "--status", "placed", "--broker-id", "251028000123",
                              "--filled-qty", "42", "--filled-price", "1171.2", "--now", "2026-10-28 09:51")
        self.assertEqual(code, 0, out)
        pf = jload("trader/portfolio.json")
        self.assertEqual(pf["positions"]["RELIANCE|MIS"]["qty"], 42)
        code, out, _ = self.t("fill", "--tradingsymbol", "RELIANCE", "--side", "sell", "--qty", "42", "--price",
                              "1163.2", "--product", "MIS", "--now", "2026-10-28 10:30")
        self.assertIn("Realised -Rs", out)
        st = jload("trader/state/broker.json")["orders"]
        self.assertLess(st[-1]["realised_pnl"], -336.0)
        code, out, _ = self.t("log", "--now", "2026-10-28 11:00")
        self.assertIn("1 placed today", out)
        self.assertIn("id 251028000123", out)

    def test_flatten(self):
        code, _, err = self.t("flatten", "--tradingsymbol", "RELIANCE", "--now", "2026-10-28 15:05")
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)
        os.makedirs("trader/paper")
        with open("trader/paper/ledger.json", "w") as fh:
            json.dump({"cash_inr": 50800, "positions": {
                "RELIANCE|MIS": {"qty": 42, "avg_price": 1171.2, "product": "MIS", "exchange": "NSE",
                                 "instrument": {"type": "EQ"}},
                "NIFTY26N0322700CE|MIS": {"qty": 65, "avg_price": 70.0, "product": "MIS", "exchange": "NSE",
                                          "instrument": {"type": "CE", "underlying": "NIFTY", "expiry": "2026-11-03",
                                                         "lot_size": 65}}}}, fh)
        code, out, _ = self.t("flatten", "--tradingsymbol", "RELIANCE", "--now", "2026-10-28 15:12")
        self.assertEqual(code, 0)
        self.assertIn("past your flat_by 15:10", out)
        o = jload("trader/orders.json")["orders"][0]
        self.assertEqual((o["side"], o["qty"], o["product"], o["order_type"], o["intent"]),
                         ("SELL", 42, "MIS", "MARKET", "exit"))
        code, out, _ = call(check.main, ["--now", "2026-10-28 15:12", "--no-write"])
        self.assertEqual(code, 0, out)
        self.assertIn("PASS  #1 SELL 42 RELIANCE MIS, exit", out)
        code, _, err = self.t("flatten", "--tradingsymbol", "NIFTY26N0322700CE", "--now", "2026-11-03 15:05")
        self.assertEqual(code, 1)
        self.assertIn("--price", err)
        code, out, _ = self.t("flatten", "--tradingsymbol", "NIFTY26N0322700CE", "--price", "75.3",
                              "--now", "2026-11-03 15:05")
        o = jload("trader/orders.json")["orders"][0]
        self.assertEqual((o["order_type"], o["price"], o["lots"]), ("LIMIT", 74.5, 1))


class Files(unittest.TestCase):
    def test_desktop_config_example_has_no_secrets(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            text = fh.read()
        doc = json.loads(text)
        self.assertIn("mcpServers", doc)
        self.assertIn("https://mcp.kite.trade/mcp", text)
        self.assertIn("Needs approval", text)
        self.assertNotIn("alpaca", text.lower())
        self.assertIsNone(re.search(r"(api_?key|secret|token)\"\s*:", text, re.I))

    def test_errors_are_one_line(self):
        code, _, err = call(ticket.main, ["show", "--orders", "missing.json"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1)


if __name__ == "__main__":
    unittest.main()
