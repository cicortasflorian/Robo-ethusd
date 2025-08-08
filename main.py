# ROBO1 – ETH/USD (sau ce SYMBOL pui în env) – cross-under BUY / cross-over SELL
# + jurnalizare detaliată în log (Render Live tail)

import time
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

def load_env():
    cfg = {}
    try:
        with open("env","r") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k,v = line.strip().split("=",1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg

CFG = load_env()

API_KEY            = CFG.get("API_KEY","")
SYMBOL             = CFG.get("SYMBOL","ETHUSD")        # ETHUSD / SEDG etc. fără slash
TIMEFRAME          = CFG.get("TIMEFRAME","90m")        # ex: 10m, 90m, 2h
RSI_PERIOD         = int(CFG.get("RSI_PERIOD",14))
INVEST_AMOUNT      = float(CFG.get("INVEST_AMOUNT",10))
MAX_SPREAD         = float(CFG.get("MAX_SPREAD_PERCENT",0.7))
MIN_NET_PROFIT     = float(CFG.get("MIN_NET_PROFIT_PERCENT",0.5))
BUY_CROSS_LEVEL    = float(CFG.get("BUY_CROSS_LEVEL",28))
SELL_CROSS_LEVEL   = float(CFG.get("SELL_CROSS_LEVEL",72))
TELEGRAM_TOKEN     = CFG.get("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID   = CFG.get("TELEGRAM_CHAT_ID","")

# ---- stare bot ----
prev_rsi = None
pozitie_deschisa = None   # {"pret": float, "ora": datetime}
ultima_cumparare = None
tranzactii_azi = 0
cumparari_azi = []
capital = 0.0  # evidență locală

def log(msg):
    # print cu timestamp UTC, forțăm flush ca să apară imediat în Live tail
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[ROBO1] {ts} | {msg}", flush=True)

def headers():
    return {"X-CAP-API-KEY": API_KEY, "Accept":"application/json"}

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Telegram FAIL: {e}")

def get_candles(limit=300):
    url = f"https://api-capital.backend-capital.com/candles/{SYMBOL}?resolution={TIMEFRAME}&limit={limit}"
    r = requests.get(url, headers=headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    candles = data['candles'] if 'candles' in data else data
    df = pd.DataFrame(candles)

    # normalizează coloana close
    for k in ['close','c','Close','CLOSE','midClose','price']:
        if k in df.columns:
            df['close'] = pd.to_numeric(df[k], errors="coerce")
            break
    if 'close' not in df.columns:
        raise ValueError("Nu găsesc coloana close în răspunsul de la API.")

    return df.dropna(subset=['close'])

def calc_rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0,1e-9)
    return 100 - (100/(1+rs))

def get_quote():
    url = f"https://api-capital.backend-capital.com/pricing/{SYMBOL}"
    r = requests.get(url, headers=headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    # încercăm câmpuri comune
    ask = None
    bid = None

    if isinstance(data, dict):
        p = data.get('price', data)
        for k in ('ask','offer','askPrice'):
            if k in p:
                ask = float(p[k]); break
        for k in ('bid','bidPrice'):
            if k in p:
                bid = float(p[k]); break
    if ask is None or bid is None:
        raise ValueError(f"Quote invalid pentru {SYMBOL}: {data}")
    return ask, bid

def pct_spread(ask, bid):
    return ((ask - bid) / bid) * 100 if bid else 999.0

def tick():
    global prev_rsi, pozitie_deschisa, ultima_cumparare, tranzactii_azi, cumparari_azi, capital

    now = datetime.now(timezone.utc)

    # reset zilnic (UTC)
    if now.hour == 0 and now.minute < 3 and tranzactii_azi > 0:
        log(f"Reset contor zilnic: tranzactii_azi={tranzactii_azi}")
        tranzactii_azi = 0
        cumparari_azi = []

    # date & RSI
    df = get_candles(limit=max(200, RSI_PERIOD * 5))
    rsi_series = calc_rsi(df['close'], RSI_PERIOD)
    cur_rsi = float(rsi_series.iloc[-1])

    ask, bid = get_quote()
    spread = pct_spread(ask, bid)

    log(f"Tick | SYMBOL={SYMBOL} TF={TIMEFRAME} RSI={cur_rsi:.2f} (prev={prev_rsi:.2f} if prev else None) "
        f"| ask={ask:.5f} bid={bid:.5f} spread={spread:.3f}% "
        f"| pozitie={'DA' if pozitie_deschisa else 'NU'} tranzactii_azi={tranzactii_azi}")

    crossed_down_buy = False
    crossed_up_sell = False
    if prev_rsi is not None:
        crossed_down_buy = (prev_rsi > BUY_CROSS_LEVEL) and (cur_rsi <= BUY_CROSS_LEVEL)
        crossed_up_sell  = (prev_rsi < SELL_CROSS_LEVEL) and (cur_rsi >= SELL_CROSS_LEVEL)
        if crossed_down_buy:
            log(f"Semnal BUY: RSI a trecut în jos sub {BUY_CROSS_LEVEL} (prev={prev_rsi:.2f} -> cur={cur_rsi:.2f})")
        if crossed_up_sell:
            log(f"Semnal SELL: RSI a trecut în sus peste {SELL_CROSS_LEVEL} (prev={prev_rsi:.2f} -> cur={cur_rsi:.2f})")

    # === SELL management ===
    if pozitie_deschisa:
        entry = pozitie_deschisa['pret']
        delta_pct = ((bid - entry) / entry) * 100
        net_profit = delta_pct - spread

        log(f"Pozitie deschisa | entry={entry:.5f} PnL={delta_pct:.2f}% net={net_profit:.2f}% (după spread)")

        if delta_pct >= 5 and net_profit >= MIN_NET_PROFIT:
            log(f"SELL TAKE +5% | net={net_profit:.2f}% | RSI={cur_rsi:.2f}")
            send_telegram(f"[ROBO1] SELL TAKE +5% | RSI={cur_rsi:.2f} | net={net_profit:.2f}%")
            pozitie_deschisa = None
            capital += INVEST_AMOUNT * (1 + net_profit / 100)
        elif delta_pct <= -2:
            log(f"SELL STOP -2% | pnl={delta_pct:.2f}% | RSI={cur_rsi:.2f}")
            send_telegram(f"[ROBO1] SELL STOP -2% | RSI={cur_rsi:.2f} | pnl={delta_pct:.2f}%")
            pozitie_deschisa = None
            capital += INVEST_AMOUNT * (1 + delta_pct / 100)
        elif crossed_up_sell and net_profit >= MIN_NET_PROFIT:
            log(f"SELL RSI CROSS-UP {SELL_CROSS_LEVEL} | net={net_profit:.2f}% | RSI={cur_rsi:.2f}")
            send_telegram(f"[ROBO1] SELL RSI CROSS-UP {SELL_CROSS_LEVEL} | RSI={cur_rsi:.2f} | net={net_profit:.2f}%")
            pozitie_deschisa = None
            capital += INVEST_AMOUNT * (1 + net_profit / 100)

    # === BUY logic ===
    else:
        if not crossed_down_buy:
            log("Nu cumpăr: nu s-a produs cross-down sub pragul BUY.")
        elif spread > MAX_SPREAD:
            log(f"Nu cumpăr: spread prea mare ({spread:.2f}% > {MAX_SPREAD}%).")
        elif tranzactii_azi >= 3:
            log("Nu cumpăr: limită zilnică de 3 intrări atinsă.")
        else:
            ok_cooldown = (
                (not ultima_cumparare) or
                (datetime.now(timezone.utc) - ultima_cumparare > timedelta(hours=2)) or
                (len(cumparari_azi) > 0 and ask < cumparari_azi[-1] * 0.995)
            )
            if not ok_cooldown:
                log("Nu cumpăr: în cooldown (nu au trecut 2h și prețul nu e cu 0.5% mai jos decât ultima cumpărare).")
            else:
                pozitie_deschisa = {"pret": ask, "ora": datetime.now(timezone.utc)}
                ultima_cumparare = datetime.now(timezone.utc)
                cumparari_azi.append(ask)
                tranzactii_azi += 1
                capital -= INVEST_AMOUNT
                log(f"BUY EXEC | price={ask:.5f} | spread={spread:.2f}% | RSI={cur_rsi:.2f} | tranzactii_azi={tranzactii_azi}")
                send_telegram(f"[ROBO1] BUY RSI CROSS-DOWN {BUY_CROSS_LEVEL} | RSI={cur_rsi:.2f} | price={ask:.5f} | spread={spread:.2f}%")

    prev_rsi = cur_rsi

def main():
    log("pornit (cross signals)")
    log(f"Config: SYMBOL={SYMBOL} TIMEFRAME={TIMEFRAME} RSI_PERIOD={RSI_PERIOD} "
        f"INVEST_AMOUNT={INVEST_AMOUNT} BUY_CROSS_LEVEL={BUY_CROSS_LEVEL} "
        f"SELL_CROSS_LEVEL={SELL_CROSS_LEVEL} MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")
    while True:
        try:
            tick()
        except Exception as e:
            log(f"Eroare: {e}")
            send_telegram(f"[ROBO1] Eroare: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()
