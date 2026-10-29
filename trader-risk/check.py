#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (India edition). Standard library only, no network.

Reads the proposed orders (trader/orders.json, broker-neutral, see
docs/CONTRACTS.md) and checks every one against your rules
(trader/config.json), your account (trader/portfolio.json, or the paper
ledger trader/paper/ledger.json in paper mode), prices (trader/prices.json)
and what was placed and lost today (trader/state/broker.json and the paper
ledger). Each order gets PASS or BLOCK with the reasons. Orders are checked in
file order as if each PASS had filled, so a batch that is fine order by order
but breaks a limit together is caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-28 09:50"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

ENTRY or EXIT. An exit only reduces a position you hold (same tradingsymbol
and product). Getting out must always be possible: an exit is checked for
format, tick and lot size only; every other rule becomes a warning.

What blocks an entry:
  format / tick   bad side, qty, product, order type, missing prices; a price
                  that is not a multiple of the exchange tick
  calendar        not an NSE/BSE trading day (weekend or exchange holiday),
                  or outside 09:15-15:30 IST unless the order is an AMO
  window          before no_entries_before / after no_entries_after; F&O on
                  its expiry day after expiry_day_last_entry; MIS at or after
                  flat_by or the broker's auto square-off time
  product         MIS only intraday; CNC needs allow_swing; short selling
                  only MIS with allow_short; never a CNC short; F&O is MIS or NRML
  series          trade-for-trade series (block_series, e.g. BE/BZ) cannot be MIS
  lists           blocked, asm_gsm; F&O ban list (no new F&O positions)
  circuit         within avoid_circuit_within_pct of the circuit you would run into
  F&O             option selling unless allow_option_selling; futures unless
                  allow_futures; qty not a multiple of the lot; more than
                  max_lots_per_trade; premium over max_premium_per_trade_inr;
                  an SL-M stop on F&O (use SL with a limit)
  stop / loss     no stop; loss at the stop INCLUDING CHARGES over
                  max_loss_per_trade_inr
  reward:risk     net of charges under min_reward_risk
  daily loss      today's loss so far (the larger of realised losses and the
                  max losses of today's entries) + this one over max_daily_loss_inr
  counts          max_trades_per_day entries, max_orders_per_day orders, duplicates
  size / money    over max_position_pct of capital, over max_order_inr, under
                  min_order_inr, margin over the cash left after the buffer
  stale price     a MARKET / SL-M entry needs a price under 15 minutes old

Exit code 0 when every order passes, 1 when any is blocked.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

FRESH_MINUTES = 15
EPS = 1e-6
PAISA = 0.005
EXTRA_DEFAULTS = {"futures_margin_pct": 15}
PRODUCTS = ("CNC", "MIS", "NRML")
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
VALIDITY = ("DAY", "IOC")
FP_FIELDS = ("exchange", "tradingsymbol", "side", "qty", "product", "order_type", "price", "trigger_price")


class RiskError(M.MarketError):
    pass


# ---------------------------------------------------------------- the fingerprint (same in trader-broker/ticket.py)

def fingerprint(order):
    """sha1 of the order's identity fields (CONTRACTS.md). trader-broker/ticket.py has the
    identical function; a test keeps them equal."""
    core = {}
    for k in FP_FIELDS:
        v = order.get(k)
        if k == "tradingsymbol" and not v:
            v = order.get("symbol")
        if v in (None, ""):
            v = None
        elif k in ("price", "trigger_price"):
            v = round(float(v), 2)
        elif k == "qty":
            v = int(round(float(v)))
        else:
            v = str(v).strip().upper()
        core[k] = v
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- helpers

def _f(x):
    try:
        if isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _px(x):
    return "%.2f" % float(x)


def _num(x):
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else ("%.2f" % x).rstrip("0").rstrip(".")


def parse_time(text):
    if not text:
        return None
    t = str(text).strip()
    try:
        if t.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", t):
            dt = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
            return dt.astimezone(M.IST).replace(tzinfo=None)
        return M.parse_now(t)
    except (ValueError, M.MarketError):
        return None


def load_prices(pdoc):
    """prices.json -> {SYM: {"price", "time" (IST or None), "prev_close"}}."""
    out = {}
    if not isinstance(pdoc, dict):
        return out
    at = parse_time(pdoc.get("at") or pdoc.get("as_of"))
    for k, q in (pdoc.get("quotes") or {}).items():
        if isinstance(q, dict) and _f(q.get("price")):
            out[str(k).upper()] = {"price": _f(q["price"]), "time": parse_time(q.get("time")) or at,
                                   "prev_close": _f(q.get("prev_close"))}
    for k, p in (pdoc.get("prices") or {}).items():
        if _f(p) and str(k).upper() not in out:
            det = (pdoc.get("detail") or {}).get(k) or {}
            out[str(k).upper()] = {"price": _f(p), "time": parse_time(det.get("time")) or at, "prev_close": None}
    return out


def is_tick(price, tick):
    q = float(price) / tick
    return abs(q - round(q)) < 1e-6


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def hhmm_or_none(value, key):
    if value in (None, "", False):
        return None
    try:
        return M.parse_hhmm(value, key)
    except M.MarketError:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))


def instrument_of(o):
    inst = o.get("instrument") if isinstance(o.get("instrument"), dict) else {}
    itype = str(inst.get("type") or "EQ").upper()
    under = str(inst.get("underlying") or o.get("symbol") or o.get("tradingsymbol") or "").upper()
    return inst, itype, under


# ---------------------------------------------------------------- account and today

def account_from(cfg, portfolio, ledger):
    """-> ({cash, positions {TSYM|PRODUCT: signed qty}, source}, warnings)."""
    warns = []
    capital = _f(cfg.get("capital_inr")) or 0.0
    if cfg.get("paper", True) and isinstance(ledger, dict):
        pos = {}
        for k, p in (ledger.get("positions") or {}).items():
            q = _f((p or {}).get("qty"))
            if q:
                pos[k.upper()] = q
        cash = _f(ledger.get("cash_inr"))
        return {"cash": cash if cash is not None else capital, "positions": pos,
                "source": "the paper ledger"}, warns
    if isinstance(portfolio, dict):
        pos = {}
        raw = portfolio.get("positions") or {}
        items = raw.items() if isinstance(raw, dict) else [
            ("%s|%s" % (p.get("tradingsymbol") or p.get("symbol"), p.get("product") or "CNC"), p) for p in raw]
        for k, p in items:
            q = _f((p or {}).get("qty"))
            if q:
                pos[str(k).upper()] = q
        cash = _f(portfolio.get("cash_inr"))
        if cash is None:
            raise RiskError("portfolio.json needs a \"cash_inr\" number")
        return {"cash": cash, "positions": pos, "source": "trader/portfolio.json"}, warns
    if capital <= 0:
        raise RiskError("no portfolio, no paper ledger and no capital_inr in the config: nothing to check against")
    return {"cash": capital, "positions": {}, "source": "a fresh account funded with capital_inr"}, warns


def today_summary(state, ledger, today, paper=True):
    """What happened today: placed orders, entries with their max loss, realised P&L.
    The paper ledger counts only in paper mode; live fills only in live mode.
    "used" is the daily-loss budget already spent: per symbol, the larger of the
    planned max losses of today's entries and the realised loss (a stop that filled
    past its trigger counts at the real loss), plus losses on symbols with no entry today."""
    placed = [r for r in (state or {}).get("orders", []) if isinstance(r, dict) and r.get("status") == "placed"
              and rec_day(r) == today]
    entries = [r for r in placed if (r.get("kind") or "entry") == "entry"]
    realised = 0.0
    loss_by = {}

    def add(sym, net):
        loss_by[sym] = loss_by.get(sym, 0.0) + net
    if not paper:
        for r in (state or {}).get("orders", []):
            if isinstance(r, dict) and r.get("status") == "filled" and rec_day(r) == today \
                    and _f(r.get("realised_pnl")) is not None and not r.get("paper"):
                realised += _f(r["realised_pnl"])
                add(str(r.get("tradingsymbol") or r.get("symbol") or "?").upper(), _f(r["realised_pnl"]))
    elif isinstance(ledger, dict):
        for r in ledger.get("realised") or []:
            if isinstance(r, dict) and str(r.get("date")) == today and _f(r.get("net")) is not None:
                realised += _f(r["net"])
                add(str(r.get("tradingsymbol") or "?").upper(), _f(r["net"]))
    at_risk = 0.0
    missing = 0
    planned_by = {}
    for r in entries:
        ml = _f(r.get("max_loss"))
        if ml is None:
            missing += 1
        else:
            at_risk += ml
            sym = str(r.get("tradingsymbol") or r.get("symbol") or "?").upper()
            planned_by[sym] = planned_by.get(sym, 0.0) + ml
    used = 0.0
    for sym in set(planned_by) | set(loss_by):
        used += max(planned_by.get(sym, 0.0), max(0.0, -loss_by.get(sym, 0.0)))
    return {"placed": placed, "entries": entries, "realised": round(realised, 2),
            "realised_loss": round(max(0.0, -realised), 2), "at_risk": round(at_risk, 2), "missing": missing,
            "used": round(used, 2)}


# ---------------------------------------------------------------- the check (pure)

def check(orders, cfg, portfolio=None, prices=None, state=None, ledger=None, now=None):
    """Returns {"date", "time", "checks": [...], "pass", "block", "warnings", ...}.
    now: naive IST datetime (default the real clock). prices: load_prices() output."""
    c = dict(M.DEFAULT_CONFIG)
    c.update(EXTRA_DEFAULTS)
    c.update(cfg or {})
    now = now or M.now_ist()
    today = now.date().isoformat()
    prices = prices or {}
    acct, top_warn = account_from(c, portfolio, ledger)
    capital = _f(c.get("capital_inr")) or acct["cash"]
    broker = str(c.get("broker") or "zerodha").lower()
    _, prof = M.broker_profile(c)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    asm = {str(b).upper() for b in c.get("asm_gsm") or []}
    ban = {str(b).upper() for b in c.get("fno_ban") or []}
    t2t = {str(b).upper() for b in c.get("block_series") or []}
    day = today_summary(state, ledger, today, bool(c.get("paper", True)))
    if day["missing"]:
        top_warn.append("%d entr%s placed today ha%s no max loss in the log; not counted in the daily loss"
                        % (day["missing"], "y" if day["missing"] == 1 else "ies", "s" if day["missing"] == 1 else "ve"))
    placed_keys = {(str(r.get("tradingsymbol") or r.get("symbol") or "").upper(), str(r.get("side") or "").upper())
                   for r in day["entries"]}
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    if not M.calendar_known(now.year):
        top_warn.append("no %d holiday list loaded: only weekends are known to be closed" % now.year)

    st = M.market_status(now)
    t = now.time().replace(second=0, microsecond=0)
    before = hhmm_or_none(c.get("no_entries_before"), "no_entries_before")
    after = hhmm_or_none(c.get("no_entries_after"), "no_entries_after")
    flat_by = hhmm_or_none(c.get("flat_by"), "flat_by")
    expiry_last = hhmm_or_none(c.get("expiry_day_last_entry"), "expiry_day_last_entry")
    sq = prof.get("auto_square_off") or {}
    trading = M.trading_day(now.date())
    why_closed = ("weekend" if now.weekday() >= 5 else ("exchange holiday: %s" % M.holiday_name(now.date())))

    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    cash = acct["cash"]
    buffer_inr = capital * float(_f(c.get("cash_buffer_pct")) or 0) / 100.0

    for i, o in enumerate(orders, 1):
        reasons, warns, info, later = [], [], [], []
        inst, itype, under = instrument_of(o)
        fno = itype in ("CE", "PE", "FUT")
        tsym = str(o.get("tradingsymbol") or o.get("symbol") or "").strip().upper()
        sym = str(o.get("symbol") or under or tsym).upper()
        side = str(o.get("side") or "").strip().upper()
        product = str(o.get("product") or "").strip().upper()
        otype = str(o.get("order_type") or "").strip().upper()
        validity = str(o.get("validity") or "DAY").strip().upper()
        exchange = str(o.get("exchange") or "NSE").strip().upper()
        variety = str(o.get("variety") or "regular").strip().lower()
        amo = variety == "amo"
        qty = _f(o.get("qty"))
        price, trig = _f(o.get("price")), _f(o.get("trigger_price"))
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        exits = o.get("exits") if isinstance(o.get("exits"), dict) else {}
        sl = exits.get("stop_loss") if isinstance(exits.get("stop_loss"), dict) else {}
        tg = exits.get("target") if isinstance(exits.get("target"), dict) else {}
        tseg = "options" if itype in ("CE", "PE") else ("futures" if itype == "FUT" else "equity")
        long = side == "BUY"

        # ---- format (every order)
        if not tsym or not M.valid_symbol(tsym):
            reasons.append("format: no valid tradingsymbol (%r)" % (o.get("tradingsymbol") or o.get("symbol")))
        if exchange not in ("NSE", "BSE"):
            reasons.append("format: exchange must be NSE or BSE, got %r" % o.get("exchange"))
        if side not in ("BUY", "SELL"):
            reasons.append("format: side must be BUY or SELL, got %r" % o.get("side"))
        if qty is None or qty <= 0 or abs(qty - round(qty)) > EPS:
            reasons.append("format: qty must be a whole number above 0, got %r" % o.get("qty"))
            qty = None
        else:
            qty = int(round(qty))
        if itype not in ("EQ", "CE", "PE", "FUT"):
            reasons.append("format: unknown instrument type %r" % itype)
        if product not in PRODUCTS:
            reasons.append("format: product must be CNC, MIS or NRML, got %r" % o.get("product"))
        elif itype == "EQ" and product == "NRML":
            reasons.append("product: NRML is for F&O; equity is MIS (intraday) or CNC (delivery)")
        elif fno and product == "CNC":
            reasons.append("product: CNC is for delivery equity; F&O is MIS or NRML")
        if otype not in ORDER_TYPES:
            reasons.append("format: order_type must be MARKET, LIMIT, SL or SL-M, got %r" % o.get("order_type"))
        if validity not in VALIDITY:
            reasons.append("format: validity must be DAY or IOC, got %r" % o.get("validity"))
        if otype in ("LIMIT", "SL") and not price:
            reasons.append("format: a %s order needs a price" % otype)
        if otype in ("SL", "SL-M") and not trig:
            reasons.append("format: a %s order needs a trigger_price" % otype)
        if otype == "MARKET" and price:
            warns.append("a MARKET order ignores its price %s" % _px(price))
        # ticks: the exchange rejects a price off the grid
        for name, v in (("price", price if otype in ("LIMIT", "SL") else None), ("trigger_price", trig),
                        ("stop trigger", _f(sl.get("trigger_price"))), ("stop limit", _f(sl.get("price"))),
                        ("target", _f(tg.get("price")))):
            if v is not None and v > 0:
                tk = M.tick_size(v, tseg)
                if not is_tick(v, tk):
                    reasons.append("tick: %s %s is not a multiple of the %s tick %s (the exchange rejects it; "
                                   "nearest %s)" % (name, _px(v), tseg, _num(tk), _px(M.round_tick(v, tk))))
        # lots
        lot = 1
        if fno:
            lot = int(_f(inst.get("lot_size")) or M.lot_size(under) or 0)
            if lot <= 0:
                reasons.append("lot: no lot size known for %s (instrument.lot_size, or market.py lots --refresh)"
                               % under)
                lot = 1
            elif qty and qty % lot:
                reasons.append("lot: qty %d is not a multiple of the %s lot size %d (try %d or %d)"
                               % (qty, under, lot, (qty // lot) * lot, (qty // lot + 1) * lot))
            exp = inst.get("expiry")
            if not exp:
                reasons.append("format: an F&O order needs instrument.expiry")
            elif str(exp) < today:
                reasons.append("format: the %s contract expired on %s" % (tsym, exp))

        # ---- entry or exit
        key = "%s|%s" % (tsym, product)
        held = acct["positions"].get(key, 0.0)
        exiting = bool(qty) and ((side == "SELL" and held > 0 and qty <= held + EPS) or
                                 (side == "BUY" and held < 0 and qty <= -held + EPS))
        kind = "exit" if exiting else "entry"

        # ---- reference entry price and freshness
        q_live = prices.get(tsym) or (prices.get(sym) if not fno else None)
        live = q_live["price"] if q_live else None
        live_fresh = bool(q_live and q_live["time"] is not None and
                          -60 <= (now - q_live["time"]).total_seconds() <= FRESH_MINUTES * 60)
        plan_entry = _f(plan.get("entry"))
        ref, ref_from = None, None
        if otype in ("LIMIT", "SL"):
            ref, ref_from = price, "the limit price"
        else:
            cands = [(plan_entry, "the plan's entry")] if plan_entry else []
            if live and live_fresh:
                cands.append((live, "a fresh price"))
            if cands:
                ref, ref_from = (max if long else min)(cands, key=lambda x: x[0])
            if otype == "SL-M" and trig:
                ref = max(ref or 0, trig) if long else min(ref or trig, trig)
                ref_from = ref_from or "the trigger"
        if kind == "entry" and otype in ("MARKET", "SL-M") and not live_fresh:
            later.append("stale price: a %s entry needs a price under %d minutes old in trader/prices.json "
                         "(%s); refresh it (trader-alerts: alerts.py quote %s --write) or use a LIMIT order"
                         % (otype, FRESH_MINUTES, "none there" if not q_live else (
                             "it is from %s" % (q_live["time"].strftime("%Y-%m-%d %H:%M") if q_live["time"] else "an unknown time")),
                            tsym))
        elif q_live and not live_fresh and kind == "entry":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used" % (tsym, FRESH_MINUTES))
        if ref is None and kind == "entry":
            later.append("price: no price to measure this order by (no limit price, plan entry or fresh quote)")

        notional = round(ref * qty, 2) if (ref and qty) else None
        segment = M.segment_for(product if product in PRODUCTS else "MIS", itype)

        # ---- calendar and session (entries block, exits warn)
        if amo:
            nxt = now.date() if (trading and t < M.OPEN) else M.next_trading_day(now.date())
            if st["open"]:
                later.append("AMO: the market is open; brokers reject AMOs during market hours, place a regular order")
            elif product == "MIS" and kind == "entry":
                later.append("AMO: an MIS AMO fills at the next open (%s 09:15), before your entry window and into "
                             "any gap; use CNC/NRML or place it in the session" % nxt.isoformat())
            else:
                warns.append("AMO: it goes to the exchange at the next session's open (%s); a gap can fill it far "
                             "from the plan" % nxt.isoformat())
            if fno and kind == "entry" and str(inst.get("expiry")) == nxt.isoformat():
                later.append("expiry day: %s expires on %s, the session this AMO would fill in; no AMO entries into "
                             "an expiry day" % (tsym, nxt.isoformat()))
        elif not trading:
            later.append("calendar: %s is not a trading day (%s)" % (today, why_closed))
        elif not st["open"]:
            later.append("session: the market is not open (%s %s IST, %s); orders go in 09:15-15:30, or mark it an AMO"
                         % (today, M.hm(t), st["label"]))

        # ---- window (entries)
        if trading and not amo:
            if before is not None and t < before:
                later.append("window: no new entries before %s IST (it is %s)" % (M.hm(before), M.hm(t)))
            if after is not None and t >= after:
                later.append("window: no new entries at or after %s IST (it is %s)" % (M.hm(after), M.hm(t)))
            if product == "MIS":
                cut_words = []
                bt = sq.get("fno" if fno else "equity")
                if bt:
                    cut_words.append((M.parse_hhmm(bt), "%s's auto square-off %s" % (broker, bt)))
                if flat_by is not None:
                    cut_words.append((flat_by, "your flat_by %s" % M.hm(flat_by)))
                if cut_words:
                    cut, words = min(cut_words, key=lambda x: x[0])
                    if t >= cut:
                        later.append("window: an MIS entry at %s IST is at or after %s; it would be squared off "
                                     "straight away" % (M.hm(t), words))
            if fno and expiry_last is not None and str(inst.get("expiry")) == today and t >= expiry_last:
                later.append("expiry day: %s expires today; no new F&O entries after %s IST (it is %s)"
                             % (tsym, M.hm(expiry_last), M.hm(t)))

        # ---- product and shorting rules
        if itype == "EQ" and side == "SELL" and kind == "entry":
            if product == "CNC":
                reasons.append("short: no CNC short. Selling shares you do not hold in delivery fails settlement "
                               "and goes to the exchange auction with a penalty. Shorts are MIS only")
            elif not c.get("allow_short"):
                reasons.append("short: selling %s you do not hold is a short sale; short selling is off "
                               "(\"allow_short\": false). If you bought it, record the fill first" % tsym)
        if itype == "EQ" and product == "CNC" and side == "BUY" and kind == "entry" and not c.get("allow_swing", True):
            later.append("product: CNC holds overnight; swing trades are off (\"allow_swing\": false). Use MIS")
        series = str(o.get("series") or inst.get("series") or "").upper()
        if series and series in t2t:
            if product == "MIS" or (itype == "EQ" and side == "SELL" and kind == "entry"):
                reasons.append("series: %s is in the %s series (trade-for-trade): delivery only, no MIS, no intraday "
                               "square-off, no short" % (tsym, series))
            else:
                warns.append("series %s is trade-for-trade: every trade settles by delivery" % series)
        if itype in ("CE", "PE") and side == "SELL" and kind == "entry" and not c.get("allow_option_selling"):
            reasons.append("options: selling an option is off (\"allow_option_selling\": false). A sold option "
                           "can lose many times its premium")
        if itype in ("CE", "PE") and side == "BUY" and kind == "entry" and not c.get("allow_option_buying", True):
            reasons.append("options: option buying is off (\"allow_option_buying\": false)")
        if itype == "FUT" and kind == "entry" and not c.get("allow_futures"):
            reasons.append("futures: futures are off (\"allow_futures\": false)")
        if fno and kind == "entry" and qty and lot > 1:
            ml = int(_f(c.get("max_lots_per_trade")) or 0)
            if ml and qty // lot > ml:
                later.append("lots: %d lots is over your max %d lots per trade" % (qty // lot, ml))
        if itype in ("CE", "PE") and side == "BUY" and kind == "entry" and notional is not None:
            mp = _f(c.get("max_premium_per_trade_inr"))
            if mp and notional > mp + PAISA:
                later.append("premium: %s of premium is over your %s max premium per trade"
                             % (M.inr(notional), M.inr(mp, False)))
        if itype in ("CE", "PE") and not M.is_index(under):
            warns.append("stock options are physically settled: an in-the-money %s held into expiry means taking "
                         "or giving delivery of %d %s shares. Close it before expiry day" % (tsym, lot, under))
        if fno and sl and str(sl.get("order_type") or "").upper() == "SL-M":
            later.append("stop: SL-M stops are restricted on F&O at most brokers; use SL with a limit price buffer")

        # ---- lists
        names = {tsym, sym, under}
        if names & blocked:
            later.append("blocked: %s is on your blocked list" % (sorted(names & blocked)[0]))
        if names & asm:
            later.append("asm/gsm: %s is under ASM/GSM surveillance (your asm_gsm list): higher margins, "
                         "trade-for-trade risk" % sorted(names & asm)[0])
        if fno and under in ban:
            later.append("F&O ban: %s is in the F&O ban period (MWPL over 95%%): no new F&O positions" % under)

        # ---- circuit
        circ = o.get("circuit") or plan.get("circuit") or {}
        limits = None
        if isinstance(circ, dict) and _f(circ.get("upper")) and _f(circ.get("lower")):
            limits = {"upper": _f(circ["upper"]), "lower": _f(circ["lower"]), "band_pct": circ.get("band_pct")}
        elif isinstance(circ, dict) and _f(circ.get("band_pct")) and q_live and q_live.get("prev_close"):
            limits = M.band_limits(q_live["prev_close"], _f(circ["band_pct"]))
        if limits and ref and itype == "EQ":
            if ref > limits["upper"] + PAISA or ref < limits["lower"] - PAISA:
                reasons.append("circuit: %s is outside today's band %s-%s; the exchange rejects it"
                               % (_px(ref), _px(limits["lower"]), _px(limits["upper"])))
            else:
                near = M.near_circuit(ref, limits, float(_f(c.get("avoid_circuit_within_pct")) or 2.0))
                if (near == "upper" and long) or (near == "lower" and not long):
                    later.append("circuit: %s is within %s%% of the %s circuit %s; at the circuit there are no "
                                 "sellers (buyers) and you cannot get out" % (
                                     _px(ref), _num(c.get("avoid_circuit_within_pct") or 2), near,
                                     _px(limits[near])))

        # ---- stop, loss incl. charges, reward:risk
        stop = _f(sl.get("trigger_price")) or _f(plan.get("stop"))
        target = _f(tg.get("price")) or _f(plan.get("target"))
        max_loss = loss_c = rr = None
        rt_stop = rt_target = None
        if kind == "entry" and side in ("BUY", "SELL"):
            if not sl and stop:
                warns.append("the stop %s is in the plan only, not an exit order: set it at the broker right after "
                             "the entry fills" % _px(stop))
            if sl and str(sl.get("order_type") or "").upper() == "SL" and _f(sl.get("price")) and stop:
                lp = _f(sl["price"])
                if (long and lp > stop + PAISA) or (not long and lp < stop - PAISA):
                    reasons.append("stop: the SL limit %s is on the wrong side of its trigger %s" % (_px(lp), _px(stop)))
                else:
                    warns.append("SL stop-limit: if the price gaps past %s the stop does not fill and the loss grows"
                                 % _px(lp))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true)")
                else:
                    warns.append("no stop: the loss is not capped, so it is not counted in the daily loss")
            elif ref and qty:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_px(stop), "below" if long else "above", _px(ref), side))
                else:
                    # an SL (stop-limit) can fill anywhere down to its limit: count the loss there
                    worst = stop
                    if sl and str(sl.get("order_type") or "").upper() == "SL" and _f(sl.get("price")):
                        lp = _f(sl["price"])
                        if (long and lp < stop) or (not long and lp > stop):
                            worst = lp
                    risk = round(abs(ref - worst), 2)
                    if long:
                        rt_stop = M.round_trip(segment, ref, worst, qty, exchange, broker, c)
                    else:
                        rt_stop = M.round_trip(segment, worst, ref, qty, exchange, broker, c)
                    max_loss = round(risk * qty, 2)
                    loss_c = round(max_loss + rt_stop["total"], 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and loss_c > cap + PAISA:
                        later.append("loss: %d x %s = %s at the stop plus %s charges = %s, over your %s max loss "
                                     "per trade" % (qty, _px(risk), M.inr(max_loss), M.inr(rt_stop["total"]),
                                                    M.inr(loss_c), M.inr(cap, False)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_px(target), "above" if long else "below", _px(ref)))
                        else:
                            if long:
                                rt_target = M.round_trip(segment, ref, target, qty, exchange, broker, c)
                            else:
                                rt_target = M.round_trip(segment, target, ref, qty, exchange, broker, c)
                            net_reward = abs(target - ref) * qty - rt_target["total"]
                            rr = round(net_reward / loss_c, 2) if loss_c > 0 else None
                            if min_rr and (rr is None or rr < min_rr - 1e-9):
                                later.append("reward:risk: %s net of charges (target %s, entry %s, stop %s) is under "
                                             "your minimum %s" % ("n/a" if rr is None else "%.2f" % rr, _px(target),
                                                                  _px(ref), _px(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)" % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            used = day["used"] + batch_loss
            if daily and loss_c is not None and used + loss_c > daily + PAISA:
                later.append("daily loss: %s already used today (realised loss %s, max loss of today's entries %s) "
                             "plus %s for this one = %s, over your %s daily loss limit"
                             % (M.inr(used), M.inr(day["realised_loss"]), M.inr(day["at_risk"] + batch_loss),
                                M.inr(loss_c), M.inr(used + loss_c), M.inr(daily, False)))
            if daily and day["realised_loss"] >= daily - PAISA:
                later.append("daily loss: you have already lost %s today, your %s limit: done for the day"
                             % (M.inr(day["realised_loss"]), M.inr(daily, False)))
            cap_t = int(_f(c.get("max_trades_per_day")) or 0)
            if cap_t and len(day["entries"]) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch; your limit is "
                             "%d a day" % (len(day["entries"]), batch_entries, cap_t))
            if ref and stop and loss_c is not None:
                info.append("entry %s (%s), stop %s, target %s: %d x %s = %s at the stop + charges %s = %s"
                            % (_px(ref), ref_from, _px(stop), _px(target) if target else "none", qty,
                               _px(abs(ref - stop)), M.inr(max_loss), M.inr(rt_stop["total"]), M.inr(loss_c)))
                info.append("limit %s a trade; net reward:risk %s (min %s)"
                            % (M.inr(c.get("max_loss_per_trade_inr") or 0, False),
                               ("%.2f" % rr) if rr is not None else "n/a", _num(c.get("min_reward_risk") or 0)))
            info.append("today before this: %d of %s trades, %s of %s daily loss used"
                        % (len(day["entries"]) + batch_entries, cap_t or "unlimited",
                           M.inr(day["used"] + batch_loss),
                           M.inr(daily, False) if daily else "no limit on"))

        # ---- money: size, position cap, margin
        margin = None
        if kind == "entry" and notional is not None:
            if itype == "FUT":
                margin = round(notional * float(_f(c.get("futures_margin_pct")) or 15) / 100.0, 2)
                exposure = notional
                warns.append("futures margin %s is an estimate (%s%% of %s): check your broker's SPAN calculator"
                             % (M.inr(margin), _num(c.get("futures_margin_pct") or 15), M.inr(notional)))
            elif itype in ("CE", "PE") and side == "SELL":
                margin = _f((o.get("margin") or {}).get("required")) or round(notional * 10, 2)
                exposure = margin
            elif itype == "EQ" and product == "MIS" and c.get("use_intraday_leverage"):
                imp = max(20.0, float(_f(c.get("intraday_margin_pct")) or 20))
                margin = round(notional * imp / 100.0, 2)
                exposure = notional
            else:
                margin = notional
                exposure = notional
            mn = _f(c.get("min_order_inr"))
            if mn and notional < mn - PAISA:
                later.append("size: %s is under your %s minimum order" % (M.inr(notional), M.inr(mn, False)))
            mo = _f(c.get("max_order_inr"))
            if mo and exposure > mo + PAISA:
                later.append("size: %s is over your %s max order" % (M.inr(exposure), M.inr(mo, False)))
            cp = _f(c.get("max_position_pct"))
            if cp and capital > 0:
                held_val = abs(held) * (ref or 0)
                if held_val + exposure > capital * cp / 100.0 + PAISA:
                    later.append("position: %s would be %s, %.1f%% of your %s capital, over your %s%% cap"
                                 % (tsym, M.inr(held_val + exposure), 100.0 * (held_val + exposure) / capital,
                                    M.inr(capital, False), _num(cp)))
            left = cash - margin
            if left < -PAISA:
                later.append("margin: needs %s but only %s cash is free (%s)" % (M.inr(margin), M.inr(max(cash, 0)),
                                                                                acct["source"]))
            elif left < buffer_inr - PAISA:
                later.append("cash: leaves %s, under your %s%% cash buffer of %s"
                             % (M.inr(max(left, 0)), _num(c.get("cash_buffer_pct") or 0), M.inr(buffer_inr)))

        # ---- duplicates and the order count
        dkey = (tsym, side)
        if kind == "entry":
            if dkey in seen:
                later.append("duplicate: a second %s %s in this batch" % (side, tsym))
            if dkey in placed_keys:
                later.append("duplicate: a %s %s entry was already placed today" % (side, tsym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(_f(c.get("max_orders_per_day")) or 0)
        if cap_n and len(day["placed"]) + passed_so_far >= cap_n:
            later.append("orders: %d placed today plus %d passed earlier in this batch; your limit is %d a day"
                         % (len(day["placed"]), passed_so_far, cap_n))
        seen.add(dkey)

        if kind == "entry":
            reasons.extend(later)
        else:
            # an exit is never blocked by the day's rules; the format/tick/lot reasons above still block
            reasons = [r for r in reasons if not r.startswith(("short:", "options:", "futures:"))]
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS" and kind == "entry":
            batch_entries += 1
            batch_loss += loss_c or 0.0
            cash -= margin or 0.0
            acct["positions"][key] = held + (qty if long else -qty)
        elif verdict == "PASS":
            acct["positions"][key] = held + (qty if long else -qty)
        results.append({
            "index": i, "fingerprint": fingerprint(o), "verdict": verdict, "reasons": reasons,
            "warnings": warns, "info": info, "date": today, "time": M.hm(t), "kind": kind,
            "tradingsymbol": tsym, "symbol": sym, "side": side, "qty": qty, "lots": (qty // lot) if (fno and qty) else None,
            "product": product, "order_type": otype, "entry_ref": ref, "entry_ref_from": ref_from,
            "stop_trigger": stop, "target": target, "max_loss": max_loss, "max_loss_with_costs": loss_c,
            "costs_at_stop": rt_stop["total"] if rt_stop else None, "net_reward_risk": rr,
            "margin": margin, "notional": notional,
        })

    return {
        "created_by": "trader-risk", "date": today, "time": M.hm(now.time()), "paper": bool(c.get("paper", True)),
        "checks": results, "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"), "warnings": top_warn,
        "cash_after": round(cash, 2), "account": acct["source"], "market": st["label"],
        "today": {"orders_placed": len(day["placed"]), "entries_placed": len(day["entries"]),
                  "realised_pnl": day["realised"], "loss_at_risk": day["at_risk"]},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["checks"]:
        amt = ("%d (%d lot%s)" % (r["qty"], r["lots"], "" if r["lots"] == 1 else "s")) if r.get("lots") else \
            ("%s" % (r["qty"] if r["qty"] is not None else "?"))
        lines.append("%-5s #%d %s %s %s %s, %s" % (r["verdict"], r["index"], r["side"], amt, r["tradingsymbol"],
                                                 r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s %s IST (%s). Cash after the passed orders: %s (%s, %s account)."
                 % (res["pass"], res["block"], res["date"], res["time"], res["market"], M.inr(res["cash_after"]),
                    res["account"], "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append(M.NOT_ADVICE)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk, India).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default=M.CONFIG_FILE)
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--ledger", default="trader/paper/ledger.json", help="the paper broker's ledger")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-28 09:50\"")
    ap.add_argument("--out", default="trader/state/risk.json")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = M.parse_now(a.now)
        doc = M.read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = M.load_config(a.config)
        portfolio = M.read_json(a.portfolio, "portfolio", required=False)
        ledger = M.read_json(a.ledger, "paper ledger", required=False)
        prices = load_prices(M.read_json(a.prices, "prices", required=False) or {})
        state = M.read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, ledger, now)
        res["orders_file"] = a.orders
        if not a.no_write:
            rec = dict(res)
            rec["checked_at"] = now.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"
            M.write_json(a.out, rec)
    except (M.MarketError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, default=str) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(M.run_cli(main))
