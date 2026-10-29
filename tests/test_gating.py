"""Regression tests from the independent review: the order gate must hold at the moment of the
yes and of the placement, not only when trader-risk ran."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

TICKET = load("trader-broker", "ticket")
PAPER = load("trader-broker", "paper")
RISK = load("trader-risk", "check")
M = TICKET.M

CE = {"exchange": "NSE", "symbol": "NIFTY", "tradingsymbol": "NIFTY26N0322700CE",
      "instrument": {"type": "CE", "underlying": "NIFTY", "expiry": "2026-11-03", "lot_size": 65},
      "side": "BUY", "qty": 65, "lots": 1, "product": "MIS", "order_type": "LIMIT", "price": 70,
      "exits": {"stop_loss": {"side": "SELL", "order_type": "SL", "trigger_price": 62, "price": 60.6},
                "target": {"side": "SELL", "order_type": "LIMIT", "price": 85}}}
REL = {"exchange": "NSE", "symbol": "RELIANCE", "tradingsymbol": "RELIANCE", "side": "BUY", "qty": 125,
       "product": "MIS", "order_type": "LIMIT", "price": 1171.2,
       "exits": {"stop_loss": {"side": "SELL", "order_type": "SL-M", "trigger_price": 1163.2},
                 "target": {"side": "SELL", "order_type": "LIMIT", "price": 1187.2}},
       "plan": {"max_loss_with_costs": 1120.4, "max_loss": 1000}}


def call(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class YesIsOnlyYes(unittest.TestCase):
    BATCH = {"RELIANCE", "NIFTY26N0322700CE", "NIFTY", "TCS"}

    def test_refused(self):
        for order in (REL, CE):
            for r in ("yes, sell it", "yes go short", "yes 1250shares", "yes 10lots", "yes for INFY",
                      "yes to the first two", "yes, at market", "yes as CNC", "yes, nope",
                      "yes... actually never mind", "yes and the other one too", "yes x2", "yes 22800CE",
                      "yes, 1.25k shares", "yes to 1 and 2"):
                self.assertFalse(TICKET.parse_yes(r, order, self.BATCH)[0], (order["symbol"], r))

    def test_still_accepted(self):
        for r in ("yes", "y", "yes!", "YES please", "yes go ahead", "yes RELIANCE", "yes buy it", "yes 125",
                  "yes at 1171.20", "yes MIS"):
            self.assertTrue(TICKET.parse_yes(r, REL, self.BATCH)[0], r)
        for r in ("yes 1 lot", "yes NIFTY26N0322700CE", "yes buy the call", "yes 65"):
            self.assertTrue(TICKET.parse_yes(r, CE, self.BATCH)[0], r)


class ClockAtYesAndPlace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader")
        with open("trader/config.json", "w") as fh:
            json.dump({"capital_inr": 200000, "paper": True, "max_loss_per_trade_inr": 1500,
                       "min_reward_risk": 1.0}, fh)
        with open("trader/orders.json", "w") as fh:
            json.dump({"orders": [CE]}, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def test_a_morning_pass_does_not_carry_past_the_expiry_cutoff(self):
        call(PAPER, ["reset", "--capital", "200000", "--now", "2026-11-03 09:00"])
        code, out, err = call(RISK, ["--now", "2026-11-03 11:00"])
        self.assertEqual(code, 0, out + err)
        code, out, err = call(TICKET, ["approve", "--index", "1", "--reply", "yes", "--now", "2026-11-03 15:20"])
        self.assertNotEqual(code, 0)
        # even a fresh-looking PASS is refused after the cut-off
        code, out, err = call(TICKET, ["approve", "--index", "1", "--reply", "yes", "--now", "2026-11-03 11:05"])
        self.assertEqual(code, 0, out + err)
        code, out, err = call(PAPER, ["place", "--index", "1", "--price", "70", "--now", "2026-11-03 13:31"])
        self.assertNotEqual(code, 0)
        self.assertIn("cannot be placed now", err)

    def test_a_failed_price_lookup_does_not_mark_the_order_placed(self):
        n = "2026-10-28 10:00"
        call(PAPER, ["reset", "--capital", "200000", "--now", n])
        call(RISK, ["--now", n])
        code, _, _ = call(TICKET, ["approve", "--index", "1", "--reply", "yes", "--now", n])
        self.assertEqual(code, 0)
        code, out, err = call(PAPER, ["place", "--index", "1", "--now", n])     # no --price, no Yahoo for options
        self.assertNotEqual(code, 0)
        code, out, err = call(PAPER, ["place", "--index", "1", "--price", "70", "--now", n])
        self.assertEqual(code, 0, out + err)
        self.assertIn("filled at 70.00", out)


if __name__ == "__main__":
    unittest.main()
