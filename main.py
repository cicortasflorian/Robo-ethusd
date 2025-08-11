# main.py — ROBO1 (Capital.com compat) — no pandas + debug env lengths
import time, json, requests
from datetime import datetime, timedelta, timezone

# ----------- Config din ./env -----------
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

# Debug: verificăm lungimea valorilor din env
def _mask(v, full=False):
    if not v:
        return ""
    return v if full else (v[:3] + "***")

for key in ["API_IDENTIFIER", "API_KEY", "API_PASSWORD"]:
    value = CFG.get(key, "")
    masked_value = _mask(value, full=(key == "API_IDENTIFIER"))
    print(f"[DEBUG] {key} length={len(value)} value='{masked_value}'", flush=True)

# Configurări
API_IDENTIFIER = CFG.get("API_IDENTIFIER", "")  # email-ul de login Capital
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

def api_headers(extra=None):
    h = {
        "X-CAP-API-KEY": API_KEY,
        "Accept": "application/json"
    }
    if CST and XSEC:
        h["CST"] = CST
        h["X-SECURITY-TOKEN"] = XSEC
    if extra: h.update(extra)
    return h

# ---------- Login ----------
def login_session():
    """Login cu email (API_IDENTIFIER) + parola de cont (API_PASSWORD)."""
    global CST, XSEC
    payload = {"identifier": API_IDENTIFIER, "password": API_PASSWORD}
    r = requests.post(
        f"{BASE}/api/v1/session",
        headers=api_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=20
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")
    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")
    if not CST or not XSEC:
        raise RuntimeError("Missing CST or X-SECURITY-TOKEN after login.")
    print("[AUTH] Login OK", flush=True)

# ---------- Helpers piețe ----------
def to_resolution(tf: str) -> str:
    t = tf.strip().lower()
    if t.endswith("m"):
        n = int(t[:-1]); return f"MINUTE_{n}" if n != 1 else "MINUTE"
    if t.endswith("h"):
        n = int(t[:-1]); return f"HOUR_{n}" if n != 1 else "HOUR"
    if t.endswith("d"):
        return "DAY"
    return "MINUTE_10"

def resolve_epic(symbol: str) -> str:
    if symbol in EPIC_CACHE: return EPIC_CACHE[symbol]
    for q in ("searchTerm", "search"):
        try:
            url = f"{BASE}/api/v1/markets?{q}={symbol}"
            r = requests.get(url, headers=api_headers(), timeout=20)
            if r.status_code >= 400: 
                continue
            js = r.json()
            items = js.get("markets") or js.get("instruments") or js
            if isinstance(items, dict):
                items = items.get("markets") or items.get("instruments")
            if not isinstance(items, list):
                continue
            for it in items:
                epic = it.get("epic") or it.get("EPIC") or it.get("id")
                sym  = (it.get("instrumentName") or it.get("symbol") or "").replace("/", "")
                if epic and (symbol.upper() in (sym.upper(), epic.upper())):
                    EPIC_CACHE[symbol] = epic
                    return epic
            # fallback: primul cu "epic"
            for it in items:
                if "epic" in it:
                    EPIC_CACHE[symbol] = it["epic"]
                    return it["epic"]
        except Exception:
            continue
    raise RuntimeError(f"Nu am putut găsi EPIC pentru {symbol}")

def get_prices(epic: str, resolution: str, max_n=200):
    url = f"{BASE}/api/v1/prices/{epic}?resolution={resolution}&max={max_n}"
    r = requests.get(url, headers=api_headers(), timeout=25)
    r.raise_for_status()
    js = r.json()
    rows = js.get("prices") or js
    closes = []
    for p in rows:
        cp = p.get("closePrice") or {}
        close = None
        if "mid" in cp and cp["mid"] is not None:
            close = float(cp["mid"])
        elif cp.get("bid") is not None and cp.get("ask") is not None:
            close = (float(cp["bid"]) + float(cp["ask"])) / 2.0
        elif p.get("lastTraded") is not None:
            close = float(p["lastTraded"])
        if close is not None:
            closes.append(close)
    if not closes:
        raise RuntimeError("N-am reușit să extrag close-urile.")
    return closes

def get_quote(epic: str):
    url = f"{BASE}/api/v1/prices/{epic}?resolution=MINUTE&max=1"
    r = requests.get(url, headers=api_headers(), timeout=15)
    r.raise_for_status()
    js = r.json()
    p = (js.get("prices") or [{}])[-1]
    cp = p.get("closePrice") or {}
    ask = cp.get("ask"); bid = cp.get("bid"); mid = cp.get("mid")
    if ask is None and bid is None and mid is not None:
        ask = bid = float(mid)
    return float(ask), float(bid)

def pct_spread(ask, bid):
    return ((ask - bid) / bid) * 100 if bid else 999

# ---------- RSI simplu ----------
def calc_rsi_simple(closes, period):
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # ultimele 'period' diferențe
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        elif diff < 0:
            losses -= diff
    avg_gain = gains / period
    avg_loss = (losses / period) if losses != 0 else 1e-9
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------- Stare bot ----------
prev_rsi = None
pozitie = None
ultima_buy = None
tranzactii_azi = 0
cumparari_azi = []
capital_local = 0.0

def tick(epic, res_code):
    global prev_rsi, pozitie, ultima_buy, tranzactii_azi, cumparari_azi, capital_local
    now = datetime.now(timezone.utc)

    closes = get_prices(epic, res_code, max_n=max(200, RSI_PERIOD * 5))
    cur_rsi = calc_rsi_simple(closes, RSI_PERIOD)
    if cur_rsi is None:
        return

    ask, bid = get_quote(epic)
    spread = pct_spread(ask, bid)

    # reset zilnic (UTC ~ la miezul nopții)
    if now.hour == 0 and now.minute < 5 and tranzactii_azi > 0:
        tranzactii_azi = 0
        cumparari_azi = []

    crossed_down_buy = (prev_rsi is not None) and (prev_rsi > BUY_LVL) and (cur_rsi <= BUY_LVL)
    crossed_up_sell  = (prev_rsi is not None) and (prev_rsi < SELL_LVL) and (cur_rsi >= SELL_LVL)

    # SELL
    if pozitie:
        entry = pozitie['pret']
        delta_pct = ((bid - entry) / entry) * 100
        net_profit = delta_pct - spread
        if delta_pct >= 5 and net_profit >= MIN_NET_PROFIT:
            print(f"[ROBO1] SELL TAKE +5% | RSI={cur_rsi:.2f} | net={net_profit:.2f}%", flush=True)
            pozitie = None; capital_local += INVEST_AMOUNT * (1 + net_profit / 100)
        elif delta_pct <= -2:
            print(f"[ROBO1] SELL STOP -2% | RSI={cur_rsi:.2f} | pnl={delta_pct:.2f}%", flush=True)
            pozitie = None; capital_local += INVEST_AMOUNT * (1 + delta_pct / 100)
        elif crossed_up_sell and net_profit >= MIN_NET_PROFIT:
            print(f"[ROBO1] SELL RSI CROSS-UP {SELL_LVL} | RSI={cur_rsi:.2f} | net={net_profit:.2f}%", flush=True)
            pozitie = None; capital_local += INVEST_AMOUNT * (1 + net_profit / 100)
    # BUY
    else:
        if tranzactii_azi < 3 and crossed_down_buy and spread <= MAX_SPREAD:
            ok_cd = (not ultima_buy) or (now - ultima_buy > timedelta(hours=2)) \
                    or (len(cumparari_azi) > 0 and ask < cumparari_azi[-1] * 0.995)
            if ok_cd:
                pozitie = {"pret": ask, "ora": now}
                ultima_buy = now
                cumparari_azi.append(ask)
                tranzactii_azi += 1
                capital_local -= INVEST_AMOUNT
                print(f"[ROBO1] BUY RSI CROSS-DOWN {BUY_LVL} | RSI={cur_rsi:.2f} | price={ask:.2f} | spread={spread:.2f}%", flush=True)

    prev_rsi = cur_rsi

def main():
    print(f"[ROBO1] START | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={RSI_PERIOD} BUY={BUY_LVL} SELL={SELL_LVL} "
          f"MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}", flush=True)
    # 1) Login
    login_session()
    # 2) EPIC
    epic = resolve_epic(SYMBOL.upper())
    res = to_resolution(TIMEFRAME)
    print(f"[ROBO1] EPIC={epic} RES={res}", flush=True)
    # 3) Loop
    while True:
        try:
            tick(epic, res)
        except Exception as e:
            print(f"[ROBO1] Eroare: {e}", flush=True)
            try:
                login_session()
            except Exception as ee:
                print(f"[ROBO1] Relogin fail: {ee}", flush=True)
        time.sleep(60)

if __name__ == "__main__":
    main()
