import time, json, requests
from datetime import datetime, timezone

# ---------- helpers ----------

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def mask(s: str, keep: int = 3) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(0, len(s) - keep)

def dbg(msg):  print(f"{now()}  [DEBUG] {msg}", flush=True)
def info(msg): print(f"{now()}  [ROBO]  {msg}", flush=True)
def warn(msg): print(f"{now()}  [WARN]  {msg}", flush=True)

# ---------- config din ./env ----------

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

API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")  # emailul din Capital
API_KEY        = CFG.get("API_KEY", "")
API_PASSWORD   = CFG.get("API_PASSWORD", "")
SYMBOL         = CFG.get("SYMBOL", "ETHUSD").replace("/", "")
TIMEFRAME      = CFG.get("TIMEFRAME", "10m")
RSI_PERIOD     = int(CFG.get("RSI_PERIOD", 3))

BASE = "https://api-capital.backend-capital.com"
CST = None
XSEC = None

# ---------- HTTP cu retry ----------

def get_json(url, headers=None, params=None, retries=3, timeout=15):
    last_text = ""
    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return {"_raw": r.text}
            last_text = r.text
            dbg(f"HTTP {r.status_code} for {url} | try {i}/{retries}")
        except Exception as e:
            last_text = str(e)
            dbg(f"HTTP exception {e} | try {i}/{retries}")
        time.sleep(0.8)
    raise RuntimeError(f"GET {url} a eșuat după {retries} încercări: {last_text}")

def post_json(url, headers=None, json_body=None, retries=2, timeout=20):
    last_text = ""
    for i in range(1, retries + 1):
        try:
            r = requests.post(url, headers=headers, json=json_body, timeout=timeout)
            if r.status_code in (200, 201):
                try:
                    return r.json(), r.headers
                except Exception:
                    return {"_raw": r.text}, r.headers
            last_text = r.text
            dbg(f"HTTP {r.status_code} for POST {url} | try {i}/{retries}")
        except Exception as e:
            last_text = str(e)
            dbg(f"POST exception {e} | try {i}/{retries}")
        time.sleep(0.8)
    raise RuntimeError(f"POST {url} a eșuat după {retries} încercări: {last_text}")

# ---------- login ----------

def login_session():
    """
    Capital.com REST:
      POST /api/v1/session
      Headers: X-CAP-API-KEY
      Body: { identifier: <email>, password: <api_password>, type: "password" }
      Răspuns: headerele CST și X-SECURITY-TOKEN + JSON cu info cont
    """
    global CST, XSEC

    # log lungimi/valori mascate
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
        "type": "password",
    }

    data, resp_headers = post_json(url, headers=headers, json_body=payload)
    # extrage token-urile din headere
    CST = resp_headers.get("CST") or resp_headers.get("cst")
    XSEC = resp_headers.get("X-SECURITY-TOKEN") or resp_headers.get("x-security-token")

    if not CST or not XSEC:
        raise RuntimeError("Login OK, dar nu am primit CST / X-SECURITY-TOKEN în headere.")

    dbg(f"CST='{mask(CST, keep=2)}' | X-SECURITY-TOKEN='{mask(XSEC, keep=2)}'")
    info("Login OK (CST & X-SECURITY-TOKEN)")

    # optional: arătăm niște info despre cont (dacă există în JSON)
    try:
        print(json.dumps(data, ensure_ascii=False), flush=True)
    except Exception:
        pass

# ---------- căutare EPIC pentru simbol ----------

def candidate_symbol_strings(s: str):
    # încercăm câteva variante tipice
    base = s.strip()
    out = {base}
    out.add(base.replace("/", ""))
    out.add(base.replace("/", "-"))
    out.add(base.replace("/", "USD"))
    out.add(base.upper())
    out.add(base.lower())
    # exemple uzuale pentru ETH/USD
    if base.upper() == "ETHUSD":
        out.update({"ETH/USD", "EthereumUSD", "Ethereum/USD", "ETH-USD"})
    return list(out)

def extract_epic_from_item(item):
    # încearcă să găsească epic/symbol/name indiferent de schemă
    keys = item.keys() if isinstance(item, dict) else []
    epic = None
    for k in ("epic", "EPIC", "id", "marketId", "instrumentId"):
        if k in keys and item.get(k):
            epic = item.get(k)
            break
    symbol = None
    for k in ("symbol", "ticker", "Symbol", "RIC"):
        if k in keys and item.get(k):
            symbol = str(item.get(k))
            break
    name = None
    for k in ("name", "instrumentName", "displayName", "description"):
        if k in keys and item.get(k):
            name = str(item.get(k))
            break
    return epic, symbol, name

def get_auth_headers():
    if not CST or not XSEC:
        raise RuntimeError("Nu ai sesiune. Apelează mai întâi login_session().")
    return {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
    }

def find_epic_for_symbol(symbol: str):
    """
    Încearcă mai multe endpoint-uri cunoscute / posibil valide.
    Dacă un endpoint răspunde 404, continuăm; logăm tot.
    """
    headers = get_auth_headers()

    # liste de endpoint-uri și parametri posibili
    endpoints = [
        f"{BASE}/api/v1/instruments",        # posibil (dar la tine a dat 404)
        f"{BASE}/api/v1/symbols",            # posibil (tot 404 la tine)
        f"{BASE}/api/v1/markets",            # unele instanțe folosesc /markets?search=
        f"{BASE}/api/v1/marketnavigation/instruments",  # unele instanțe custom
        f"{BASE}/api/v1/price/symbols",      # fallback generic
    ]
    query_templates = [
        {"search": symbol},
        {"symbol": symbol},
        {"name": symbol},
        {},  # unele endpoint-uri ignoră parametrii și dau listă completă
    ]

    tried = set()
    variants = candidate_symbol_strings(symbol)

    for url in endpoints:
        for q in variants:
            for params in query_templates:
                key = (url, tuple(sorted(params.items())), q)
                if key in tried:
                    continue
                tried.add(key)

                ps = dict(params) if params else {}
                # suprascriem dacă e cazul
                if "search" in ps:
                    ps["search"] = q
                elif "symbol" in ps:
                    ps["symbol"] = q
                elif "name" in ps:
                    ps["name"] = q

                dbg(f"Caut '{q}' la {url} cu params={ps or '-'}")
                try:
                    data = get_json(url, headers=headers, params=ps, retries=3)
                except RuntimeError as e:
                    # e.g. 404 Not Found sau alt cod – logăm și trecem mai departe
                    warn(str(e))
                    continue

                # normalizăm: vrem o listă de itemi
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    # caută câmpuri care conțin liste
                    for k, v in data.items():
                        if isinstance(v, list):
                            items = v
                            break
                    # dacă nu am găsit listă, poate e un singur obiect
                    if not items:
                        items = [data]

                # scanează rezultatele pentru match
                for it in items:
                    epic, sym, name = extract_epic_from_item(it)
                    if not epic and not sym and not name:
                        continue
                    dbg(f"candidate: epic={epic} | symbol={sym} | name={name}")
                    target = q.replace("/", "").upper()
                    hay = [(sym or ""), (name or "")]
                    hay = [h.replace("/", "").upper() for h in hay]
                    if target in hay or (sym and sym.upper() == target):
                        if epic:
                            info(f"Match găsit: EPIC={epic} pentru SYMBOL='{q}'")
                            return epic

                # dacă tot n-am găsit, arată o mostră mică din ce s-a primit
                try:
                    sample = items[:3]
                    dbg(f"fără match la {url} '{q}'. mostre={json.dumps(sample, ensure_ascii=False)[:400]}")
                except Exception:
                    pass

    raise RuntimeError(f"Nu am putut găsi EPIC pentru '{symbol}'. Vezi log-urile de mai sus pentru mostre/endpoint-uri încercate.")

# ---------- main ----------

def main():
    info(f"START | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={RSI_PERIOD}")

    login_session()

    # încearcă să găsești EPIC
    epic = find_epic_for_symbol(SYMBOL)
    info(f"EPIC final pentru '{SYMBOL}': {epic}")

    # aici ai EPIC-ul; poți continua cu orice logică vrei (quotes, ordine, etc.)
    # momentan doar stăm în viață
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
