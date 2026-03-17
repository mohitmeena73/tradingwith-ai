"""
╔══════════════════════════════════════════════════════════════╗
║          ZERODHA BRIDGE — AI Trading Terminal V3             ║
║          Production-Grade Real-Time Server                   ║
╠══════════════════════════════════════════════════════════════╣
║  INSTALL (one time):                                         ║
║    pip install flask flask-cors kiteconnect requests         ║
║                                                              ║
║  RUN:  python zerodha_bridge.py                              ║
║  URL:  http://localhost:5001                                 ║
║                                                              ║
║  FEATURES:                                                   ║
║  • Real-time WebSocket tick streaming to browser via SSE     ║
║  • Live P&L updates at exchange speed                        ║
║  • Auto-reconnect on disconnect                              ║
║  • Token persistence across restarts                         ║
║  • Full order management (place/modify/cancel/GTT/SL)        ║
║  • Built-in risk calculator                                  ║
╚══════════════════════════════════════════════════════════════╝
"""

import hashlib, json, os, time, threading, logging, queue
from datetime import datetime, date
from collections import defaultdict

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("BRIDGE")
log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("  %(asctime)s  %(message)s", "%H:%M:%S"))
log.addHandler(_h)

import requests
from flask import Flask, request, jsonify, redirect, Response, stream_with_context
from flask_cors import CORS

try:
    from kiteconnect import KiteConnect, KiteTicker
    KITE_LIB = True
except ImportError:
    KITE_LIB = False

app = Flask(__name__)
CORS(app, origins=["*"], supports_credentials=True)

# ── Global state ───────────────────────────────────────────────
TOKEN_FILE  = "zerodha_token.json"
CONFIG_FILE = "zerodha_config.json"

state = {
    "api_key": "", "api_secret": "", "access_token": "",
    "user_name": "", "user_id": "", "connected": False, "ticker_live": False,
}

ticks       = {}          # {instrument_token: tick_dict}
subscribers = []          # SSE subscriber queues
sub_lock    = threading.Lock()

cache = {
    "holdings": None,  "holdings_ts": 0,
    "positions": None, "positions_ts": 0,
    "orders": None,    "orders_ts": 0,
}
TTL = {"holdings": 30, "positions": 4, "orders": 4}

DEFAULT_TOKENS = [256265, 260105]   # NIFTY 50, BANK NIFTY

# ── Load saved credentials on startup ─────────────────────────
def load_saved():
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = json.load(open(CONFIG_FILE))
            state["api_key"]    = cfg.get("api_key", "")
            state["api_secret"] = cfg.get("api_secret", "")
        except: pass
    if os.path.exists(TOKEN_FILE):
        try:
            tok = json.load(open(TOKEN_FILE))
            state["access_token"] = tok.get("access_token", "")
            state["user_name"]    = tok.get("user_name", "")
            state["user_id"]      = tok.get("user_id", "")
            if state["access_token"] and state["api_key"]:
                state["connected"] = True
                log.info(f"Token loaded for {state['user_name']}")
        except: pass

load_saved()

# ── HTTP helpers ───────────────────────────────────────────────
def _headers():
    return {"Authorization": f"token {state['api_key']}:{state['access_token']}",
            "X-Kite-Version": "3"}

def kget(path, params=None):
    r = requests.get(f"https://api.kite.trade{path}", headers=_headers(), params=params, timeout=8)
    return r.json()

def kpost(path, data=None):
    r = requests.post(f"https://api.kite.trade{path}", headers=_headers(), data=data, timeout=8)
    return r.json()

def kput(path, data=None):
    r = requests.put(f"https://api.kite.trade{path}", headers=_headers(), data=data, timeout=8)
    return r.json()

def kdel(path):
    r = requests.delete(f"https://api.kite.trade{path}", headers=_headers(), timeout=8)
    return r.json()

# ── SSE broadcast ──────────────────────────────────────────────
def broadcast(payload):
    dead = []
    with sub_lock:
        for q in subscribers:
            try: q.put_nowait(payload)
            except: dead.append(q)
        for q in dead:
            subscribers.remove(q)

# ── Real-time KiteTicker ───────────────────────────────────────
ticker_obj = None

def start_ticker(extra_tokens=None):
    global ticker_obj
    if not KITE_LIB or not state["connected"]: return
    if ticker_obj:
        try: ticker_obj.close()
        except: pass

    tokens = list(set(DEFAULT_TOKENS + (extra_tokens or [])))

    def on_ticks(ws, tick_list):
        for t in tick_list:
            tok = t["instrument_token"]
            ticks[tok] = {
                "instrument_token": tok,
                "last_price": t.get("last_price", 0),
                "change":     t.get("change", 0),
                "volume":     t.get("volume", 0),
                "buy_qty":    t.get("buy_quantity", 0),
                "sell_qty":   t.get("sell_quantity", 0),
                "open":  t.get("ohlc", {}).get("open", 0),
                "high":  t.get("ohlc", {}).get("high", 0),
                "low":   t.get("ohlc", {}).get("low", 0),
                "close": t.get("ohlc", {}).get("close", 0),
                "oi":    t.get("oi", 0),
                "ts":    time.time(),
            }
        broadcast(json.dumps({"type": "tick", "data": ticks}))

    def on_connect(ws, resp):
        state["ticker_live"] = True
        log.info(f"Ticker connected — {len(tokens)} instruments")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(ws, code, reason):
        state["ticker_live"] = False
        log.warning(f"Ticker closed: {reason}")

    def on_error(ws, code, reason):
        log.error(f"Ticker error: {reason}")

    def on_reconnect(ws, attempt):
        log.info(f"Ticker reconnecting... #{attempt}")

    ticker_obj = KiteTicker(state["api_key"], state["access_token"])
    ticker_obj.on_ticks     = on_ticks
    ticker_obj.on_connect   = on_connect
    ticker_obj.on_close     = on_close
    ticker_obj.on_error     = on_error
    ticker_obj.on_reconnect = on_reconnect
    threading.Thread(target=ticker_obj.connect,
                     kwargs={"threaded": True}, daemon=True).start()
    log.info("Ticker thread started")

# ── Background portfolio broadcaster (every 3s) ────────────────
def _portfolio_loop():
    while True:
        time.sleep(3)
        if not state["connected"] or not subscribers: continue
        try:
            pos = _positions_raw()
            hld = _holdings_raw()
            broadcast(json.dumps({"type": "portfolio", "positions": pos,
                                   "holdings": hld, "ts": time.time()}))
        except: pass

threading.Thread(target=_portfolio_loop, daemon=True).start()

# ── Cached fetchers ────────────────────────────────────────────
def _holdings_raw():
    now = time.time()
    if cache["holdings"] is not None and (now - cache["holdings_ts"]) < TTL["holdings"]:
        return cache["holdings"]
    r = kget("/portfolio/holdings")
    if r.get("status") == "success":
        cache["holdings"]    = r.get("data", [])
        cache["holdings_ts"] = now
    return cache["holdings"] or []

def _positions_raw():
    now = time.time()
    if cache["positions"] is not None and (now - cache["positions_ts"]) < TTL["positions"]:
        return cache["positions"]
    r = kget("/portfolio/positions")
    if r.get("status") == "success":
        cache["positions"]    = r["data"].get("net", [])
        cache["positions_ts"] = now
    return cache["positions"] or []

# ════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════

@app.route("/config", methods=["POST"])
def set_config():
    d = request.json or {}
    state["api_key"]    = d.get("api_key", "").strip()
    state["api_secret"] = d.get("api_secret", "").strip()
    json.dump({"api_key": state["api_key"], "api_secret": state["api_secret"]},
              open(CONFIG_FILE, "w"))
    return jsonify({"status": "ok"})

@app.route("/login")
def login():
    if not state["api_key"]:
        return "<h3 style='font-family:monospace;color:red'>Error: set API key first</h3>", 400
    return redirect(f"https://kite.zerodha.com/connect/login?v=3&api_key={state['api_key']}")

@app.route("/callback")
def callback():
    rt = request.args.get("request_token", "")
    if not rt: return "<h3>No request token</h3>", 400
    try:
        cs = hashlib.sha256(f"{state['api_key']}{rt}{state['api_secret']}".encode()).hexdigest()
        resp = requests.post("https://api.kite.trade/session/token",
                             data={"api_key": state["api_key"], "request_token": rt, "checksum": cs},
                             headers={"X-Kite-Version": "3"}, timeout=10).json()
        if resp.get("status") == "success":
            d = resp["data"]
            state.update({"access_token": d["access_token"],
                           "user_name": d.get("user_name","Trader"),
                           "user_id": d.get("user_id",""), "connected": True})
            json.dump({"access_token": d["access_token"],
                       "user_name": state["user_name"],
                       "user_id": state["user_id"]}, open(TOKEN_FILE, "w"))
            log.info(f"Login OK: {state['user_name']}")
            threading.Thread(target=start_ticker, daemon=True).start()
            broadcast(json.dumps({"type": "connected", "user": state["user_name"]}))
            return f"""<html><head><style>
              body{{background:#060910;color:#00e676;font-family:monospace;display:flex;
                   align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:10px;}}
              h2{{color:#00e5ff;letter-spacing:2px;}}p{{color:#7a90b8;font-size:13px;}}
              .d{{width:10px;height:10px;border-radius:50%;background:#00e676;box-shadow:0 0 12px #00e676;animation:p 1s infinite;}}
              @keyframes p{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
            </style></head><body><div class='d'></div>
            <h2>ZERODHA CONNECTED</h2>
            <p>Welcome, {state['user_name']}</p><p>Real-time feed started. Close this tab.</p>
            <script>setTimeout(()=>window.close(),2500)</script></body></html>"""
        return f"<h3>Login failed: {resp}</h3>", 400
    except Exception as e:
        return f"<h3>Error: {e}</h3>", 500

@app.route("/logout", methods=["POST"])
def logout():
    global ticker_obj
    try: kdel(f"/session/token?api_key={state['api_key']}&access_token={state['access_token']}")
    except: pass
    if ticker_obj:
        try: ticker_obj.close()
        except: pass
    state.update({"access_token":"","connected":False,"ticker_live":False,"user_name":"","user_id":""})
    if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
    return jsonify({"status": "ok"})

@app.route("/status")
def status():
    return jsonify({"connected": state["connected"], "ticker_live": state["ticker_live"],
                    "user_name": state["user_name"], "user_id": state["user_id"],
                    "subscribers": len(subscribers), "ticks_count": len(ticks),
                    "ts": datetime.now().isoformat()})

@app.route("/ping")
def ping():
    return jsonify({"pong": True, "ts": time.time()})

# ── SSE Real-time Stream ───────────────────────────────────────
@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=100)
    with sub_lock: subscribers.append(q)

    def generate():
        yield f"data: {json.dumps({'type':'init','ticks':ticks,'connected':state['connected'],'user':state['user_name'],'ticker_live':state['ticker_live']})}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type':'heartbeat','ts':time.time()})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with sub_lock:
                try: subscribers.remove(q)
                except: pass

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                             "Connection":"keep-alive","Access-Control-Allow-Origin":"*"})

# ── Market Data ────────────────────────────────────────────────
@app.route("/quote")
def quote():
    instruments = request.args.get("i","NSE:NIFTY 50,NSE:NIFTY BANK").split(",")
    return jsonify(kget("/quote", params={"i": instruments}))

@app.route("/ltp")
def ltp():
    instruments = request.args.get("i","NSE:NIFTY 50").split(",")
    return jsonify(kget("/quote/ltp", params={"i": instruments}))

@app.route("/ticks")
def get_ticks():
    return jsonify({"status":"success","data":ticks})

@app.route("/history")
def history():
    """Historical candle data for chart analysis"""
    token    = request.args.get("instrument_token","256265")
    interval = request.args.get("interval","day")
    from_d   = request.args.get("from", str(date.today()))
    to_d     = request.args.get("to",   str(date.today()))
    r = kget(f"/instruments/historical/{token}/{interval}",
             params={"from": from_d, "to": to_d, "continuous": 0, "oi": 1})
    return jsonify(r)

# ── Portfolio ──────────────────────────────────────────────────
@app.route("/holdings")
def holdings():
    if request.args.get("force") == "true": cache["holdings_ts"] = 0
    return jsonify({"status":"success","data":_holdings_raw()})

@app.route("/positions")
def positions():
    if request.args.get("force") == "true": cache["positions_ts"] = 0
    data = _positions_raw()
    return jsonify({"status":"success","data":{"net":data,
        "day_pnl": round(sum(p.get("pnl",0) for p in data),2),
        "open_count": len([p for p in data if p.get("quantity",0)!=0])}})

@app.route("/margins")
def margins():
    return jsonify(kget("/user/margins"))

@app.route("/orders")
def orders():
    if request.args.get("force") == "true": cache["orders_ts"] = 0
    return jsonify(kget("/orders"))

# ── Order Placement ────────────────────────────────────────────
@app.route("/order/place", methods=["POST"])
def place_order():
    d = request.json or {}
    missing = [k for k in ["symbol","exchange","transaction_type","quantity"] if not d.get(k)]
    if missing: return jsonify({"status":"error","message":f"Missing: {', '.join(missing)}"}), 400
    try:
        payload = {
            "tradingsymbol":    d["symbol"].upper(),
            "exchange":         d.get("exchange","NSE"),
            "transaction_type": d["transaction_type"].upper(),
            "order_type":       d.get("order_type","MARKET"),
            "quantity":         int(d["quantity"]),
            "product":          d.get("product","MIS"),
            "validity":         "DAY",
        }
        price = float(d.get("price",0))
        if payload["order_type"] in ("LIMIT","SL") and price > 0:
            payload["price"] = price
        tp = float(d.get("trigger_price",0))
        if tp > 0: payload["trigger_price"] = tp
        if d.get("tag"): payload["tag"] = str(d["tag"])[:20]
        r = kpost("/orders/regular", data=payload)
        if r.get("status") == "success":
            cache["orders_ts"] = 0
            log.info(f"Order placed: {payload['transaction_type']} {payload['quantity']} {payload['tradingsymbol']} id={r['data']['order_id']}")
        return jsonify(r)
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/order/modify", methods=["POST"])
def modify_order():
    d = request.json or {}
    oid = d.get("order_id")
    if not oid: return jsonify({"status":"error","message":"order_id required"}), 400
    payload = {k: d[k] for k in ["quantity","price","order_type","trigger_price","validity"] if d.get(k)}
    r = kput(f"/orders/regular/{oid}", data=payload)
    cache["orders_ts"] = 0
    return jsonify(r)

@app.route("/order/cancel/<order_id>", methods=["DELETE"])
def cancel_order(order_id):
    r = kdel(f"/orders/regular/{order_id}")
    cache["orders_ts"] = 0
    return jsonify(r)

@app.route("/order/sl", methods=["POST"])
def place_sl():
    d = request.json or {}
    payload = {
        "tradingsymbol":    d.get("symbol","").upper(),
        "exchange":         d.get("exchange","NSE"),
        "transaction_type": d.get("transaction_type","SELL"),
        "order_type":       "SL",
        "quantity":         int(d.get("quantity",1)),
        "product":          d.get("product","MIS"),
        "price":            float(d.get("price",0)),
        "trigger_price":    float(d.get("trigger_price",0)),
        "validity":         "DAY",
        "tag":              "AI_SL",
    }
    return jsonify(kpost("/orders/regular", data=payload))

# ── GTT ───────────────────────────────────────────────────────
@app.route("/gtt/place", methods=["POST"])
def gtt_place():
    d = request.json or {}
    return jsonify(kpost("/gtt/triggers", data={
        "trigger_type":   "single",
        "tradingsymbol":  d.get("symbol","").upper(),
        "exchange":       d.get("exchange","NSE"),
        "trigger_values": json.dumps([float(d.get("trigger_price",0))]),
        "last_price":     float(d.get("last_price",0)),
        "orders": json.dumps([{"transaction_type":d.get("type","SELL"),"quantity":int(d.get("quantity",1)),
                                "order_type":"LIMIT","product":d.get("product","CNC"),
                                "price":float(d.get("trigger_price",0))}])
    }))

# ── Risk Calculator ────────────────────────────────────────────
@app.route("/risk/calc", methods=["POST"])
def risk_calc():
    d = request.json or {}
    capital  = float(d.get("capital",0))
    risk_pct = float(d.get("risk_pct",1))
    entry    = float(d.get("entry",0))
    sl       = float(d.get("stop_loss",0))
    if not entry or not sl:
        return jsonify({"status":"error","message":"entry and stop_loss required"}), 400
    rps = abs(entry - sl)
    qty = int((capital * risk_pct / 100) / rps) if rps > 0 else 0
    return jsonify({"status":"success","data":{
        "recommended_qty": qty,
        "risk_amount":     round(qty * rps, 2),
        "risk_pct_actual": round((qty*rps/capital)*100,2) if capital>0 else 0,
        "risk_per_share":  round(rps, 2),
    }})

# ── Ticker subscription ────────────────────────────────────────
@app.route("/ticker/subscribe", methods=["POST"])
def subscribe():
    d = request.json or {}
    tokens = [int(t) for t in d.get("tokens",[])]
    if ticker_obj and state["ticker_live"] and tokens:
        try:
            ticker_obj.subscribe(tokens)
            ticker_obj.set_mode(ticker_obj.MODE_FULL, tokens)
            return jsonify({"status":"ok","subscribed":tokens})
        except Exception as e:
            return jsonify({"status":"error","message":str(e)})
    return jsonify({"status":"error","message":"Ticker not live"})

# ── STARTUP ────────────────────────────────────────────────────
if __name__ == "__main__":
    W = 62
    print("\n" + "═"*W)
    print("   🚀  ZERODHA BRIDGE  ─  AI Trading Terminal V3")
    print("═"*W)
    print(f"   Endpoint  →  http://localhost:5001")
    print(f"   Live Feed →  http://localhost:5001/stream  (SSE)")
    print("─"*W)
    if not KITE_LIB:
        print("   ⚠  kiteconnect missing! Run: pip install kiteconnect")
    else:
        print("   ✅ kiteconnect ready")
    if state["connected"]:
        print(f"   ✅ Token loaded  →  {state['user_name']} ({state['user_id']})")
        threading.Thread(target=start_ticker, daemon=True).start()
        print(f"   📡 Real-time ticker starting...")
    else:
        print(f"   ⚠  No token  →  login via dashboard")
    print("─"*W)
    print("   Keep this window OPEN while trading.\n")
    app.run(port=5001, debug=False, host="127.0.0.1", threaded=True)
