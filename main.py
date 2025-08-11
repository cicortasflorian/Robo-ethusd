# main.py — ROBO1 (Capital.com compat) : ETHUSD RSI cross
# rulează ca Background Worker pe Render

import time, json, requests
from datetime import datetime, timedelta, timezone

# ---------- Config din ./env ----------
def load_env():
    cfg = {}
    try:
        with open("env", "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg

CFG = load_env()

# !! COMPLETEAZĂ în env (NU aici):
# API_IDENTIFIER = emailul tău de login Capital.com
# API_KEY        = cheia API nouă (din API integrations, cu Trade permission)
# API_PASSWORD   = parola setată la crearea cheii API

API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")  # emailul de login
API_KEY        = CFG.get("API_KEY", "")
API_PASSWORD   = CFG.get("API_PASSWORD", "")

SYMBOL         = CFG.get("SYMBOL","ETHUSD").replace("/", "")
TIMEFRAME      = CFG.get("TIMEFRAME","10m")
RSI_PERIOD     = int(CFG.get("RSI_PERIOD", 3))
INVEST_AMOUNT  = float(CFG.get("INVEST_AMOUNT", 10))
MAX_SPREAD     = float(CFG.get("MAX_SPREAD_PERCENT", 0.7))
MIN_NET_PROFIT = float(CFG.get("MIN_NET_PROFIT_PERCENT", 0.5))
BUY_LVL        = float(CFG.get("BUY_CROSS_LEVEL", 28))
SELL_LVL       = float(CFG.get("SELL_CROSS_LEVEL", 72))

# endpoint LIVE (nu demo)
BASE = "https://api-capital.backend-capital.com"

CST = None
XSEC = None
EPIC_CACHE = {}

# ---------- util ----------
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def mask(s, keep=3):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s)-keep)

def dbg(msg):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg):
    print(f"{now()} [ROBO1] {msg}", flush=True)

# ---------- sesiune login ----------
def login_session():
    """
    Capital.com REST:
      POST /api/v1/session
      Headers: X-CAP-API-KEY
      Body: { "identifier": <email>, "password": <api_password>, "type": "password" }
      Răspuns: headers CST și X-SECURITY-TOKEN
    """
    global CST, XSEC

    # log lungimi/valori mascate pentru debug 401
    dbg(f"API_IDENTIFIER length={len(API_IDENTIFIER)} value='{API_IDENTIFIER}'")
    dbg(f"API_KEY length={len(API_KEY)} value='{mask(API_KEY)}'")
    dbg(f"API_PASSWORD length={len(API_PASSWORD)} value='{mask(API_PASSWORD)}'")

    url = f"{BASE}/api/v1/session"
    headers = {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY
    }
    payload = {
        "identifier": API_IDENTIFIER,
        "password": API_PASSWORD,
        "type": "password"
    }

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)

    dbg(f"Login HTTP {r.status_code}")
    try:
        dbg(f"Login body: {r.text[:500]}")
    except Exception:
        pass

    # Important: serverul trimite token-urile în headers
    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")

    dbg(f"Headers CST='{CST}' X-SECURITY-TOKEN='{XSEC}'")

    if r.status_code != 200 or not CST or not XSEC:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    info("Logged in OK")
    return True

# ---------- market utils (simplificat) ----------
def auth_headers():
    if not CST or not XSEC:
        raise RuntimeError("No session tokens")
    return {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
    }

def get_epic(symbol):
    """Cache pentru EPIC-ul pieței. Încercăm lookup by symbol."""
    if symbol in EPIC_CACHE:
        return EPIC_CACHE[symbol]

    url = f"{BASE}/api/v1/markets?searchTerm={symbol}"
    r = requests.get(url, headers=auth_headers(), timeout=30)
    dbg(f"markets lookup {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        for it in data.get("markets", []):
            if it.get("instrumentName","").replace("/","") == symbol:
                EPIC_CACHE[symbol] = it.get("epic")
                break

    epic = EPIC_CACHE.get(symbol)
    if not epic:
        raise RuntimeError(f"EPIC not found for {symbol}")
    return epic

def fetch_prices(epic, tf="10m", n=200):
    # Capital.com: /api/v1/prices/{epic}/{resolution}?max={n}
    url = f"{BASE}/api/v1/prices/{epic}/{tf}?max={n}"
    r = requests.get(url, headers=auth_headers(), timeout=30)
    dbg(f"prices {tf} {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"prices failed: {r.status_code} {r.text}")
    js = r.json()
    # luăm close din "prices"
    closes = []
    for p in js.get("prices", []):
        # preferăm mid close dacă există; fallback bid/ask
        c = p.get("closePrice", {}).get("mid")
        if c is None:
            bid = p.get("closePrice", {}).get("bid")
            ask = p.get("closePrice", {}).get("ask")
            if bid is not None and ask is not None:
                c = (bid + ask) / 2.0
        if c is not None:
            closes.append(float(c))
    return closes

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    # medii mobile simple
    import math
    if len(gains) < period or len(losses) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# ---------- trading (demo logic simplu RSI cross) ----------
def can_trade_spread_ok(epic):
    # citim market details ca să verificăm spreadul curent
    url = f"{BASE}/api/v1/markets/{epic}"
    r = requests.get(url, headers=auth_headers(), timeout=30)
    if r.status_code != 200:
        dbg(f"market details fail {r.status_code}")
        return True  # nu blocăm dacă nu putem verifica
    js = r.json()
    spread = js.get("snapshot", {}).get("offer", 0) - js.get("snapshot", {}).get("bid", 0)
    mid = js.get("snapshot", {}).get("mid", 0) or (js.get("snapshot", {}).get("offer",0)+js.get("snapshot", {}).get("bid",0))/2.0
    if mid <= 0:
        return True
    spread_pct = 100.0 * spread / mid
    dbg(f"spread%={spread_pct:.4f}")
    return spread_pct <= MAX_SPREAD

def place_market(epic, direction, size):
    # Place order — simplified (verifică permisiunile la cheie: “Trade”)
    url = f"{BASE}/api/v1/positions"
    payload = {
        "epic": epic,
        "direction": direction.upper(),  # "BUY" / "SELL"
        "size": float(size)
    }
    r = requests.post(url, headers=auth_headers(), data=json.dumps(payload), timeout=30)
    dbg(f"place {direction} {r.status_code} {r.text[:300]}")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"order failed {r.status_code}: {r.text}")
    return r.json()

# ---------- main ----------
def main():
    info(f"START | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={RSI_PERIOD} BUY={BUY_LVL} SELL={SELL_LVL} MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")
    # 1) login
    login_session()

    # 2) epic
    epic = get_epic(SYMBOL)
    info(f"EPIC for {SYMBOL}: {epic}")

    # 3) loop
    while True:
        try:
            # refresh token la 25 min (tokenurile Capital.com expiră)
            # simplu: relogin din 25 în 25 de minute
            if datetime.utcnow().minute % 25 == 0 and datetime.utcnow().second < 5:
                dbg("refresh session")
                login_session()

            closes = fetch_prices(epic, TIMEFRAME, n=200)
            rsi_val = rsi(closes, RSI_PERIOD)
            dbg(f"RSI={None if rsi_val is None else round(rsi_val,2)} closes={len(closes)}")

            if rsi_val is not None and can_trade_spread_ok(epic):
                if rsi_val <= BUY_LVL:
                    info(f"BUY signal RSI={rsi_val:.2f}")
                    # exemplu: mărime mică
                    # place_market(epic, "BUY", INVEST_AMOUNT)
                elif rsi_val >= SELL_LVL:
                    info(f"SELL signal RSI={rsi_val:.2f}")
                    # place_market(epic, "SELL", INVEST_AMOUNT)
            time.sleep(10)
        except Exception as e:
            dbg(f"loop error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
