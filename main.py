import time, json, requests
from datetime import datetime, timezone, timedelta

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

API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")   # email Capital.com
API_KEY        = CFG.get("API_KEY", "")
API_PASSWORD   = CFG.get("API_PASSWORD", "")
SYMBOL         = CFG.get("SYMBOL", "ETHUSD").replace("/", "")
TIMEFRAME      = CFG.get("TIMEFRAME", "10m")

BASE = "https://api-capital.backend-capital.com"
CST = None
XSEC = None

# ---------- util ----------
def now():
    return datetime.now(timezone(timedelta(hours=0))).strftime("%Y-%m-%d %H:%M:%S")

def mask(s, keep=3):
    if not s: return ""
    if len(s) <= keep: return "*" * len(s)
    return s[:keep] + "*" * (len(s)-keep)

def dbg(msg):
    print(f"{now()} [DEBUG] {msg}", flush=True)

def info(msg):
    print(f"{now()} [ROBO1] {msg}", flush=True)

def warn(msg):
    print(f"{now()} [WARN] {msg}", flush=True)

def err(msg):
    print(f"{now()} [ERROR] {msg}", flush=True)

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

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")

    if not CST or not XSEC:
        raise RuntimeError("Login OK, dar lipsesc token-urile CST/X-SECURITY-TOKEN")

    info("Login OK (CST & X-SECURITY-TOKEN)")
    # opțional, afisăm puține info de cont ca sanity check
    try:
        acc = get_json(f"{BASE}/api/v1/accounts", auth=True, retries=1)
        dbg(json.dumps(acc, ensure_ascii=False)[:800])
    except Exception as e:
        warn(f"Nu am putut lista conturile: {e}")

# ---------- HTTP helper cu retry ----------
def get_json(url, params=None, auth=False, retries=3, sleep_s=1.0):
    headers = {"Accept": "application/json"}
    if auth:
        headers["CST"] = CST or ""
        headers["X-SECURITY-TOKEN"] = XSEC or ""
    last_text = ""
    for i in range(1, retries+1):
        r = requests.get(url, headers=headers, params=params, timeout=15)
        last_text = r.text
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                raise RuntimeError(f"JSON parse failed for {url}: {r.text[:300]}")
        else:
            dbg(f"HTTP {r.status_code} for {url} | try {i}/{retries}")
            time.sleep(sleep_s)
    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: {last_text}")

# ---------- căutare EPIC pentru simbol ----------
def find_epic_in_payload(data, wanted):
    """
    Caută EPIC într-o colecție de obiecte necunoscute.
    Acceptă chei: 'symbol', 'epic', 'id', 'name' etc.
    Returnează (epic, match_obj) sau (None, None)
    """
    if not isinstance(data, list):
        return (None, None)

    wanted_norm = wanted.replace("/", "").upper()

    for it in data:
        if not isinstance(it, dict):
            continue
        sym = (it.get("symbol") or it.get("Symbol") or it.get("marketCode") or it.get("code") or it.get("ticker") or "").replace("/", "").upper()
        nm  = (it.get("name") or it.get("Name") or it.get("description") or "")
        epic = it.get("epic") or it.get("EPIC") or it.get("id") or it.get("marketId")

        if sym == wanted_norm or nm.strip().replace(" ", "").upper() == wanted_norm:
            return (epic, it)

        # fallback: dacă simbolul dorit e “ETHUSD”, acceptăm și variante care conțin ETH și USD
        if wanted_norm == "ETHUSD" and "ETH" in sym and "USD" in sym:
            return (epic, it)

    return (None, None)

def get_epic_by_trying_endpoints(symbol):
    """
    Încearcă pe rând endpoint-uri posibile.
    """
    candidates = [
        ("/api/v1/instruments", {"search": symbol}),
        ("/api/v1/instruments/", {"search": symbol}),
        ("/api/v1/symbols", {"name": symbol}),
        ("/api/v1/symbols/", {"name": symbol}),
        ("/api/v1/markets", {"search": symbol}),
        ("/api/v1/markets/", {"search": symbol}),
    ]

    last_err = None
    for path, params in candidates:
        url = f"{BASE}{path}"
        try:
            dbg(f"Caut '{symbol}' la {url} cu params={params}")
            data = get_json(url, params=params, auth=True, retries=3)
            epic, obj = find_epic_in_payload(data, symbol)
            if epic:
                info(f"Găsit EPIC='{epic}' pentru SYMBOL='{symbol}' la {path}")
                dbg(f"match sample: {json.dumps(obj, ensure_ascii=False)[:600]}")
                return epic
            else:
                # arată un eșantion din răspuns, ca să vedem formatul
                sample = data[:3] if isinstance(data, list) else data
                warn(f"Nu am găsit EPIC în {path}. Sample: {json.dumps(sample, ensure_ascii=False)[:800]}")
        except Exception as e:
            last_err = e
            warn(str(e))
            continue

    if last_err:
        raise last_err
    raise RuntimeError(f"EPIC pentru {symbol} nu a putut fi găsit pe niciun endpoint candidat")

# ---------- main ----------
def main():
    info(f"START | SYMBOL={SYMBOL}")
    login_session()
    epic = get_epic_by_trying_endpoints(SYMBOL)

    # Aici te-ai putea opri sau continua cu logica ta de trading.
    info(f"READY | EPIC={epic} | (de aici ai putea continua cu subscribe/quotes/trade)")

if __name__ == "__main__":
    main()
