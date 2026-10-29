"""End to end, offline: one RELIANCE opening-range breakout through every skill on the order path,
into the paper broker and out at the target, with Indian charges on every step.

scanner -> entry -> exit -> timemachine -> budget -> risk -> ticket (yes) -> paper broker"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import _india as F
from _load import ROOT, load

SCAN = load("trader-scanner", "scan")
ENTRY = load("trader-entry", "entry")
EXIT = load("trader-exit", "exit")
TM = load("trader-timemachine", "timemachine")
SIZE = load("trader-budget", "size")
RISK = load("trader-risk", "check")
TICKET = load("trader-broker", "ticket")
PAPER = load("trader-broker", "paper")
M = F.M

DAY = datetime.date(2026, 10, 28)
NOW = "2026-10-28 09:50"


def today_bars():
    t = lambda hhmm: datetime.datetime.combine(DAY, M.parse_hhmm(hhmm))  # noqa: E731
    rows = [("09:15", 1160.0, 1166.0, 1158.0, 1164.0), ("09:20", 1164.0, 1168.0, 1161.0, 1165.0),
            ("09:25", 1165.0, 1167.0, 1160.0, 1163.0), ("09:30", 1163.0, 1172.0, 1162.0, 1171.0),
            ("09:35", 1171.0, 1173.0, 1169.0, 1172.0), ("09:40", 1172.0, 1174.0, 1170.0, 1173.0),
            ("09:45", 1173.0, 1175.0, 1171.0, 1174.0)]
    return [(t(h), o, hi, lo, c, 400000) for h, o, hi, lo, c in rows]


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        hist = F.history(DAY, 60, start=1160.0, seed=11)
        # every past session trades ~Rs 30 crore so the liquidity floor passes
        hist = [(b[0], b[1], b[2], b[3], b[4], 350000) for b in hist]
        prev_close = hist[-1][4]
        # re-anchor today's bars on the previous close so the gap stays small
        shift = prev_close - 1160.0
        today = [(b[0], b[1] + shift, b[2] + shift, b[3] + shift, b[4] + shift, b[5]) for b in today_bars()]
        cls.shift = shift
        cls.prev_close = prev_close
        os.makedirs("data")
        cls.chart = F.write(os.path.join("data", "RELIANCE.json"),
                            F.chart_doc("RELIANCE", hist + today, prev_close=prev_close))
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        cfg.update({"capital_inr": 300000, "max_loss_per_trade_inr": 1000, "min_reward_risk": 1.5})
        M.write_json(os.path.join("trader", "config.json"), cfg)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp)

    def test_full_flow(self):
        code, out, err = run(SCAN, ["orb", "--offline", "data", "--now", NOW, "--symbols", "RELIANCE",
                                    "--min-turnover-cr", "0", "--min-price", "0", "--no-index"])
        self.assertEqual(code, 0, out + err)
        wl = M.read_json(os.path.join("trader", "watchlist.json"))
        self.assertEqual([h["symbol"] for h in wl["hits"]], ["RELIANCE"])

        code, out, err = run(ENTRY, ["check", "RELIANCE", "--offline", self.chart, "--now", NOW, "--write"])
        self.assertEqual(code, 0, out + err)
        entry = M.read_json(os.path.join("trader", "entry.json"))
        self.assertEqual(entry["status"], "triggered")
        self.assertAlmostEqual(entry["opening_range"]["high"], 1168.0 + self.shift, places=2)

        code, out, err = run(EXIT, ["plan", "--offline", self.chart, "--now", NOW, "--write"])
        self.assertEqual(code, 0, out + err)
        plan = M.read_json(os.path.join("trader", "plan.json"))
        self.assertLess(plan["stop"], plan["entry"])
        self.assertGreater(plan["target"], plan["entry"])
        self.assertLessEqual(plan["flat_by"], "15:10")
        self.assertIn("net_reward_risk", plan)

        code, out, err = run(TM, ["run", "--offline", self.chart, "--now", NOW, "--replays", "2000"])
        self.assertIn(code, (0, 1), out + err)
        self.assertIn("%", out)

        code, out, err = run(SIZE, ["--now", NOW, "--write"])
        self.assertEqual(code, 0, out + err)
        orders = M.read_json(os.path.join("trader", "orders.json"))["orders"]
        o = orders[0]
        self.assertEqual((o["exchange"], o["symbol"], o["product"], o["side"]), ("NSE", "RELIANCE", "MIS", "BUY"))
        self.assertLessEqual(o["plan"]["max_loss_with_costs"], 1000.0)

        code, out, err = run(RISK, ["--now", NOW])
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASS", out)

        code, out, err = run(TICKET, ["approve", "--index", "1", "--reply", "yes to all of them", "--now", NOW])
        self.assertNotEqual(code, 0)       # a batch yes is not a yes
        code, out, err = run(TICKET, ["approve", "--index", "1", "--reply", "yes", "--now", NOW])
        self.assertEqual(code, 0, out + err)

        price = o["price"] or plan["entry"]
        code, out, err = run(PAPER, ["place", "--index", "1", "--price", "RELIANCE=%s" % price,
                                     "--now", "2026-10-28 09:51"])
        self.assertEqual(code, 0, out + err)
        code, out, err = run(PAPER, ["check", "--price", "RELIANCE=%s" % (plan["target"] + 1),
                                     "--now", "2026-10-28 11:00"])
        self.assertEqual(code, 0, out + err)
        ledger = M.read_json(os.path.join("trader", "paper", "ledger.json"))
        text = json.dumps(ledger)
        self.assertIn("RELIANCE", text)
        code, out, err = run(PAPER, ["status", "--now", "2026-10-28 11:01"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("Rs", out)
        notes = os.listdir(os.path.join("trader", "paper", "contract_notes"))
        self.assertEqual(notes, ["2026-10-28.json"])


if __name__ == "__main__":
    unittest.main()
