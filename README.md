# ROBO1 — ETHUSD (Capital.com)

Rule set:
- BUY când RSI **coboară** sub prag (cross-down) `BUY_CROSS_LEVEL`.
- SELL când RSI **urcă** peste prag (cross-up) `SELL_CROSS_LEVEL` **și** profitul net ≥ `MIN_NET_PROFIT_PERCENT`.
- TP: +5% (net ≥ min).  SL: −2%.
- Max 3 BUY/zi (UTC). Cooldown: 2h; în primele 2h permite a 2‑a cumpărare doar dacă prețul e cu ≥0.5% mai mic.
- Limite: spread ≤ `MAX_SPREAD_PERCENT`.

## Rulare pe Render
- Background Worker (Python 3), **Start Command:** `python main.py`
- Variabile de mediu (chei fără ghilimele): vezi fișierul `env`.

