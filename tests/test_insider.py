"""trader-insider: SEBI PIT disclosures and NSE bulk / block / short deals, the
new-since-last-check state and the watch list. Offline, on small handwritten
answers in the shapes NSE returns (fictional companies and people)."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

ins = load("trader-insider", "insider")
M = ins.M
DIR = os.path.join(FIX, "trader-insider")
PIT = os.path.join(DIR, "pit-2026-10-28.json")
DEALS = os.path.join(DIR, "largedeal-2026-10-28.json")
NOW = "2026-10-28 20:00"


def doc(path):
    with open(path) as fh:
        return json.load(fh)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(args)
    return code, out.getvalue(), err.getvalue()


class Pit(unittest.TestCase):
    def setUp(self):
        self.rows = ins.parse_pit(doc(PIT))
        self.by = {(r["symbol"], r["person"], r["kind"]): r for r in self.rows}

    def test_rows_are_read_tolerantly(self):
        self.assertEqual(len(self.rows), 11)                   # the symbol-less row and the string are skipped
        self.assertEqual(self.rows[0]["disclosed"], "2026-10-28 18:12")   # newest first
        r = self.by[("SAMPLECO", "Ramesh Kumar Sample", "promoter_market_buy")]
        self.assertEqual((r["qty"], r["value"], r["value_cr"]), (120000.0, 14064000.0, 1.41))
        self.assertEqual((r["traded_from"], r["traded_to"]), ("2026-10-26", "2026-10-27"))
        self.assertEqual((r["shares_before"], r["shares_after"]), (45100000.0, 45220000.0))
        self.assertEqual((r["intimated"], r["regulation"]), ("2026-10-28", "7(2)"))
        self.assertEqual(r["trading_days_to_disclose"], 1)
        self.assertFalse(r["late"])
        bare = self.by[("BETACEM", "Unknown Filer", "pledge_released")]
        self.assertIsNone(bare["qty"])
        self.assertIsNone(bare["value"])
        self.assertNotIn("late", bare)
        self.assertEqual(ins.num("5,000"), 5000.0)
        # live rows can carry only buyQuantity / sellquantity (lower-case q) and buyValue / sellValue
        o = self.by[("OTHERCO", "Other Holder", "other")]
        self.assertEqual((o["side"], o["qty"], o["value"], o["category_key"]), ("SELL", 2320.0, 3294168.0, "other"))
        self.assertIsNone(ins.num("-"))

    def test_classification(self):
        kinds = {(r["symbol"], r["person"]): r["kind"] for r in self.rows}
        self.assertEqual(kinds[("SAMPLECO", "Anita Rao")], "market_sell")          # a director, not a promoter
        self.assertEqual(kinds[("ALPHAPHARM", "Suresh Iyer")], "other")            # ESOP
        self.assertEqual(kinds[("ALPHAPHARM", "Alpha Family Trust")], "other")     # inter-se transfer
        self.assertEqual(kinds[("GAMMATEX", "Gamma Promoters LLP")], "promoter_market_buy")
        self.assertEqual(ins.classify("Pledge Creation", "Pledge Creation", "Promoter Group"), "pledge_created")
        self.assertEqual(ins.classify("Invocation of pledge", "Invocation of pledge", "Promoters"), "pledge_invoked")
        self.assertEqual(ins.classify("Sell", "Off Market", "Promoters"), "other")
        self.assertEqual(ins.classify("Sell", "Market Sale", "Promoter Group"), "promoter_market_sell")
        self.assertEqual(ins.category_of("Designated Employees"), "employee")
        self.assertEqual(ins.category_of("Key Managerial Personnel"), "kmp")

    def test_late_disclosure_counts_trading_days_not_calendar_days(self):
        r = self.by[("BETACEM", "Kiran Mehta", "market_sell")]
        # traded 15 Oct, told the company (intimDt) 26 Oct: 16, 19, 21, 22, 23, 26 (20 Oct, Dussehra, is a
        # holiday), so the insider was late; the company then took 2 trading days (27, 28): on time
        self.assertEqual(r["trading_days_to_disclose"], 6)
        self.assertTrue(r["late"])
        self.assertEqual(r["trading_days_company"], 2)
        self.assertFalse(r["company_late"])
        z = self.by[("ZETAAUTO", "Zeta Director", "market_buy")]
        # told the company the next day, but the company took 23, 26, 27, 28 Oct to tell NSE
        self.assertEqual((z["trading_days_to_disclose"], z["late"]), (1, False))
        self.assertEqual((z["trading_days_company"], z["company_late"]), (4, True))
        self.assertIn("the company took 4 trading days to tell NSE (LATE: over 2, reg 7(2)(b))",
                      ins.describe_pit(z))
        inv = self.by[("DEMOFIN", "Demo Holdings Private Limited", "pledge_invoked")]
        self.assertEqual(inv["trading_days_to_disclose"], 2)
        self.assertFalse(inv["late"])

    def test_filters(self):
        start, end = datetime.date(2026, 10, 21), datetime.date(2026, 10, 28)
        self.assertEqual(len(ins.select_pit(self.rows, start=start, end=end)), 10)  # GAMMATEX is older
        promo = ins.select_pit(self.rows, categories=["promoter"])
        self.assertTrue(all(r["category_key"] == "promoter" for r in promo))
        self.assertEqual(len(promo), 6)
        self.assertEqual([r["symbol"] for r in ins.select_pit(self.rows, ["demofin"], kind="pledge")],
                         ["DEMOFIN", "DEMOFIN"])
        self.assertEqual(len(ins.select_pit(self.rows, kind="market")), 5)
        self.assertEqual([r["symbol"] for r in ins.select_pit(self.rows, min_cr=10)], ["ALPHAPHARM"])
        with self.assertRaises(ins.InsiderError):
            ins.select_pit(self.rows, categories=["astrologer"])

    def test_bad_answers(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_pit({"acqNameList": []})
        self.assertEqual(ins.parse_pit([]), [])


class Deals(unittest.TestCase):
    def setUp(self):
        self.as_on, self.rows = ins.parse_deals(doc(DEALS))

    def test_bulk_block_short(self):
        self.assertEqual(self.as_on, datetime.date(2026, 10, 28))
        self.assertEqual([r["kind"] for r in self.rows], ["bulk", "bulk", "bulk", "block", "short"])
        b = self.rows[0]
        self.assertEqual((b["symbol"], b["side"], b["qty"], b["price"]), ("SAMPLECO", "BUY", 650000.0, 117.2))
        self.assertEqual(b["value_cr"], 7.62)
        blk = self.rows[3]
        self.assertEqual(blk["value_cr"], 140.5)
        s = self.rows[4]
        self.assertEqual((s["side"], s["client"], s["value"]), ("SHORT", None, None))

    def test_filters_and_watched_clients(self):
        self.assertEqual(len(ins.select_deals(self.rows, client="mutual fund")), 1)
        self.assertEqual(len(ins.select_deals(self.rows, kind="bulk", min_cr=5)), 2)
        self.assertEqual(len(ins.select_deals(self.rows, symbols=["samplECO"])), 2)
        watch = ins.watch_from({"watch_clients": ["Example Global"]})
        line = ins.describe_deal(self.rows[0], watch)
        self.assertIn("<- watched client: Example Global", line)
        self.assertNotIn("<-", ins.describe_deal(self.rows[1], watch))

    def test_bad_answer(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_deals({"hello": 1})


class FakeNet(object):
    def __init__(self):
        self.calls = []

    def nse_json(self, path, referer=None):
        self.calls.append((path, referer))
        return doc(PIT) if "corporates-pit" in path else doc(DEALS)


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = ins.NET_FACTORY

    def tearDown(self):
        ins.NET_FACTORY = self.saved

    def test_nse_paths_with_dd_mm_yyyy_dates(self):
        net = FakeNet()
        ins.NET_FACTORY = lambda: net
        code, out, _ = run(["insider", "--now", NOW, "--from", "2026-10-21", "--config", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertEqual(net.calls[0][0], "/api/corporates-pit?index=equities&from_date=21-10-2026"
                                          "&to_date=28-10-2026")
        run(["deals", "--now", NOW, "--config", "/nonexistent"])
        self.assertEqual(net.calls[1][0], "/api/snapshot-capital-market-largedeal")

    def test_nse_blocked(self):
        def blocked():
            class N(object):
                def nse_json(self, path, referer=None):
                    raise M.NetError("NSE did not answer %s" % path.split("?")[0])
            return N()
        ins.NET_FACTORY = blocked
        code, _, err = run(["deals", "--config", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: NSE did not answer /api/snapshot-capital-market-largedeal"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "config.json")
        with open(self.cfg, "w") as fh:
            json.dump({"watch_clients": ["example mutual fund"],
                       "insider_watch": {"symbols": ["DEMOFIN"], "names": ["Ramesh Kumar Sample"]}}, fh)
        self.state = os.path.join(self.d, "state", "insider.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_insider_report(self):
        code, out, _ = run(["insider", "--offline", PIT, "--now", NOW, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("SEBI PIT disclosures on NSE, 2026-10-21 to 2026-10-28: 10 row(s)", out)
        self.assertIn("Promoter market trades and pledges:", out)
        self.assertIn("SAMPLECO   Ramesh Kumar Sample (Promoters): BUY 1,20,000 Equity Shares, Rs 1.41 cr, Market "
                      "Purchase; traded 2026-10-26 to 2026-10-27, told the company 1 trading day later; shares held "
                      "4,51,00,000 -> 4,52,20,000   <- PROMOTER MARKET BUY, on your watch list", out)
        self.assertIn("PLEDGE INVOKED (the lender enforced it)", out)
        self.assertIn("(LATE: over 2, reg 7(2)(a))", out)
        self.assertIn("within 2 trading days", out)
        self.assertIn("Rs 10 lakh", out)
        self.assertIn("not inside information", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_insider_filters_and_json(self):
        code, out, _ = run(["insider", "--offline", PIT, "--now", NOW, "--config", self.cfg, "--category",
                            "director", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)["rows"]
        self.assertEqual([r["person"] for r in rows], ["Anita Rao", "Zeta Director"])
        code, out, _ = run(["insider", "--offline", PIT, "--now", NOW, "--config", self.cfg, "--from",
                            "01-10-2026", "--to", "2026-10-12", "--json"])
        self.assertEqual([r["symbol"] for r in json.loads(out)["rows"]], ["GAMMATEX"])
        code, out, _ = run(["insider", "--offline", PIT, "--now", NOW, "--config", self.cfg, "--symbol", "NONE"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing matched", out)
        code, _, err = run(["insider", "--offline", PIT, "--from", "2026-10-28", "--to", "2026-10-01"])
        self.assertEqual(code, 1)
        self.assertIn("is after", err)
        code, _, err = run(["insider", "--offline", PIT, "--from", "yesterday"])
        self.assertEqual(code, 1)
        self.assertIn("error: --from wants", err)

    def test_deals_report(self):
        code, out, _ = run(["deals", "--offline", DEALS, "--now", NOW, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("NSE large deals for 2026-10-28: 3 bulk, 1 block, 1 short.", out)
        self.assertIn("BLOCK DEMOFIN    Demo Finance Limited: BUY 20,00,000 @ Rs 702.50 = Rs 140.50 cr", out)
        self.assertIn("<- watched client: example mutual fund", out)
        self.assertIn("08:45-09:00 and 14:05-14:20 IST", out)
        self.assertIn("0.5%", out)
        self.assertNotIn("not today", out)
        code, out, _ = run(["deals", "--offline", DEALS, "--now", NOW, "--config", self.cfg, "--watched-only",
                            "--json"])
        self.assertEqual([r["symbol"] for r in json.loads(out)["rows"]], ["DEMOFIN"])

    def test_new_alerts_once(self):
        base = ["new", "--offline-pit", PIT, "--offline-deals", DEALS, "--config", self.cfg, "--state", self.state,
                "--portfolio", os.path.join(self.d, "none.json")]
        code, out, _ = run(base + ["--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("first run", out)
        self.assertIn("NEW 2026-10-28 18:12  SAMPLECO", out)
        self.assertIn("NEW BLOCK DEMOFIN", out)
        self.assertNotIn("Anita Rao", out)                    # a director's small sale: not a highlight
        self.assertNotIn("GAMMATEX", out)                     # older than the first-run window
        self.assertIn("--all shows them", out)
        with open(self.state) as fh:
            st = json.load(fh)
        self.assertEqual(st["last_check"], "2026-10-28T20:00:00+05:30")
        self.assertEqual(len(st["seen"]), 10 + 5)             # 10 PIT rows since 25 Oct + 5 deals
        code, out, _ = run(base + ["--now", "2026-10-28 21:00"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing new worth showing", out)

    def test_new_all_dry_run_and_partial(self):
        base = ["new", "--offline-pit", PIT, "--config", self.cfg, "--state", self.state, "--now", NOW,
                "--portfolio", os.path.join(self.d, "none.json")]
        code, out, _ = run(base + ["--offline-deals", DEALS, "--all", "--dry-run"])
        self.assertIn("Anita Rao", out)
        self.assertFalse(os.path.exists(self.state))
        code, out, _ = run(base + ["--offline-deals", os.path.join(self.d, "missing.json")])
        self.assertEqual(code, 0)
        self.assertIn("partial: large deals not read", out)
        code, _, err = run(["new", "--offline-pit", os.path.join(self.d, "x.json"), "--offline-deals",
                            os.path.join(self.d, "y.json"), "--state", self.state, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("error: insider disclosures not read", err)

    def test_watch(self):
        code, out, _ = run(["watch", "--offline-pit", PIT, "--offline-deals", DEALS, "--config", self.cfg,
                            "--now", NOW, "--portfolio", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("Watching 1 symbol(s), 1 name(s), 1 client(s)", out)
        self.assertIn("Ramesh Kumar Sample", out)
        self.assertIn("DEMOFIN", out)
        self.assertNotIn("BETACEM", out)
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"positions": {"BETACEM|CNC": {"qty": 10}}}, fh)
        code, out, _ = run(["watch", "--offline-pit", PIT, "--offline-deals", DEALS, "--config", "/nonexistent",
                            "--now", NOW, "--portfolio", pf, "--json"])
        data = json.loads(out)
        self.assertEqual({r["symbol"] for r in data["pit"]} | {r["symbol"] for r in data["deals"]}, {"BETACEM"})
        code, _, err = run(["watch", "--offline-pit", PIT, "--offline-deals", DEALS, "--config", "/nonexistent",
                            "--portfolio", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 1)
        self.assertIn("nothing to watch", err)

    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-insider", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-insider\n"))
        low = " ".join(text.lower().split())
        self.assertIn("not financial advice", low)
        self.assertIn("yes", low)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "trader-pelosi")))


if __name__ == "__main__":
    unittest.main()
