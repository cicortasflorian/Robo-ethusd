import time, json, requests
from datetime import datetime, timezone

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

API_IDENTIFIER      = CFG.get("API_IDENTIFIER", "")   # emailul cu care intri în Capital
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

CST = None
XSEC = None

# ---------- util ----------
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def mask(s, keep=3):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

def dbg(msg):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg):
    print(f"{now()} [ROBO] {msg}", flush=True)

# ---------- HTTP helpers ----------
def auth_headers(extra=None):
    h = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
        "X-CAP-API-KEY": API_KEY,
    }
    if CST:
        h["CST"] = CST
    if XSEC:
        h["X-SECURITY-TOKEN"] = XSEC
    if extra:
        h.update(extra)
    return h

def get_json(url, params=None, retries=3, sleep_s=1.0):
    last_text = ""
    for i in range(1, retries + 1):
        r = requests.get(url, headers=auth_headers(), params=params, timeout=20)
        if r.ok:
            try:
                return r.json()
            except Exception:
                return json.loads(r.text or "{}")
        last_text = r.text
        dbg(f"HTTP {r.status_code} for {url} | try {i}/{retries}")
        time.sleep(sleep_s)
    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: {last_text}")

# ---------- sesiune login ----------
def login_session():
    """
    Capital.com REST:
      POST /api/v1/session
      Headers: X-CAP-API-KEY
      Body: { "identifier": <email>, "password": <pwd>, "type": "password" }
      Răspuns: headers CST și X-SECURITY-TOKEN
    """
    global CST, XSEC

    # log lungimi/valori mascate pentru debug
    dbg(f"API_IDENTIFIER length={len(API_IDENTIFIER)} value='{API_IDENTIFIER}'")
    dbg(f"API_KEY length={len(API_KEY)} value='{mask(API_KEY)}'")
    dbg(f"API_PASSWORD length={len(API_PASSWORD)} value='{mask(API_PASSWORD)}'")

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
    if not r.ok:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")
    dbg(f"CST='{mask(CST, keep=2)}' | X-SECURITY-TOKEN='{mask(XSEC, keep=2)}'")

    # opțional: info despre cont, util pt. confirmare
    try:
        me = get_json(f"{BASE}/api/v1/accounts")
        print(json.dumps(me, ensure_ascii=False))
    except Exception as e:
        dbg(f"accounts error: {e}")

    if not CST or not XSEC:
        raise RuntimeError("Login OK, dar lipsesc token-urile (CST/X-SECURITY-TOKEN)")

    info("Login OK (CST & X-SECURITY-TOKEN)")

# ---------- căutare EPIC pentru SYMBOL ----------
def get_ethusd_epic():
    """
    Caută EPIC pentru SYMBOL folosind /instruments.
    Dacă 404, încearcă /instruments/ și apoi /symbols.
    """
    urls = [
        f"{BASE}/api/v1/instruments",
        f"{BASE}/api/v1/instruments/",
        f"{BASE}/api/v1/symbols",
    ]
    data = None
    last_err = None
    dbg(f"Caut '{SYMBOL}' în /instruments …")

    for u in urls:
        try:
            data = get_json(u, params={"search": SYMBOL})
            break
        except RuntimeError as e:
            last_err = e
            continue

    if data is None:
        raise last_err

    # răspunsul e o listă; găsim exact SYMBOL
    for it in data:
        try:
            symbol = it.get("symbol")
            if symbol == SYMBOL:
                epic = it.get("epic") or it.get("id")
                name = it.get("name") or ""
                dbg(f"match: epic={epic} | symbol={symbol} | name={name}")
                if epic:
                    return epic
        except Exception:
            continue

    # dacă nu am găsit, arătăm câteva prime elemente ca diagnostic
    sample = data[:3] if isinstance(data, list) else data
    raise RuntimeError(f"EPIC pentru {SYMBOL} nu a fost găsit. Sample: {sample}")

# ---------- main ----------
def main():
    info(f"START | SYMBOL={SYMBOL} | TF={TIMEFRAME} RSI={RSI_PERIOD} "
         f"BUY={BUY_LVL} SELL={SELL_LVL} MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")

    login_session()

    epic = get_ethusd_epic()
    info(f"EPIC găsit pentru {SYMBOL}: {epic}")

    # Aici ai deja EPIC-ul. Poți continua cu restul strategiei/fluxului tău.
    # Pentru moment, doar dormim ca workerul să rămână „Live”.
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()    
