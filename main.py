import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ---------- citire config din ./env ----------
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

API_IDENTIFIER      = CFG.get("API_IDENTIFIER", "")   # emailul din Capital.com
API_KEY             = CFG.get("API_KEY", "")
API_PASSWORD        = CFG.get("API_PASSWORD", "")
SYMBOL              = CFG.get("SYMBOL", "ETHUSD").replace("/", "")
TIMEFRAME           = CFG.get("TIMEFRAME", "10m")
RSI_PERIOD          = int(CFG.get("RSI_PERIOD", 3))
INVEST_AMOUNT       = float(CFG.get("INVEST_AMOUNT", 10))
MAX_SPREAD          = float(CFG.get("MAX_SPREAD_PERCENT", 0.7))
MIN_NET_PROFIT      = float(CFG.get("MIN_NET_PROFIT_PERCENT", 0.5))
BUY_LVL             = float(CFG.get("BUY_CROSS_LEVEL", 28))
SELL_LVL            = float(CFG.get("SELL_CROSS_LEVEL", 72))

BASE = "https://api-capital.backend-capital.com"

# tokenurile de sesiune
CST  = None
XSEC = None

# ---------- utilitare ----------
def now():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def mask(s: str, keep: int = 3) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

def dbg(msg: str):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg: str):
    print(f"{now()} [ROBO1] {msg}", flush=True)

# ---------- request helper cu retry ----------
def get_json(url, headers=None, params=None, retries=3, timeout=20):
    last = None
    for i in range(retries):
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.ok:
            try:
                return r.json()
            except Exception:
                raise RuntimeError(f"JSON invalid de la {url}: {r.text[:500]}")
        last = (r.status_code, r.text)
        time.sleep(1 + i)
    code, txt = last or ("n/a", "")
    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: {code} {txt}")

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

    # log lungimi/valori mascate pentru debug (fără să scăpăm secretele)
    dbg(f"API_IDENTIFIER length={len(API_IDENTIFIER)} value='{API_IDENTIFIER}'")
    dbg(f"API_KEY length={len(API_KEY)} value='{mask(API_KEY)}'")
    dbg(f"API_PASSWORD length={len(API_PASSWORD)} value='{mask(API_PASSWORD)}'")

    url = f"{BASE}/api/v1/session"
    headers = {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY,
    }
    payload = {
        "identifier": API_IDENTIFIER,
        "password": API_PASSWORD,
        "type": "password"
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")

    if not CST or not XSEC:
        raise RuntimeError("Tokenele de sesiune lipsesc (CST/X-SECURITY-TOKEN).")

    dbg(f"Sesiune OK | CST len={len(CST)}, XSEC len={len(XSEC)}")

def auth_headers():
    if not CST or not XSEC:
        raise RuntimeError("Nu există sesiune activă. Apelează login_session() mai întâi.")
    return {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
    }

# ---------- căutare instrumente (endpoint corect /markets) ----------
def search_symbols(q: str):
    """
    Folosește /api/v1/markets?search=<q>
    Răspunsul conține o listă sub cheia 'markets'.
    Fiecare element are câmpuri precum: 'epic', 'symbol', 'instrumentName', 'provider', etc.
    """
    url = f"{BASE}/api/v1/markets"
    data = get_json(url, headers=auth_headers(), params={"search": q})
    markets = data.get("markets", []) if isinstance(data, dict) else []
    results = []
    for m in markets:
        results.append({
            "epic": m.get("epic"),
            "symbol": (m.get("symbol") or "").upper(),
            "name": m.get("instrumentName") or m.get("name") or "",
            "provider": m.get("provider") or "",
        })
    # log primele rezultate pentru debug
    for item in results[:30]:
        info(f"- epic={item['epic']} | symbol={item['symbol']} | name={item['name']} | provider={item['provider']}")
    return results

def get_epic(symbol: str) -> str:
    """
    Găsește EPIC pentru un 'symbol' (ex: 'ETHUSD').
    - caută direct după symbol exact (case-insensitive)
    - altfel încearcă match-uri parțiale
    - dacă nu găsește, ridică o eroare clară
    """
    s = (symbol or "").upper()
    if not s:
        raise RuntimeError("Symbol gol.")

    markets = search_symbols(s)
    # match exact pe symbol
    for m in markets:
        if (m.get("symbol") or "").upper() == s:
            return m.get("epic")

    # fallback: prima intrare care conține symbol în name/symbol
    for m in markets:
        name = (m.get("name") or "").upper()
        sym  = (m.get("symbol") or "").upper()
        if s in sym or s in name:
            return m.get("epic")

    raise RuntimeError(f"EPIC not found for {symbol}")

# ---------- punctul de intrare ----------
def main():
    info(f"START | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={RSI_PERIOD} "
         f"BUY={BUY_LVL} SELL={SELL_LVL} MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")
    login_session()
    epic = get_epic(SYMBOL)
    info(f"FOUND EPIC '{epic}' pentru SYMBOL '{SYMBOL}'")
    # aici poți continua cu restul pașilor (quotes, RSI, ordine etc.)
    # momentan oprim după validarea EPIC-ului
    return

if __name__ == "__main__":
    main()
