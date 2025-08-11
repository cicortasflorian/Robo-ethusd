import time, json, requests
from datetime import datetime, timedelta, timezone

# ---------- util ----------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def mask(s, keep=3):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep - keep) + s[-keep:]

def dbg(msg):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg):
    print(f"{now()} [ROBO1] {msg}", flush=True)

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

API_IDENTIFIER  = CFG.get("API_IDENTIFIER", "")   # emailul cu care intri în Capital
API_KEY         = CFG.get("API_KEY","")
API_PASSWORD    = CFG.get("API_PASSWORD","")

SYMBOL          = CFG.get("SYMBOL","ETHUSD").replace("/","").upper()
TIMEFRAME       = CFG.get("TIMEFRAME","10m")
RSI_PERIOD      = int(CFG.get("RSI_PERIOD", 3))
INVEST_AMOUNT   = float(CFG.get("INVEST_AMOUNT", 10))
MAX_SPREAD      = float(CFG.get("MAX_SPREAD_PERCENT", 0.7))
MIN_NET_PROFIT  = float(CFG.get("MIN_NET_PROFIT_PERCENT", 0.5))
BUY_LVL         = float(CFG.get("BUY_CROSS_LEVEL", 28))
SELL_LVL        = float(CFG.get("SELL_CROSS_LEVEL", 72))

# Diagnostic: dacă pui în env linia FIND_EPIC=ETHUSD, rulăm doar căutarea EPIC
FIND_EPIC       = CFG.get("FIND_EPIC","").strip()

BASE = "https://api-capital.backend-capital.com"
CST = None
XSEC = None
EPIC_CACHE = {}

# ---------- login ----------
def login_session():
    """
    Capital.com REST:
      POST /api/v1/session
      Headers: X-CAP-API-KEY
      Body: { "identifier": <email>, "password": <api password>, "type":"password" }
      Răspuns: headers CST și X-SECURITY-TOKEN
    """
    global CST, XSEC

    # log lungimi/valori mascate pentru debugging rapid
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
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST  = r.headers.get("CST") or r.headers.get("cst")
    XSEC = r.headers.get("X-SECURITY-TOKEN") or r.headers.get("x-security-token")

    if not CST or not XSEC:
        raise RuntimeError("Login succeeded but missing auth tokens (CST/X-SECURITY-TOKEN)")

    dbg("Login OK, tokens received.")

# ---------- HTTP helper cu token-uri ----------
def get_json(url, params=None):
    if not (CST and XSEC):
        raise RuntimeError("Not logged in")
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC
    }
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text}")
    return r.json()

# ---------- Diagnostic: caută EPIC după text ----------
def debug_find_epic(query):
    """
    Încearcă să găsească EPIC-uri pentru un text (ex: 'ETHUSD').
    Folosim endpoint-ul de căutare piețe.
    """
    url = f"{BASE}/api/v1/markets"
    data = get_json(url, params={"search": query})
    items = data.get("markets") or data.get("content") or data.get("items") or []
    if not items:
        print(f"[DIAG] Nu am găsit nimic pentru '{query}'. Încearcă variații: 'ETH/USD', 'ETHUSD', 'ETHUSDT'.", flush=True)
        return

    print(f"[DIAG] Rezultate pentru '{query}':", flush=True)
    count = 0
    for it in items:
        epic  = it.get("epic") or it.get("EPIC") or it.get("instrumentEpic")
        sym   = (it.get("symbol") or it.get("instrumentName") or it.get("instrumentDisplayName") or "").upper()
        name  = it.get("instrumentName") or it.get("displayName") or it.get("name") or ""
        prov  = it.get("provider") or it.get("exchange") or ""
        print(f"  - epic={epic} | symbol={sym} | name={name} | provider={prov}", flush=True)
        count += 1
        if count >= 30:
            print("  (…trunchiat la 30 rezultate)", flush=True)
            break

# ---------- EPIC util ----------
def get_epic(symbol):
    # cache simplu
    if symbol in EPIC_CACHE:
        return EPIC_CACHE[symbol]

    url = f"{BASE}/api/v1/markets"
    data = get_json(url, params={"search": symbol})
    items = data.get("markets") or data.get("content") or data.get("items") or []

    target = symbol.replace("/", "").replace("-", "").upper()

    # încercăm să potrivim strict simbolul în câmpurile posibile
    for it in items:
        epic  = it.get("epic") or it.get("EPIC") or it.get("instrumentEpic")
        sym   = (it.get("symbol") or it.get("instrumentName") or it.get("instrumentDisplayName") or "").upper()
        sym_c = sym.replace("/", "").replace("-", "")
        if epic and (sym_c == target or sym == symbol.upper()):
            EPIC_CACHE[symbol] = epic
            return epic

    # dacă nu am găsit, aruncăm eroare cu un mesaj util
    raise RuntimeError(f"EPIC not found for {symbol}")

# ---------- main ----------
def main():
    info(f"START | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={RSI_PERIOD} BUY={BUY_LVL} SELL={SELL_LVL} "
         f"MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")

    login_session()

    # Mod diagnostic: doar căutăm EPIC-urile pentru un text și ieșim
    if FIND_EPIC:
        print(f"[DIAG] FIND_EPIC='{FIND_EPIC}'", flush=True)
        debug_find_epic(FIND_EPIC)
        return

    # Normal flow: găsim EPIC-ul exact pentru SYMBOL
    epic = get_epic(SYMBOL)
    info(f"EPIC for {SYMBOL} -> {epic}")

    # Aici ar urma logica ta de prețuri / RSI / ordine etc.
    # Pentru moment, ne oprim după ce am confirmat EPIC-ul.
    info("Done (diagnostic mode).")

if __name__ == "__main__":
    main()
