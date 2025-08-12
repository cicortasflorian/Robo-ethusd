import json
import time
import requests
from datetime import datetime, timezone

# ------------ citire config din ./env ------------
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

API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")  # emailul de login Capital
API_KEY        = CFG.get("API_KEY", "")
API_PASSWORD   = CFG.get("API_PASSWORD", "")
SYMBOL         = (CFG.get("SYMBOL", "ETHUSD") or "ETHUSD").replace("/", "")

BASE = "https://api-capital.backend-capital.com"

CST  = None
XSEC = None

# ------------ utilitare log ------------
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def info(msg):
    print(f"{now()} [ROBO1] {msg}", flush=True)

def dbg(msg):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def mask(s, keep=3):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

# ------------ apeluri HTTP ------------
def get_json(url, params=None, retries=3, sleep_s=0.8):
    """GET cu token-urile de sesiune şi retry simplu."""
    global CST, XSEC
    headers = {
        "Accept": "application/json",
        "X-SECURITY-TOKEN": XSEC or "",
        "CST": CST or "",
    }
    last_text = ""
    for i in range(1, retries + 1):
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        last_text = r.text
        time.sleep(sleep_s)
    raise RuntimeError(f"GET {url} a eşuat după {retries} încercări: "
                       f"{r.status_code} {last_text}")

def post_json(url, payload, headers=None):
    h = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    }
    if headers:
        h.update(headers)
    r = requests.post(url, data=json.dumps(payload), headers=h, timeout=20)
    return r

# ------------ login ------------
def login_session():
    """
    Capital.com REST:
      POST /api/v1/session
      Headers:  X-CAP-API-KEY
      Body:     {"identifier": <email>, "password": <api_password>, "type":"password"}
      Răspuns:  headere CST + X-SECURITY-TOKEN
    """
    global CST, XSEC

    # log lungimi (valorile sunt mascate)
    dbg(f"API_IDENTIFIER length={len(API_IDENTIFIER)} value='{API_IDENTIFIER}'")
    dbg(f"API_KEY length={len(API_KEY)} value='{mask(API_KEY)}'")
    dbg(f"API_PASSWORD length={len(API_PASSWORD)} value='{mask(API_PASSWORD)}'")

    url = f"{BASE}/api/v1/session"
    headers = {"X-CAP-API-KEY": API_KEY}
    payload = {
        "identifier": API_IDENTIFIER,
        "password": API_PASSWORD,
        "type": "password",
    }

    r = post_json(url, payload, headers=headers)
    if r.status_code != 200:
        # arătăm textul brut ca să vedem codul de eroare de la Capital
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")

    if not CST or not XSEC:
        raise RuntimeError("Login OK, dar lipsesc headerele CST/X-SECURITY-TOKEN")

    info("Login OK (CST & X-SECURITY-TOKEN setate).")

# ------------ căutare EPIC pentru ETHUSD ------------
def get_ethusd_epic():
    """
    Foloseşte endpointul corect /api/v1/instruments cu param 'search'.
    Filtrăm după symbol exact 'ETHUSD' şi luăm primul EPIC.
    """
    url = f"{BASE}/api/v1/instruments"
    data = get_json(url, params={"search": SYMBOL})

    # răspunsul e o listă de instrumente; căutăm symbol == ETHUSD
    for it in data:
        try:
            symbol = (it.get("symbol") or "").replace("/", "")
            if symbol == "ETHUSD":
                epic = it.get("epic") or ""
                name = it.get("name") or ""
                dbg(f"match: epic={epic} | symbol={symbol} | name={name}")
                if epic:
                    return epic
        except Exception:
            continue

    # dacă nu am găsit, arătăm câteva prime intrări pentru debug
    sample = data[:3] if isinstance(data, list) else data
    raise RuntimeError(f"EPIC pentru ETHUSD nu a fost găsit. Mostră răspuns: {sample}")

# ------------ main ------------
def main():
    info(f"START | SYMBOL={SYMBOL}")

    login_session()

    epic = get_ethusd_epic()
    info(f"EPIC pentru ETHUSD: {epic}")

    # aici poţi continua cu logica de preţuri / plasare ordine, folosind EPIC-ul
    # momentan doar dormim in buclă ca să ţinem workerul viu
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
