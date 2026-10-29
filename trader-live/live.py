#!/usr/bin/env python3
"""trader-live: real-time NSE monitoring, signals and order management through Upstox.
Paper first. Live only after `approve-live`, typed by you at a terminal.

    python3 live.py setup                       # create ~/.config/trader-india/.env (you fill it in)
    python3 live.py login                       # Upstox login in the browser (each trading morning)
    python3 live.py doctor                      # credentials, session, static IP, clock, data
    python3 live.py validate                    # the whole workflow on a simulated day (no network)
    python3 live.py run                         # paper trading on real-time Upstox data
    python3 live.py status                      # trades, P&L, halts, last heartbeat
    python3 live.py confirm 3                   # say yes (or no) to signal #3 (live: at a terminal)
    python3 live.py kill [--flatten]            # stop new entries now (and close everything)
    python3 live.py resume                      # remove the kill switch
    python3 live.py record RELIANCE TCS         # save today's 1-minute bars for a replay
    python3 live.py validate --replay trader/live/recordings/2026-10-29
    python3 live.py approve-live                # after paper validation: unlock live mode (typed, at a terminal)
    python3 live.py run --mode live             # real orders; every entry still waits for your yes
    python3 live.py revoke-live

Live mode refuses to start unless ALL of these hold:
  - trader/live/LIVE_APPROVAL.json exists, was typed at a terminal by you, and its
    limits fingerprint matches today's config (change a limit -> approve again);
  - `validate` passed (a simulated day with zero invariant violations);
  - at least one full paper session ran on the real-time Upstox feed without problems;
  - the Upstox session is valid and the feed is Upstox (not Yahoo, which is delayed).
Entries in live mode wait for `live.py confirm N`, typed at a terminal. Stop-loss,
target and square-off orders are placed by the engine without asking.
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402
import upstox as U  # noqa: E402
import engine as E  # noqa: E402

BASE = E.LIVE_DIR
APPROVAL = os.path.join(BASE, "LIVE_APPROVAL.json")
PHRASE = "I APPROVE LIVE TRADING WITH REAL MONEY"
LIMIT_KEYS = ("capital_inr", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "max_order_inr",
              "max_position_pct", "allow_short", "allow_futures", "allow_option_selling", "use_intraday_leverage",
              "broker", "flat_by", "no_entries_after")
LIVE_KEYS = ("watchlist", "strategy", "sides", "stop", "target_r", "max_open_trades")
ENV_TEMPLATE = """# Upstox API credentials for trader-live. Keep this file private (chmod 600).
# Create an app at https://account.upstox.com/developer/apps with this redirect URL:
#   http://127.0.0.1:8765/callback
UPSTOX_API_KEY=
UPSTOX_API_SECRET=
UPSTOX_REDIRECT_URI=http://127.0.0.1:8765/callback
"""


class LiveError(M.MarketError):
    pass


def tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def limits_of(cfg):
    lc = E.live_cfg(cfg)
    d = {k: cfg.get(k, M.DEFAULT_CONFIG.get(k)) for k in LIMIT_KEYS}
    d.update({"live." + k: lc.get(k) for k in LIVE_KEYS})
    return d


def limits_hash(cfg):
    return hashlib.sha256(json.dumps(limits_of(cfg), sort_keys=True, default=str).encode()).hexdigest()[:16]


def reports(pattern):
    out = []
    for f in sorted(glob.glob(os.path.join(BASE, "reports", pattern))):
        try:
            with open(f, encoding="utf-8") as fh:
                out.append((f, json.load(fh)))
        except (OSError, ValueError):
            continue
    return out


def readiness(cfg):
    """What still stands between this setup and live mode."""
    gaps = []
    val = [r for f, r in reports("validation-*.json") if r.get("validated")]
    if not val:
        gaps.append("no passing `live.py validate` run")
    paper = [r for f, r in reports("*-paper-upstox.json") if r.get("validated") and not r.get("delayed_feed")]
    if not paper:
        gaps.append("no complete paper session on the real-time Upstox feed (run `live.py run` for a full day)")
    return gaps


def approval_ok(cfg):
    doc = M.read_json(APPROVAL, "live approval", required=False)
    if not doc:
        return False, "live trading is not approved: run `live.py approve-live` yourself at a terminal"
    if doc.get("limits_hash") != limits_hash(cfg):
        return False, ("your limits or watchlist changed since you approved live trading on %s; approve again"
                       % doc.get("approved_at"))
    if doc.get("phrase") != PHRASE or doc.get("via") != "tty":
        return False, "the approval file is not valid; run `live.py approve-live` again"
    return True, doc


# ================================================================ commands

def cmd_setup(a):
    path = os.path.join(U.CONFIG_DIR, ".env")
    os.makedirs(U.CONFIG_DIR, exist_ok=True)
    os.chmod(U.CONFIG_DIR, 0o700)
    if os.path.exists(path):
        print("%s already exists (not touched). Edit it to change the credentials." % path)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(ENV_TEMPLATE)
        print("Created %s (private, mode 600)." % path)
    print("Next: open it in a text editor, paste your Upstox API key and secret, save. Do not paste them into "
          "any chat. Then run: python3 trader-live/live.py login")
    gi = os.path.join(os.getcwd(), ".gitignore")
    try:
        text = open(gi).read() if os.path.exists(gi) else ""
        if ".env" not in text.split():
            with open(gi, "a") as fh:
                fh.write("\n.env\n/trader/\n")
            print("Added .env and trader/ to .gitignore here.")
    except OSError:
        pass
    return 0


def cmd_login(a):
    creds = U.Credentials.load()
    doc = U.run_login(creds, timeout=a.timeout, open_browser=not a.no_browser)
    print("Logged in as %s (%s). Session valid until %s IST." % (doc.get("user_name") or "?", doc.get("user_id") or "?",
                                                                doc["expires_at"].replace("T", " ")))
    return 0


def client_from_token():
    tok = U.load_token()
    return U.Client(tok["access_token"], log=lambda m: None), tok


def cmd_doctor(a):
    ok = True
    print("Python %s" % sys.version.split()[0])
    try:
        creds = U.Credentials.load()
        print("credentials: found (%s), api key %s..." % (creds.source, creds.api_key[:4]))
    except U.UpstoxError as e:
        ok = False
        print("credentials: MISSING - %s" % e)
    try:
        c, tok = client_from_token()
        print("session: valid until %s" % tok["expires_at"])
        prof = c.profile()
        print("profile: %s (%s), exchanges %s" % (prof.get("user_name"), prof.get("user_id"),
                                                  ",".join(prof.get("exchanges") or [])))
        try:
            f = (c.funds() or {}).get("equity") or {}
            print("funds: available margin %s" % M.inr(f.get("available_margin")))
        except U.UpstoxError as e:
            print("funds: %s (the funds service sleeps 00:00-05:30 IST)" % e)
        try:
            ips = c.static_ips() or {}
            print("static IP: primary %s, secondary %s%s" % (ips.get("primary_ip") or "NOT SET",
                                                             ips.get("secondary_ip") or "none",
                                                             "" if ips.get("primary_ip") else
                                                             " - SEBI's algo rules: Upstox may reject API orders "
                                                             "from an unregistered IP"))
        except U.UpstoxError as e:
            print("static IP: could not read (%s)" % e)
        st = c.market_status("NSE") or {}
        print("NSE status (Upstox): %s" % st.get("status"))
    except U.UpstoxError as e:
        ok = False
        print("session: %s" % e)
    now = M.now_ist()
    print("clock: %s IST, %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), M.market_status(now)["label"]))
    cfg = M.load_config(a.config)
    lc = E.live_cfg(cfg)
    print("mode in config: %s; watchlist: %s" % (lc["mode"], ", ".join(lc["watchlist"]) or "EMPTY (set live.watchlist)"))
    gaps = readiness(cfg)
    print("live readiness: %s" % ("ready to approve" if not gaps else "not yet - " + "; ".join(gaps)))
    good, why = approval_ok(cfg)
    print("live approval: %s" % ("approved %s" % why.get("approved_at") if good else why))
    return 0 if ok else 1


def build_engine(cfg, mode, feed_name, base=BASE, echo=True, clock=None):
    lc = E.live_cfg(cfg)
    now = (clock or M.now_ist)()
    log = E.make_logger(mode, now.date().isoformat(), base, echo)
    keys = None
    if feed_name == "upstox" or mode == "live":
        c, _ = client_from_token()
        c.log = lambda m: log.info(U.redact(m))
        inst = U.Instruments.load(now.date())
        feed = E.UpstoxFeed(c, inst, cfg.get("exchange", "NSE"))
        keys = lambda t: inst.key_for(t["order"])  # noqa: E731
    elif feed_name == "yahoo":
        feed = E.YahooFeed(exchange=cfg.get("exchange", "NSE"))
    else:
        raise LiveError("feed must be upstox or yahoo for run (replay is for validate)")
    if mode == "live":
        broker = E.UpstoxBroker(c, inst, U)
    else:
        broker = E.PaperBroker(cfg)
    eng = E.Engine(cfg, feed, broker, mode=mode, base=base, log=log, upstox_keys=keys)
    if mode == "live":
        try:
            eng.live_cash = float(((c.funds() or {}).get("equity") or {}).get("available_margin") or 0) or None
        except U.UpstoxError:
            eng.live_cash = None
    return eng, lc


def cmd_run(a):
    cfg = M.load_config(a.config)
    lc = E.live_cfg(cfg)
    mode = a.mode or lc["mode"]
    feed = a.feed or lc["feed"]
    if not lc["watchlist"]:
        raise LiveError("set \"live\": {\"watchlist\": [\"RELIANCE\", ...]} in trader/config.json")
    if mode == "live":
        if feed != "upstox":
            raise LiveError("live mode needs the real-time Upstox feed (Yahoo is delayed)")
        if cfg.get("paper", True):
            raise LiveError("\"paper\" is true in trader/config.json; set it to false for live mode")
        gaps = readiness(cfg)
        if gaps:
            raise LiveError("live mode is locked: " + "; ".join(gaps))
        good, why = approval_ok(cfg)
        if not good:
            raise LiveError(why)
    elif not cfg.get("paper", True):
        print("note: \"paper\" is false in your config, but this run is PAPER (no real orders).")
    eng, lc = build_engine(cfg, mode, feed)
    now = M.now_ist()
    eng.load(now.date())
    issues = eng.reconcile(now)
    if issues:
        print("Reconciliation found problems; new entries are halted: %s" % "; ".join(issues))
    print("%s engine started: %s feed, watchlist %s, strategy %s, poll %ss. Ctrl+C stops it (open trades keep "
          "their stop and target at the broker in live mode)." % (mode.upper(), eng.feed.name,
                                                                 ", ".join(lc["watchlist"]), lc["strategy"],
                                                                 lc["poll_seconds"]))
    if eng.feed.delayed:
        print("warning: the Yahoo feed is DELAYED; paper results on it are only a rough check.")
    until = None
    if a.until:
        until = datetime.datetime.combine(now.date(), M.parse_hhmm(a.until))
    try:
        E.run(eng, until=until, max_ticks=1 if a.once else None)
    except KeyboardInterrupt:
        eng.save()
        print("\nstopped. Journal saved; `live.py status` shows open trades.")
    return 0


def latest_journal(mode=None):
    files = sorted(glob.glob(os.path.join(BASE, "journal-*-%s.json" % (mode or "*"))))
    return (files[-1], M.read_json(files[-1], "journal")) if files else (None, None)


def cmd_status(a):
    path, j = latest_journal(a.mode)
    hb = M.read_json(os.path.join(BASE, "heartbeat.json"), "heartbeat", required=False)
    if hb:
        age = (M.now_ist() - datetime.datetime.fromisoformat(hb["at"])).total_seconds()
        print("engine: %s mode, %s feed, last heartbeat %s (%ds ago)%s" % (
            hb["mode"], hb["feed"], hb["at"][11:19], age, "" if age < 60 else " - NOT RUNNING?"))
    if not j:
        print("no journal yet")
        return 0
    print("%s %s: day net %s%s" % (j["date"], j["mode"].upper(), M.inr(j.get("day_net", 0)),
                                   ("; HALTED: %s" % j["halted"]["why"]) if j.get("halted") else ""))
    for t in sorted(j["trades"].values(), key=lambda x: x["n"]):
        print("  #%d %-10s %-4s %4d  %-15s %s%s" % (t["n"], t["symbol"], t["side"], t["qty"], t["state"],
                                                   ("net %s" % M.inr(t["net"])) if t["state"] == "closed" else
                                                   "entry %.2f stop %.2f target %.2f" % (
                                                       t["plan"]["entry"], t["plan"]["stop"], t["plan"]["target"]),
                                                   ("  (%s)" % t["why"]) if t.get("why") else ""))
    for line in j["events"][-a.events:]:
        print("    " + line)
    return 0


def cmd_confirm(a, decision="yes"):
    _, j = latest_journal()
    if not j:
        raise LiveError("no engine journal today")
    t = j["trades"].get(str(a.n))
    if not t or t["state"] != "pending_confirm":
        raise LiveError("signal #%s is not waiting for a confirmation (%s)" % (a.n, t["state"] if t else "unknown"))
    if j["mode"] == "live" and not tty():
        raise LiveError("live entries are confirmed by you, typing at a terminal (not by a script or an assistant)")
    print("Signal #%d (%s): %s" % (t["n"], j["mode"].upper(), t["ticket"]))
    reply = a.reply
    if reply is None:
        if not tty():
            raise LiveError("give --reply or run it at a terminal")
        reply = input("Type yes to place this one order, anything else to skip: ").strip()
    ok, why = (False, "you said no") if decision == "no" else E.TICKET.parse_yes(reply, t["order"], ())
    doc = {"n": t["n"], "fingerprint": E.TICKET.fingerprint(t["order"]), "reply": reply,
           "decision": "yes" if ok else "no", "at": M.now_ist().isoformat(timespec="seconds"), "tty": tty()}
    M.write_json(os.path.join(BASE, "confirmed", "%s-%02d.json" % (j["date"], t["n"])), doc)
    print(("Confirmed. The engine places it on its next tick." if ok else "Not placed: %s." % why))
    return 0 if ok else 1


def cmd_kill(a):
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, "KILL"), "w") as fh:
        fh.write("flatten\n" if a.flatten else "stop new entries\n")
    print("Kill switch set: no new entries%s. The engine acts on its next tick. `live.py resume` removes it." % (
        "; open trades will be closed at market" if a.flatten else ""))
    return 0


def cmd_resume(a):
    p = os.path.join(BASE, "KILL")
    if os.path.exists(p):
        os.remove(p)
    print("Kill switch removed. A halt already recorded today stays until tomorrow's session.")
    return 0


def cmd_record(a):
    c, _ = client_from_token()
    now = M.now_ist()
    inst = U.Instruments.load(now.date())
    out = os.path.join(BASE, "recordings", now.date().isoformat())
    os.makedirs(out, exist_ok=True)
    keys = {s: inst.key_for(s) for s in a.symbols}
    ltp = c.ltp(list(keys.values()))
    for s, k in keys.items():
        bars = c.intraday_candles(k, 1)
        M.write_json(os.path.join(out, s.upper() + ".json"), {
            "symbol": s.upper(), "prev_close": (ltp.get(k) or {}).get("prev_close"),
            "bars": [[b["dt"].isoformat(), b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]})
        print("%s: %d one-minute bars" % (s.upper(), len(bars)))
    print("saved to %s (replay with: live.py validate --replay %s)" % (out, out))
    return 0


# ---------------------------------------------------------------- validation

def synthetic_day(day, seed=7):
    """Five scripted stocks for one day: a winner, a loser, a square-off, no signal, a late signal."""
    rnd = random.Random(seed)
    o = datetime.datetime.combine(day, M.OPEN)
    scripts = {
        "WINCO": (500.0, [(0, 15, 0.0), (15, 25, 0.004), (25, 200, 0.0035), (200, 375, 0.0)]),   # breaks out, runs to target
        "LOSECO": (800.0, [(0, 15, 0.0), (15, 22, 0.003), (22, 120, -0.004), (120, 375, 0.0)]),  # breaks out, fails to stop
        "DRIFTCO": (1200.0, [(0, 15, 0.0), (15, 20, 0.0025), (20, 375, 0.00004)]),              # breaks out, drifts to 15:10
        "FLATCO": (300.0, [(0, 15, 0.0), (15, 375, -0.00006)]),                                  # never breaks out
        "LATECO": (650.0, [(0, 15, 0.0), (15, 335, -0.00004), (335, 375, 0.004)]),               # breaks out after 14:45
    }
    bars, prev = {}, {}
    for sym, (start, legs) in scripts.items():
        px = start
        out = []
        for i in range(375):
            drift = next(d for a0, a1, d in legs if a0 <= i < a1)
            wig = start * 0.0004
            op = px
            px = max(1.0, px * (1 + drift) + (rnd.random() - 0.5) * wig * 0.2)
            hi = max(op, px) + wig * 0.3
            lo = min(op, px) - wig * 0.3
            out.append({"dt": o + datetime.timedelta(minutes=i), "o": round(op, 2), "h": round(hi, 2),
                        "l": round(lo, 2), "c": round(px, 2), "v": 50000.0})
        bars[sym] = out
        prev[sym] = start
    return bars, prev


def load_replay(folder):
    bars, prev = {}, {}
    for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
        d = M.read_json(f, "recording")
        s = d["symbol"].upper()
        bars[s] = [{"dt": datetime.datetime.fromisoformat(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                    "v": r[5]} for r in d["bars"]]
        prev[s] = d.get("prev_close")
    if not bars:
        raise LiveError("no recordings in %s" % folder)
    return bars, prev


def validate(cfg, bars, prev, day, base, step_seconds=15, echo=False):
    """Run the engine over one replayed day with the paper broker and a simulated clock."""
    cfg = dict(cfg)
    lc = dict(E.live_cfg(cfg))
    lc.update(watchlist=sorted(bars), mode="paper", feed="replay", paper_auto_confirm=True,
              max_open_trades=max(3, int(lc.get("max_open_trades") or 1)))
    cfg["live"] = lc
    cfg["paper"] = True
    cfg.setdefault("capital_inr", 500000)
    cfg["max_trades_per_day"] = max(int(cfg.get("max_trades_per_day") or 3), 5)
    cfg["max_position_pct"] = max(float(cfg.get("max_position_pct") or 50), 100)
    cfg["max_order_inr"] = max(float(cfg.get("max_order_inr") or 0), 400000)
    cfg["min_avg_turnover_cr"] = 0
    if os.path.isdir(base):
        for f in glob.glob(os.path.join(base, "journal-*")) + glob.glob(os.path.join(base, "pending", "*")):
            os.remove(f)
    feed = E.ReplayFeed(bars, prev)
    log = E.make_logger("paper", day.isoformat(), base, echo)
    eng = E.Engine(cfg, feed, E.PaperBroker(cfg), mode="paper", base=base, log=log, notifier=lambda *x: None)
    t = datetime.datetime.combine(day, datetime.time(9, 10))
    end = datetime.datetime.combine(day, datetime.time(15, 31))
    while t <= end:
        eng.tick(t)
        t += datetime.timedelta(seconds=step_seconds)
    j = eng.journal
    closed = [x for x in j["trades"].values() if x["state"] == "closed"]
    rep = {"date": day.isoformat(), "symbols": sorted(bars), "trades": len(j["trades"]), "closed": len(closed),
           "by_exit": {k: len([x for x in closed if x.get("exit_reason", "").startswith(k)]) for k in
                       ("target", "stop-loss", "flat_by", "square-off")},
           "net": round(sum(x["net"] for x in closed), 2), "charges": round(sum(x["charges"] for x in closed), 2),
           "invariant_violations": j.get("invariants") or [], "halted": j.get("halted"),
           "validated": bool(j.get("ended")) and not (j.get("invariants") or []) and len(j["trades"]) > 0,
           "events": j["events"]}
    return rep, j


def cmd_validate(a):
    cfg = M.load_config(a.config)
    if a.replay:
        bars, prev = load_replay(a.replay)
        day = bars[sorted(bars)[0]][0]["dt"].date()
        src = a.replay
    else:
        day = datetime.date(2026, 10, 28)
        bars, prev = synthetic_day(day)
        src = "synthetic day (5 scripted stocks)"
    rep, j = validate(cfg, bars, prev, day, os.path.join(BASE, "validation"), echo=a.verbose)
    rep["source"] = src
    rep["at"] = M.now_ist().isoformat(timespec="seconds")
    M.write_json(os.path.join(BASE, "reports", "validation-%s.json" % M.now_ist().strftime("%Y%m%d-%H%M%S")), rep)
    print("Validation on %s (%s): %d signals, %d closed (%s), net %s after %s charges." % (
        src, day, rep["trades"], rep["closed"], ", ".join("%s %d" % kv for kv in rep["by_exit"].items() if kv[1]),
        M.inr(rep["net"]), M.inr(rep["charges"])))
    for t in sorted(j["trades"].values(), key=lambda x: x["n"]):
        print("  #%d %-8s %s -> %s%s" % (t["n"], t["symbol"], t["created_at"][11:16], t["state"],
                                         (" by %s, net %s" % (t.get("exit_reason"), M.inr(t["net"])))
                                         if t["state"] == "closed" else (" (%s)" % t.get("why") if t.get("why") else "")))
    if rep["invariant_violations"]:
        print("FAILED checks:")
        for b in rep["invariant_violations"]:
            print("  - " + b)
    print("RESULT: %s" % ("PASS - every signal was sized, risk-checked, filled, protected by a stop and closed"
                          if rep["validated"] else "FAIL"))
    return 0 if rep["validated"] else 1


# ---------------------------------------------------------------- approval

def cmd_approve(a):
    cfg = M.load_config(a.config)
    if not tty():
        raise LiveError("live approval must be typed by you at a terminal; it cannot be scripted")
    gaps = readiness(cfg)
    if gaps:
        raise LiveError("not ready for live: " + "; ".join(gaps))
    lim = limits_of(cfg)
    print("You are about to allow REAL orders through Upstox with these limits:")
    for k, v in sorted(lim.items()):
        print("  %-28s %s" % (k, v))
    print("Every new entry will still wait for your typed yes (live.py confirm N). Stops, targets and the square-off "
          "are placed automatically. You can lose money; most intraday traders do. Not financial advice.")
    typed = input("Type exactly: %s\n> " % PHRASE).strip()
    if typed != PHRASE:
        print("Not approved (the phrase did not match).")
        return 1
    M.write_json(APPROVAL, {"approved_at": M.now_ist().isoformat(timespec="seconds"), "via": "tty", "phrase": PHRASE,
                            "limits_hash": limits_hash(cfg), "limits": lim, "user": os.environ.get("USER")})
    print("Live trading approved for these limits. Set \"paper\": false and \"live\": {\"mode\": \"live\"} in "
          "trader/config.json, then `live.py run --mode live`. `live.py revoke-live` undoes this.")
    return 0


def cmd_revoke(a):
    if os.path.exists(APPROVAL):
        os.remove(APPROVAL)
    print("Live approval removed. The engine refuses live mode until you approve again.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="live.py", description="Real-time NSE monitoring and trading through Upstox "
                                "(paper first).")
    p.add_argument("--config", default=M.CONFIG_FILE)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    s = sub.add_parser("login")
    s.add_argument("--timeout", type=int, default=300)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("run")
    s.add_argument("--mode", choices=("paper", "live"))
    s.add_argument("--feed", choices=("upstox", "yahoo"))
    s.add_argument("--until", help="HH:MM IST")
    s.add_argument("--once", action="store_true", help="one tick, then exit")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status")
    s.add_argument("--mode", choices=("paper", "live"))
    s.add_argument("--events", type=int, default=12)
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("confirm")
    s.add_argument("n", type=int)
    s.add_argument("--reply")
    s.set_defaults(fn=cmd_confirm)
    s = sub.add_parser("reject")
    s.add_argument("n", type=int)
    s.add_argument("--reply", default="no")
    s.set_defaults(fn=lambda a: cmd_confirm(a, "no"))
    s = sub.add_parser("kill")
    s.add_argument("--flatten", action="store_true")
    s.set_defaults(fn=cmd_kill)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    s = sub.add_parser("record")
    s.add_argument("symbols", nargs="+")
    s.set_defaults(fn=cmd_record)
    s = sub.add_parser("validate")
    s.add_argument("--replay", help="a folder from `live.py record`")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_validate)
    sub.add_parser("approve-live").set_defaults(fn=cmd_approve)
    sub.add_parser("revoke-live").set_defaults(fn=cmd_revoke)
    for name in list(sub.choices):
        sub.choices[name].add_argument("--config", default=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if not getattr(a, "cmd", None):
        p.print_help()
        return 2
    try:
        return a.fn(a)
    except M.MarketError as e:
        print("error: %s" % U.redact(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
