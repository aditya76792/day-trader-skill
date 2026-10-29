"""trader-alerts, India edition: NSE clock, Yahoo .NS quotes, rules (stop, target,
square-off, expiry, circuit, gap, index move), the plan flow and the CLI. Offline."""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

import _india as I
from _load import FIX, ROOT, load

alerts = load("trader-alerts", "alerts")
M = alerts.M

DIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(DIR, "plan-reliance.json")
PLAN_CE = os.path.join(DIR, "plan-nifty-ce.json")
PLAN_SHORT = os.path.join(DIR, "plan-short.json")
DAY = datetime.date(2026, 10, 28)       # a Wednesday


def ist(text):
    return M.parse_now(text)


def minute_doc(sym, day, first_open, last_close, until="10:14", prev_close=None, exchange="NSE"):
    """1-minute Yahoo chart from 09:15 to `until` (inclusive), closes on a straight line."""
    start = datetime.datetime.combine(day, datetime.time(9, 15))
    end = datetime.datetime.combine(day, M.parse_hhmm(until))
    n = int((end - start).total_seconds() // 60) + 1
    bars, prev = [], first_open
    for i in range(n):
        c = round(first_open + (last_close - first_open) * (i + 1) / float(n), 2)
        o = prev
        bars.append((start + datetime.timedelta(minutes=i), o, round(max(o, c) + 0.5, 2),
                     round(min(o, c) - 0.5, 2), c, 1000 + i))
        prev = c
    return I.chart_doc(sym, bars, prev_close=prev_close, interval="1m", exchange=exchange)


def write_quotes(folder, day=DAY):
    I.write(os.path.join(folder, "yahoo-RELIANCE.json"),
            minute_doc("RELIANCE", day, 1180.0, 1171.2, prev_close=1196.2))
    I.write(os.path.join(folder, "yahoo-TCS.json"), minute_doc("TCS", day, 3050.0, 3080.0, prev_close=3040.0))
    I.write(os.path.join(folder, "yahoo-SAMPLECO.json"), minute_doc("SAMPLECO", day, 101.0, 103.5, prev_close=100.0))
    I.write(os.path.join(folder, "yahoo-NIFTY.json"), minute_doc("NIFTY", day, 24900.0, 24400.0, prev_close=25000.0))
    I.write(os.path.join(folder, "yahoo-NOPE.json"),
            {"chart": {"result": None, "error": {"code": "Not Found",
                                                  "description": "No data found, symbol may be delisted"}}})


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules(path=PLAN):
    with open(path) as fh:
        return alerts.rules_from_plan(json.load(fh))


class QuoteDir(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        write_quotes(self.d)
        self.q, self.err = alerts.get_quotes(["RELIANCE", "TCS", "SAMPLECO", "NIFTY"], self.d)

    def tearDown(self):
        shutil.rmtree(self.d)


# ---------------------------------------------------------------- clock

class Clock(unittest.TestCase):
    def test_normal_market_and_expiries(self):
        code, text, _ = run(["clock", "--now", "2026-10-28 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN, normal market (09:15-15:30)", text)
        self.assertIn("Next NIFTY expiry: 2026-11-03 (Tuesday)", text)
        self.assertIn("Next SENSEX expiry: 2026-10-29 (Thursday)", text)

    def test_pre_open_and_matching(self):
        code, text, _ = run(["clock", "--now", "2026-10-28 09:05"])
        self.assertEqual(code, 1)
        self.assertIn("pre-open order entry (09:00-09:08)", text)
        self.assertIn("Next normal market: 2026-10-28 (Wednesday) from 09:15", text)
        doc = alerts.clock_doc(ist("2026-10-28 09:10"))
        self.assertEqual(doc["phase"], "pre-open-matching")
        self.assertEqual(alerts.clock_doc(ist("2026-10-28 15:45"))["phase"], "closing-session")
        self.assertEqual(alerts.clock_doc(ist("2026-10-28 15:30"))["phase"], "closed")

    def test_diwali_holiday_and_muhurat(self):
        code, text, _ = run(["clock", "--now", "2026-11-10 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("exchange holiday: Diwali-Balipratipada", text)
        self.assertIn("Next normal market: 2026-11-11 (Wednesday)", text)
        code, text, _ = run(["clock", "--now", "2026-11-06 10:00"])
        self.assertIn("Coming up: Muhurat trading on 2026-11-08 (Sunday)", text)
        self.assertIn("by circular", text)
        code, text, _ = run(["clock", "--now", "2026-11-08 18:00"])
        self.assertIn("Today: Muhurat trading", text)

    def test_weekend_json(self):
        code, text, _ = run(["clock", "--now", "2026-10-31 10:00", "--json"])
        self.assertEqual(code, 1)
        doc = json.loads(text)
        self.assertEqual(doc["phase"], "weekend")
        self.assertEqual(doc["next_open"], "2026-11-02 09:15")

    def test_bad_now_is_one_plain_line(self):
        code, _, err = run(["clock", "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: --now wants"))


# ---------------------------------------------------------------- quotes

class Quotes(QuoteDir):
    def test_latest_close_prev_close_and_open(self):
        q = self.q["RELIANCE"]
        self.assertEqual((q["price"], q["prev_close"], q["open"]), (1171.2, 1196.2, 1180.0))
        self.assertEqual(q["time"], "2026-10-28T10:14:00+05:30")
        self.assertEqual(q["currency"], "INR")
        self.assertEqual(self.err, [])

    def test_bars_before_0915_are_not_the_open(self):
        doc = minute_doc("TCS", DAY, 3050.0, 3080.0, prev_close=3040.0)
        res = doc["chart"]["result"][0]
        res["timestamp"].insert(0, M.ts_from_ist(datetime.datetime(2026, 10, 28, 9, 8)))
        for k, v in (("open", 3000.0), ("high", 3001.0), ("low", 2999.0), ("close", 3000.5), ("volume", 1)):
            res["indicators"]["quote"][0][k].insert(0, v)
        self.assertEqual(alerts.parse_quote(doc, "TCS")["open"], 3050.0)

    def test_missing_and_unknown_symbols_are_errors_not_crashes(self):
        q, errors = alerts.get_quotes(["RELIANCE", "NOPE", "ZZZZ"], self.d)
        self.assertEqual(sorted(q), ["RELIANCE"])
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline quote file", errors[1])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(self.d, "yahoo-TCS.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-TCS.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["TCS"], d)
            self.assertEqual((q["TCS"]["price"], errors), (3080.0, []))
        finally:
            shutil.rmtree(d)

    def test_price_overrides(self):
        q = alerts.parse_price_overrides(["nifty26n0322700ce=118.5"], ist("2026-11-03 10:30"))
        self.assertEqual(q["NIFTY26N0322700CE"]["price"], 118.5)
        self.assertEqual(q["NIFTY26N0322700CE"]["source"], "manual")
        for bad in (["X"], ["X=abc"], ["X=-1"]):
            with self.assertRaises(alerts.AlertError):
                alerts.parse_price_overrides(bad, ist("2026-11-03 10:30"))

    def test_saved_premium_must_be_fresh(self):
        doc = {"quotes": {"NIFTY26N0322700CE": {"price": 101.0, "time": "2026-11-03T10:20:00+05:30"}}}
        self.assertEqual(alerts.saved_quote(doc, "NIFTY26N0322700CE", ist("2026-11-03 10:30"))["price"], 101.0)
        self.assertIsNone(alerts.saved_quote(doc, "NIFTY26N0322700CE", ist("2026-11-03 10:40")))
        self.assertIsNone(alerts.saved_quote({}, "X", ist("2026-11-03 10:40")))

    def test_derivative_symbols(self):
        for s in ("NIFTY26N0322700CE", "NIFTY26NOV22700PE", "BANKNIFTY26NOVFUT", "RELIANCE26NOV1200CE"):
            self.assertTrue(alerts.is_derivative(s), s)
        for s in ("RELIANCE", "M&M", "BAJAJ-AUTO", "NIFTY", "3MINDIA"):
            self.assertFalse(alerts.is_derivative(s), s)


class FakeResp(object):
    def __init__(self, body):
        self.body = body
        self.headers = {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener(object):
    def __init__(self, codes, body):
        self.codes, self.body, self.urls = list(codes), body, []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        code = self.codes.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return FakeResp(self.body)


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = alerts.NET_FACTORY

    def tearDown(self):
        alerts.NET_FACTORY = self.saved

    def net(self, codes, sym="RELIANCE", exchange="NSE"):
        body = json.dumps(minute_doc(sym, DAY, 1180.0, 1171.2, prev_close=1196.2, exchange=exchange)).encode()
        op = FakeOpener(codes, body)
        alerts.NET_FACTORY = lambda: M.Net(pause=0, sleep=lambda s: None, opener=op)
        return op

    def test_yahoo_ns_one_minute_bars(self):
        op = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1171.2, []))
        self.assertEqual(op.urls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS"
                                   "?range=1d&interval=1m"])

    def test_index_and_bse_symbols(self):
        op = self.net([200, 200])
        alerts.get_quotes(["NIFTY", "SBIN"], exchanges={"SBIN": "BSE"}, interval="5m")
        self.assertIn("/chart/^NSEI?range=1d&interval=5m", op.urls[0])
        self.assertIn("/chart/SBIN.BO?", op.urls[1])

    def test_falls_back_to_query1_and_reports_failure(self):
        op = self.net([503, 503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertIn("query1", op.urls[-1])
        self.net([500] * 6)
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("Yahoo Finance", errors[0])


# ---------------------------------------------------------------- rules

class Rules(QuoteDir):
    def at(self, text, rules, state=None, quotes=None, cfg=None, notes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, M.market_status(now),
                               state or {}, now, cfg, notes)

    def test_a_target_rings_once_and_says_rupees(self):
        rules = [{"id": "tcs-3075", "symbol": "TCS", "type": "above", "price": 3075}]
        rings, st = self.at("2026-10-28 10:15", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("TCS is Rs 3,080.00: at or above your alert level Rs 3,075.00", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 10:20", rules, st)[0], [])
        self.assertEqual(st["rung"]["tcs-3075"], "2026-10-28T10:15:00+05:30")

    def test_stop_and_rearm(self):
        rules = [{"id": "rel-stop", "symbol": "RELIANCE", "type": "stop", "price": 1172, "rearm": True}]
        rings, st = self.at("2026-10-28 10:15", rules)
        self.assertIn("at or below your stop Rs 1,172.00", rings[0]["message"])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1175.0
        rings, st = self.at("2026-10-28 10:20", rules, st, q)
        self.assertNotIn("rel-stop", st["rung"])
        q["RELIANCE"]["price"] = 1170.0
        self.assertEqual(len(self.at("2026-10-28 10:25", rules, st, q)[0]), 1)

    def test_a_short_trade_stop_is_above(self):
        rules = plan_rules(PLAN_SHORT)
        self.assertEqual([(r["type"], r["side"]) for r in rules[:2]], [("stop", "sell"), ("target", "sell")])
        q = {"TCS": {"price": 3072.0, "time": "2026-10-28T11:00:00+05:30", "prev_close": 3040.0}}
        rings, st = self.at("2026-10-28 11:00", rules, quotes=q)
        self.assertEqual([r["id"] for r in rings], ["tcs-plan-stop"])
        self.assertIn("at or above your stop Rs 3,071.50", rings[0]["message"])
        q["TCS"]["price"] = 3006.0
        rings, _ = self.at("2026-10-28 12:00", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["tcs-plan-target"])
        self.assertIn("at or below your target Rs 3,007.00", rings[0]["message"])

    def test_pct_move(self):
        rules = [{"id": "rel-1", "symbol": "RELIANCE", "type": "pct_move", "pct": 3}]
        self.assertEqual(self.at("2026-10-28 10:15", rules)[0], [])
        up = [{"id": "rel-up", "symbol": "RELIANCE", "type": "pct_move", "pct": 1.5, "direction": "up"}]
        self.assertEqual(self.at("2026-10-28 10:15", up)[0], [])
        rules = [{"id": "rel-1", "symbol": "RELIANCE", "type": "pct_move", "pct": 1.5, "direction": "down"}]
        rings, st = self.at("2026-10-28 10:15", rules)
        self.assertIn("RELIANCE moved -2.09% to Rs 1,171.20 (from Rs 1,196.20)", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 11:15", rules, st)[0], [])

    def test_a_quote_from_another_day_does_not_ring_moves(self):
        rules = [{"id": "rel-1", "symbol": "RELIANCE", "type": "pct_move", "pct": 1}]
        notes = []
        rings, _ = self.at("2026-10-29 10:15", rules, notes=notes)
        self.assertEqual(rings, [])
        self.assertIn("latest quote is from 2026-10-28", notes[0])

    def test_opening_bell_from_0915_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-10-28 09:10", rules)[0], [])        # pre-open: not the bell
        rings, st = self.at("2026-10-28 09:15", rules)
        self.assertIn("NSE normal market is open (09:15-15:30 IST", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 11:00", rules, st)[0], [])
        self.assertEqual(self.at("2026-11-10 09:20", rules, st)[0], [])    # Diwali holiday
        rings, _ = self.at("2026-11-03 09:16", rules, st)
        self.assertIn("NIFTY expiry today", rings[0]["message"])

    def test_time_rules_only_on_trading_days(self):
        rules = [{"id": "t", "type": "time", "at": "15:10", "label": "flat by 15:10", "symbol": "RELIANCE"}]
        self.assertEqual(self.at("2026-10-28 15:09", rules)[0], [])
        rings, st = self.at("2026-10-28 15:10", rules)
        self.assertIn("Flat by 15:10: it is 15:10 IST (set for 15:10), the normal market closes at 15:30",
                      rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol RELIANCE", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 15:20", rules, st)[0], [])
        self.assertEqual(self.at("2026-10-31 15:20", rules)[0], [])        # Saturday
        self.assertEqual(self.at("2026-11-24 15:20", rules)[0], [])        # Guru Nanak Jayanti
        rings, _ = self.at("2026-10-28 15:45", rules)
        self.assertIn("closing session", rings[0]["message"])

    def test_square_off_ten_minutes_before_the_broker(self):
        rule = [{"id": "sq", "type": "square_off", "segment": "equity", "minutes_before": 10, "symbol": "RELIANCE"}]
        self.assertEqual(self.at("2026-10-28 15:14", rule)[0], [])
        rings, _ = self.at("2026-10-28 15:15", rule)                    # zerodha 15:25
        self.assertIn("zerodha squares off open MIS (intraday) equity positions itself at 15:25 IST",
                      rings[0]["message"])
        self.assertIn("Rs 50 plus GST", rings[0]["message"])
        cfg = dict(M.DEFAULT_CONFIG, broker="upstox")
        self.assertEqual(self.at("2026-10-28 15:09", rule, cfg=cfg)[0], [])
        self.assertEqual(len(self.at("2026-10-28 15:10", rule, cfg=cfg)[0]), 1)
        fno = [dict(rule[0], segment="fno", id="sq-fno")]
        self.assertEqual(self.at("2026-10-28 15:15", fno)[0], [])
        self.assertIn("F&O", self.at("2026-10-28 15:16", fno)[0][0]["message"])
        with self.assertRaises(alerts.AlertError):
            self.at("2026-10-28 15:16", rule, cfg=dict(M.DEFAULT_CONFIG, broker="mybroker"))
        custom = [dict(rule[0], auto_square_off="15:20")]
        self.assertEqual(len(self.at("2026-10-28 15:10", custom, cfg=dict(M.DEFAULT_CONFIG, broker="mybroker"))[0]), 1)

    def test_expiry_days_including_a_holiday_shift(self):
        nifty = [{"id": "nx", "type": "expiry", "underlying": "NIFTY", "at": "13:30"}]
        self.assertEqual(self.at("2026-10-28 13:30", nifty)[0], [])        # a Wednesday
        self.assertEqual(self.at("2026-11-03 13:29", nifty)[0], [])
        rings, _ = self.at("2026-11-03 13:30", nifty)                      # weekly, Tuesday
        self.assertIn("NIFTY weekly contracts expire today (Tuesday 03 Nov)", rings[0]["message"])
        self.assertIn("cash-settled", rings[0]["message"])
        # the November monthly: last Tuesday 24 Nov is a holiday, so it moves to Monday 23 Nov
        stock = [{"id": "rx", "type": "expiry", "underlying": "RELIANCE", "at": "14:00"}]
        rings, _ = self.at("2026-11-23 14:00", stock)
        self.assertIn("RELIANCE monthly contracts expire today (Monday 23 Nov)", rings[0]["message"])
        self.assertIn("PHYSICALLY settled", rings[0]["message"])
        self.assertEqual(self.at("2026-11-24 14:00", stock)[0], [])
        sensex = [{"id": "sx", "type": "expiry", "underlying": "SENSEX", "at": "13:30"}]
        self.assertEqual(len(self.at("2026-10-29 13:30", sensex)[0]), 1)  # Thursday

    def test_circuit_with_a_known_band(self):
        rule = [{"id": "c", "type": "circuit", "symbol": "SAMPLECO", "within_pct": 2, "band_pct": 5}]
        rings, st = self.at("2026-10-28 10:15", rule)
        msg = rings[0]["message"]
        self.assertIn("SAMPLECO Rs 103.50 is within 2% of its upper circuit Rs 105.00 (5% band", msg)
        self.assertIn("no sellers", msg)
        self.assertEqual(self.at("2026-10-28 10:20", rule, st)[0], [])
        cfg = dict(M.DEFAULT_CONFIG, bands={"SAMPLECO": {"upper": 110.0, "lower": 90.0}})
        bare = [{"id": "c2", "type": "circuit", "symbol": "SAMPLECO", "within_pct": 2}]
        self.assertEqual(self.at("2026-10-28 10:15", bare, cfg=cfg)[0], [])
        cfg = dict(M.DEFAULT_CONFIG, bands={"SAMPLECO": 5})
        self.assertEqual(len(self.at("2026-10-28 10:15", bare, cfg=cfg)[0]), 1)
        notes = []
        self.assertEqual(self.at("2026-10-28 10:15", bare, notes=notes)[0], [])
        self.assertIn("dynamic bands", notes[0])

    def test_opening_gap_at_0915_0916(self):
        rule = [{"id": "g", "type": "gap", "symbol": "RELIANCE", "pct": 1.0}]
        self.assertEqual(self.at("2026-10-28 09:14", rule)[0], [])
        rings, st = self.at("2026-10-28 09:16", rule)
        self.assertIn("RELIANCE opened at Rs 1,180.00, a small gap-down of -1.35% on the previous close "
                      "Rs 1,196.20", rings[0]["message"])
        self.assertEqual(self.at("2026-10-28 09:16", rule, st)[0], [])
        self.assertEqual(self.at("2026-10-28 09:30", rule)[0], [])
        self.assertEqual(len(self.at("2026-10-28 09:30", [dict(rule[0], until="09:30")])[0]), 1)
        self.assertEqual(self.at("2026-10-28 09:16", [dict(rule[0], direction="up")])[0], [])

    def test_index_move_and_market_wide_circuit_breakers(self):
        rule = [{"id": "nm", "type": "index_move", "symbol": "NIFTY", "pct": 2}]
        rings, st = self.at("2026-10-28 11:00", rule)
        self.assertEqual(len(rings), 1)
        self.assertIn("NIFTY moved -2.40% to 24,400.00", rings[0]["message"])
        self.assertIn("10%, 15% or 20%", rings[0]["message"])
        q = json.loads(json.dumps(self.q))
        q["NIFTY"]["price"] = 22400.0                                       # -10.4%
        rings, st = self.at("2026-10-28 11:05", rule, st, q)
        self.assertEqual([r["id"] for r in rings], ["nm:mwcb10"])
        self.assertIn("45 minutes halt", rings[0]["message"])
        q["NIFTY"]["price"] = 21000.0                                       # -16%
        rings, st = self.at("2026-10-28 13:10", rule, st, q)
        self.assertEqual([r["id"] for r in rings], ["nm:mwcb15"])
        self.assertIn("45 minutes halt", rings[0]["message"])
        late = self.at("2026-10-28 14:40", rule, None, dict(q, NIFTY=dict(q["NIFTY"], price=22400.0)))[0]
        self.assertIn("no halt (after 2:30 PM)", late[1]["message"])
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "index_move", "symbol": "RELIANCE", "pct": 2})

    def test_the_plan_gives_stop_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"], rules[2]["at"]), (1163.2, 1187.2, "15:10"))
        self.assertTrue(all(r["date"] == "2026-10-28" for r in rules))
        self.assertEqual(self.at("2026-10-28 10:15", rules)[0], [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1163.0
        rings, st = self.at("2026-10-28 11:30", rules, quotes=q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is Rs 1,163.00: at or below your stop Rs 1,163.20 (plan stop: opening-range "
                      "midpoint)", rings[0]["message"])
        self.assertEqual(self.at("2026-10-29 15:15", rules, st)[0], [])    # plan rules: their day only

    def test_flat_by_comes_from_config_when_the_plan_has_none(self):
        with open(PLAN) as fh:
            plan = json.load(fh)
        del plan["flat_by"]
        self.assertEqual(alerts.rules_from_plan(plan)[2]["at"], "15:10")
        self.assertEqual(alerts.rules_from_plan(plan, "14:55")[2]["at"], "14:55")

    def test_an_option_plan_watches_the_premium(self):
        rules = plan_rules(PLAN_CE)
        self.assertEqual(rules[0]["symbol"], "NIFTY26N0322700CE")
        self.assertTrue(rules[0]["premium"])
        self.assertEqual(rules[2]["at"], "14:45")
        q = alerts.parse_price_overrides(["NIFTY26N0322700CE=94"], ist("2026-11-03 11:00"))
        rings, _ = self.at("2026-11-03 11:00", rules, quotes=q)
        self.assertIn("NIFTY26N0322700CE premium is Rs 94.00: at or below your stop Rs 95.00", rings[0]["message"])

    def test_bad_plans_and_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1163.2})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "side": "buy", "entry": 1170, "stop": 1180, "target": 1190})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "NIFTY", "instrument": {"type": "CE"}, "stop": 1, "target": 2})
        for bad in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "symbol": "RELIANCE"},
                    {"id": "x", "type": "time", "at": "4pm"}, {"id": "x", "type": "expiry", "at": "13:30"},
                    {"id": "x", "type": "square_off", "segment": "cash"},
                    {"id": "x", "type": "above", "symbol": "bad sym!", "price": 1},
                    {"id": "x", "type": "gap", "symbol": "TCS", "pct": 1, "until": "9am"},
                    {"id": "x", "type": "above", "symbol": "TCS", "price": 1, "date": "28-10-2026"}):
            with self.assertRaises(alerts.AlertError):
                alerts.validate(bad)

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-plan") or r["id"] == "reliance-flat-by"],
                         plan_rules())


# ---------------------------------------------------------------- CLI

class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.q = os.path.join(self.d, "quotes")
        write_quotes(self.q)
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.cfg = os.path.join(self.d, "config.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now, *extra):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", self.q, "--now", now, "--config", self.cfg] + list(extra))

    def test_the_plan_day(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)
        code, text, _ = self.check("2026-10-28 09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-28 10:15")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE Rs 1,171.20.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-alerts")
        self.assertEqual(doc["source"], "yahoo")
        self.assertEqual(doc["at"], "2026-10-28T10:15:00+05:30")
        self.assertEqual(doc["quotes"]["RELIANCE"]["price"], 1171.2)
        self.assertEqual(doc["quotes"]["RELIANCE"]["prev_close"], 1196.2)
        self.assertEqual(doc["quotes"]["RELIANCE"]["time"], "2026-10-28T10:14:00+05:30")
        code, text, _ = self.check("2026-10-28 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by 15:10", text)
        self.assertIn("not an order", text)
        self.assertIn("SL-M", text)
        self.assertEqual(self.check("2026-10-28 15:12")[0], 1)
        code, text, _ = self.check("2026-10-29 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-29 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)
        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state, "--config", self.cfg])
        with open(self.rules) as fh:
            ids = sorted(r["id"] for r in json.load(fh)["rules"])
        self.assertEqual(ids, ["opening-bell", "reliance-flat-by", "reliance-plan-stop", "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_option_plan_needs_a_premium(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN_CE, "--rules", self.rules, "--state", self.state,
                             "--config", self.cfg])
        self.assertIn("trader-fno", text)
        code, text, _ = self.check("2026-11-03 11:00")
        self.assertEqual(code, 1)
        self.assertIn("note: NIFTY26N0322700CE: no premium quote", text)
        code, text, _ = self.check("2026-11-03 11:01", "--price", "NIFTY26N0322700CE=171")
        self.assertEqual(code, 0)
        self.assertIn("ALERT NIFTY26N0322700CE premium is Rs 171.00: at or above your target Rs 170.00", text)
        with open(self.prices) as fh:
            self.assertEqual(json.load(fh)["quotes"]["NIFTY26N0322700CE"]["source"], "manual")

    def test_add_square_off_expiry_circuit_gap(self):
        with open(self.cfg, "w") as fh:
            json.dump({"broker": "upstox"}, fh)
        code, text, _ = run(["add", "--rules", self.rules, "--square-off", "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("It rings at 15:10 IST on trading days (upstox auto square-off 15:20 minus 10 minutes)", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--expiry", "nifty"])[0], 0)
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "SAMPLECO", "--circuit", "2",
                              "--band", "5"])[0], 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--gap", "1"])
        self.assertIn("09:15 to 09:16", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "NIFTY", "--index-move", "2"])[0], 0)
        with open(self.rules) as fh:
            rules = {r["id"]: r for r in json.load(fh)["rules"]}
        self.assertEqual(sorted(rules), ["nifty-expiry-1330", "nifty-index-move-2", "reliance-gap-1",
                                         "sampleco-circuit-2", "square-off-equity"])
        code, text, _ = self.check("2026-10-28 09:16")
        self.assertEqual(code, 0)
        self.assertIn("small gap-down", text)
        self.assertIn("upper circuit", text)
        self.assertIn("NIFTY moved -2.40%", text)
        code, text, _ = self.check("2026-10-28 15:10")
        self.assertIn("ALERT Square-off reminder: upstox", text)

    def test_duplicate_and_bad_adds(self):
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "TCS", "--above", "3100"])[0], 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "TCS", "--above", "3100"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        self.assertEqual(run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--index-move", "2"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_list_and_reset(self):
        run(["add", "--rules", self.rules, "--opening-bell"])
        self.check("2026-10-28 09:20")
        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang 2026-10-28T09:20:00+05:30", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_quote_write_merges_and_partial(self):
        code, text, err = run(["quote", "RELIANCE", "NOPE", "--offline", self.q, "--write", self.prices,
                               "--now", "2026-10-28 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   Rs 1,171.20 (-2.09% vs prev close Rs 1,196.20)  at 2026-10-28 10:14 IST, 1 min old",
                      text)
        self.assertIn("partial: quotes for 1 of 2 symbols", text)
        self.assertIn("error: NOPE", err)
        self.assertIn("Stooq does not cover NSE", text)
        run(["quote", "TCS", "--offline", self.q, "--write", self.prices, "--now", "2026-10-28 10:16"])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["quotes"]), ["RELIANCE", "TCS"])
        code, _, err = run(["quote", "NIFTY26N0322700CE", "--offline", self.q])
        self.assertEqual(code, 1)
        self.assertIn("trader-fno", err)

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash_inr": 0, "positions": {
                "TCS|MIS": {"qty": 5, "avg_price": 3050, "product": "MIS", "instrument": {"type": "EQ"}},
                "NIFTY26N0322700CE|NRML": {"qty": 65, "instrument": {"type": "CE"}}}}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", self.q, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["RELIANCE", "TCS"])

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", self.q])
        self.assertEqual(code, 1)
        self.assertIn("error: no rules", err)

    def test_no_em_dash_in_output(self):
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state, "--config", self.cfg])
        _, text, _ = self.check("2026-10-28 15:10")
        self.assertNotIn(chr(0x2014), text)


if __name__ == "__main__":
    unittest.main()
