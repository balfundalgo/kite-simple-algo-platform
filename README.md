# Balfund Simple Algo Platform — Zerodha Kite

A desktop app that replaces the client's Excel sheet (`BALFUND_ALGO_XL_DETAILS.xlsx`).
It holds a watchlist of NSE shares, buys them as delivery (CNC) when they reach their buy
price, and sells them by the target, ageing, averaging and S-level rules below.
Index futures are parked for now: the 40% reserve is used only for averaging down.

## Money

| | |
|---|---|
| Equity (default 60%) | split equally into **N slots** (N chosen in Settings). One share per slot. |
| Reserve (the rest, 40%) | used only for averaging down |
| On a sale | proceeds go back to the slot and the reserve in the same ratio as each funded the holding |

## Rules

| Rule | What happens | Default (all configurable) |
|---|---|---|
| Entry | A free slot buys the **first** watchlist share whose LTP reaches its buy price. qty = floor(slot cash / price) | LIMIT at the buy price |
| Target | Sell all when LTP ≥ multiplier × average cost | BULL 1.15 · BEAR 1.09 · BUY ON DIP 1.05 · SELL ON RALLY 1.05 |
| Ageing | Every *ageing period* unsold, target × factor again (repeats) | 30 days · BULL 0.95 · BEAR 0.80 · others 0.70 |
| Averaging | LTP ≤ last buy price − 5% → buy more with 50% of the free reserve; repeats from the new buy price | max adds 0 = unlimited |
| S-levels | S1–S5 on a share sells that % of the holding, only while it is at a loss or within 1% above cost. Fires once per level set. | 20 / 40 / 60 / 80 / 100% |
| Kill switch | No orders at all; working orders cancelled. Holdings are **not** sold. Survives restarts. | header button |

The market type and S-level are read live from the watchlist, so changing them takes effect
on the next second. A share removed from the watchlist while held is still managed.

## Running from source (Mac)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/test_logic.py
python src/main.py
```

1. **Account** tab: API key + secret → *Save* → *Login*.
2. **Watchlist** tab: *Import Excel / CSV* (the client's sheet works as is) → *Resolve NSE symbols*
   turns names like "YES BANK" into `YESBANK`. Check market types, fix any NOT FOUND rows.
3. **Settings** tab: capital, equity %, N → *Initialise portfolio*. Keep **PAPER** for testing.
4. **Start**.

Redirect URL on developers.kite.trade must be exactly `http://127.0.0.1:8765/`.
LIVE orders need the static IP registered on the Kite app.

## Files

| | |
|---|---|
| `src/portfolio.py` | All rules and the money book, pure Python |
| `src/engine.py` | Loop: quotes, decisions, fills, book persistence |
| `src/broker.py` | Kite session, PAPER/LIVE orders, order tracking |
| `src/watchlist.py` | Watchlist store, Excel/CSV import-export, NSE symbol resolution |
| `src/indicators.py` | Daily RSI(14) for display |
| `src/kite_auth.py` | Daily Kite login with local redirect catcher |
| `src/gui.py` | CustomTkinter app |
| `src/test_logic.py` | Offline tests (CI runs these before building) |

Next to the EXE: `settings.json`, `tokens/`, `state/watchlist.json`,
`state/book_paper.json`, `state/book_live.json`, `logs/app_*.log`, `logs/orders_*.csv`,
`logs/trades_*.csv`. Paper and live books are separate files.

## Orders (LIVE)

* CNC on NSE. LIMIT orders not filled within 60 s are cancelled, then re-decided at the new price.
* An order whose state cannot be read is kept as working and polled again: the book only
  changes on a confirmed fill. Every order has a unique tag, so an order placed during a network
  error is found again instead of being sent twice.
* At start, the book is compared with Zerodha holdings + today's CNC positions; a shortfall is
  flagged as MISMATCH (nothing is changed automatically).

## Build

Every push to `main` runs the tests on Ubuntu, then builds `Balfund_Simple_Algo.exe` on
Windows. Download it from the Actions run page.
