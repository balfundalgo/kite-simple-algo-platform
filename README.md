# Balfund Simple Algo Platform — Zerodha Kite

A desktop app that replaces the client's Excel sheet (`BALFUND_ALGO_XL_DETAILS.xlsx`).
It holds a watchlist of NSE shares, buys them as delivery (CNC) when they reach their buy
price, and sells them by the target, ageing, averaging and S-level rules below. Positions are
held for days: the app remembers everything between runs and reconciles with Zerodha.
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

## Stock search

The Watchlist tab has a type-ahead search over the full NSE share list (downloaded from Kite
once a day, cached in `state/instruments_nse.json` so it works before login). Type part of the
symbol or company name ("yes", "ashok", "bank of") and pick from the drop-down with the arrow
keys + Enter or a click. Only listed shares can be added. Imported Excel names are matched
automatically; anything that cannot be matched is shown in red until picked with the search.

## Multi-day state and Zerodha reconciliation

| File (in `state/`) | Holds |
|---|---|
| `book_live.json` / `book_paper.json` | slots, cash, reserve, holdings (qty, average cost, first buy date, last buy price, adds, S-level done), realized P/L, Zerodha order ids already applied |
| `orders_live.json` | write-ahead journal of every LIVE order still working |
| `watchlist.json` | the watchlist |

Every start (each trading day): book restored → working orders from the journal are found in
Zerodha's order book by id/tag and settled (filled → book, open → tracked, rejected → dropped;
previous-day orders are settled against holdings) → holdings reconciled → management resumes.

While running (LIVE): working orders are settled every 2 s from one `orders()` call; holdings
are reconciled every 60 s (Settings). Zerodha quantity = holdings `quantity + t1_quantity` +
today's CNC buys − today's CNC sells beyond `used_quantity`.

| State | Meaning |
|---|---|
| OK | Zerodha quantity = book |
| EXTRA | Zerodha holds more (e.g. your own shares) — noted, not managed |
| MISMATCH | Zerodha holds **less** than the book → share **FROZEN** (no orders) until you choose **Sync to Zerodha** (book takes Zerodha's qty, shortfall booked as sold at today's sell price or LTP) or **Keep book** |

A fill is never applied twice: the book keeps every Zerodha order id it has applied.

## Running from source (Mac)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/test_logic.py
python src/main.py
```

1. **Account** tab: API key + secret → *Save* → *Login* (downloads the NSE share list too).
2. **Watchlist** tab: search and add shares, or *Import Excel / CSV* (the client's sheet works
   as is). Fix any red rows with the search box. Check market types.
3. **Settings** tab: capital, equity %, N → *Initialise portfolio* (once). Keep **PAPER** for testing.
4. **Start**. Every later day: Login → Start; yesterday's holdings continue.

Redirect URL on developers.kite.trade must be exactly `http://127.0.0.1:8765/`.
LIVE orders need the static IP registered on the Kite app.

## Files

| | |
|---|---|
| `src/portfolio.py` | All rules and the money book, pure Python |
| `src/reconcile.py` | Book vs Zerodha quantities, pure Python |
| `src/engine.py` | Start-up recovery, loop, fills, reconciliation, book persistence |
| `src/broker.py` | Kite session, PAPER/LIVE orders, write-ahead order journal |
| `src/instruments.py` | NSE share list cache and ranked search |
| `src/watchlist.py` | Watchlist store, Excel/CSV import-export, name matching |
| `src/indicators.py` | Daily RSI(14) for display |
| `src/kite_auth.py` | Daily Kite login with local redirect catcher |
| `src/gui.py` | CustomTkinter app (light theme) |
| `src/test_logic.py` | Offline tests (CI runs these before building) |

Logs next to the EXE: `logs/app_*.log`, `logs/orders_*.csv`, `logs/trades_*.csv`.

## Build

Every push to `main` runs the tests on Ubuntu, then builds `Balfund_Simple_Algo.exe` on
Windows. Download it from the Actions run page.
