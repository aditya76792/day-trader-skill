"""trader-analyst, India edition: SEBI integrated filings and NSE's older results
listing, Ind AS XBRL parsing (contexts by date, full-rupee values), growth across
filings, the CSV fallback and the CLI. Offline.

Fixtures: the Q2 FY27 consolidated file is trimmed from TCS's real integrated
filing (real OneD values); the other quarters and files are plausible made-up
values in the same shapes."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

facts = load("trader-analyst", "facts")
M = facts.M
DIR = os.path.join(FIX, "trader-analyst")
CR = 1e7
D = datetime.date
Q2 = "INTEGRATED_FILING_INDAS_1734607_08102026100810_WEB.xml"
Q1_ORIG = "INTEGRATED_FILING_INDAS_1700001_10072026193000_WEB.xml"
Q1_REV = "INTEGRATED_FILING_INDAS_1700420_25072026100000_WEB.xml"
Q4 = "INTEGRATED_FILING_INDAS_1650001_09042026190000_WEB.xml"
OLD = "INDAS_TCS_Q3FY25_CONS.xml"


def read(name, mode="r"):
    with open(os.path.join(DIR, name), mode) as fh:
        return fh.read()


def integrated():
    return facts.parse_listing(json.loads(read("integrated-TCS.json")), "TCS", "integrated")


def old_listing():
    return facts.parse_listing(json.loads(read("results-TCS.json")), "TCS", "old")


def xbrl(name):
    return facts.parse_xbrl(read(name, "rb"))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


class Listing(unittest.TestCase):
    def test_integrated_filing_rows(self):
        rows = integrated()
        self.assertEqual(len(rows), 7)                      # the Governance filing is skipped
        q2 = rows[0]
        self.assertEqual((q2["to"], q2["basis"], q2["listing"]), ("2026-09-30", "consolidated", "integrated"))
        self.assertEqual(q2["company"], "Tata Consultancy Services Limited")
        self.assertEqual(q2["filed"], "08-Oct-2026 22:07:33")
        self.assertTrue(q2["xbrl"].endswith(Q2))
        self.assertEqual(rows[1]["basis"], "standalone")    # "Non-Consolidated"
        self.assertEqual([r["to"] for r in rows if r["basis"] == "consolidated"],
                         ["2026-09-30", "2026-06-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])

    def test_old_listing_is_sorted_by_period_end(self):
        rows = old_listing()
        self.assertEqual([r["to"] for r in rows], ["2024-12-31", "2024-09-30", "2024-06-30"])
        self.assertEqual(rows[0]["filed"], "09-Jan-2025 19:00")
        self.assertFalse(rows[0]["bank"])
        with self.assertRaises(facts.FactsError):
            facts.parse_listing("nonsense", "X")

    def test_a_revision_wins_and_one_basis_per_table(self):
        chosen, basis, note = facts.choose_filings(integrated(), "consolidated", 6)
        self.assertEqual(basis, "consolidated")
        self.assertIsNone(note)
        self.assertEqual([c["to"] for c in chosen], ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31",
                                                     "2025-09-30"])
        self.assertTrue(chosen[1]["xbrl"].endswith(Q1_REV))
        self.assertTrue(chosen[1]["revised"])
        self.assertEqual(chosen[1]["filed"], "25-Jul-2026 10:00:00")
        self.assertEqual(chosen[0]["bases"], ["consolidated", "standalone"])
        chosen, basis, _ = facts.choose_filings(integrated(), "standalone", 6)
        self.assertEqual([c["to"] for c in chosen], ["2026-09-30"])
        only_standalone = [e for e in integrated() if e["basis"] == "standalone"]
        chosen, basis, note = facts.choose_filings(only_standalone, "consolidated", 6)
        self.assertEqual(basis, "standalone")
        self.assertIn("no consolidated results", note)

    def test_dates_as_nse_writes_them(self):
        for text in ("30-SEP-2026", "30-Sep-2026", "30-Sep-2026 19:42", "2026-09-30", "30-09-2026"):
            self.assertEqual(facts.parse_date(text), D(2026, 9, 30), text)
        self.assertEqual(facts.parse_datetime("08-Oct-2026 22:07:33"), datetime.datetime(2026, 10, 8, 22, 7, 33))
        self.assertIsNone(facts.parse_date("-"))
        self.assertIsNone(facts.parse_datetime(None))


class Xbrl(unittest.TestCase):
    def setUp(self):
        self.x = xbrl(Q2)

    def test_contexts(self):
        c = self.x["contexts"]
        self.assertEqual((c["OneD"]["start"], c["OneD"]["end"]), ("2026-07-01", "2026-09-30"))
        self.assertEqual((c["FourD"]["start"], c["FourD"]["end"]), ("2026-04-01", "2026-09-30"))
        self.assertEqual(c["OneI"]["instant"], "2026-09-30")
        self.assertIn("PY_I", c)
        self.assertTrue(c["OneD_Seg1"]["dims"])
        self.assertEqual(facts._duration_groups(self.x), {("2026-07-01", "2026-09-30"): ["OneD"]})
        self.assertEqual(facts.text_fact(self.x, "rounding"), "Crores")
        self.assertEqual(facts.text_fact(self.x, "currency"), "INR")
        self.assertEqual(facts.text_fact(self.x, "period_end"), "2026-09-30")

    def test_real_values_are_full_rupees_never_scaled_by_the_rounding(self):
        q = facts.filing_quarters(self.x, "2026-09-30")
        cur = q["current"]
        self.assertEqual(cur["revenue"], 731880000000.0)      # Rs 73,188 crore, read as is
        self.assertEqual(cur["other_income"], 13370000000.0)
        self.assertEqual(cur["finance_costs"], 2520000000.0)
        self.assertEqual(cur["depreciation"], 12620000000.0)
        self.assertEqual(cur["pbt"], 186380000000.0)
        self.assertEqual(cur["tax"], 47040000000.0)
        self.assertEqual(cur["net_profit"], 139340000000.0)
        self.assertEqual(cur["net_profit_owners"], 138840000000.0)
        self.assertEqual(cur["exceptional"], 0.0)              # the -668 cr is the year-to-date (FourD)
        self.assertEqual((cur["eps_basic"], cur["eps_diluted"]), (38.37, 38.37))
        self.assertEqual(cur["unit"], "iso4217:INR")
        self.assertEqual(cur["tags"]["net_profit"], "ProfitLossForPeriod")
        self.assertIsNone(q["preceding"])                      # an integrated filing has no comparison columns
        self.assertIsNone(q["year_ago"])
        with self.assertRaises(facts.FactsError):
            facts.filing_quarters(self.x, "2026-12-31")

    def test_a_unit_name_never_scales_only_the_manual_override_does(self):
        doc = read(Q2).replace("<xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>\n<xbrli:unit id=\"INRPer",
                               "<xbrli:measure>in-capmkt:INRLakhs</xbrli:measure></xbrli:unit>\n<xbrli:unit id=\"INRPer")
        x = facts.parse_xbrl(doc)
        self.assertEqual(x["units"]["INR"], "in-capmkt:INRLakhs")
        self.assertEqual(facts.filing_quarters(x, "2026-09-30")["current"]["revenue"], 731880000000.0)
        q = facts.filing_quarters(self.x, "2026-09-30", scale=CR)
        self.assertEqual(q["current"]["revenue"], 731880000000.0 * CR)
        self.assertEqual(q["current"]["eps_diluted"], 38.37)   # EPS is never scaled

    def test_q4_quarter_is_picked_by_dates_not_by_id(self):
        doc = read(Q4).replace('id="OneD"', 'id="TMP"').replace('id="FourD"', 'id="OneD"').replace(
            'id="TMP"', 'id="FourD"').replace('contextRef="OneD"', 'contextRef="TMP"').replace(
            'contextRef="FourD"', 'contextRef="OneD"').replace('contextRef="TMP"', 'contextRef="FourD"')
        x = facts.parse_xbrl(doc)
        self.assertEqual((x["contexts"]["OneD"]["start"], x["contexts"]["OneD"]["end"]), ("2025-04-01", "2026-03-31"))
        cur = facts.filing_quarters(x, "2026-03-31")["current"]
        self.assertEqual((cur["start"], cur["revenue"]), ("2026-01-01", 70100 * CR))

    def test_q1_where_year_to_date_equals_the_quarter(self):
        cur = facts.filing_quarters(xbrl(Q1_REV), "2026-06-30")["current"]
        self.assertEqual(cur["revenue"], 71520 * CR)
        self.assertEqual(cur["exceptional"], -668 * CR)

    def test_old_format_comparison_columns_are_still_used(self):
        q = facts.filing_quarters(xbrl(OLD), "2024-12-31")
        self.assertEqual(q["preceding"]["end"], "2024-09-30")
        self.assertEqual(q["year_ago"]["end"], "2023-12-31")
        self.assertEqual(q["current"]["revenue"], 63973 * CR)
        self.assertEqual(facts.text_fact(xbrl(OLD), "rounding"), "Crores")

    def test_local_names_whatever_the_namespace(self):
        doc = read(Q2).replace("in-capmkt", "in-other").replace(
            "http://www.sebi.gov.in/xbrl/2026-01-31/in-other", "http://example.org/other-taxonomy")
        self.assertEqual(facts.filing_quarters(facts.parse_xbrl(doc), "2026-09-30")["current"]["revenue"],
                         731880000000.0)

    def test_bad_xml(self):
        with self.assertRaises(facts.FactsError):
            facts.parse_xbrl(b"<not xml")
        with self.assertRaises(facts.FactsError):
            facts.parse_xbrl(b"<xbrl></xbrl>")
        self.assertIn("OneD", facts.parse_xbrl(b"\xef\xbb\xbf" + read(Q2, "rb"))["contexts"])


class Numbers(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse_symbol("TCS", facts.Source(DIR))

    def test_latest_quarter_tcs_q2_fy27(self):
        q = self.r["rows"][0]
        self.assertEqual((q["end"], q["source"]), ("2026-09-30", "filing"))
        self.assertEqual(q["revenue"], 73188 * CR)
        self.assertEqual(q["net_profit"], 13884 * CR)          # attributable to owners of the parent
        self.assertEqual(q["net_profit_total"], 13934 * CR)
        self.assertEqual(q["ebitda"], 18815 * CR)              # 18638 + 252 + 1262 - 1337
        self.assertEqual((q["operating_margin"], q["net_margin"], q["eps"]), (25.7, 19.0, 38.37))
        self.assertEqual((q["revenue_yoy"], q["revenue_qoq"]), (10.1, 2.3))   # vs 66,500 and the revised 71,520
        self.assertEqual((q["net_profit_yoy"], q["eps_yoy"]), (14.7, 14.7))
        self.assertEqual(q["growth_from"], "table")            # from the earlier filings, not comparison columns
        self.assertEqual(self.r["basis"], "consolidated")

    def test_growth_needs_the_earlier_filings(self):
        self.assertEqual([(r["end"], r["source"]) for r in self.r["rows"]],
                         [("2026-09-30", "filing"), ("2026-06-30", "filing"), ("2026-03-31", "filing"),
                          ("2025-12-31", "filing"), ("2025-09-30", "filing")])
        self.assertEqual((self.r["read"], self.r["tried"]), (6, 6))
        self.assertEqual(self.r["listings_used"], ["integrated", "old"])   # Dec 2024 comes from the old listing
        self.assertEqual(self.r["rows"][3]["revenue_yoy"], 7.5)            # 68,800 vs 63,973
        self.assertEqual(self.r["rows"][4]["revenue_yoy"], 3.5)            # vs the old filing's comparison column
        self.assertEqual(self.r["rows"][1]["revenue"], 71520 * CR)         # the revised Q1 filing
        self.assertEqual(self.r["ttm"]["revenue"], 283608 * CR)
        self.assertEqual(self.r["ttm"]["eps"], 145.11)
        fewer = facts.analyse_symbol("TCS", facts.Source(DIR), quarters=3, filings_n=3)
        self.assertIsNone(fewer["rows"][0]["revenue_yoy"])
        self.assertEqual(fewer["listings_used"], ["integrated"])

    def test_indian_fiscal_quarters_and_due_dates(self):
        self.assertEqual(facts.quarter_label(D(2026, 9, 30)), "Q2 FY27")
        self.assertEqual(facts.quarter_label(D(2026, 3, 31)), "Q4 FY26")
        self.assertEqual(facts.quarter_label(D(2025, 12, 31)), "Q3 FY26")
        self.assertEqual(facts.results_due(D(2026, 9, 30)), D(2026, 11, 14))
        self.assertEqual(facts.results_due(D(2026, 3, 31)), D(2026, 5, 30))
        self.assertEqual(facts.next_quarter_end(D(2026, 9, 30)), D(2026, 12, 31))
        self.assertEqual(facts.next_quarter_end(D(2026, 12, 31)), D(2027, 3, 31))

    def test_what_changed_in_words(self):
        lines = facts.changes(self.r)
        self.assertIn("Quarter ending 2026-09-30 (Q2 FY27, consolidated, audited, filed 08-Oct-2026 22:07:33)",
                      lines[0])
        self.assertIn("revenue Rs 73,188.00 cr (+10.1% vs a year earlier, +2.3% vs the quarter before)", lines[0])
        self.assertIn("net profit Rs 13,884.00 cr (+14.7% YoY), EPS Rs 38.37 (+14.7% YoY)", lines[0])
        self.assertTrue(any(l.startswith("Operating margin (EBITDA, approx.) 25.7% (+1.0 points") for l in lines))

    def test_one_offs_are_flagged(self):
        odd = {"basis": None, "rows": [facts.derive({"revenue": 100.0, "pbt": 10.0, "tax": 0.5, "net_profit": 40.0,
                                                     "other_income": 5.0, "finance_costs": 0, "depreciation": 0,
                                                     "exceptional": -3.0, "end": "2026-09-30"})]}
        lines = facts.changes(odd)
        self.assertTrue(any("Effective tax rate 5.0%" in l for l in lines))
        self.assertTrue(any("Net profit is above EBITDA" in l for l in lines))
        self.assertTrue(any("Other income is" in l for l in lines))
        self.assertTrue(any("Exceptional items of" in l for l in lines))

    def test_stale_and_upcoming_notes(self):
        self.assertIn("were due by 2027-02-14", facts.stale_note(self.r, D(2027, 3, 1))[0])
        self.assertIn("Next results (quarter ending 2026-12-31) are due by 2027-02-14",
                      facts.stale_note(self.r, D(2027, 1, 5))[0])
        self.assertEqual(facts.stale_note(self.r, D(2026, 10, 28)), [])


class FromFile(unittest.TestCase):
    def test_csv_quarters_in_crore(self):
        r = facts.analyse_file("DEMOIND", os.path.join(DIR, "quarters-DEMOIND.csv"))
        self.assertEqual(r["rows"][0]["revenue"], 1250 * CR)
        self.assertEqual(r["rows"][0]["revenue_yoy"], 25.0)
        self.assertEqual(r["ttm"]["revenue"], 4550 * CR)
        self.assertEqual(r["ttm"]["eps"], 63.5)

    def test_json_and_bad_rows(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "q.json")
            with open(p, "w") as fh:
                json.dump({"quarters": [{"quarter_end": "30-Sep-2026", "revenue": "1,250", "net_profit": 187.5,
                                         "eps": 18.7}]}, fh)
            self.assertEqual(facts.analyse_file("DEMOIND", p)["rows"][0]["net_margin"], 15.0)
            for bad in ([{"quarter_end": "someday", "revenue": 1}], [{"quarter_end": "2026-09-30", "revenue": "lots"}]):
                with open(p, "w") as fh:
                    json.dump(bad, fh)
                with self.assertRaises(facts.FactsError):
                    facts.analyse_file("DEMOIND", p)
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    def __init__(self, block=()):
        self.paths, self.urls, self.block = [], [], block

    def nse_json(self, path, referer=None):
        self.paths.append((path, referer))
        kind = "integrated" if "integrated-filing" in path else "old"
        if kind in self.block:
            raise M.NetError("NSE did not answer %s" % path.split("?")[0])
        return json.loads(read("integrated-TCS.json" if kind == "integrated" else "results-TCS.json"))

    def nse_archive(self, url, binary=False):
        self.urls.append(url)
        try:
            return read(url.rsplit("/", 1)[-1], "rb")
        except OSError:
            raise M.NetError("nsearchives.nseindia.com answered HTTP 404")


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = facts.NET_FACTORY

    def tearDown(self):
        facts.NET_FACTORY = self.saved

    def use(self, net):
        facts.NET_FACTORY = lambda: net
        return net

    def test_integrated_endpoint_first_then_the_old_listing(self):
        net = self.use(FakeNet())
        r = facts.analyse_symbol("TCS", facts.Source())
        self.assertEqual(net.paths[0][0], "/api/integrated-filing-results?index=equities&symbol=TCS"
                                          "&type=Integrated%20Filing-%20Financials&page=1&size=20")
        self.assertEqual(net.paths[1][0], "/api/corporates-financial-results?index=equities&symbol=TCS"
                                          "&period=Quarterly")
        self.assertEqual(len(net.urls), 6)
        self.assertTrue(all(u.startswith("https://nsearchives.nseindia.com/corporate/xbrl/") for u in net.urls))
        self.assertEqual(r["rows"][0]["revenue"], 73188 * CR)
        net = self.use(FakeNet())
        facts.analyse_symbol("TCS", facts.Source(), quarters=4, filings_n=5)
        self.assertEqual(len(net.paths), 1)            # five integrated quarters: no need for the old listing

    def test_integrated_blocked_falls_back_and_says_partial(self):
        self.use(FakeNet(block=("integrated",)))
        r = facts.analyse_symbol("TCS", facts.Source())
        self.assertEqual(r["rows"][0]["end"], "2024-12-31")
        self.assertIn("partial: integrated filings: NSE did not answer /api/integrated-filing-results",
                      r["listing_notes"][0])

    def test_nse_blocked_is_one_plain_error(self):
        self.use(FakeNet(block=("integrated", "old")))
        code, out, err = run(["TCS", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("error: TCS: integrated filings: NSE did not answer", err)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.metrics = os.path.join(self.d, "trader", "metrics.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_report_and_metrics_file(self):
        code, out, err = run(["TCS", "--offline", DIR, "--metrics", self.metrics, "--now", "2026-10-28 10:00"])
        self.assertEqual(code, 0, err)
        self.assertIn("Tata Consultancy Services Limited (TCS), NSE. Rs crore except EPS (Rs). Consolidated "
                      "(also filed: standalone).", out)
        self.assertIn("2026-09-30  Q2 FY27   73,188.00   +10.1%    +2.3%   25.7%   19.0%  13,884.00   +14.7%    "
                      "38.37   +14.7%", out)
        self.assertIn("Last 4 quarters (to 2026-09-30): revenue Rs 2,83,608.00 cr", out)
        self.assertIn("full rupees (unit iso4217:INR), shown here in crore; the filing's level of rounding is "
                      "Crores", out)
        self.assertIn("attributable to owners of the parent", out)
        self.assertIn("Unofficial parsing of exchange filings", out)
        self.assertNotIn(chr(0x2014), out)
        with open(self.metrics) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-analyst")
        t = {r["symbol"]: r for r in doc["companies"]}["TCS"]
        self.assertEqual((t["revenue_cr"], t["net_profit_cr"], t["eps"]), (73188.0, 13884.0, 38.37))
        self.assertEqual((t["quarter"], t["basis"], t["operating_margin"]), ("Q2 FY27", "consolidated", 25.7))
        self.assertEqual((t["revenue_ttm_cr"], t["eps_ttm"]), (283608.0, 145.11))
        run(["TCS", "--offline", DIR, "--metrics", self.metrics, "--standalone"])
        with open(self.metrics) as fh:
            self.assertEqual(json.load(fh)["companies"][0]["basis"], "standalone")

    def test_json_output(self):
        code, out, err = run(["TCS", "--offline", DIR, "--json", "--quarters", "3", "--no-write"])
        self.assertEqual(code, 0, err)
        row = json.loads(out)[0]["rows"][0]
        self.assertEqual((row["revenue_cr"], row["net_profit_cr"], row["net_profit_total_cr"]),
                         (73188.0, 13884.0, 13934.0))
        self.assertEqual(row["tags"]["revenue"], "RevenueFromOperations")
        self.assertNotIn("_year_ago", row)
        self.assertEqual(len(json.loads(out)[0]["rows"]), 3)

    def test_more_filings_and_partial_reads(self):
        code, out, err = run(["TCS", "--offline", DIR, "--no-write", "--quarters", "8", "--filings", "8"])
        self.assertEqual(code, 0, err)
        self.assertIn("2023-12-31  Q3 FY24", out)
        self.assertIn("  c\n", out)
        self.assertIn("partial: 6 of 8 XBRL files read", out)
        self.assertIn("older results listing", out)

    def test_a_newer_filing_without_xbrl_is_named(self):
        for name in os.listdir(DIR):
            shutil.copy(os.path.join(DIR, name), self.d)
        doc = json.loads(read("integrated-TCS.json"))
        doc["data"].insert(0, dict(doc["data"][0], qe_Date="31-DEC-2026", broadcast_Date="12-Jan-2027 21:00:00",
                                   xbrl=""))
        with open(os.path.join(self.d, "integrated-TCS.json"), "w") as fh:
            json.dump(doc, fh)
        code, out, err = run(["TCS", "--offline", self.d, "--no-write", "--now", "2027-01-13"])
        self.assertEqual(code, 0, err)
        self.assertIn("A newer filing (quarter ending 2026-12-31, consolidated, filed 12-Jan-2027 21:00:00) could "
                      "not be read (no XBRL file in the listing)", out)
        self.assertIn("partial: 5 of 6 XBRL files read", out)

    def test_from_file(self):
        code, out, err = run(["DEMOIND", "--from-file", os.path.join(DIR, "quarters-DEMOIND.csv"), "--no-write"])
        self.assertEqual(code, 0, err)
        self.assertIn("Last 4 quarters (to 2026-09-30): revenue Rs 4,550.00 cr, net profit Rs 637.00 cr, "
                      "EPS Rs 63.50.", out)
        self.assertIn("came from your file", out)
        code, _, err = run(["DEMOIND", "TCS", "--from-file", "x.csv"])
        self.assertEqual(code, 1)
        self.assertIn("one symbol", err)

    def test_unknown_symbol_partial_and_bad_input(self):
        code, out, err = run(["NOPE", "--offline", DIR, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("error: NOPE: integrated filings: saved integrated listing not found", err)
        code, out, err = run(["TCS", "NOPE", "--offline", DIR, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("Tata Consultancy Services Limited", out)
        self.assertIn("partial: 1 of 2 symbols read", err)
        code, _, err = run(["bad symbol!", "--offline", DIR])
        self.assertEqual(code, 1)
        self.assertIn("not an NSE symbol", err)
        self.assertEqual(run([])[0], 2)


if __name__ == "__main__":
    unittest.main()
