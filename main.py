import time
import json
import requests
from datetime import datetime, timezone, timedelta

# =============== utilități mici =================
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def mask(s: str, keep: int = 3) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

def dbg(msg: str):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg: str):
    print(f"{now()} [ROBO]  {msg}", flush=True)

# =============== citire config din ./env =================
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

API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")     # emailul cu care intri în Capital
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

# =============== sesiune login =================
def login_session():
    """
    Capital.com REST
      POST /api/v1/session
      Headers: X-CAP-API-KEY
      Body: { "identifier": <email>, "password": <api password>, "type": "password" }
      Răspuns: headers CST și X-SECURITY-TOKEN
    """
    global CST, XSEC

    # log lungimi/valori mascate pentru debug
    dbg(f"API_IDENTIFIER length={len(API_IDENTIFIER)} value='{API_IDENTIFIER}'")
    dbg(f"API_KEY       length={len(API_KEY)} value='{mask(API_KEY)}'")
    dbg(f"API_PASSWORD  length={len(API_PASSWORD)} value='{mask(API_PASSWORD)}'")

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
    # Pentru transparență la 401: afișăm body-ul răspunsului (nu conține secretele noastre)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")

    if not CST or not XSEC:
        raise RuntimeError("Login ok dar lipsesc header-ele CST/X-SECURITY-TOKEN.")

    info("LOGIN OK (am primit CST & X-SECURITY-TOKEN)")

def auth_headers():
    if not CST or not XSEC:
        raise RuntimeError("Nu există sesiune încă. Apelează login_session() mai întâi.")
    return {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC
    }

# =============== helper request JSON cu retry ===============
def get_json(url, params=None, retries=3, sleep_s=1.5):
    last = None
    for i in range(retries):
        r = requests.get(url, headers=auth_headers(), params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        last = f"{r.status_code} {r.text}"
        time.sleep(sleep_s)
    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: {last}")

# =============== listare simboluri după căutare ===============
def search_symbols(q: str):
    """
    Listează instrumentele care conțin textul căutat (ex: 'ETH', 'ETHUSD').
    """
    url = f"{BASE}/api/v1/instruments"
    data = get_json(url, params={"search": q})
    items = data.get("instruments") or data.get("markets") or data.get("data") or []

    if not isinstance(items, list):
        # unele răspunsuri pot fi de forma {"instruments": {"values":[...]}}
        for key in ("instruments", "markets", "data"):
            if isinstance(data.get(key), dict):
                maybe = data[key].get("values") or data[key].get("items")
                if isinstance(maybe, list):
                    items = maybe
                    break

    if not items:
        info(f"NU am găsit nimic pentru '{q}'")
        return

    info(f"Am găsit {len(items)} rezultate pentru '{q}':")
    count = 0
    for it in items:
        epic     = it.get("epic") or it.get("id") or ""
        symbol   = it.get("symbol") or it.get("ticker") or ""
        name     = it.get("name") or it.get("description") or ""
        provider = it.get("provider") or it.get("providerName") or ""
        print(f"{now()} [INFO]  - epic={epic} | symbol={symbol} | name={name} | provider={provider}", flush=True)
        count += 1
        if count >= 200:  # să nu inundăm logul dacă lista e uriașă
            print(f"{now()} [INFO]  (…trunchiat la {count} rezultate)", flush=True)
            break

# =============== MAIN =================
def main():
    info(f"START | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={RSI_PERIOD} "
         f"BUY={BUY_LVL} SELL={SELL_LVL} MAX_SPREAD%={MAX_SPREAD} "
         f"MIN_NET_PROFIT%={MIN_NET_PROFIT}")

    # 1) Login
    login_session()

    # 2) Căutăm întâi ‘ETH’ (larg), apoi țintit ‘ETHUSD’
    info("Caut instrumente care conțin 'ETH'…")
    search_symbols("ETH")

    print("")  # separare mică în log
    info("Caut instrumente care conțin 'ETHUSD'…")
    search_symbols("ETHUSD")

    info("Gata listarea. Alege varianta corectă a simbolului din log și pune-o în fișierul 'env' la SYMBOL=…")
    info("După ce confirmăm simbolul, îți trimit versiunea curățată a codului (fără partea de căutare).")

if __name__ == "__main__":
    main()
