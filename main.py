# main.py — ROBO1 (Capital.com compat) : EPIC resolver + v1 prices
import time, json, requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# --------- Config din ./env ----------
def load_env():
    cfg={}
    try:
        with open("env","r") as f:
            for line in f:
                if "=" in line:
                    k,v=line.strip().split("=",1); cfg[k]=v
    except FileNotFoundError:
        pass
    return cfg

CFG = load_env()

API_KEY        = CFG.get("API_KEY","")
API_PASSWORD   = CFG.get("API_PASSWORD","")   # <— nou!
SYMBOL         = CFG.get("SYMBOL","ETHUSD").replace("/","")
TIMEFRAME      = CFG.get("TIMEFRAME","10m")
RSI_PERIOD     = int(CFG.get("RSI_PERIOD",14))
INVEST_AMOUNT  = float(CFG.get("INVEST_AMOUNT",10))
MAX_SPREAD     = float(CFG.get("MAX_SPREAD_PERCENT",0.7))
MIN_NET_PROFIT = float(CFG.get("MIN_NET_PROFIT_PERCENT",0.5))
BUY_LVL        = float(CFG.get("BUY_CROSS_LEVEL",28))
SELL_LVL       = float(CFG.get("SELL_CROSS_LEVEL",72))
TG_TOKEN       = CFG.get("TELEGRAM_TOKEN","")
TG_CHAT        = CFG.get("TELEGRAM_CHAT_ID","")

BASE = "https://api-capital.backend-capital.com"
CST = None
XSEC = None
EPIC_CACHE = {}

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg}, timeout=10
        )
    except: pass

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

def login_session():
    """Obține CST & X-SECURITY-TOKEN pe baza API key + custom password."""
    global CST, XSEC
    # Capital.com (IG) cere POST /api/v1/session cu password
    payload = {"identifier": API_KEY, "password": API_PASSWORD}
    r = requests.post(f"{BASE}/api/v1/session", headers=api_headers(), json=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text}")
    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")
    if not CST or not XSEC:
        raise RuntimeError("Missing CST or X-SECURITY-TOKEN after login.")

def to_resolution(tf: str) -> str:
    """Mapează 10m -> MINUTE_10, 1H -> HOUR, 1D -> DAY etc."""
    t = tf.strip().lower()
    if t.endswith("m"):
        n=int(t[:-1]); return f"MINUTE_{n}" if n!=1 else "MINUTE"
    if t.endswith("h"):
        n=int(t[:-1]); return f"HOUR_{n}" if n!=1 else "HOUR"
    if t.endswith("d"):
        return "DAY"
    return "MINUTE_10"

def resolve_epic(symbol: str) -> str:
    """Caută EPIC pentru simbol (probăm ambii parametri de căutare)."""
    if symbol in EPIC_CACHE: return EPIC_CACHE[symbol]

    for qparam in ("searchTerm", "search"):
        try:
            url = f"{BASE}/api/v1/markets?{qparam}={symbol}"
            r = requests.get(url, headers=api_headers(), timeout=20)
            if r.status_code >= 400: continue
            js = r.json()
            # încercăm câteva forme posibile ale răspunsului
            items = js.get("markets") or js.get("instruments") or js
            if isinstance(items, dict): items = items.get("markets") or items.get("instruments")
            if not isinstance(items, list): continue
            for it in items:
                # epic poate sta sub 'epic' sau 'instrument' etc.
                epic = it.get("epic") or it.get("EPIC") or it.get("instrument") or it.get("id")
                sym  = (it.get("instrumentName") or it.get("symbol") or "").replace("/","")
                if epic and (symbol.upper() in (sym.upper(), epic.upper())):
                    EPIC_CACHE[symbol]=epic; return epic
            # fallback: ia primul care pare corect
            for it in items:
                if "epic" in it:
                    EPIC_CACHE[symbol]=it["epic"]; return it["epic"]
        except Exception:
            continue
    raise RuntimeError(f"Nu am putut găsi EPIC pentru {symbol}")

def get_prices(epic: str, resolution: str, max_n=200):
    url = f"{BASE}/api/v1/prices/{epic}?resolution={resolution}&max={max_n}"
    r = requests.get(url, headers=api_headers(), timeout=25); r.raise_for_status()
    js = r.json()
    # Capital/IG întoarce listă sub 'prices'; luăm 'closePrice' mid/bid/ask
    rows = js.get("prices") or js
    closes = []
    for p in rows:
        close = None
        cp = p.get("closePrice") or {}
        # prefer mid -> apoi bid/ask medie
        if "mid" in cp and cp["mid"] is not None:
            close = float(cp["mid"])
        elif "bid" in cp and "ask" in cp and cp["bid"] is not None and cp["ask"] is not None:
            close = (float(cp["bid"])+float(cp["ask"]))/2
        elif "lastTraded" in p and p["lastTraded"] is not None:
            close = float(p["lastTraded"])
        if close is not None:
            closes.append(close)
    if not closes:
        raise RuntimeError("N-am reușit să extrag close-urile din răspunsul de prețuri.")
    return pd.Series(closes)

def get_quote(epic: str):
    # luăm ultima lumânare de 1min ca proxy pt ask/bid
    url = f"{BASE}/api/v1/prices/{epic}?resolution=MINUTE&max=1"
    r = requests.get(url, headers=api_headers(), timeout=15); r.raise_for_status()
    js = r.json()
    p = (js.get("prices") or [{}])[-1]
    cp = p.get("closePrice") or {}
    ask = cp.get("ask")
    bid = cp.get("bid")
    # dacă lipsesc, aproximăm din mid
    mid = cp.get("mid")
    if ask is None and bid is None and mid is not None:
        ask = bid = float(mid)
    return float(ask), float(bid)

def pct_spread(ask,bid):
    return ((ask-bid)/bid)*100 if bid else 999

def calc_rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0,1e-9)
    return 100 - (100/(1+rs))

# --------- Stare bot ----------
prev_rsi = None
pozitie = None
ultima_buy = None
tranzactii_azi = 0
cumparari_azi=[]
capital_local = 0.0

def tick(epic, res_code):
    global prev_rsi, pozitie, ultima_buy, tranzactii_azi, cumparari_azi, capital_local
    now = datetime.now(timezone.utc)

    closes = get_prices(epic, res_code, max_n=max(200,RSI_PERIOD*5))
    rsi_series = calc_rsi(pd.Series(closes), RSI_PERIOD)
    cur_rsi = float(rsi_series.iloc[-1])

    ask,bid = get_quote(epic)
    spread = pct_spread(ask,bid)

    # reset simplu daily UTC
    if now.hour == 0 and now.minute < 5 and tranzactii_azi>0:
        tranzactii_azi = 0; cumparari_azi = []

    crossed_down_buy = prev_rsi is not None and (prev_rsi > BUY_LVL) and (cur_rsi <= BUY_LVL)
    crossed_up_sell  = prev_rsi is not None and (prev_rsi < SELL_LVL) and (cur_rsi >= SELL_LVL)

    # SELL
    if pozitie:
        entry = pozitie['pret']
        delta_pct = ((bid - entry)/entry)*100
        net_profit = delta_pct - spread
        if delta_pct >= 5 and net_profit >= MIN_NET_PROFIT:
            send_telegram(f"[ROBO1] SELL TAKE +5% | RSI={cur_rsi:.2f} | net={net_profit:.2f}%")
            pozitie=None; capital_local += INVEST_AMOUNT*(1+net_profit/100)
        elif delta_pct <= -2:
            send_telegram(f"[ROBO1] SELL STOP -2% | RSI={cur_rsi:.2f} | pnl={delta_pct:.2f}%")
            pozitie=None; capital_local += INVEST_AMOUNT*(1+delta_pct/100)
        elif crossed_up_sell and net_profit >= MIN_NET_PROFIT:
            send_telegram(f"[ROBO1] SELL RSI CROSS-UP {SELL_LVL} | RSI={cur_rsi:.2f} | net={net_profit:.2f}%")
            pozitie=None; capital_local += INVEST_AMOUNT*(1+net_profit/100)
    # BUY
    else:
        if tranzactii_azi < 3 and crossed_down_buy and spread <= MAX_SPREAD:
            ok_cd = (not ultima_buy) or (now - ultima_buy > timedelta(hours=2)) \
                    or (len(cumparari_azi)>0 and ask < cumparari_azi[-1]*0.995)
            if ok_cd:
                pozitie={"pret":ask,"ora":now}
                ultima_buy=now
                cumparari_azi.append(ask)
                tranzactii_azi += 1
                capital_local -= INVEST_AMOUNT
                send_telegram(f"[ROBO1] BUY RSI CROSS-DOWN {BUY_LVL} | RSI={cur_rsi:.2f} | price={ask:.2f} | spread={spread:.2f}%")

    prev_rsi = cur_rsi

def main():
    send_telegram("[ROBO1] pornit (compat v1 + EPIC)")
    # 1) Login pt token-ele necesare
    login_session()
    # 2) Rezolvăm EPIC-ul
    epic = resolve_epic(SYMBOL.upper())
    # 3) Mapăm timeframe la rezoluția API
    res = to_resolution(TIMEFRAME)
    send_telegram(f"[ROBO1] Config: SYMBOL={SYMBOL} EPIC={epic} RES={res} RSI={RSI_PERIOD} "
                  f"BUY={BUY_LVL} SELL={SELL_LVL} MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")
    while True:
        try:
            tick(epic, res)
        except Exception as e:
            send_telegram(f"[ROBO1] Eroare: {e}")
            # dacă token-urile expiră, încercăm relogin rapid
            try:
                login_session()
            except Exception as ee:
                send_telegram(f"[ROBO1] Relogin fail: {ee}")
        time.sleep(60)

if __name__ == "__main__":
    main()
