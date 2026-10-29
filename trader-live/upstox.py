#!/usr/bin/env python3
"""Upstox API client for trader-live. Standard library only.

Credentials never go in the chat and never in the repository:

    ~/.config/trader-india/.env      (or ./.env, or $TRADER_ENV_FILE), mode 600:
        UPSTOX_API_KEY=...
        UPSTOX_API_SECRET=...
        UPSTOX_REDIRECT_URI=http://127.0.0.1:8765/callback

The daily access token (Upstox: valid until 03:30 IST the next morning) is
saved by `live.py login` to ~/.config/trader-india/upstox-token.json, mode 600.

Endpoints (Upstox API docs, read 2026-10-29):
  login dialog   GET  https://api.upstox.com/v2/login/authorization/dialog
  token          POST https://api.upstox.com/v2/login/authorization/token
  place order    POST https://api-hft.upstox.com/v3/order/place
  modify order   PUT  https://api-hft.upstox.com/v3/order/modify
  cancel order   DEL  https://api-hft.upstox.com/v3/order/cancel?order_id=
  order book     GET  https://api.upstox.com/v2/order/retrieve-all
  order details  GET  https://api.upstox.com/v2/order/details?order_id=
  order trades   GET  https://api.upstox.com/v2/order/trades?order_id=
  positions      GET  https://api.upstox.com/v2/portfolio/short-term-positions
  funds          GET  https://api.upstox.com/v2/user/get-funds-and-margin?segment=SEC
  profile        GET  https://api.upstox.com/v2/user/profile
  LTP            GET  https://api.upstox.com/v3/market-quote/ltp?instrument_key=
  full quote     GET  https://api.upstox.com/v2/market-quote/quotes?instrument_key=
  1-min candles  GET  https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/1
  market status  GET  https://api.upstox.com/v2/market/status/NSE
  static IPs     GET  https://api.upstox.com/v2/user/ip
  instruments    https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
Rate limits: order APIs 10/s, 500/min, 2000/30 min; others 50/s, 500/min, 2000/30 min.
"""

import datetime
import gzip
import http.server
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "trader-market"), os.path.join(HERE, "trader-market")):
    if os.path.isfile(os.path.join(_p, "market.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import market as M  # noqa: E402

API = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/%s.json.gz"
CONFIG_DIR = os.path.expanduser(os.environ.get("TRADER_CONFIG_DIR", "~/.config/trader-india"))
CACHE_DIR = os.path.expanduser(os.environ.get("TRADER_CACHE_DIR", "~/.cache/trader-india"))
TOKEN_FILE = os.path.join(CONFIG_DIR, "upstox-token.json")
ENV_NAMES = ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI")
# Upstox sits behind Cloudflare, which refuses Python's default "Python-urllib" signature
# (error 1010). Every request names this tool instead.
UA = "day-trader-skill-india/1.1 (+https://github.com/aditya76792/day-trader-skill)"
PRODUCT = {"MIS": "I", "CNC": "D", "NRML": "D"}
PRODUCT_BACK = {"I": "MIS", "D": "CNC", "MTF": "MTF", "CO": "MIS"}
INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank", "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
              "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT", "NIFTYNXT50": "NSE_INDEX|Nifty Next 50",
              "INDIAVIX": "NSE_INDEX|India VIX", "SENSEX": "BSE_INDEX|SENSEX", "BANKEX": "BSE_INDEX|BANKEX"}
# Upstox order status -> ours
STATUS = {"complete": "filled", "rejected": "rejected", "cancelled": "cancelled", "open": "open",
          "trigger pending": "open", "after market order req received": "open", "validation pending": "pending",
          "put order req received": "pending", "open pending": "pending", "modify pending": "open",
          "modify validation pending": "open", "modified": "open", "cancel pending": "open",
          "not modified": "open", "not cancelled": "open"}


class UpstoxError(M.MarketError):
    def __init__(self, message, code=None, http_status=None, retryable=False):
        M.MarketError.__init__(self, message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


class TokenError(UpstoxError):
    pass


# ================================================================ secrets

def _check_private(path):
    """Refuse a secrets file other users can read."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UpstoxError("%s can be read by other users; run: chmod 600 %s" % (path, path))


def env_files():
    out = []
    if os.environ.get("TRADER_ENV_FILE"):
        out.append(os.path.expanduser(os.environ["TRADER_ENV_FILE"]))
    out += [os.path.join(os.getcwd(), ".env"), os.path.join(CONFIG_DIR, ".env")]
    return out


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


class Credentials(object):
    """API key/secret from the environment or a private .env file. Never printed."""

    def __init__(self, api_key, api_secret, redirect_uri, source):
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri or "http://127.0.0.1:8765/callback"
        self.source = source

    def __repr__(self):
        return "Credentials(api_key=%s..., secret=***, redirect=%s, from %s)" % (
            (self.api_key or "")[:4], self.redirect_uri, self.source)

    @classmethod
    def load(cls, environ=None, files=None):
        environ = os.environ if environ is None else environ
        vals = {k: environ.get(k) for k in ENV_NAMES if environ.get(k)}
        source = "environment" if vals else None
        for f in (files if files is not None else env_files()):
            if len(vals) == len(ENV_NAMES):
                break
            if os.path.isfile(f):
                _check_private(f)
                with open(f, encoding="utf-8") as fh:
                    got = parse_env(fh.read())
                for k in ENV_NAMES:
                    if k not in vals and got.get(k):
                        vals[k] = got[k]
                        source = source or f
        missing = [k for k in ENV_NAMES[:2] if not vals.get(k)]
        if missing:
            raise UpstoxError("missing %s. Create %s with your Upstox app's API key and secret (see "
                              "trader-live/env.example), then chmod 600 it. Never paste them into the chat."
                              % (", ".join(missing), os.path.join(CONFIG_DIR, ".env")))
        return cls(vals["UPSTOX_API_KEY"], vals["UPSTOX_API_SECRET"], vals.get("UPSTOX_REDIRECT_URI"), source)


def redact(text, extra=()):
    """Remove anything that looks like a token or secret before it is logged."""
    s = str(text)
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***", s)
    s = re.sub(r"(?i)(access_token|extended_token|client_secret|api_secret|code)([\"'=:\s]+)[A-Za-z0-9._\-]{6,}",
               r"\1\2***", s)
    for x in extra:
        if x and len(x) >= 6:
            s = s.replace(x, "***")
    return s


def token_expiry(created):
    """Upstox tokens last until 03:30 IST the following day."""
    nxt = created.date() + datetime.timedelta(days=1)
    if created.time() < datetime.time(3, 30):
        nxt = created.date()
    return datetime.datetime.combine(nxt, datetime.time(3, 30))


def save_token(data, now=None, path=TOKEN_FILE):
    now = now or M.now_ist()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    doc = {"access_token": data["access_token"], "user_id": data.get("user_id"), "user_name": data.get("user_name"),
           "created_at": now.isoformat(timespec="seconds"), "expires_at": token_expiry(now).isoformat(timespec="seconds")}
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return doc


def load_token(now=None, path=TOKEN_FILE):
    now = now or M.now_ist()
    if not os.path.isfile(path):
        raise TokenError("no Upstox session: run `python3 trader-live/live.py login` (once each trading morning)")
    _check_private(path)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    exp = datetime.datetime.fromisoformat(doc["expires_at"])
    if now >= exp:
        raise TokenError("the Upstox session expired at %s (tokens end at 03:30 IST each day): run "
                         "`live.py login` again" % doc["expires_at"])
    return doc


# ================================================================ login (OAuth code flow on localhost)

def login_url(creds, state):
    q = urllib.parse.urlencode({"client_id": creds.api_key, "redirect_uri": creds.redirect_uri,
                                "response_type": "code", "state": state})
    return API + "/v2/login/authorization/dialog?" + q


def exchange_code(creds, code, opener=None):
    body = urllib.parse.urlencode({"code": code, "client_id": creds.api_key, "client_secret": creds.api_secret,
                                   "redirect_uri": creds.redirect_uri, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(API + "/v2/login/authorization/token", data=body, method="POST",
                                 headers={"Accept": "application/json", "User-Agent": UA,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=30) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise UpstoxError("Upstox refused the login code: %s" % _error_text(e), http_status=e.code)
    data = doc.get("data") if isinstance(doc, dict) and "data" in doc else doc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise UpstoxError("Upstox's token answer had no access_token")
    return data


def run_login(creds, timeout=300, open_browser=True, out=print):
    """Open Upstox's login page, catch the redirect on localhost, swap the code for a token."""
    u = urllib.parse.urlsplit(creds.redirect_uri)
    if u.hostname not in ("127.0.0.1", "localhost"):
        raise UpstoxError("UPSTOX_REDIRECT_URI must be http://127.0.0.1:PORT/callback (the same as in your Upstox "
                          "app) so the login can come back to this computer")
    state = secrets.token_urlsafe(16)
    box = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(p.query)
            if p.path != (u.path or "/"):
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                box["error"] = "state mismatch (an old or forged redirect); try again"
            elif "code" in q:
                box["code"] = q["code"][0]
            else:
                box["error"] = q.get("error", ["no code in the redirect"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h3>%s</h3>You can close this tab." % (
                "Logged in to Upstox." if "code" in box else "Login failed: " + box.get("error", ""))).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = login_url(creds, state)
    out("Open this link and log in to Upstox (it opens in your browser):\n  %s" % url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    t0 = time.time()
    try:
        while "code" not in box and "error" not in box and time.time() - t0 < timeout:
            time.sleep(0.2)
    finally:
        srv.shutdown()
    if "error" in box:
        raise UpstoxError("login failed: %s" % box["error"])
    if "code" not in box:
        raise UpstoxError("no login within %d seconds" % timeout)
    data = exchange_code(creds, box["code"])
    return save_token(data)


# ================================================================ client

class RateLimiter(object):
    def __init__(self, per_sec, per_min, sleep=time.sleep, clock=time.monotonic):
        self.per_sec, self.per_min = per_sec, per_min
        self.sleep, self.clock = sleep, clock
        self.calls = []
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            while True:
                now = self.clock()
                self.calls = [t for t in self.calls if now - t < 60]
                last_sec = [t for t in self.calls if now - t < 1]
                if len(last_sec) < self.per_sec and len(self.calls) < self.per_min:
                    self.calls.append(now)
                    return
                if len(self.calls) >= self.per_min:
                    self.sleep(max(0.05, 60 - (now - self.calls[0])))
                else:
                    self.sleep(max(0.02, 1 - (now - last_sec[0])))


def _error_text(e):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    try:
        doc = json.loads(body)
        errs = doc.get("errors") or []
        if errs:
            x = errs[0]
            return "%s %s" % (x.get("errorCode") or x.get("error_code") or "", x.get("message") or "")
        return doc.get("message") or body[:200]
    except ValueError:
        return body[:200] or ("HTTP %s" % getattr(e, "code", "?"))


class Client(object):
    """Thin, careful Upstox REST client. Reads retry; order writes NEVER auto-retry
    (a timed-out place may have gone through: the engine reconciles by tag instead)."""

    def __init__(self, token, opener=None, sleep=time.sleep, timeout=15, log=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener()
        self.sleep = sleep
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        self.orders_rl = RateLimiter(8, 400, sleep)      # under 10/s, 500/min
        self.reads_rl = RateLimiter(40, 450, sleep)      # under 50/s, 500/min

    def _req(self, method, base, path, params=None, body=None, write=False):
        url = base + path + (("?" + urllib.parse.urlencode(params, safe="|,")) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        tries = 1 if write else 3
        last = None
        for attempt in range(tries):
            (self.orders_rl if write else self.reads_rl).wait()
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Accept": "application/json", "Content-Type": "application/json", "User-Agent": UA,
                "Authorization": "Bearer " + self.token})
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                doc = json.loads(raw) if raw.strip() else {}
                if isinstance(doc, dict) and doc.get("status") == "error":
                    errs = doc.get("errors") or [{}]
                    raise UpstoxError("Upstox: %s" % (errs[0].get("message") or errs), errs[0].get("errorCode") or
                                      errs[0].get("error_code"))
                return doc.get("data", doc) if isinstance(doc, dict) else doc
            except urllib.error.HTTPError as e:
                text = _error_text(e)
                if e.code == 401:
                    raise TokenError("Upstox says the session is not valid (%s): run `live.py login`" % text,
                                     http_status=401)
                if e.code in (429, 500, 502, 503, 504) and not write:
                    last = UpstoxError("Upstox %s: %s" % (e.code, text), http_status=e.code, retryable=True)
                    self.sleep(1.5 * (attempt + 1))
                    continue
                raise UpstoxError("Upstox %s on %s: %s" % (e.code, path.split("?")[0], text), http_status=e.code,
                                  retryable=e.code in (429, 500, 502, 503, 504))
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = UpstoxError("network problem talking to Upstox (%s): %s" % (path.split("?")[0],
                                                                                   getattr(e, "reason", e)),
                                   retryable=True)
                if write:
                    raise last
                self.sleep(1.5 * (attempt + 1))
        raise last

    # ---- account
    def profile(self):
        return self._req("GET", API, "/v2/user/profile")

    def funds(self):
        return self._req("GET", API, "/v2/user/get-funds-and-margin", {"segment": "SEC"})

    def positions(self):
        return self._req("GET", API, "/v2/portfolio/short-term-positions") or []

    def static_ips(self):
        return self._req("GET", API, "/v2/user/ip")

    def market_status(self, exchange="NSE"):
        return self._req("GET", API, "/v2/market/status/%s" % exchange)

    # ---- market data
    def ltp(self, keys):
        out = {}
        keys = list(keys)
        for i in range(0, len(keys), 450):
            data = self._req("GET", API, "/v3/market-quote/ltp", {"instrument_key": ",".join(keys[i:i + 450])})
            for _, q in (data or {}).items():
                tok = q.get("instrument_token")
                if tok:
                    out[tok] = {"price": q.get("last_price"), "prev_close": q.get("cp"), "volume": q.get("volume")}
        return out

    def quotes(self, keys):
        data = self._req("GET", API, "/v2/market-quote/quotes", {"instrument_key": ",".join(keys)})
        out = {}
        for _, q in (data or {}).items():
            tok = q.get("instrument_token")
            if tok:
                out[tok] = q
        return out

    def intraday_candles(self, key, minutes=1):
        data = self._req("GET", API, "/v3/historical-candle/intraday/%s/minutes/%d"
                         % (urllib.parse.quote(key, safe=""), minutes))
        rows = (data or {}).get("candles") or []
        out = []
        for r in rows:
            ts = datetime.datetime.fromisoformat(r[0]).astimezone(M.IST).replace(tzinfo=None)
            out.append({"dt": ts, "t": M.ts_from_ist(ts), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                        "c": float(r[4]), "v": float(r[5] or 0)})
        out.sort(key=lambda b: b["dt"])
        return out

    # ---- orders
    def place(self, payload):
        data = self._req("POST", HFT, "/v3/order/place", body=payload, write=True)
        ids = (data or {}).get("order_ids") or ([data["order_id"]] if (data or {}).get("order_id") else [])
        if not ids:
            raise UpstoxError("Upstox accepted the request but returned no order id")
        return ids

    def modify(self, payload):
        return self._req("PUT", HFT, "/v3/order/modify", body=payload, write=True)

    def cancel(self, order_id):
        return self._req("DELETE", HFT, "/v3/order/cancel", {"order_id": order_id}, write=True)

    def order_book(self):
        data = self._req("GET", API, "/v2/order/retrieve-all")
        return data if isinstance(data, list) else []

    def order(self, order_id):
        return self._req("GET", API, "/v2/order/details", {"order_id": order_id})

    def trades(self, order_id):
        return self._req("GET", API, "/v2/order/trades", {"order_id": order_id}) or []


def order_payload(order, instrument_key, tag):
    """Broker-neutral order (CONTRACTS.md) -> Upstox v3 place body."""
    ot = str(order["order_type"]).upper()
    return {"quantity": int(order["qty"]), "product": PRODUCT[str(order.get("product") or "MIS").upper()],
            "validity": str(order.get("validity") or "DAY").upper(), "price": float(order.get("price") or 0),
            "tag": tag, "instrument_token": instrument_key, "order_type": ot,
            "transaction_type": str(order["side"]).upper(), "disclosed_quantity": 0,
            "trigger_price": float(order.get("trigger_price") or 0), "is_amo": False, "slice": False}


def normalise_order(o):
    """Upstox order row -> {id, status, filled_qty, avg_price, message, tag}."""
    st = STATUS.get(str(o.get("status") or "").lower(), "pending")
    filled = int(o.get("filled_quantity") or 0)
    if st == "open" and filled:
        st = "partial"
    return {"id": o.get("order_id"), "status": st, "raw_status": o.get("status"), "filled_qty": filled,
            "qty": int(o.get("quantity") or 0), "avg_price": float(o.get("average_price") or 0) or None,
            "message": o.get("status_message") or "", "tag": o.get("tag"),
            "instrument_key": o.get("instrument_token"), "side": o.get("transaction_type"),
            "order_type": o.get("order_type"), "trigger_price": o.get("trigger_price"), "price": o.get("price")}


# ================================================================ instruments

class Instruments(object):
    """Upstox's NSE instrument file, cached for the day: symbol <-> instrument key."""

    def __init__(self, rows):
        self.by_key = {}
        self.eq = {}
        self.fo = {}
        for r in rows:
            k = r.get("instrument_key")
            if not k:
                continue
            self.by_key[k] = r
            seg = r.get("segment")
            it = r.get("instrument_type")
            if seg in ("NSE_EQ", "BSE_EQ") and it in ("EQ", "BE", "SM", "ST", None):
                self.eq[(seg[:3], str(r.get("trading_symbol")).upper())] = r
            elif seg in ("NSE_FO", "BSE_FO"):
                exp = r.get("expiry")
                if isinstance(exp, (int, float)):
                    exp = M.ist_from_ts(exp / 1000.0).date().isoformat()
                strike = float(r.get("strike_price") or 0)
                und = str(r.get("underlying_symbol") or r.get("name") or "").upper()
                self.fo[(und, str(exp)[:10], strike if it in ("CE", "PE") else 0.0, it)] = r

    @classmethod
    def load(cls, day=None, exchange="NSE", fetch=None, path=None):
        day = day or M.now_ist().date()
        if path:
            with open(path, "rb") as fh:
                raw = fh.read()
        else:
            os.makedirs(CACHE_DIR, exist_ok=True)
            cache = os.path.join(CACHE_DIR, "upstox-%s-%s.json.gz" % (exchange, day.isoformat()))
            if os.path.isfile(cache):
                with open(cache, "rb") as fh:
                    raw = fh.read()
            else:
                url = INSTRUMENTS_URL % exchange
                raw = fetch(url) if fetch else urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60).read()
                with open(cache + ".tmp", "wb") as fh:
                    fh.write(raw)
                os.replace(cache + ".tmp", cache)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        rows = json.loads(raw.decode("utf-8"))
        return cls(rows)

    def key_for(self, order_or_symbol, exchange="NSE"):
        """Instrument key for a symbol or a broker-neutral order."""
        if isinstance(order_or_symbol, dict):
            o = order_or_symbol
            inst = o.get("instrument") or {"type": "EQ"}
            itype = str(inst.get("type") or "EQ").upper()
            if itype in ("CE", "PE", "FUT"):
                und = str(inst.get("underlying") or o.get("symbol")).upper()
                k = (und, str(inst.get("expiry"))[:10], float(inst.get("strike") or 0) if itype != "FUT" else 0.0,
                     itype)
                r = self.fo.get(k)
                if not r:
                    raise UpstoxError("Upstox has no %s %s %s %s contract in today's instrument file" % (
                        und, inst.get("expiry"), inst.get("strike") or "", itype))
                return r["instrument_key"]
            sym = str(o.get("symbol") or o.get("tradingsymbol")).upper()
            exchange = o.get("exchange") or exchange
        else:
            sym = str(order_or_symbol).upper()
        if sym in INDEX_KEYS:
            return INDEX_KEYS[sym]
        r = self.eq.get((exchange[:3], sym))
        if not r:
            raise UpstoxError("%s is not in Upstox's %s equity list today" % (sym, exchange))
        return r["instrument_key"]

    def row(self, key):
        return self.by_key.get(key) or {}
