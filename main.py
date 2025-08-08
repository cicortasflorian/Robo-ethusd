# main.py — ROBO1 (ETH/USD) with cross-under BUY & cross-over SELL
# - Reads config from ./env (simple KEY=VALUE file). Render can override via Environment Variables.
# - Signals:
#     BUY  when RSI crosses DOWN through BUY_CROSS_LEVEL (e.g., from >28 to <=28), spread <= MAX_SPREAD_PERCENT
#     SELL when RSI crosses UP   through SELL_CROSS_LEVEL (e.g., from <72 to >=72) with NET profit >= MIN_NET_PROFIT_PERCENT
#     TP   when price gain >= +5% and net profit >= min
#     SL   when price loss <= -2% (safety stop)
# - Cooldown: after a BUY, for the next 2 hours a second BUY is allowed only if price is at least 0.5% lower than last buy.
# - Max 3 buys / trading day (UTC). For crypto, day is 00:00–23:59 UTC.
# - Heartbeat log every minute with current RSI/price/spread & active config.
#
# NOTE: This is a reference implementation. Make sure your Capital.com API plan allows the endpoints used.
#       Endpoints may differ by account/region; adjust get_candles/get_quote if necessary.

import time
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# ---------- load config from ./env ----------
def load_env():
    cfg = {}
    try:
        with open("env","r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"): 
                    continue
                if "=" in line:
                    k,v = line.split("=",1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg

CFG = load_env()

API_KEY = CFG.get("API_KEY","")
SYMBOL = CFG.get("SYMBOL","ETHUSD")  # no slash for Capital endpoints
TIMEFRAME = CFG.get("TIMEFRAME","10m")
RSI_PERIOD = int(CFG.get("RSI_PERIOD", 3))
INVEST_AMOUNT = float(CFG.get("INVEST_AMOUNT", 10))
MAX_SPREAD = float(CFG.get("MAX_SPREAD_PERCENT", 0.7))
MIN_NET_PROFIT = float(CFG.get("MIN_NET_PROFIT_PERCENT", 0.5))
BUY_CROSS_LEVEL = float(CFG.get("BUY_CROSS_LEVEL", 28))
SELL_CROSS_LEVEL = float(CFG.get("SELL_CROSS_LEVEL", 72))
TELEGRAM_TOKEN = CFG.get("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID = CFG.get("TELEGRAM_CHAT_ID","")

# ---------- bot state (in-memory) ----------
prev_rsi = None
pozitie_deschisa = None
ultima_cumparare = None
tranzactii_azi = 0
cumparari_azi = []
capital = 0.0  # evidență locală (NU banilor reali)

def headers():
    return {"X-CAP-API-KEY": API_KEY, "Accept":"application/json"}

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass

def get_candles(limit=300):
    # NOTE: Endpoint/shape can vary. Adjust if your account returns a different JSON schema.
    url = f"https://api-capital.backend-capital.com/candles/{SYMBOL}?resolution={TIMEFRAME}&limit={limit}"
    r = requests.get(url, headers=headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    candles = data.get("candles", data)
    df = pd.DataFrame(candles)
    # normalize close column
    for k in ['close','c','Close','CLOSE','midClose']:
        if k in df.columns:
            df['close'] = pd.to_numeric(df[k], errors='coerce')
            break
    if 'close' not in df.columns:
        raise ValueError("Nu găsesc coloana close în răspunsul de la API.")
    df = df.dropna(subset=['close']).reset_index(drop=True)
    return df

def calc_rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0,1e-9)
    rsi = 100 - (100/(1+rs))
    return rsi

def get_quote():
    # NOTE: Endpoint/shape can vary.
    url = f"https://api-capital.backend-capital.com/pricing/{SYMBOL}"
    r = requests.get(url, headers=headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    # Attempt multiple field names for compatibility
    ask = None; bid = None
    if isinstance(data, dict) and 'price' in data:
        p = data['price']
        ask = p.get('ask') or p.get('offer') or p.get('askPrice')
        bid = p.get('bid') or p.get('bidPrice')
    if ask is None: ask = data.get('ask')
    if bid is None: bid = data.get('bid')
    ask = float(ask)
    bid = float(bid)
    return ask, bid

def pct_spread(ask,bid):
    if not bid:
        return 999.0
    return ((ask-bid)/bid)*100.0

def day_reset_needed(now_utc, tz_hour=0):
    # Resetează la 00:00 UTC (în primele 5 minute) — suficient pentru crypto.
    return (now_utc.hour == tz_hour and now_utc.minute < 5)

def log_heartbeat(cur_rsi, ask, bid, spread):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] HB | SYMBOL={SYMBOL} TIMEFRAME={TIMEFRAME} RSI_P={RSI_PERIOD} "
          f"BUY_LVL={BUY_CROSS_LEVEL} SELL_LVL={SELL_CROSS_LEVEL} "
          f"ASK={ask:.6f} BID={bid:.6f} SPREAD%={spread:.3f} RSI={cur_rsi:.2f}")
    # flush pentru Render
    try:
        import sys; sys.stdout.flush()
    except Exception:
        pass

def tick():
    global prev_rsi, pozitie_deschisa, ultima_cumparare, tranzactii_azi, cumparari_azi, capital
    now = datetime.now(timezone.utc)

    df = get_candles(limit=max(200, RSI_PERIOD*5))
    rsi_series = calc_rsi(df['close'], RSI_PERIOD)
    cur_rsi = float(rsi_series.iloc[-1])

    ask, bid = get_quote()
    spread = pct_spread(ask, bid)

    # heartbeat
    log_heartbeat(cur_rsi, ask, bid, spread)

    # reset zilnic
    if day_reset_needed(now) and tranzactii_azi > 0:
        tranzactii_azi = 0
        cumparari_azi = []

    # semnale
    crossed_down_buy = crossed_up_sell = False
    if prev_rsi is not None:
        crossed_down_buy = (prev_rsi > BUY_CROSS_LEVEL) and (cur_rsi <= BUY_CROSS_LEVEL)
        crossed_up_sell  = (prev_rsi < SELL_CROSS_LEVEL) and (cur_rsi >= SELL_CROSS_LEVEL)

    # SELL management
    if pozitie_deschisa:
        entry = pozitie_deschisa['pret']
        delta_pct = ((bid - entry)/entry)*100.0
        net_profit = delta_pct - spread

        if delta_pct >= 5.0 and net_profit >= MIN_NET_PROFIT:
            print(f"→ SELL TAKE +5% | entry={entry:.6f} bid={bid:.6f} net={net_profit:.2f}% RSI={cur_rsi:.2f}")
            send_telegram(f"[ROBO1] SELL TAKE +5% | net={net_profit:.2f}% | RSI={cur_rsi:.2f}")
            pozitie_deschisa = None
            capital += INVEST_AMOUNT*(1+net_profit/100.0)

        elif delta_pct <= -2.0:
            print(f"→ SELL STOP -2% | entry={entry:.6f} bid={bid:.6f} pnl={delta_pct:.2f}% RSI={cur_rsi:.2f}")
            send_telegram(f"[ROBO1] SELL STOP -2% | pnl={delta_pct:.2f}% | RSI={cur_rsi:.2f}")
            pozitie_deschisa = None
            capital += INVEST_AMOUNT*(1+delta_pct/100.0)

        elif crossed_up_sell and net_profit >= MIN_NET_PROFIT:
            print(f"→ SELL RSI CROSS-UP {SELL_CROSS_LEVEL} | entry={entry:.6f} bid={bid:.6f} net={net_profit:.2f}%")
            send_telegram(f"[ROBO1] SELL RSI CROSS-UP {SELL_CROSS_LEVEL} | net={net_profit:.2f}% | RSI={cur_rsi:.2f}")
            pozitie_deschisa = None
            capital += INVEST_AMOUNT*(1+net_profit/100.0)

    # BUY
    else:
        if tranzactii_azi < 3 and crossed_down_buy and spread <= MAX_SPREAD:
            ok_cooldown = (
                (not ultima_cumparare) or
                (now - ultima_cumparare > timedelta(hours=2)) or
                (len(cumparari_azi)>0 and ask < cumparari_azi[-1]*0.995)  # -0.5%
            )
            if ok_cooldown:
                pozitie_deschisa = {"pret": ask, "ora": now}
                ultima_cumparare = now
                cumparari_azi.append(ask)
                tranzactii_azi += 1
                capital -= INVEST_AMOUNT
                print(f"→ BUY RSI CROSS-DOWN {BUY_CROSS_LEVEL} | ask={ask:.6f} spread%={spread:.3f} RSI={cur_rsi:.2f}")
                send_telegram(f"[ROBO1] BUY RSI CROSS-DOWN {BUY_CROSS_LEVEL} | price={ask:.6f} | spread={spread:.2f}% | RSI={cur_rsi:.2f}")

    prev_rsi = cur_rsi

def main():
    print("ROBO1 started with configuration:")
    print(f"  SYMBOL={SYMBOL} TIMEFRAME={TIMEFRAME} RSI_PERIOD={RSI_PERIOD} INVEST_AMOUNT={INVEST_AMOUNT}")
    print(f"  BUY_CROSS_LEVEL={BUY_CROSS_LEVEL} SELL_CROSS_LEVEL={SELL_CROSS_LEVEL}")
    print(f"  MAX_SPREAD%={MAX_SPREAD} MIN_NET_PROFIT%={MIN_NET_PROFIT}")
    try:
        import sys; sys.stdout.flush()
    except Exception:
        pass
    send_telegram(f"[ROBO1] Pornit | {SYMBOL} {TIMEFRAME} | RSI_P={RSI_PERIOD}")

    while True:
        try:
            tick()
        except Exception as e:
            print(f"[E] {e}")
            send_telegram(f"[ROBO1] Eroare: {e}")
        time.sleep(60)  # o verificare pe minut

if __name__ == "__main__":
    main()
