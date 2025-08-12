# main.py
import time
import json
import requests
from datetime import datetime, timezone

# ------------- utilitare -------------
BASE = "https://api-capital.backend-capital.com"

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def mask(s, keep=3):
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

def dbg(msg):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg):
    print(f"{now()} [ROBO]  {msg}", flush=True)

# ------------- Config din ./env -------------
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

# chei de interes (poți adăuga altele în env fără să umbli în cod)
API_IDENTIFIER   = CFG.get("API_IDENTIFIER", "")   # emailul cu care intri în Capital
API_KEY          = CFG.get("API_KEY", "")
API_PASSWORD     = CFG.get("API_PASSWORD", "")
SYMBOL           = CFG.get("SYMBOL", "ETHUSD")
PROVIDER_FILTER  = CFG.get("PROVIDER", "").strip()      # opțional (ex. "Capital.com" sau "CAPITALCOMSB")
DIRECT_EPIC      = CFG.get("DIRECT_EPIC", "").strip()   # dacă îl știi, sari căutarea
# restul parametrilor de strategie rămân în env (nu ne trebuie aici pentru login/căutare)

info(f"START | SYMBOL={SYMBOL}")

CST = None
XSEC = None

# ------------- HTTP helper cu retry -------------
def get_json(url, method="GET", headers=None, params=None, data=None, retries=3, sleep_s=1):
    for i in range(1, retries + 1):
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=20)
            else:
                r = requests.post(url, headers=headers, data=data, timeout=20)

            if r.status_code == 200:
                if r.text.strip() == "":
                    return None
                return r.json()
            else:
                last_text = r.text
                dbg(f"HTTP {r.status_code} for {url} | try {i}/{retries}")
        except Exception as e:
            last_text = str(e)
            dbg(f"Exception for {url} | try {i}/{retries}: {e}")

        time.sleep(sleep_s)

    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: {last_text}")

# ------------- sesiune login -------------
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

    # nu folosim get_json aici ca să putem inspecta header-ele răspunsului
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=25)

    dbg(f"login response status={r.status_code}")
    dbg(f"login response text={r.text}")

    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    # header-ele critice
    CST = r.headers.get("CST") or r.headers.get("cst")
    XSEC = r.headers.get("X-SECURITY-TOKEN") or r.headers.get("x-security-token")

    dbg(f"CST='{mask(CST)}' | X-SECURITY-TOKEN='{mask(XSEC)}'")

    if not CST or not XSEC:
        raise RuntimeError("Login OK, dar lipsesc CST sau X-SECURITY-TOKEN")
    info("Login OK (CST & X-SECURITY-TOKEN)")

def auth_headers():
    """Headerele pentru apeluri după login."""
    return {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
    }

# ------------- căutare EPIC -------------
def normalize_symbol(s: str) -> str:
    if not s:
        return ""
    s = s.upper().replace(" ", "")
    # normalizează variante comune
    s = s.replace("/", "").replace("-", "")
    return s

def candidate_queries(sym: str):
    base = sym.strip()
    yield base
    # câteva variații utile
    yield base.replace("/", "")
    yield base.replace("-", "")
    yield base.replace(" ", "")
    if base.upper() == "ETHUSD":
        # câteva alternative posibile
        for alt in ["ETH/USD", "ETH US D", "ETH-USD", "ETHUSD", "ETH W USD", "ETHWUSD", "ETH W/USD", "Ethereum USD", "Ethereum / USD"]:
            yield alt

def find_epic(symbol: str):
    """
    Caută EPIC pentru simbolul dat folosind /api/v1/instruments?search=...
    Încearcă câteva variante și filtrează providerul dacă e setat.
    Dacă DIRECT_EPIC e în env, îl returnează direct.
    """
    if DIRECT_EPIC:
        info(f"Folosesc DIRECT_EPIC din .env: {DIRECT_EPIC}")
        return DIRECT_EPIC

    url = f"{BASE}/api/v1/instruments"
    wanted_norm = normalize_symbol(symbol)

    headers = auth_headers()

    for q in candidate_queries(symbol):
        info(f"Caut '{q}' în /instruments …")
        data = get_json(url, headers=headers, params={"search": q}, retries=3, sleep_s=1)

        if not isinstance(data, list):
            dbg(f"Răspuns neașteptat (nu e listă).")
            continue

        # log câteva rezultate pentru debug
        preview = data[:10]
        for it in preview:
            sym = it.get("symbol")
            epic = it.get("epic")
            name = it.get("name")
            prov = it.get("provider")
            dbg(f"- epic={epic} | symbol={sym} | name={name} | provider={prov}")

        # întâi încercăm potrivire strictă pe symbol normalizat
        for it in data:
            sym = normalize_symbol(it.get("symbol"))
            prov = (it.get("provider") or "").strip()
            if sym == wanted_norm and (not PROVIDER_FILTER or prov == PROVIDER_FILTER):
                epic = it.get("epic")
                if epic:
                    info(f"Match strict: symbol={it.get('symbol')} | provider={prov} | epic={epic}")
                    return epic

        # apoi încercăm potrivire lejeră (conține ETH + USD)
        for it in data:
            sym_raw = it.get("symbol") or ""
            sym = normalize_symbol(sym_raw)
            prov = (it.get("provider") or "").strip()
            if "ETH" in sym and "USD" in sym and (not PROVIDER_FILTER or prov == PROVIDER_FILTER):
                epic = it.get("epic")
                if epic:
                    info(f"Match lejer: symbol={sym_raw} | provider={prov} | epic={epic}")
                    return epic

    # dacă nu am găsit, ridicăm eroare și arătăm un eșantion
    raise RuntimeError(f"EPIC pentru {symbol} nu a fost găsit după mai multe încercări.")

# ------------- main -------------
def main():
    login_session()

    try:
        epic = find_epic(SYMBOL)
        info(f"EPIC găsit pentru {SYMBOL}: {epic}")
        # aici, dacă vrei, continui cu strategia/orderele pe 'epic'
        # deocamdată doar dormim ca să păstrăm workerul viu
        while True:
            time.sleep(30)
    except RuntimeError as e:
        info(str(e))
        # Nu oprim imediat; doar așteptăm ca să poți vedea logurile
        time.sleep(120)

if __name__ == "__main__":
    main()
