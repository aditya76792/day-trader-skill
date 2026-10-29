"""trader-live: the Upstox client, the engine's order flow and guards, and the live-mode locks.
Offline: a fake HTTP opener stands in for Upstox; the replay feed and paper broker stand in for the market."""

import datetime
import gzip
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "trader-live"))
import upstox as U  # noqa: E402
import engine as E  # noqa: E402
import live as L  # noqa: E402

M = U.M
DT = datetime.datetime
DAY = datetime.date(2026, 10, 28)


class Reply(object):
    def __init__(self, body):
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener(object):
    def __init__(self, answers):
        self.answers = list(answers)
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        a = self.answers.pop(0)
        if isinstance(a, tuple):
            code, body = a
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body.encode()))
        return Reply(a)


def client(answers):
    op = FakeOpener(answers)
    return U.Client("TOKEN123456", opener=op, sleep=lambda s: None), op


class Secrets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_env_file_must_be_private(self):
        p = os.path.join(self.tmp, ".env")
        with open(p, "w") as fh:
            fh.write("UPSTOX_API_KEY=abcd1234\nexport UPSTOX_API_SECRET='s3cr3t-value'\n")
        os.chmod(p, 0o644)
        with self.assertRaises(U.UpstoxError) as cm:
            U.Credentials.load(environ={}, files=[p])
        self.assertIn("chmod 600", str(cm.exception))
        os.chmod(p, 0o600)
        c = U.Credentials.load(environ={}, files=[p])
        self.assertEqual((c.api_key, c.api_secret), ("abcd1234", "s3cr3t-value"))
        self.assertNotIn("s3cr3t", repr(c))

    def test_missing_credentials_never_ask_for_them_in_chat(self):
        with self.assertRaises(U.UpstoxError) as cm:
            U.Credentials.load(environ={}, files=[])
        self.assertIn("Never paste them into the chat", str(cm.exception))

    def test_redaction(self):
        s = U.redact('Authorization: Bearer eyJabc.def-123 {"access_token": "eyJzzz111222"} code=abcdef123')
        self.assertNotIn("eyJabc", s)
        self.assertNotIn("eyJzzz", s)
        self.assertNotIn("abcdef123", s)

    def test_token_file_is_private_and_expires_at_0330(self):
        p = os.path.join(self.tmp, "cfg", "tok.json")
        U.save_token({"access_token": "tok-abcdef", "user_id": "X1"}, now=DT(2026, 10, 28, 8, 0), path=p)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(U.load_token(DT(2026, 10, 29, 3, 29), p)["access_token"], "tok-abcdef")
        with self.assertRaises(U.TokenError):
            U.load_token(DT(2026, 10, 29, 3, 30), p)
        self.assertEqual(U.token_expiry(DT(2026, 10, 29, 2, 0)), DT(2026, 10, 29, 3, 30))

    def test_token_exchange_sends_a_user_agent(self):
        op = FakeOpener([json.dumps({"status": "success", "data": {"access_token": "tok-1", "user_id": "X"}})])
        c = U.Credentials("KEY1", "SEC1", "http://127.0.0.1:8765/callback", "test")
        self.assertEqual(U.exchange_code(c, "code1", opener=op)["access_token"], "tok-1")
        self.assertTrue(op.reqs[0].get_header("User-agent").startswith("day-trader-skill-india"))

    def test_login_url_and_redirect_must_be_local(self):
        c = U.Credentials("KEY1", "SEC1", "http://127.0.0.1:8765/callback", "test")
        url = U.login_url(c, "st8")
        self.assertIn("/v2/login/authorization/dialog?", url)
        self.assertIn("response_type=code", url)
        self.assertIn("state=st8", url)
        self.assertNotIn("SEC1", url)
        with self.assertRaises(U.UpstoxError):
            U.run_login(U.Credentials("K", "S", "https://example.com/cb", "t"), timeout=1, open_browser=False)


class ClientCalls(unittest.TestCase):
    def test_ltp_maps_back_by_instrument_token(self):
        c, op = client([json.dumps({"status": "success", "data": {"NSE_EQ:RELIANCE": {
            "last_price": 1171.2, "instrument_token": "NSE_EQ|INE002A01018", "ltq": 5, "volume": 100, "cp": 1196.2}}})])
        got = c.ltp(["NSE_EQ|INE002A01018"])
        self.assertEqual(got["NSE_EQ|INE002A01018"]["price"], 1171.2)
        self.assertEqual(got["NSE_EQ|INE002A01018"]["prev_close"], 1196.2)
        self.assertIn("/v3/market-quote/ltp?instrument_key=NSE_EQ|INE002A01018", op.reqs[0].full_url)
        self.assertEqual(op.reqs[0].get_header("Authorization"), "Bearer TOKEN123456")
        self.assertTrue(op.reqs[0].get_header("User-agent").startswith("day-trader-skill-india"))

    def test_candles_newest_first_become_oldest_first(self):
        c, op = client([json.dumps({"status": "success", "data": {"candles": [
            ["2026-10-28T09:16:00+05:30", 2, 3, 1, 2.5, 10, 0], ["2026-10-28T09:15:00+05:30", 1, 2, 0.5, 2, 20, 0]]}})])
        bars = c.intraday_candles("NSE_EQ|INE002A01018", 1)
        self.assertEqual([b["dt"].strftime("%H:%M") for b in bars], ["09:15", "09:16"])
        self.assertIn("/v3/historical-candle/intraday/NSE_EQ%7CINE002A01018/minutes/1", op.reqs[0].full_url)

    def test_reads_retry_but_order_writes_never_do(self):
        c, op = client([(503, "{}"), json.dumps({"status": "success", "data": []})])
        self.assertEqual(c.order_book(), [])
        self.assertEqual(len(op.reqs), 2)
        c, op = client([(503, "{}"), json.dumps({"status": "success", "data": {"order_ids": ["1"]}})])
        with self.assertRaises(U.UpstoxError) as cm:
            c.place({"quantity": 1})
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(op.reqs), 1)
        self.assertTrue(op.reqs[0].full_url.startswith("https://api-hft.upstox.com/v3/order/place"))

    def test_401_and_error_bodies(self):
        c, _ = client([(401, '{"status":"error","errors":[{"errorCode":"UDAPI100050","message":"Invalid token"}]}')])
        with self.assertRaises(U.TokenError):
            c.profile()
        c, _ = client([(400, '{"status":"error","errors":[{"errorCode":"UDAPI1026","message":"Insufficient funds"}]}')])
        with self.assertRaises(U.UpstoxError) as cm:
            c.place({})
        self.assertIn("Insufficient funds", str(cm.exception))

    def test_payload_and_status_mapping(self):
        o = {"symbol": "RELIANCE", "side": "BUY", "qty": 64, "product": "MIS", "order_type": "SL-M",
             "trigger_price": 1163.2, "price": None}
        p = U.order_payload(o, "NSE_EQ|INE002A01018", "TDR1028-01S")
        self.assertEqual((p["product"], p["order_type"], p["trigger_price"], p["price"], p["slice"]),
                         ("I", "SL-M", 1163.2, 0.0, False))
        self.assertEqual(U.order_payload(dict(o, product="CNC"), "k", "t")["product"], "D")
        n = U.normalise_order({"order_id": "1", "status": "open", "filled_quantity": 10, "quantity": 64,
                               "average_price": 1171.0, "tag": "TDR"})
        self.assertEqual(n["status"], "partial")
        self.assertEqual(U.normalise_order({"status": "trigger pending"})["status"], "open")
        self.assertEqual(U.normalise_order({"status": "complete", "filled_quantity": 64})["status"], "filled")


class InstrumentLookup(unittest.TestCase):
    def test_eq_fo_and_index_keys(self):
        rows = [{"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|INE002A01018",
                 "trading_symbol": "RELIANCE", "lot_size": 1, "tick_size": 10},
                {"segment": "NSE_FO", "instrument_type": "CE", "instrument_key": "NSE_FO|51834",
                 "underlying_symbol": "NIFTY", "expiry": M.ts_from_ist(DT(2026, 11, 3, 14, 30)) * 1000,
                 "strike_price": 22700.0, "lot_size": 65, "trading_symbol": "NIFTY 22700 CE 03 NOV 26"}]
        raw = gzip.compress(json.dumps(rows).encode())
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "NSE.json.gz")
            with open(p, "wb") as fh:
                fh.write(raw)
            inst = U.Instruments.load(path=p)
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(inst.key_for("reliance"), "NSE_EQ|INE002A01018")
        self.assertEqual(inst.key_for("NIFTY"), "NSE_INDEX|Nifty 50")
        o = {"symbol": "NIFTY", "instrument": {"type": "CE", "underlying": "NIFTY", "expiry": "2026-11-03",
                                               "strike": 22700}}
        self.assertEqual(inst.key_for(o), "NSE_FO|51834")
        with self.assertRaises(U.UpstoxError):
            inst.key_for("NOSUCH")


# ---------------------------------------------------------------- the engine

def cfg(**kw):
    c = dict(M.DEFAULT_CONFIG, capital_inr=500000, max_order_inr=400000, max_position_pct=100,
             max_trades_per_day=5, min_avg_turnover_cr=0)
    live = dict(kw.pop("live", {}))
    c.update(kw)
    c["live"] = dict({"watchlist": ["WINCO", "LOSECO", "DRIFTCO", "FLATCO", "LATECO"], "max_open_trades": 3}, **live)
    return c


class Workdir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def engine(self, mode="paper", c=None, bars=None):
        b, prev = bars or L.synthetic_day(DAY)
        c = c or cfg()
        notes = []
        eng = E.Engine(c, E.ReplayFeed(b, prev), E.PaperBroker(c), mode=mode, base=os.path.join("trader", "live"),
                       notifier=lambda *x: notes.append(x))
        eng.notes = notes
        return eng

    def play(self, eng, start="09:10", end="15:31", step=15):
        t = DT.combine(DAY, M.parse_hhmm(start))
        stop = DT.combine(DAY, M.parse_hhmm(end))
        while t <= stop:
            eng.tick(t)
            t += datetime.timedelta(seconds=step)
        return eng.journal


class Flow(Workdir):
    def test_validation_day_hits_target_stop_and_square_off(self):
        rep, j = L.validate(cfg(), *L.synthetic_day(DAY), day=DAY, base=os.path.join("trader", "live", "v"))
        self.assertTrue(rep["validated"], rep["invariant_violations"])
        self.assertEqual(rep["by_exit"]["target"], 1)
        self.assertEqual(rep["by_exit"]["stop-loss"], 1)
        self.assertEqual(rep["by_exit"]["flat_by"], 1)
        late = [t for t in j["trades"].values() if t["symbol"] == "LATECO"]
        self.assertEqual(late, [])                  # its breakout came after no_entries_after
        for t in j["trades"].values():
            self.assertIn("S", t["legs"])           # every filled entry got a stop
            self.assertEqual(self.pnl_open(t), 0)
            self.assertLessEqual(t["max_loss"], 1000)

    def pnl_open(self, t):
        return sum(f["qty"] if f["side"] == "BUY" else -f["qty"] for f in t["fills"])

    def test_oco_cancels_the_other_exit(self):
        j = self.play(self.engine())
        win = [t for t in j["trades"].values() if t["symbol"] == "WINCO"][0]
        self.assertEqual(win["legs"]["T"]["status"], "filled")
        self.assertEqual(win["legs"]["S"]["status"], "cancelled")

    def test_live_entries_wait_for_a_matching_confirmation(self):
        eng = self.engine(mode="live")
        self.play(eng, end="09:35")
        pend = [t for t in eng.journal["trades"].values() if t["state"] == "pending_confirm"]
        self.assertTrue(pend)
        self.assertFalse(any(t["legs"] for t in pend))            # nothing sent without a yes
        self.assertTrue(eng.notes)
        t1, t2 = pend[0], pend[1]
        M.write_json(eng.confirm_path(t1), {"fingerprint": E.TICKET.fingerprint(t1["order"]), "decision": "yes",
                                            "reply": "yes"})
        M.write_json(eng.confirm_path(t2), {"fingerprint": "not-this-order", "decision": "yes", "reply": "yes"})
        eng.tick(DT(2026, 10, 28, 9, 35, 15))
        self.assertEqual(eng.journal["trades"][str(t1["n"])]["state"], "entry_open")
        self.assertEqual(eng.journal["trades"][str(t2["n"])]["state"], "expired")
        eng.tick(DT(2026, 10, 28, 9, 38))
        later = [t for t in eng.journal["trades"].values() if t["state"] == "expired" and "no yes" in
                 " ".join(eng.journal["events"])]
        self.assertTrue(later)                                     # the unanswered one timed out

    def test_kill_switch_with_flatten_closes_everything(self):
        eng = self.engine()
        self.play(eng, end="09:36")
        self.assertTrue([t for t in eng.journal["trades"].values() if t["state"] == "open"])
        with open(os.path.join("trader", "live", "KILL"), "w") as fh:
            fh.write("flatten")
        j = self.play(eng, start="09:37", end="09:39")
        self.assertIn("kill switch", j["halted"]["why"])
        self.assertFalse([t for t in j["trades"].values() if t["state"] in ("open", "entry_open")])

    def test_daily_loss_halts_and_flattens(self):
        # LOSECO's stop fills past its trigger: the day's loss crosses the limit and the engine halts
        j = self.play(self.engine(c=cfg(max_daily_loss_inr=1100, live={"watchlist": ["LOSECO"]})), end="10:00")
        self.assertIn("daily loss", j["halted"]["why"])
        self.assertFalse([t for t in j["trades"].values() if t["state"] == "open"])

    def test_restart_resumes_from_the_journal(self):
        eng = self.engine()
        self.play(eng, end="09:36")
        open_before = sorted(t["n"] for t in eng.journal["trades"].values() if t["state"] == "open")
        eng2 = self.engine()
        eng2.load(DAY)
        eng2.last_bar_slot = M.bar_slot(DT(2026, 10, 28, 9, 36), 5)
        j = self.play(eng2, start="09:37", end="15:31")
        self.assertTrue(open_before)
        self.assertTrue(all(j["trades"][str(n)]["state"] == "closed" for n in open_before))
        self.assertEqual(len(j["trades"]), 3)                      # no duplicate signals after the restart

    def test_both_exits_filled_flattens_and_halts(self):
        eng = self.engine()
        self.play(eng, end="09:36")
        t = [x for x in eng.journal["trades"].values() if x["state"] == "open"][0]
        for leg in ("S", "T"):
            o = eng.broker.orders[t["legs"][leg]["oid"]]
            o.update(status="filled", filled_qty=o["qty"], avg_price=t["plan"]["entry"])
        eng.tick(DT(2026, 10, 28, 9, 36, 30))
        self.assertIn("two exits", eng.journal["halted"]["why"])
        self.assertIn("X2", t["legs"] if t["legs"] else eng.journal["trades"][str(t["n"])]["legs"])

    def test_unknown_place_is_resolved_by_tag(self):
        eng = self.engine()

        class Flaky(E.PaperBroker):
            calls = 0

            def place(self, order, tag, now):
                Flaky.calls += 1
                oid = E.PaperBroker.place(self, order, tag, now)
                if Flaky.calls == 1:
                    raise U.UpstoxError("timeout", retryable=True)
                return oid

            def find_by_tag(self, tag):
                return [o for o in self.orders.values() if o["tag"] == tag]
        eng.broker = Flaky(eng.cfg)
        j = self.play(eng, end="09:37")
        first = j["trades"]["1"]["legs"]["E"]
        self.assertTrue(first["oid"])
        self.assertNotEqual(first["status"], "unknown")


class Locks(Workdir):
    def run_live(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = L.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write_cfg(self, **kw):
        M.write_json(os.path.join("trader", "config.json"), cfg(**kw))

    def test_live_run_is_locked_until_validated_and_approved(self):
        self.write_cfg(paper=False, live={"mode": "live"})
        code, out, err = self.run_live(["run", "--mode", "live"])
        self.assertEqual(code, 1)
        self.assertIn("live mode is locked", err)
        self.write_cfg(paper=True)
        code, _, err = self.run_live(["run", "--mode", "live"])
        self.assertIn("paper", err)

    def test_live_run_refuses_the_delayed_feed(self):
        self.write_cfg(paper=False)
        code, _, err = self.run_live(["run", "--mode", "live", "--feed", "yahoo"])
        self.assertIn("real-time Upstox feed", err)

    def test_approve_live_needs_a_human_at_a_terminal(self):
        self.write_cfg()
        code, _, err = self.run_live(["approve-live"])
        self.assertEqual(code, 1)
        self.assertIn("typed by you at a terminal", err)
        self.assertFalse(os.path.exists(L.APPROVAL))

    def test_a_forged_or_stale_approval_is_refused(self):
        c = cfg()
        M.write_json(L.APPROVAL, {"via": "tty", "phrase": L.PHRASE, "limits_hash": L.limits_hash(c)})
        self.assertTrue(L.approval_ok(c)[0])
        self.assertFalse(L.approval_ok(dict(c, max_loss_per_trade_inr=5000))[0])
        self.assertFalse(L.approval_ok(dict(c, live=dict(c["live"], watchlist=["ADANIENT"])))[0])
        M.write_json(L.APPROVAL, {"via": "script", "phrase": L.PHRASE, "limits_hash": L.limits_hash(c)})
        self.assertFalse(L.approval_ok(c)[0])

    def test_validate_command_writes_a_passing_report(self):
        self.write_cfg()
        code, out, _ = self.run_live(["validate"])
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: PASS", out)
        self.assertNotIn("no passing `live.py validate` run", L.readiness(cfg()))
        self.assertIn("no complete paper session on the real-time Upstox feed (run `live.py run` for a full day)",
                      L.readiness(cfg()))

    def test_kill_resume_and_status(self):
        self.write_cfg()
        self.run_live(["kill", "--flatten"])
        with open(os.path.join("trader", "live", "KILL")) as fh:
            self.assertIn("flatten", fh.read())
        self.run_live(["resume"])
        self.assertFalse(os.path.exists(os.path.join("trader", "live", "KILL")))
        code, out, _ = self.run_live(["status"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
