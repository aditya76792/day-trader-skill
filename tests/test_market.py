"""trader-market: the shared India core. These pin the statutory numbers so a change is deliberate."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

M = load("trader-market", "market")
D = datetime.date
DT = datetime.datetime


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = M.main(argv)
    return code, out.getvalue(), err.getvalue()


class Clock(unittest.TestCase):
    def test_sessions(self):
        self.assertEqual(M.market_status(DT(2026, 10, 28, 8, 59))["phase"], "pre-market")
        self.assertEqual(M.market_status(DT(2026, 10, 28, 9, 0))["phase"], "pre-open")
        self.assertEqual(M.market_status(DT(2026, 10, 28, 9, 8))["phase"], "pre-open-matching")
        self.assertTrue(M.market_status(DT(2026, 10, 28, 9, 15))["open"])
        self.assertTrue(M.market_status(DT(2026, 10, 28, 15, 29))["open"])
        self.assertEqual(M.market_status(DT(2026, 10, 28, 15, 30))["phase"], "closed")
        self.assertEqual(M.market_status(DT(2026, 10, 28, 15, 45))["phase"], "closing-session")

    def test_holidays_2026_from_nse_master(self):
        for d in (D(2026, 1, 15), D(2026, 1, 26), D(2026, 3, 3), D(2026, 10, 20), D(2026, 11, 10),
                  D(2026, 11, 24), D(2026, 12, 25)):
            self.assertFalse(M.trading_day(d), d)
        self.assertEqual(M.market_status(DT(2026, 11, 10, 10, 0))["phase"], "holiday")
        self.assertTrue(M.trading_day(D(2026, 10, 28)))
        self.assertFalse(M.trading_day(D(2026, 10, 31)))   # Saturday
        self.assertEqual(len([d for d in M.holidays_for(2026) if d.weekday() < 5]), 16)

    def test_muhurat_is_a_special_session(self):
        st = M.market_status(DT(2026, 11, 8, 18, 0))
        self.assertIn("Muhurat", st["label"])
        self.assertFalse(st["open"])

    def test_unknown_year_warns(self):
        self.assertIn("No 2027 holiday list", M.market_status(DT(2027, 1, 4, 10, 0))["warning"])

    def test_extra_holidays_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            with open(path, "w") as fh:
                json.dump({"FO": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"}]}, fh)
            M._extra_loaded.pop(path, None)
            self.assertIn(D(2027, 1, 26), M._load_extra_holidays(path))

    def test_ist_conversions(self):
        t = M.ts_from_ist(DT(2026, 10, 28, 9, 15))
        self.assertEqual(M.ist_from_ts(t), DT(2026, 10, 28, 9, 15))
        self.assertEqual(M.parse_now("2026-10-28T03:45:00Z"), DT(2026, 10, 28, 9, 15))
        self.assertEqual(M.utc_iso(DT(2026, 10, 28, 9, 15)), "2026-10-28T03:45:00Z")
        with self.assertRaises(M.MarketError):
            M.parse_now("tomorrow")


class Expiries(unittest.TestCase):
    def test_nifty_matches_nse_list(self):
        # NSE /api/option-chain-contract-info?symbol=NIFTY on 2026-10-28 listed:
        # 03-Nov, 09-Nov (Tue 10 Nov is a holiday), 17-Nov, 23-Nov (monthly, Tue 24 Nov holiday), 01-Dec, 29-Dec
        got = [e["date"] for e in M.expiries("NIFTY", D(2026, 10, 28), weeks=5, months=2)]
        self.assertEqual(got, ["2026-11-03", "2026-11-09", "2026-11-17", "2026-11-23", "2026-12-01",
                               "2026-12-29"])

    def test_sensex_thursday_and_banknifty_monthly_only(self):
        s = M.expiries("SENSEX", D(2026, 10, 28), weeks=2, months=1)
        self.assertEqual(s[0], {"date": "2026-10-29", "kind": "monthly", "weekday": "Thursday"})
        b = M.expiries("BANKNIFTY", D(2026, 10, 28), months=2)
        self.assertEqual([e["kind"] for e in b], ["monthly", "monthly"])
        self.assertEqual(b[0]["date"], "2026-11-23")

    def test_stock_expiry_and_is_expiry_day(self):
        self.assertEqual(M.next_expiry("RELIANCE", D(2026, 10, 28)), D(2026, 11, 23))
        self.assertTrue(M.is_expiry_day("NIFTY", D(2026, 11, 3)))
        self.assertFalse(M.is_expiry_day("BANKNIFTY", D(2026, 11, 3)))

    def test_option_symbols(self):
        self.assertEqual(M.nse_option_symbol("NIFTY", "2026-11-03", 22700, "CE"), "NIFTY26N0322700CE")
        self.assertEqual(M.nse_option_symbol("NIFTY", "2026-11-23", 22700, "PE"), "NIFTY26NOV22700PE")
        self.assertEqual(M.nse_option_symbol("NIFTY", "2026-12-01", 23000, "CE"), "NIFTY26D0123000CE")
        self.assertEqual(M.futures_symbol("BANKNIFTY", "2026-11-23"), "BANKNIFTY26NOVFUT")


class Instruments(unittest.TestCase):
    def test_lots_and_steps(self):
        self.assertEqual(M.lot_size("NIFTY"), 65)
        self.assertEqual(M.lot_size("banknifty"), 30)
        self.assertEqual(M.lot_size("SENSEX"), 20)
        self.assertEqual(M.lot_size("RELIANCE"), 500)
        self.assertEqual(M.strike_step("NIFTY"), 50)
        self.assertEqual(M.strike_step("BANKNIFTY"), 100)

    def test_parse_mktlots(self):
        text = ("UNDERLYING,SYMBOL,OCT-26,NOV-26,DEC-26\n"
                "Derivatives on Individual Securities,,,,\n"
                "RELIANCE INDUSTRIES LTD,RELIANCE,500,500,500\n"
                "INFOSYS LIMITED,INFY,  400,400,400\n")
        self.assertEqual(M.parse_mktlots(text), {"RELIANCE": 500, "INFY": 400})

    def test_yahoo_symbols(self):
        self.assertEqual(M.yahoo_symbol("reliance"), "RELIANCE.NS")
        self.assertEqual(M.yahoo_symbol("RELIANCE", "BSE"), "RELIANCE.BO")
        self.assertEqual(M.yahoo_symbol("NIFTY"), "^NSEI")
        self.assertEqual(M.yahoo_symbol("SENSEX"), "^BSESN")
        self.assertEqual(M.yahoo_symbol("indiavix"), "^INDIAVIX")
        self.assertEqual(M.plain_symbol("^NSEBANK"), "BANKNIFTY")
        self.assertEqual(M.plain_symbol("M&M.NS"), "M&M")
        self.assertTrue(M.valid_symbol("BAJAJ-AUTO"))
        self.assertFalse(M.valid_symbol("RELIANCE; rm"))


class Ticks(unittest.TestCase):
    def test_tick_table(self):
        self.assertEqual(M.tick_size(99), 0.01)
        self.assertEqual(M.tick_size(500), 0.05)
        self.assertEqual(M.tick_size(1158), 0.10)
        self.assertEqual(M.tick_size(181.55, "options"), 0.05)
        self.assertEqual(M.round_tick(1158.37), 1158.4)
        self.assertEqual(M.round_tick(1158.37, mode="down"), 1158.3)
        self.assertEqual(M.round_tick(181.52, segment="options", mode="up"), 181.55)

    def test_bands_and_mwcb(self):
        lim = M.band_limits(100.0, 5)
        self.assertEqual((lim["upper"], lim["lower"]), (105.0, 95.0))
        self.assertIsNone(M.band_limits(100.0, None))
        self.assertEqual(M.near_circuit(104.0, lim, 2.0), "upper")
        self.assertIsNone(M.near_circuit(100.0, lim, 2.0))
        self.assertEqual(M.index_circuit_halt(-10.5, datetime.time(12, 0))[0], 10)
        self.assertIn("no halt", M.index_circuit_halt(-10.5, datetime.time(14, 45))[1])
        self.assertIn("rest of the day", M.index_circuit_halt(20.1, datetime.time(9, 30))[1])


class Charges(unittest.TestCase):
    def test_statutory_rates_are_pinned(self):
        self.assertEqual(M.STT["intraday"], (0.00025, "sell"))
        self.assertEqual(M.STT["delivery"], (0.001, "both"))
        self.assertEqual(M.STT["futures"], (0.0005, "sell"))      # Budget 2026, from 1 Apr 2026
        self.assertEqual(M.STT["options"], (0.0015, "sell"))      # Budget 2026, from 1 Apr 2026
        self.assertEqual(M.EXCHANGE_TXN["NSE"]["options"], 0.0003553)
        self.assertEqual(M.STAMP_BUY["delivery"], 0.00015)
        self.assertEqual(M.GST, 0.18)

    def test_intraday_round_trip_zerodha(self):
        rt = M.round_trip("intraday", 1158, 1170, 50)
        self.assertEqual(rt["by_type"]["brokerage"], 34.92)    # 0.03% each side, under the Rs 20 cap
        self.assertEqual(rt["by_type"]["stt"], 14.63)          # 0.025% of the sell side
        self.assertEqual(rt["total"], 61.94)
        self.assertEqual(rt["net_pnl"], 538.06)

    def test_delivery_has_dp_and_stt_both_sides(self):
        rt = M.round_trip("delivery", 1158, 1250, 10)
        self.assertEqual(rt["dp"], 15.34)
        self.assertEqual(rt["by_type"]["stt"], 24.08)
        self.assertEqual(rt["by_type"]["brokerage"], 0.0)

    def test_options_upstox(self):
        rt = M.round_trip("options", 120, 150, 65, broker="upstox")
        self.assertEqual(rt["by_type"]["brokerage"], 40.0)
        self.assertEqual(rt["sell"]["stt"], 14.63)              # 0.15% of 9,750 premium
        self.assertEqual(rt["buy"]["stt"], 0.0)

    def test_bse_futures_have_no_exchange_charge(self):
        self.assertEqual(M.leg_charges("futures", "buy", 80000, 20, exchange="BSE")["exchange_txn"], 0.0)

    def test_custom_brokerage_and_exercise_stt(self):
        cfg = {"broker": "custom", "brokerage": {"intraday": ["flat", 10.0]}}
        self.assertEqual(M.leg_charges("intraday", "buy", 100, 10, broker="custom", cfg=cfg)["brokerage"], 10.0)
        self.assertEqual(M.exercise_stt(50, 65), 4.88)

    def test_segment_for(self):
        self.assertEqual(M.segment_for("MIS"), "intraday")
        self.assertEqual(M.segment_for("CNC"), "delivery")
        self.assertEqual(M.segment_for("NRML", "CE"), "options")
        self.assertEqual(M.segment_for("MIS", "FUT"), "futures")


class Tax(unittest.TestCase):
    def test_flat_rates(self):
        self.assertEqual(M.tax_estimate("stcg", 100000)["tax"], 20800.0)
        lt = M.tax_estimate("ltcg", 200000)
        self.assertEqual(lt["taxable"], 75000.0)
        self.assertEqual(lt["tax"], 9750.0)

    def test_slab_income_and_losses(self):
        t = M.tax_estimate("fno", 200000, other_income=1000000)
        self.assertEqual(t["tax"], 20800.0)   # 2L at 10% (10-12L slab) + cess
        self.assertIn("carry", M.tax_estimate("intraday", -5000)["note"])
        with self.assertRaises(M.MarketError):
            M.tax_estimate("crypto", 1)


class Money(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(M.inr(12345678.9), "Rs 1,23,45,678.90")
        self.assertEqual(M.inr(999.999), "Rs 1,000.00")
        self.assertEqual(M.inr(-950.5), "-Rs 950.50")
        self.assertEqual(M.inr(0), "Rs 0.00")


class Bars(unittest.TestCase):
    def test_parse_drops_nulls_live_tick_and_forming_bar(self):
        day = D(2026, 10, 28)
        t0 = M.ts_from_ist(DT(2026, 10, 28, 9, 15))
        doc = {"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"},
                                     "timestamp": [t0, t0 + 300, t0 + 600, t0 + 700],
                                     "indicators": {"quote": [{"open": [1, 2, None, 4], "high": [1, 2, 3, 4],
                                                               "low": [1, 2, 3, 4], "close": [1, 2, None, 4],
                                                               "volume": [10, 20, 30, 40]}]}}]}}
        bars = M.bars_from_series(M.parse_chart(doc), 300, now=DT(2026, 10, 28, 9, 24))
        self.assertEqual([b["dt"].strftime("%H:%M") for b in bars], ["09:15"])
        self.assertEqual(M.bar_slot(DT(2026, 10, 28, 9, 20)), 1)
        self.assertEqual(M.bars_per_session(5), 75)
        self.assertEqual(list(M.by_session(bars)), [day])

    def test_no_data_error(self):
        with self.assertRaises(M.MarketError):
            M.parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}}, "X.NS")

    def test_indicators(self):
        self.assertEqual(M.classify_gap(M.gap_pct(102.5, 100)), "gap-up")
        self.assertEqual(M.classify_gap(-0.7), "small gap-down")
        self.assertAlmostEqual(M.rsi([1] * 15 + [2]), 100.0)
        bars = [{"h": 11, "l": 9, "c": 10, "v": 100}, {"h": 12, "l": 10, "c": 11, "v": 300}]
        self.assertAlmostEqual(M.vwap(bars), 10.75)


class FakeOpener(object):
    def __init__(self, answers):
        self.answers = list(answers)
        self.urls = []

    def open(self, req, timeout=None):
        import urllib.error
        self.urls.append(req.full_url)
        a = self.answers.pop(0)
        if isinstance(a, int):
            raise urllib.error.HTTPError(req.full_url, a, "err", {}, io.BytesIO(b""))

        class R(object):
            headers = {}

            def read(self):
                return a.encode()

            def __enter__(self):
                return self

            def __exit__(self, *x):
                return False
        return R()


class Network(unittest.TestCase):
    def test_retry_then_success(self):
        op = FakeOpener([429, '{"ok": 1}'])
        net = M.Net(pause=0, sleep=lambda s: None, opener=op)
        self.assertEqual(net.get("https://x.test/a"), '{"ok": 1}')

    def test_yahoo_falls_back_to_query1(self):
        op = FakeOpener([500, 500, '{"chart": {"result": []}}'])
        net = M.Net(pause=0, sleep=lambda s: None, opener=op)
        net.yahoo("/v8/finance/chart/RELIANCE.NS")
        self.assertIn("query1", op.urls[-1])

    def test_hard_404_is_one_plain_error(self):
        op = FakeOpener([404])
        net = M.Net(pause=0, sleep=lambda s: None, opener=op)
        with self.assertRaises(M.NetError):
            net.get("https://x.test/a")

    def test_nse_blocked_says_what_to_do(self):
        op = FakeOpener([403] * 12)
        net = M.Net(pause=0, sleep=lambda s: None, opener=op)
        with self.assertRaises(M.NetError) as cm:
            net.nse_json("/api/option-chain-v3?type=Indices&symbol=NIFTY")
        self.assertIn("--offline", str(cm.exception))


class Cli(unittest.TestCase):
    def test_commands(self):
        code, out, _ = run(["clock", "--now", "2026-11-03 10:00"])
        self.assertEqual(code, 0)
        self.assertIn("TODAY", out)
        code, out, _ = run(["expiry", "NIFTY", "--now", "2026-10-28", "--json"])
        self.assertEqual(json.loads(out)[0]["date"], "2026-11-03")
        code, out, _ = run(["costs", "--segment", "intraday", "--buy", "1158", "--sell", "1170", "--qty", "50"])
        self.assertIn("Rs 61.94", out)
        code, out, _ = run(["tax", "--kind", "stcg", "--profit", "100000"])
        self.assertIn("Rs 20,800.00", out)
        code, out, _ = run(["calendar", "--year", "2026"])
        self.assertIn("Diwali-Balipratipada", out)

    def test_bad_input_is_one_line(self):
        code, _, err = run(["clock", "--now", "someday"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))


if __name__ == "__main__":
    unittest.main()
