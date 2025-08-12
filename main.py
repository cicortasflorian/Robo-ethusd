import time
import json
import requests
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

API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")  # emailul cu care intri în Capital
API_KEY        = CFG.get("API_KEY", "")
API_PASSWORD   = CFG.get("API_PASSWORD", "")
SYMBOL         = CFG.get("SYMBOL", "ETHUSD").replace("/", "")
TIMEFRAME      = CFG.get("TIMEFRAME", "10m")
RSI_PERIOD     = int(CFG.get("RSI_PERIOD", 3))
INVEST_AMOUNT  = float(CFG.get("INVEST_AMOUNT", 10))
MAX_SPREAD     = float(CFG.get("MAX_SPREAD_PERCENT", 0.7))
MIN_NET_PROFIT = float(CFG.get("MIN_NET_PROFIT_PERCENT", 0.5))
BUY_LVL        = float(CFG.get("BUY_CROSS_LEVEL", 28))
SELL_LVL       = float(CFG.get("SELL_CROSS_LEVEL", 72))

BASE = "https://api-capital.backend-capital.com"
CST = None
XSEC = None
EPIC_CACHE = {}


# ---------- utilitare log ----------
def now():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def mask(s, keep=3):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

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
      Body: { "identifier": <email>, "password": <pass>, "type": "password" }
      Răspuns: headers CST și X-SECURITY-TOKEN
    """
    global CST, XSEC

    # log lungimi/valori mascate pentru debug
    dbg(f"API_IDENTIFIER length={len(API_IDENTIFIER)} value='{API_IDENTIFIER}'")
    dbg(f"API_KEY length={len(API_KEY)}")
    dbg(f"API_PASSWORD length={len(API_PASSWORD)}")

    url = f"{BASE}/api/v1/session"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-CAP-API-KEY": API_KEY
    }
    payload = {
        "identifier": API_IDENTIFIER,
        "password": API_PASSWORD,
        "type": "password"
    }

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")
    if not CST or not XSEC:
        raise RuntimeError("Login OK, dar lipsesc token-urile CST/X-SECURITY-TOKEN")

    info("Login OK (CST & X-SECURITY-TOKEN primite)")


# ---------- GET JSON cu retry ----------
def get_json(url, params=None, retries=3):
    if not (CST and XSEC):
        raise RuntimeError("Nu există sesiune activă (CST/XSEC)")

    headers = {
        "Accept": "application/json",
        "X-SECURITY-TOKEN": XSEC,
        "CST": CST
    }

    last_text = ""
    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=20)
            last_text = r.text
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return json.loads(last_text)
            # 404 / 400 / etc – încercăm fallback
            dbg(f"GET {url} încercarea {i} -> {r.status_code}")
        except Exception as e:
            dbg(f"GET {url} încercarea {i} a aruncat: {e}")
        time.sleep(0.6)

    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: "
                       f"{r.status_code if 'r' in locals() else '?'} {last_text}")


# ---------- extragere EPIC din răspunsuri variate ----------
def _pick_epic_from_any(data, wanted_symbol="ETHUSD"):
    """
    Acceptă diferite formate de răspuns:
      - listă de instrumente
      - {"instruments": [...]}
      - {"markets": [...]}
      - {"values": [...]}
    Caută în fiecare item câmpurile uzuale: symbol, epic, name.
    """
    if data is None:
        return None

    # normalizează la listă
    if isinstance(data, dict):
        for key in ("instruments", "markets", "values", "data"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break

    if not isinstance(data, list):
        return None

    for it in data:
        try:
            symbol = (it.get("symbol") or it.get("ticker") or it.get("instrument") or "").upper()
            name   = it.get("name") or it.get("description") or ""
            epic   = it.get("epic") or it.get("id") or it.get("marketId") or it.get("instrumentId")
            dbg(f"cand: epic={epic} | symbol={symbol} | name={name} | provider={it.get('provider')}")
            if symbol == wanted_symbol and epic:
                return epic
        except Exception:
            continue
    return None


# ---------- căutare EPIC pentru ETHUSD cu fallback pe 4 rute ----------
def get_ethusd_epic():
    """
    Încearcă mai multe endpoint-uri, pentru că pe unele conturi
    /api/v1/instruments poate răspunde 404.
    Ordine:
      1) /api/v1/instruments?search=ETHUSD
      2) /api/v1/markets?search=ETHUSD
      3) /api/v1/markets?query=ETHUSD
      4) /api/v1/instruments?query=ETHUSD
    Se oprește la primul răspuns valid care conține un EPIC pentru ETHUSD.
    """
    routes = [
        (f"{BASE}/api/v1/instruments", {"search": SYMBOL}),
        (f"{BASE}/api/v1/markets",     {"search": SYMBOL}),
        (f"{BASE}/api/v1/markets",     {"query":  SYMBOL}),
        (f"{BASE}/api/v1/instruments", {"query":  SYMBOL}),
    ]

    for url, params in routes:
        try:
            data = get_json(url, params=params, retries=3)
            epic = _pick_epic_from_any(data, wanted_symbol=SYMBOL)
            if epic:
                info(f"EPIC pentru {SYMBOL}: {epic} (ruta {url})")
                return epic
        except Exception as e:
            dbg(f"Fallback next: {url} -> {e}")

    # dacă nu am găsit, arătăm primele câteva rezultate brute ca debug
    try:
        sample = data[:3] if isinstance(data, list) else []
    except Exception:
        sample = []
    raise RuntimeError(f"EPIC pentru {SYMBOL} nu a putut fi găsit prin rutele fallback. "
                       f"Exemplu răspuns: {sample}")


# ---------- main ----------
def main():
    info(f"START | SYMBOL={SYMBOL}")
    login_session()
    epic = EPIC_CACHE.get(SYMBOL)
    if not epic:
        epic = get_ethusd_epic()
        EPIC_CACHE[SYMBOL] = epic

    info(f"Ready: SYMBOL={SYMBOL} | EPIC={epic}")
    # Aici poți continua cu logica de trading (RSI, ordine, etc.)
    # Eu las un loop “doar să stea” ca worker-ul să rămână în viață.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
