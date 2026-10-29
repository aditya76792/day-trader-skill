#!/usr/bin/env python3
"""Time Machine for trader-timemachine (India edition). Standard library only.

Replays a planned NSE/BSE day trade before you risk a rupee: 10,000 simulated
runs of the rest of the session from your entry, the same rule on every past
session in the data, and (new) a plain backtest of a simple rule over the
last ~60 sessions, all net of Indian charges and slippage.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:10            # stop each replay at this time (default: the plan's flat_by)
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-28 --entry 1171.2 \\
        --stop 1163.2 --target 1187.2 --entry-time 09:50
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py backtest RELIANCE TCS --rule orb --target 2R --write
    python3 timemachine.py backtest SBIN --rule gap --side sell     # gap-down-and-go shorts
    python3 timemachine.py backtest NIFTY --rule orb                # an index: costed as 1 lot of futures

How the replays work (the original sim.py algorithm, on Indian sessions):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d, interval=5m;
     one request, the most Yahoo keeps). Keep only FULL sessions before the
     plan's date: 75 bars, 09:15 to 15:30 IST, on NSE trading days (the NSE
     holiday list in market.py). Short sessions, a Muhurat session, holidays
     and today are left out.
  2. The pool: for every bar slot 1..74, each past session's bar as ratios to
     the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry and walks the rest of the session. For
     each bar i it picks a slot j = i + randint(-window, window), kept in
     1..74, then one past bar from pool[j], scaled to the current price. The
     stop is checked before the target in every bar (a bar touching both
     counts as the stop); a bar that OPENS through the stop fills at its open.
  4. Outcomes: target, stop, or neither (still open at --until, default the
     plan's flat_by, else 15:30). A trade still open exits at the close of
     the bar that ends at --until.
  5. Costs: every replay's round trip goes through market.round_trip for the
     plan's product (MIS = intraday, CNC = delivery, FUT = futures) and a
     reference size (your max_loss_per_trade_inr / risk, or 1 lot), with
     slippage_pct (config, default 0.05%) against you on both fills. Net
     expectancy is reported in R and in rupees.
  6. CNC (swing) plans: replays still open at the close draw an overnight gap
     from the data's own close -> next-open gaps; a gap through the stop
     fills at the open (worse than the stop). The odds of that are shown.
  7. History: the plan's own rule (orb or gap-and-go with the same opening
     range, stop rule and target R; vwap_reclaim) re-run on every one of
     those past sessions, with the same charges and slippage.

The verdict looks at the replays that hit one side and compares the target's
share, target / (target + stop), with 1 / (1 + R): under 5 points "Close to a
coin flip" (1R) / "Close to break-even", 5 to 15 "Leans toward", 15 or more
"Strongly toward". Lines under it: many replays hit neither, the average R
points the other way, or charges turn a gross edge into a net loss.

Data: https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m
(unofficial, free, delayed, can break). --offline FILE reads a saved chart
JSON (.json or .json.gz) or a folder with <SYMBOL>-60d5m.json / <SYMBOL>.json.
Times are IST; --now "2026-10-28 10:15" pins the clock. Same plan, same data,
same --seed: the same numbers on any machine. Past results do not predict
future results, and 60 days is a small sample.
"""

import argparse
import datetime
import json
import math
import os
import random
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

Err = M.MarketError
TMError = Err
NET_FACTORY = M.Net
INTERVAL = 300
SESSION_BARS = M.bars_per_session(5)          # 75
OPEN_MIN = M.OPEN.hour * 60 + M.OPEN.minute   # 555
CLOSE_MIN = M.CLOSE.hour * 60 + M.CLOSE.minute
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40
GAP_LARGE = 2.0
RULES = ("orb", "gap", "vwap_reclaim")
SMALL_SAMPLE = ("Past results do not predict future results, and about 60 sessions is a small sample: a few "
                "lucky or unlucky days move every number here.")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------- time

def parse_clock(text, what):
    """'09:50', '9:50 AM', '15:10' or '3:10' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise Err("%s wants an IST time like 09:50 or 15:10, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise Err("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def slot_close(k):
    """Bar k's close time in minutes (bar 0 closes 09:20)."""
    return OPEN_MIN + 5 * (k + 1)


# ---------------------------------------------------------------- data

def read_offline(path, sym):
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym, "%s-60d5m.json.gz" % sym, "%s.json" % sym, "%s.json.gz" % sym):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise Err("no %s-60d5m.json(.gz) or %s.json in %s" % (sym, sym, path))
    if not os.path.exists(path):
        raise Err("no offline chart file %s" % path)
    try:
        return M.read_text_maybe_gz(path)
    except OSError as e:
        raise Err("could not read %s: %s" % (path, e))


def fetch(sym, exchange, offline=None, net=None):
    """(Yahoo chart doc or text, source)."""
    if offline:
        return read_offline(offline, sym), "offline"
    net = net or NET_FACTORY()
    return net.chart(sym, "60d", "5m", exchange), "yahoo"


def parse_bars(text_or_doc, sym, exchange, now):
    """-> (meta, bars, dropped). Regular-session 5-minute bars, oldest first."""
    series = M.parse_chart(text_or_doc, sym)
    meta = series["meta"]
    want = M.yahoo_symbol(sym, exchange).upper()
    got = str(meta.get("symbol") or want).upper()
    if got != want:
        raise Err("the data is for %s but the plan is for %s (%s)" % (got, sym, want))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise Err("the data has %s bars; the time machine needs 5-minute bars" % gran)
    as_of = now
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and M.ist_from_ts(rmt) < as_of:
        as_of = M.ist_from_ts(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(series["t"]):
        o, h, lo, c = series["o"][i], series["h"][i], series["l"][i], series["c"][i]
        if ts is None or None in (o, h, lo, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        dt = M.ist_from_ts(ts)
        mins = dt.hour * 60 + dt.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if dt + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": dt.date().isoformat(), "slot": mins // 5, "start": dt.strftime("%H:%M"),
                    "o": o, "h": h, "l": lo, "c": c, "v": series["v"][i] or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before=None):
    """Full NSE sessions (all 75 bars, on a trading day) strictly before `before`,
    oldest first, and the ones left out."""
    full, short = [], []
    for d in sorted(days):
        if before and d >= before:
            continue
        s = days[d]
        if not M.trading_day(datetime.date.fromisoformat(d)):
            short.append({"date": d, "bars": len(s), "why": "not an NSE trading day (special session)"})
        elif len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s), "why": "short session or missing bars"})
    return full, short


def overnight_gaps(sessions):
    """next open / previous close for consecutive NSE trading days among the full sessions."""
    out = []
    for (d0, D0), (d1, D1) in zip(sessions, sessions[1:]):
        if M.next_trading_day(datetime.date.fromisoformat(d0)).isoformat() == d1:
            out.append(D1[0]["o"] / D0[-1]["c"])
    return out


# ---------------------------------------------------------------- costs

def trade_pnl(entry, exit_px, qty, long, segment, exchange, cfg, slip_pct):
    """Gross and net rupees for one round trip, slippage against you on both fills."""
    s = slip_pct / 100.0
    if long:
        buy, sell = entry * (1 + s), exit_px * (1 - s)
    else:
        sell, buy = entry * (1 - s), exit_px * (1 + s)
    rt = M.round_trip(segment, buy, sell, qty, exchange, (cfg.get("broker") or "zerodha").lower(), cfg)
    return {"gross": round(((exit_px - entry) if long else (entry - exit_px)) * qty, 2),
            "slippage": round(((exit_px - entry) if long else (entry - exit_px)) * qty - rt["gross_pnl"], 2),
            "charges": rt["total"], "net": rt["net_pnl"]}


def size_for(entry, risk, cfg, lot=None, qty=None):
    """(qty, note). Fixed risk sizing from max_loss_per_trade_inr; F&O in whole lots."""
    max_loss = float(cfg.get("max_loss_per_trade_inr") or 1000)
    if qty:
        return int(qty), "your quantity"
    if lot:
        lots = int(math.floor(max_loss / (risk * lot) + 1e-9)) if risk > 0 else 0
        if lots < 1:
            return lot, "1 lot of %d (it risks %s, more than your %s max loss)" % (
                lot, M.inr(risk * lot), M.inr(max_loss, paise=False))
        lots = min(lots, int(cfg.get("max_lots_per_trade") or lots))
        return lots * lot, "%d lot%s of %d" % (lots, "" if lots == 1 else "s", lot)
    q = int(math.floor(max_loss / risk + 1e-9)) if risk > 0 else 1
    cap = cfg.get("max_order_inr")
    if cap and entry > 0:
        q = min(q, int(math.floor(float(cap) / entry + 1e-9)))
    return max(1, q), "%d shares (max loss %s / risk %s%s)" % (
        max(1, q), M.inr(max_loss, paise=False), M.inr(risk), ", capped by max_order_inr" if cap else "")


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the original sim.py."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, exits, paths = [], [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        exit_px = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, lo, c = choice(pool[slot(rng, i)])
            op, hi, low, cl = p * o, p * h, p * lo, p * c
            if outcome is None:
                if long:
                    if low <= stop:
                        outcome, exit_px = "stop", min(op, stop)   # an open through the stop fills at the open
                    elif hi >= target:
                        outcome, exit_px = "target", target
                else:
                    if hi >= stop:
                        outcome, exit_px = "stop", max(op, stop)
                    elif low <= target:
                        outcome, exit_px = "target", target
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        exits.append(exit_px if exit_px is not None else p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, lo, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    mc = {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / float(replays), 4), "p_stop": round(cnt["stop"] / float(replays), 4),
        "p_neither": round(cnt["close"] / float(replays), 4), "p_green": round(green / float(replays), 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }
    return mc, outcomes, exits


def apply_gaps(outcomes, exits, gaps, stop, long, seed):
    """CNC: replays still open at the close take tomorrow's opening gap, drawn from the
    data's own overnight gaps. A gap through the stop fills at the open."""
    rng = random.Random(seed + 1)          # its own stream: the session draws stay as they were
    held = through = 0
    new_exits = list(exits)
    new_out = list(outcomes)
    for i, o in enumerate(outcomes):
        if o != "close" or not gaps:
            continue
        held += 1
        nxt = exits[i] * rng.choice(gaps)
        new_exits[i] = nxt
        if (nxt <= stop) if long else (nxt >= stop):
            through += 1
            new_out[i] = "gap_stop"
    return new_out, new_exits, held, through


def net_of_costs(outcomes, exits, plan, qty, segment, cfg, slip_pct):
    """Average net rupees and net R over the replays, charges and slippage included."""
    long = plan["side"] == "buy"
    risk_inr = plan["risk_per_share"] * qty
    cache = {}
    nets, charges, gross = [], [], []
    for px in exits:
        key = round(px, 2)
        if key not in cache:
            cache[key] = trade_pnl(plan["entry"], key, qty, long, segment, plan["exchange"], cfg, slip_pct)
        t = cache[key]
        nets.append(t["net"])
        charges.append(t["charges"])
        gross.append(t["gross"])
    n = float(len(nets)) or 1.0
    return {"qty": qty, "segment": segment, "slippage_pct": slip_pct,
            "net_expectancy_inr": round(sum(nets) / n, 2),
            "net_expectancy_r": round(sum(nets) / n / risk_inr, 3) if risk_inr else None,
            "gross_expectancy_inr": round(sum(gross) / n, 2),
            "avg_charges_inr": round(sum(charges) / n, 2),
            "costs_per_share": round(sum(charges) / n / qty, 2) if qty else None,
            "p_net_positive": round(sum(1 for x in nets if x > 0) / n, 4)}


# ---------------------------------------------------------------- rules on real sessions

def opening_range(D, n):
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def running_vwap(D, upto):
    pv = vv = 0.0
    out = []
    for b in D[:upto + 1]:
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * (b["v"] or 0)
        vv += b["v"] or 0
        out.append(pv / vv if vv else None)
    return out


def atr_bars(D, k, n=14):
    trs, prev = [], None
    for b in D[:k + 1]:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use)


def rule_stop(rule, oh, ol, entry, long, D=None, k=None, vw=None):
    r = rule["stop_rule"]
    away = "down" if long else "up"
    if r == "or_mid":
        raw = (oh + ol) / 2.0
    elif r in ("or_low", "or_high"):
        raw = ol if long else oh
    elif r == "atr":
        raw = entry - (1 if long else -1) * rule["stop_k"] * atr_bars(D, k)
    elif r == "vwap":
        if vw is None:
            return None
        raw = vw - (1 if long else -1) * M.tick_size(vw)
    else:
        frac = rule["stop_pct"] / 100.0
        raw = entry * (1 - frac) if long else entry * (1 + frac)
    return M.round_tick(raw, mode=away)


def find_trade(D, rule, last, prev_close=None, window=None, long=True):
    """The rule on one real session -> {k, entry, stop, target} or (None, why)."""
    n = rule["or_minutes"] // 5
    oh, ol = opening_range(D, n)
    setup = rule["setup"]
    lo_k, hi_k = n, last
    if window:
        lo_k = max(lo_k, int(math.ceil((window[0] - OPEN_MIN) / 5.0)) - 1)
        hi_k = min(hi_k, (window[1] - OPEN_MIN) // 5 - 1)
    if setup == "gap":
        if not prev_close:
            return None, "no previous close"
        gp = M.gap_pct(D[0]["o"], prev_close)
        if (gp < GAP_LARGE) if long else (gp > -GAP_LARGE):
            return None, "no gap"
        if (ol <= prev_close) if long else (oh >= prev_close):
            return None, "gap filled"
    vw = running_vwap(D, last) if (setup == "vwap_reclaim" or rule["stop_rule"] == "vwap") else None
    if setup == "vwap_reclaim" and not any(vw):
        return None, "no volume"
    k = None
    for i in range(max(lo_k, 1 if setup == "vwap_reclaim" else lo_k), hi_k + 1):
        c = D[i]["c"]
        if setup == "vwap_reclaim":
            a, b = vw[i - 1], vw[i]
            if a is None or b is None:
                continue
            if (D[i - 1]["c"] < a and c > b) if long else (D[i - 1]["c"] > a and c < b):
                k = i
                break
        elif (c > oh) if long else (c < ol):
            k = i
            break
    if k is None:
        return None, "no trigger"
    e = round(D[k]["c"], 2)
    s = rule_stop(rule, oh, ol, e, long, D, k, vw[k] if vw else None)
    if s is None:
        return None, "no stop"
    risk = (e - s) if long else (s - e)
    if risk <= 0:
        return None, "stop on the wrong side"
    t = M.round_tick(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk,
                     mode="down" if long else "up")
    return {"k": k, "entry": e, "stop": s, "target": t, "risk": risk}, None


def walk(D, tr, last, long):
    """Stop first, then target, bar by bar after the entry; else the close of bar `last`."""
    s, t = tr["stop"], tr["target"]
    for b in D[tr["k"] + 1:last + 1]:
        if (b["l"] <= s) if long else (b["h"] >= s):
            return "stop", (min(b["o"], s) if long else max(b["o"], s))
        if (b["h"] >= t) if long else (b["l"] <= t):
            return "target", t
    return "close", D[last]["c"]


def run_rule(sessions, rule, last, side, cfg, segment, exchange, lot=None, window=None, prev_closes=None,
             symbol=""):
    """The rule on every session, with fixed-risk size, charges and slippage."""
    long = side == "buy"
    slip = float(cfg.get("slippage_pct") or 0.0)
    trades, skipped = [], {}
    for d, D in sessions:
        pc = (prev_closes or {}).get(d)
        tr, why = find_trade(D, rule, last, pc, window, long)
        if tr is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        qty, _ = size_for(tr["entry"], tr["risk"], cfg, lot)
        if lot and tr["risk"] * lot > float(cfg.get("max_loss_per_trade_inr") or 1000):
            skipped["risk per lot above max loss"] = skipped.get("risk per lot above max loss", 0) + 1
            continue
        oc, px = walk(D, tr, last, long)
        p = trade_pnl(tr["entry"], px, qty, long, segment, exchange, cfg, slip)
        R = ((px - tr["entry"]) if long else (tr["entry"] - px)) / tr["risk"]
        trades.append({"symbol": symbol, "date": d, "time": clock24(slot_close(tr["k"])), "entry": tr["entry"],
                       "stop": tr["stop"], "target": tr["target"], "exit": round(px, 2), "outcome": oc,
                       "qty": qty, "R": round(R, 2), "gross": p["gross"], "charges": p["charges"],
                       "slippage": p["slippage"],
                       "net": p["net"], "net_R": round(p["net"] / (tr["risk"] * qty), 2)})
    return trades, skipped


def stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gross_profit = sum(t["gross"] for t in trades if t["gross"] > 0)
    charges = sum(t["charges"] for t in trades)
    return {"trades": n, "target": sum(1 for t in trades if t["outcome"] == "target"),
            "stop": sum(1 for t in trades if t["outcome"] == "stop"),
            "close": sum(1 for t in trades if t["outcome"] == "close"),
            "wins": sum(1 for x in nets if x > 0), "win_rate": round(sum(1 for x in nets if x > 0) / float(n), 4),
            "avg_R": round(statistics.mean(t["R"] for t in trades), 3),
            "expectancy_r": round(statistics.mean(t["net_R"] for t in trades), 3),
            "avg_net_pnl": round(sum(nets) / n, 2), "expectancy_inr": round(sum(nets) / n, 2),
            "net_pnl": round(sum(nets), 2), "gross_pnl": round(sum(t["gross"] for t in trades), 2),
            "total_charges": round(charges, 2), "gross_profit": round(gross_profit, 2),
            "total_slippage": round(sum(t.get("slippage", 0.0) for t in trades), 2),
            "charges_pct_of_gross_profit": round(100.0 * charges / gross_profit, 1) if gross_profit > 0 else None,
            "max_drawdown": round(dd, 2)}


def prev_close_map(sessions):
    out = {}
    for (d0, D0), (d1, _) in zip(sessions, sessions[1:]):
        if M.next_trading_day(datetime.date.fromisoformat(d0)).isoformat() == d1:
            out[d1] = D0[-1]["c"]
    return out


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise Err("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise Err("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now):
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise Err("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or now.date().isoformat(), "entry": a.entry,
               "stop": a.stop, "target": a.target, "entry_time": a.entry_time, "setup": a.setup,
               "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule, "interval": "5m",
               "product": a.product or "MIS", "exchange": a.exchange or "NSE", "flat_by": a.flat_by,
               "instrument": {"type": "EQ"}}
        src = "flags"
    else:
        raw = M.read_json(a.plan, "plan (run /trader-exit first, or pass --symbol --date --entry --stop --target "
                          "--entry-time)")
        if not isinstance(raw, dict):
            raise Err("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    sym = str(raw.get("symbol") or "").strip().upper()
    if not M.valid_symbol(sym):
        raise Err("the plan needs an NSE/BSE symbol, got %r" % raw.get("symbol"))
    inst = raw.get("instrument") or {}
    itype = str((inst.get("type") if isinstance(inst, dict) else inst) or "EQ").upper()
    if itype in ("CE", "PE"):
        raise Err("the time machine replays the stock's or index's own bars; an option premium moves with delta, "
                  "time decay and IV, so a %s plan cannot be replayed here. Replay the underlying's plan "
                  "(its entry, stop and target on %s) instead" % (itype, sym))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise Err("side must be buy or sell, got %r" % raw.get("side"))
    if str(raw.get("interval") or "5m") != "5m":
        raise Err("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about 7 days "
                  "of 1-min bars, too few sessions for a fair replay pool" % raw.get("interval"))
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise Err("the plan needs a date like 2026-10-28, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise Err("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise Err("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        cm = parse_clock(raw["entry_time"], "entry_time")
        if (cm - OPEN_MIN) % 5:
            raise Err("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 09:50)"
                      % raw["entry_time"])
        k = (cm - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise Err("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise Err("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                      % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise Err("the plan needs entry_time (the entry bar's close, e.g. 09:50) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise Err("the entry must close between 09:20 and 15:25 to leave bars to replay (got %s)"
                  % clock24(slot_close(k)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min or 15)
    except (TypeError, ValueError):
        raise Err("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= M.SESSION_MINUTES:
        raise Err("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    product = str(raw.get("product") or "MIS").upper()
    exchange = str(raw.get("exchange") or M.exchange_of(sym)).upper()
    risk = (entry - stop) if long else (stop - entry)
    flat_by = raw.get("flat_by")
    if product != "MIS" and itype == "EQ":
        flat_by = None
    return {
        "symbol": sym, "exchange": exchange, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "instrument": itype, "lot_size": (inst.get("lot_size") if isinstance(inst, dict) else None),
        "product": product, "entry": entry, "entry_time": clock24(slot_close(k)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m", "flat_by": flat_by, "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses. None for a level setup (a price level does not carry across days)."""
    setup = plan["setup"].lower()
    if setup not in RULES:
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": setup, "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"], "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    sr = rule["stop_rule"]
    if sr == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(M.round_tick((oh + ol) / 2, mode="down" if long else "up") - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(M.round_tick(ol if long else oh, mode="down" if long else "up") - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif sr == "vwap":
        rule["stop_rule"] = "vwap"
    elif sr in ("or_low", "or_high"):
        rule["stop_rule"] = far if sr == far else "pct"
    elif sr != "or_mid":
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- verdict

def _until12(mc):
    t = mc.get("until") or "15:30"
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc, net=None):
    notes = []
    pn = mc.get("p_neither", 0)
    lv = lean(mc)
    if pn > MANY_NEITHER and lv is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    if net and net.get("net_expectancy_r") is not None:
        ne = net["net_expectancy_r"]
        if avg is not None and avg > 0 >= ne:
            notes.append("After charges and slippage it turns negative: %+.3f R (%s) a trade. The costs eat the edge."
                         % (ne, M.inr(net["net_expectancy_inr"])))
        else:
            notes.append("After charges and slippage: %+.3f R (%s) a trade on average." % (
                ne, M.inr(net["net_expectancy_inr"])))
    return notes


# ---------------------------------------------------------------- the run

def time_machine(text, plan, now, replays=10000, seed=19, window=2, until=None, source="yahoo", cfg=None,
                 qty=None):
    if not 1 <= replays <= 1000000:
        raise Err("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise Err("--window must be between 0 and 10 bars")
    cfg = dict(M.DEFAULT_CONFIG, **(cfg or {}))
    sym = plan["symbol"]
    meta, bars, dropped = parse_bars(text, sym, plan["exchange"], now)
    if not bars:
        raise Err("no complete 5-minute bars for %s in the data" % sym)
    day = datetime.date.fromisoformat(plan["date"])
    if not M.trading_day(day):
        raise Err("%s is not an NSE trading day (%s)" % (plan["date"], M.holiday_name(day) or "weekend"))
    k = plan["entry_bar"]
    swing = plan["product"] == "CNC"
    if until is not None:
        until_min = parse_clock(until, "--until")
    elif plan.get("flat_by") and not swing:
        until_min = parse_clock(plan["flat_by"], "the plan's flat_by")
        until_min -= (until_min - OPEN_MIN) % 5   # an off-grid flat_by (15:12) exits at the bar that closes before it
    else:
        until_min = CLOSE_MIN
    if (until_min - OPEN_MIN) % 5 or until_min > CLOSE_MIN or until_min <= OPEN_MIN:
        raise Err("--until must be on the 5-minute grid, between 09:20 and 15:30")
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise Err("--until %s leaves nothing to replay after the %s entry" % (clock24(until_min), plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise Err("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                  % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    tick = M.tick_size(plan["entry"])
    if not eb["l"] - tick <= plan["entry"] <= eb["h"] + tick:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f" % (plan["entry"], eb["l"],
                                                                                       eb["h"]))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise Err("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                  % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    mc, outcomes, exits = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed,
                                   window, plan["side"])
    mc.update(until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], band_times=[clock24(slot_close(j)) for j in range(k, last + 1)])
    long = plan["side"] == "buy"

    # size, segment and costs
    lot = None
    if plan["instrument"] == "FUT" or (M.is_index(sym) and plan["instrument"] == "EQ"):
        lot = plan.get("lot_size") or M.lot_size(sym)
        if not lot:
            raise Err("no lot size known for %s: run market.py lots --refresh" % sym)
        segment = "futures"
        if plan["instrument"] == "EQ":
            warn.append("%s is an index: costed as 1 lot of %s futures" % (sym, sym))
    else:
        segment = M.segment_for(plan["product"], "EQ")
    q, basis = size_for(plan["entry"], plan["risk_per_share"], cfg, lot, qty)
    slip = float(cfg.get("slippage_pct") or 0.0)
    gap = None
    if swing:
        gaps = overnight_gaps(sessions)
        outcomes, exits, held, through = apply_gaps(outcomes, exits, gaps, plan["stop"], long, seed)
        need = plan["risk_per_share"] / plan["entry"]
        hist_through = sum(1 for g in gaps if ((1 - g) if long else (g - 1)) >= need)
        gap = {"gaps_in_data": len(gaps), "held_overnight": held, "gap_through_stop": through,
               "p_gap_through_stop": round(through / float(replays), 4),
               "p_gap_through_if_held": round(through / float(held), 4) if held else None,
               "historical_gaps_past_stop_distance": round(hist_through / float(len(gaps)), 4) if gaps else None,
               "worst_gap_pct": round(100 * ((min(gaps) - 1) if long else (max(gaps) - 1)), 2) if gaps else None}
        if not gaps:
            warn.append("no consecutive full sessions in the data to draw overnight gaps from")
    net = net_of_costs(outcomes, exits, plan, q, segment, cfg, slip)
    net["basis"] = basis

    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        trades, skipped = run_rule(sessions, rule, last, plan["side"], cfg, segment, plan["exchange"], lot,
                                   None, prev_close_map(sessions), sym)
        history = dict(stats(trades), sessions=len(sessions), triggered=len(trades), rule=rule, trades=trades,
                       skipped=skipped)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows orb, gap and vwap_reclaim; a %r setup's price level "
                           "does not carry from one day to the next" % plan["setup"]}
    last_bar = bars[-1]
    doc = {
        "created_by": "trader-timemachine",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30",
        "source": source, "symbol": sym,
        "data": {"symbol": meta.get("symbol") or M.yahoo_symbol(sym, plan["exchange"]), "interval": "5m",
                 "range": "60d", "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out, "mc": mc, "costs": net, "gap_risk": gap, "history": history,
        "replays": replays, "seed": seed, "target_first": mc["p_target"], "stop_first": mc["p_stop"],
        "neither": mc["p_neither"], "net_expectancy_r": net["net_expectancy_r"],
        "net_expectancy_inr": net["net_expectancy_inr"], "costs_per_share": net["costs_per_share"],
        "verdict": verdict(mc), "verdict_notes": verdict_notes(mc, net), "warnings": warn,
        "note": ("A simulation built from %s's own last %d full NSE sessions. It is not a prediction: results, "
                 "news, circuits and F&O expiry days can do things those sessions never did. %s Same seed, same "
                 "numbers." % (sym, len(sessions), SMALL_SAMPLE)),
    }
    return doc


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h, co = doc["plan"], doc["mc"], doc["history"], doc["costs"]
    long = p["side"] == "buy"
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    lines = [
        "%s %s (%s %s), %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%gR, risk %.2f a share)."
        % (p["symbol"], p["side"], p["exchange"], p["product"], wd, p["date"], p["entry"], p["entry_time"],
           p["entry_bar"], p["stop"], p["target"], p["target_r"], p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full NSE sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"],
                                             m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s, out at that bar's close" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]), "above" if long else
                                                                         "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R before costs: %+.3f (R = the %.2f risk: +%gR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], m["r_at_target"]),
        "  After costs (%s, %s, %s slippage a side): %+.3f R, %s a trade; charges about %s a trade (%s a share)."
        % (co["basis"], co["segment"], "%g%%" % co["slippage_pct"], co["net_expectancy_r"],
           M.inr(co["net_expectancy_inr"]), M.inr(co["avg_charges_inr"]), M.inr(co["costs_per_share"])),
    ]
    g = doc.get("gap_risk")
    if g:
        lines.append("  Overnight (CNC): %d of the replays were still open at 15:30 and took a gap drawn from the "
                     "data's %d real overnight gaps; %s gapped through the stop (%s of those held). In the data, %s "
                     "of overnight gaps were bigger than this stop's distance; the worst was %+.2f %%."
                     % (g["held_overnight"], g["gaps_in_data"], pc(g["p_gap_through_stop"]),
                        pc(g["p_gap_through_if_held"] or 0), pc(g["historical_gaps_past_stop_distance"] or 0),
                        g["worst_gap_pct"] or 0))
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high", "vwap": "one tick beyond VWAP"}.get(
            ru["stop_rule"], "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%s, %d-min range, stop at the %s, %gR target) on each of those %d "
                         "real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f before "
                         "costs, %+.3f after; net %s in total, %s of it charges."
                         % (ru["setup"], ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"], h["expectancy_r"],
                            M.inr(h["net_pnl"]), M.inr(h["total_charges"])))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d session(s): %s." % (
            len(doc["data"]["short_sessions_left_out"]),
            ", ".join("%s (%s)" % (x["date"], x["why"]) for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s IST entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- backtest

def backtest(symbols, rule, side, cfg, now, flat_by=None, offline=None, net=None, exchange="NSE", product="MIS"):
    cfg = dict(M.DEFAULT_CONFIG, **(cfg or {}))
    fb = parse_clock(flat_by or cfg.get("flat_by") or "15:10", "flat_by")
    fb -= (fb - OPEN_MIN) % 5
    if (fb - OPEN_MIN) % 5 or not OPEN_MIN < fb <= CLOSE_MIN:
        raise Err("flat_by must be on the 5-minute grid inside 09:20-15:30")
    last = (fb - OPEN_MIN) // 5 - 1
    window = (parse_clock(cfg.get("no_entries_before") or "09:30", "no_entries_before"),
              parse_clock(cfg.get("no_entries_after") or "14:45", "no_entries_after"))
    slip = float(cfg.get("slippage_pct") or 0.0)
    per, all_trades, problems = {}, [], {}
    for sym in symbols:
        try:
            text, source = fetch(sym, M.exchange_of(sym, exchange), offline, net)
            _, bars, _ = parse_bars(text, sym, M.exchange_of(sym, exchange), now)
            sessions, short = full_sessions(split_sessions(bars), now.date().isoformat())
            if len(sessions) < 5:
                raise Err("only %d full session(s) in the data" % len(sessions))
            lot = None
            seg = M.segment_for(product, "EQ")
            if M.is_index(sym):
                lot, seg = M.lot_size(sym), "futures"
            trades, skipped = run_rule(sessions, rule, last, side, cfg, seg, M.exchange_of(sym, exchange), lot,
                                       window, prev_close_map(sessions), sym)
            per[sym] = dict(stats(trades), sessions=len(sessions), first=sessions[0][0], last=sessions[-1][0],
                            skipped=skipped, segment=seg, source=source,
                            costed_as=("1+ lots of %s futures (lot %d)" % (sym, lot)) if lot else seg)
            all_trades += trades
        except Err as e:
            problems[sym] = str(e)
    if not per:
        raise Err("no symbol could be backtested: " + "; ".join("%s: %s" % kv for kv in problems.items()))
    all_trades.sort(key=lambda t: (t["date"], t["time"], t["symbol"]))
    return {"created_by": "trader-timemachine", "kind": "backtest",
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30", "symbols": list(symbols),
            "rule": dict(rule, side=side, flat_by=clock24(fb), entry_window=[clock24(window[0]), clock24(window[1])],
                         product=product),
            "sizing": "fixed risk: max_loss_per_trade_inr %s / the trade's risk, capped by max_order_inr; index "
                      "symbols in whole futures lots" % M.inr(cfg.get("max_loss_per_trade_inr"), paise=False),
            "slippage_pct": slip, "broker": cfg.get("broker"), "per_symbol": per, "total": stats(all_trades),
            "partial": bool(problems), "errors": problems, "trades": all_trades,
            "note": SMALL_SAMPLE + " Charges are today's statutory rates and your broker's tariff; fills are bar "
                    "closes plus slippage, not real fills."}


def backtest_report(doc):
    ru = doc["rule"]
    stop = ru["stop_rule"] if ru["stop_rule"] != "pct" else "%g%% stop" % ru["stop_pct"]
    lines = ["Backtest: %s %s, %d-min range, stop %s, %gR target, entries %s-%s, out by %s, %s, slippage %g%% a side."
             % (ru["setup"], "long" if ru["side"] == "buy" else "short", ru["or_minutes"], stop, ru["target_r"],
                ru["entry_window"][0], ru["entry_window"][1], ru["flat_by"], ru["product"], doc["slippage_pct"]),
             "Sizing: %s." % doc["sizing"], ""]
    lines.append("  %-11s %6s %6s %8s %12s %8s %14s %14s %12s %9s" % (
        "SYMBOL", "DAYS", "TRADES", "WIN %", "AVG NET", "EXP R", "NET TOTAL", "CHARGES", "MAX DD", "CHG/GP"))

    def row(name, s, days):
        if not s.get("trades"):
            return "  %-11s %6s %6d   no trades" % (name, days, 0)
        return "  %-11s %6s %6d %7.1f%% %12s %+8.3f %14s %14s %12s %9s" % (
            name, days, s["trades"], 100 * s["win_rate"], M.inr(s["avg_net_pnl"]), s["expectancy_r"],
            M.inr(s["net_pnl"]), M.inr(s["total_charges"]), M.inr(s["max_drawdown"]),
            ("%.0f%%" % s["charges_pct_of_gross_profit"]) if s.get("charges_pct_of_gross_profit") is not None
            else "-")
    for sym, s in doc["per_symbol"].items():
        lines.append(row(sym, s, s["sessions"]))
        if s.get("skipped"):
            lines.append("  %-11s skipped: %s" % ("", ", ".join("%s %d" % kv for kv in sorted(s["skipped"].items()))))
    if len(doc["per_symbol"]) > 1:
        lines.append(row("ALL", doc["total"], ""))
    for sym, e in doc["errors"].items():
        lines.append("  %-11s error: %s" % (sym, e))
    t = doc["total"]
    lines.append("")
    if t.get("trades"):
        lines.append("Win % counts trades that made money after charges. EXP R is the average net result in units of "
                     "each trade's own risk. CHG/GP is charges as a share of the winning trades' gross profit.")
        if t["gross_pnl"] > 0 >= t["net_pnl"]:
            lines.append("Before charges it made %s; after charges (%s) and slippage (%s) it lost %s. The costs ate "
                         "the edge." % (M.inr(t["gross_pnl"]), M.inr(t["total_charges"]), M.inr(t["total_slippage"]),
                                        M.inr(-t["net_pnl"])))
        else:
            lines.append("Gross %s, charges %s, slippage %s, net %s over %d trades." % (
                M.inr(t["gross_pnl"]), M.inr(t["total_charges"]), M.inr(t["total_slippage"]), M.inr(t["net_pnl"]),
                t["trades"]))
    if doc["partial"]:
        lines.append("PARTIAL: %d of %d symbol(s) could not be tested (see the errors above)."
                     % (len(doc["errors"]), len(doc["symbols"])))
    lines.append(doc["note"])
    return lines


def parse_rule_args(a):
    setup = a.rule.replace("-", "_")
    side = a.side
    if setup in ("orb_short", "orb_sell"):
        setup, side = "orb", "sell"
    if setup not in RULES:
        raise Err("--rule is orb, orb-short, gap or vwap_reclaim")
    long = side == "buy"
    st = a.stop_rule.lower().replace("-", "_")
    rule = {"setup": setup, "or_minutes": a.or_minutes, "target_r": None}
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", a.target.strip())
    if not m or float(m.group(1)) <= 0:
        raise Err("--target is an R multiple like 2R")
    rule["target_r"] = float(m.group(1))
    if a.or_minutes < 5 or a.or_minutes % 5:
        raise Err("--or-minutes must be a multiple of 5")
    if st in ("or_mid",):
        rule["stop_rule"] = "or_mid"
    elif st in ("or_low", "or_high", "far"):
        rule["stop_rule"] = "or_low" if long else "or_high"
    elif st == "vwap":
        rule["stop_rule"] = "vwap"
    elif st.startswith("pct:"):
        rule["stop_rule"], rule["stop_pct"] = "pct", float(st[4:])
    elif st.startswith("atr:"):
        rule["stop_rule"], rule["stop_k"] = "atr", float(st[4:])
    else:
        raise Err("--stop-rule is or-mid, or-low (or-high for shorts), vwap, pct:X or atr:K")
    if rule.get("stop_pct") is not None and not 0 < rule["stop_pct"] < 100:
        raise Err("--stop-rule pct:X needs X between 0 and 100")
    return rule, side


# ---------------------------------------------------------------- commands

def compute(a):
    now = M.parse_now(a.now)
    cfg = M.load_config(a.config)
    plan = load_plan(a, now)
    text, source = fetch(plan["symbol"], plan["exchange"], a.offline)
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, cfg, a.qty)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        M.write_json(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your max loss per trade. Nothing was placed. " + M.NOT_ADVICE)
    return 0


def cmd_chart(a):
    if a.from_file:
        doc = M.read_json(a.from_file, "time machine result (run timemachine.py run --write first)")
        try:
            doc["mc"]["bands"]
        except (KeyError, TypeError):
            raise Err("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def cmd_backtest(a):
    now = M.parse_now(a.now)
    cfg = M.load_config(a.config)
    rule, side = parse_rule_args(a)
    syms = []
    for s in a.symbols:
        s = s.strip().upper()
        if s.endswith((".NS", ".BO")):
            s = s[:-3]
        if not M.valid_symbol(s):
            raise Err("%r is not an NSE/BSE symbol" % s)
        syms.append(s)
    doc = backtest(syms, rule, side, cfg, now, a.flat_by, a.offline, None, a.exchange or cfg.get("exchange", "NSE"),
                   (a.product or "MIS").upper())
    if a.write:
        M.write_json(a.write, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(backtest_report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print(M.NOT_ADVICE)
    return 0 if not doc["partial"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine (trader-timemachine, India): replay a planned trade, "
                                 "with Indian charges.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol")
        p.add_argument("--exchange", choices=["NSE", "BSE"])
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help="the entry bar's close, e.g. 09:50")
        p.add_argument("--setup", default="orb")
        p.add_argument("--product", choices=["MIS", "CNC"])
        p.add_argument("--flat-by", help="the plan's flat_by (flags only)")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct", "vwap"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this IST time (default: the plan's flat_by, else 15:30)")
        p.add_argument("--qty", type=int, help="size for the costs (default: max_loss_per_trade_inr / risk)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-28 10:15"')
        p.add_argument("--config", default=M.CONFIG_FILE)

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    p = sub.add_parser("backtest", help="a simple rule over every session in the last ~60 days, net of charges")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--rule", default="orb", help="orb, orb-short, gap or vwap_reclaim")
    p.add_argument("--side", default="buy", choices=["buy", "sell"])
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--stop-rule", default="or-mid", help="or-mid, or-low (or-high for shorts), vwap, pct:X, atr:K")
    p.add_argument("--target", default="2R")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--product", choices=["MIS", "CNC"], help="charges segment (default MIS intraday)")
    p.add_argument("--exchange", choices=["NSE", "BSE"])
    p.add_argument("--offline", metavar="PATH", help="a saved chart file (one symbol) or a folder of them")
    p.add_argument("--now", help='pretend it is this IST time')
    p.add_argument("--config", default=M.CONFIG_FILE)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/backtest.json", metavar="PATH",
                   help="save the result (default trader/backtest.json)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart, "backtest": cmd_backtest}[a.cmd](a)
    except (Err, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
