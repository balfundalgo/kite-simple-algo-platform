"""
engine.py
=========
The running loop.

  start   -> NSE instrument list, resolve watchlist symbols, load the book,
             quote snapshot (REST), daily RSI history, broker check (LIVE),
             WebSocket in QUOTE mode for NIFTY 50 + every watchlist share
  loop    -> every second, inside trading hours and with the kill switch
             off: portfolio.decide() -> orders. LIVE orders are polled
             until final; only confirmed fills change the book.
  kill    -> kill switch on: no new orders, working orders cancelled.
             Holdings are delivery shares and are NOT sold by the switch.

The book (slots, reserve, holdings, buy dates) lives in
state/book_paper.json or state/book_live.json, so paper testing can never
touch the live book. It survives restarts and is the source of truth for
ageing (buy dates) and averaging (last buy price).
"""

import json
import threading
import time
from datetime import timedelta
from typing import Dict

from kiteconnect import KiteTicker

import config
import portfolio as pf
import watchlist
from broker import SESSION, OrderManager
from indicators import live_rsi
from logger import logger, trade_row

NIFTY_KEY = "NSE:NIFTY 50"
NIFTY_TOKEN = 256265


# ----------------------------------------------------------------------
# book persistence
# ----------------------------------------------------------------------
def book_path(mode: str):
    return config.STATE_DIR / f"book_{mode.lower()}.json"


def load_book(mode: str) -> pf.Book:
    p = book_path(mode)
    if p.exists():
        try:
            return pf.Book.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            logger.error("Could not read %s: %s", p.name, e)
            raise
    return pf.Book()


def save_book(mode: str, book: pf.Book) -> None:
    config.atomic_write_json(book_path(mode), book.to_dict())


class Quote:
    __slots__ = ("ltp", "open", "high", "low", "prev_close", "closes", "rsi")

    def __init__(self):
        self.ltp = self.open = self.high = self.low = self.prev_close = None
        self.closes = []
        self.rsi = None


class Engine:
    def __init__(self, status_cb=None):
        self.status_cb = status_cb or (lambda snap: None)
        self.mode = config.SETTINGS["mode"]
        self.live = self.mode == "LIVE"
        self.lock = threading.RLock()
        self.running = False
        self.status = "IDLE"
        self.thread = None
        self.kws = None
        self.feed_live = False
        self.ticks = 0
        self.book = load_book(self.mode)
        self.index: Dict[str, dict] = {}
        self.tokens: Dict[str, int] = {}          # symbol -> token
        self.by_token: Dict[int, str] = {}
        self.quotes: Dict[str, Quote] = {NIFTY_KEY: Quote()}
        self.ticksize: Dict[str, float] = {}
        self.orders = None
        self.broker_check = "—"
        self._sub = set()
        self._last_poll = 0.0
        self._kill_seen = bool(config.SETTINGS.get("kill_switch"))
        self._dirty = False

    # ==================================================================
    # control
    # ==================================================================
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="engine")
        self.thread.start()

    def stop(self):
        self.running = False
        self._close_feed()
        if self.orders:
            self.orders.shutdown()
        self._set_status("STOPPED")
        logger.info("Engine stopped. Working LIVE orders (if any) stay at Zerodha.")

    # ==================================================================
    # main thread
    # ==================================================================
    def _run(self):
        try:
            self._set_status("STARTING")
            if not SESSION.logged_in:
                raise RuntimeError("Not logged in. Log in on the Account tab first.")
            if not self.book.initialised:
                raise RuntimeError("Portfolio not initialised. Settings tab -> Initialise portfolio.")
            kite = SESSION.kite
            logger.info("Mode %s | account %s | book: capital %.0f, %d slots, reserve %.0f, "
                        "%d holdings", self.mode, SESSION.user_id, self.book.capital,
                        len(self.book.slots), self.book.reserve, len(self.book.holdings))

            self._load_instruments(kite)
            self.orders = OrderManager(self.live, self._on_fill, kite=kite)
            self._refresh_symbols()
            self._snapshot_quotes(kite)
            threading.Thread(target=self._load_rsi, args=(kite,), daemon=True,
                             name="rsi").start()
            if self.live:
                self._reconcile(kite)
            self._connect_feed()
            self._set_status("RUNNING")

            while self.running:
                self._step()
                self._emit()
                time.sleep(1.0)
        except Exception as e:
            logger.exception("Engine error: %s", e)
            self._set_status("ERROR")
            self.running = False
        finally:
            self._emit()

    # ==================================================================
    # set-up
    # ==================================================================
    def _load_instruments(self, kite):
        logger.info("Loading NSE instruments...")
        self.index = watchlist.build_index(kite.instruments("NSE"))
        logger.info("%d NSE equities loaded", len(self.index))
        changed, missing = watchlist.resolve_all(self.index)
        for old, new in changed:
            logger.info("Watchlist: %s -> NSE symbol %s", old, new)
        for m in missing:
            logger.error("Watchlist: '%s' is not an NSE symbol. Fix it on the Watchlist tab; "
                         "it is skipped until then.", m)

    def _refresh_symbols(self):
        """Watchlist + held symbols -> tokens. Returns new tokens."""
        syms = {w.symbol for w in watchlist.items()} | set(self.book.holdings)
        new = []
        for sym in syms:
            if sym in self.tokens:
                continue
            row = self.index.get(sym)
            if not row:
                continue
            tok = int(row["instrument_token"])
            self.tokens[sym] = tok
            self.by_token[tok] = sym
            self.ticksize[sym] = float(row.get("tick_size") or 0.05)
            self.quotes.setdefault(sym, Quote())
            new.append(tok)
        return new

    def _snapshot_quotes(self, kite, syms=None):
        keys = [NIFTY_KEY] + [f"NSE:{s}" for s in (syms or self.tokens)]
        for i in range(0, len(keys), 400):
            try:
                data = kite.quote(keys[i:i + 400])
            except Exception as e:
                logger.warning("quote snapshot: %s", e)
                continue
            with self.lock:
                for k, q in (data or {}).items():
                    sym = k if k == NIFTY_KEY else k.split(":", 1)[1]
                    self._apply_quote(sym, q.get("last_price"), q.get("ohlc") or {})

    def _apply_quote(self, sym, ltp, ohlc):
        qt = self.quotes.setdefault(sym, Quote())
        if ltp:
            qt.ltp = float(ltp)
        if ohlc:
            qt.open = ohlc.get("open") or qt.open
            qt.high = ohlc.get("high") or qt.high
            qt.low = ohlc.get("low") or qt.low
            qt.prev_close = ohlc.get("close") or qt.prev_close
        if qt.closes:
            qt.rsi = live_rsi(qt.closes, qt.ltp)

    def _load_rsi(self, kite, syms=None):
        """Daily closes for RSI(14). Needs Kite historical-data access;
        failure only hides the RSI column."""
        today = config.now_ist().date()
        frm = today - timedelta(days=60)
        todo = [(NIFTY_KEY, NIFTY_TOKEN)] if syms is None else []
        todo += [(s, self.tokens[s]) for s in (syms or list(self.tokens)) if s in self.tokens]
        ok = 0
        for sym, tok in todo:
            if not self.running:
                return
            try:
                rows = kite.historical_data(tok, frm, today, "day")
                closes = [float(r["close"]) for r in rows
                          if r["date"].date() < today] if rows else []
                with self.lock:
                    qt = self.quotes.setdefault(sym, Quote())
                    qt.closes = closes[-40:]
                    qt.rsi = live_rsi(qt.closes, qt.ltp)
                ok += 1
            except Exception as e:
                logger.warning("RSI history %s: %s", sym, e)
                if "permission" in str(e).lower() or "subscription" in str(e).lower():
                    logger.warning("Historical data not enabled on this Kite app: RSI hidden")
                    return
            time.sleep(0.35)              # historical API: ~3 requests / second
        logger.info("RSI history loaded for %d instruments", ok)

    def _reconcile(self, kite):
        """LIVE: compare the book with Zerodha. Informational: nothing is changed."""
        try:
            broker = {}
            for h in kite.holdings() or []:
                broker[h["tradingsymbol"]] = broker.get(h["tradingsymbol"], 0) + \
                    int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
            for p in (kite.positions() or {}).get("day", []):
                if p.get("product") == "CNC" and p.get("exchange") == "NSE":
                    broker[p["tradingsymbol"]] = broker.get(p["tradingsymbol"], 0) + \
                        int(p.get("buy_quantity") or 0) - int(p.get("sell_quantity") or 0)
            bad = []
            for sym, h in self.book.holdings.items():
                if broker.get(sym, 0) < h.qty:
                    bad.append(f"{sym} book {h.qty} / Zerodha {broker.get(sym, 0)}")
            if bad:
                self.broker_check = "MISMATCH"
                logger.error("BROKER CHECK: Zerodha holds less than the book for: %s. "
                             "Verify in Kite before trading.", "; ".join(bad))
            else:
                self.broker_check = "OK"
                logger.info("Broker check OK: every book holding is present at Zerodha")
        except Exception as e:
            self.broker_check = "FAILED"
            logger.warning("Broker check failed: %s", e)

    # ==================================================================
    # WebSocket (QUOTE mode: LTP + day OHLC)
    # ==================================================================
    def _connect_feed(self):
        self._sub = {NIFTY_TOKEN} | set(self.tokens.values())
        self.kws = KiteTicker(config.SETTINGS["account"]["api_key"].strip(),
                              SESSION.kite.access_token)

        def on_connect(ws, resp):
            self.feed_live = True
            toks = list(self._sub)
            ws.subscribe(toks)
            ws.set_mode(ws.MODE_QUOTE, toks)
            logger.info("WebSocket connected (QUOTE), %d instruments", len(toks))

        def on_close(ws, code, reason):
            self.feed_live = False
            if self.running:
                logger.warning("WebSocket closed: %s %s", code, reason)

        def on_error(ws, code, reason):
            self.feed_live = False
            logger.warning("WebSocket error: %s %s", code, reason)

        self.kws.on_connect = on_connect
        self.kws.on_ticks = lambda ws, ticks: self._on_ticks(ticks)
        self.kws.on_close = on_close
        self.kws.on_error = on_error
        self.kws.on_reconnect = lambda ws, n: logger.warning("WebSocket reconnecting, attempt %d", n)
        self.kws.on_noreconnect = lambda ws: logger.error("WebSocket could not reconnect")
        self.kws.connect(threaded=True)

    def _subscribe(self, toks):
        if not toks:
            return
        self._sub |= set(toks)
        if self.kws is not None and self.feed_live:
            try:
                self.kws.subscribe(list(toks))
                self.kws.set_mode(self.kws.MODE_QUOTE, list(toks))
            except Exception as e:
                logger.warning("subscribe: %s", e)

    def _close_feed(self):
        if self.kws is not None:
            try:
                self.kws.close(1000, "stop")   # never kws.stop(): the reactor cannot restart
            except Exception:
                pass
            self.kws = None
        self.feed_live = False

    def _on_ticks(self, ticks):
        with self.lock:
            for t in ticks:
                self.ticks += 1
                tok = t.get("instrument_token")
                sym = NIFTY_KEY if tok == NIFTY_TOKEN else self.by_token.get(tok)
                if sym:
                    self._apply_quote(sym, t.get("last_price"), t.get("ohlc") or {})

    # ==================================================================
    # trading
    # ==================================================================
    def trading_open(self) -> bool:
        s = config.SETTINGS
        if s.get("ignore_market_hours") and not self.live:
            return True
        return config.in_window(config.now_ist(), s["trade_start"], s["trade_end"])

    def _step(self):
        s = config.SETTINGS
        new = self._refresh_symbols()
        if new:
            syms = [self.by_token[t] for t in new]
            logger.info("Watchlist added %s: subscribing", ", ".join(syms))
            self._subscribe(new)
            threading.Thread(target=self._snapshot_quotes, args=(SESSION.kite, syms),
                             daemon=True).start()
            threading.Thread(target=self._load_rsi, args=(SESSION.kite, syms),
                             daemon=True).start()

        kill = bool(s.get("kill_switch"))
        if kill and not self._kill_seen:
            logger.warning("KILL SWITCH ON: no orders; cancelling working orders")
            self.orders.cancel_all("kill switch")
        elif not kill and self._kill_seen:
            logger.warning("Kill switch released: trading resumes")
        self._kill_seen = kill

        if self.live and time.time() - self._last_poll > 2:
            self._last_poll = time.time()
            self.orders.poll()

        if kill:
            self.status = "KILLED"
        elif not self.trading_open():
            self.status = "MARKET CLOSED"
        else:
            self.status = "RUNNING"
            today = config.now_ist().date()
            with self.lock:
                ltps = {sym: q.ltp for sym, q in self.quotes.items() if q.ltp}
                items = [w for w in watchlist.items() if w.symbol in self.index]
                acts = pf.decide(self.book, items, ltps, today, s,
                                 busy=self.orders.busy_symbols(),
                                 reserve_busy=self.orders.reserve_busy(),
                                 ticks=self.ticksize,
                                 busy_slots=self.orders.busy_slots())
                for a in acts:
                    self.orders.submit(a, self.ticksize.get(a["symbol"], 0.05))
        if self._dirty:
            with self.lock:
                save_book(self.mode, self.book)
                self._dirty = False

    def _on_fill(self, action, qty, price):
        today = config.now_ist().date()
        with self.lock:
            kind, sym = action["kind"], action["symbol"]
            pnl = ""
            slot = action.get("slot_id")
            if kind == "BUY":
                h = pf.apply_buy(self.book, sym, qty, price, "SLOT", today,
                                 slot_id=action["slot_id"], market_type=action["market_type"])
            elif kind == "ADD":
                h = pf.apply_buy(self.book, sym, qty, price, "RESERVE", today)
                slot = h.slot_id
            else:
                h = self.book.holdings.get(sym)
                slot = h.slot_id if h else ""
                pnl = pf.apply_sell(self.book, sym, qty, price)
                h = self.book.holdings.get(sym)
                if kind == "SELL_SLEVEL" and h is not None:
                    h.s_done = action["level"]
            save_book(self.mode, self.book)
            logger.info("FILL %s %s x%d @ %.2f%s | reserve %.0f, slot cash %.0f",
                        kind, sym, qty, price,
                        f" | P/L {pnl:+,.2f}" if pnl != "" else "",
                        self.book.reserve, self.book.slot_cash)
            trade_row({
                "kind": kind, "symbol": sym, "slot": slot, "qty": qty,
                "price": f"{price:.2f}", "amount": f"{qty * price:.2f}",
                "avg_cost": f"{h.avg:.2f}" if h else "",
                "realized_pnl": f"{pnl:.2f}" if pnl != "" else "",
                "reason": action.get("reason", ""), "mode": self.mode,
            })

    # ==================================================================
    # status
    # ==================================================================
    def _set_status(self, st):
        self.status = st
        self._emit()

    def _emit(self):
        try:
            self.status_cb(self.snapshot())
        except Exception as e:
            logger.debug("status cb: %s", e)

    def snapshot(self):
        s = config.SETTINGS
        today = config.now_ist().date()
        with self.lock:
            ltps = {sym: q.ltp for sym, q in self.quotes.items() if q.ltp}
            items = watchlist.items()
            by_sym = {w.symbol: w for w in items}
            busy = self.orders.busy_symbols() if self.orders else set()
            holdings = []
            for sl in self.book.slots:
                h = self.book.holdings.get(sl.symbol) if sl.symbol else None
                if h is None:
                    holdings.append({"slot": sl.id, "symbol": "", "cash": sl.cash})
                    continue
                w = by_sym.get(h.symbol)
                mt = (w.market_type if w else "") or h.market_type
                ltp = ltps.get(h.symbol)
                tgt = pf.target_price(h, mt, today, s, self.ticksize.get(h.symbol, 0.05))
                holdings.append({
                    "slot": sl.id, "symbol": h.symbol, "cash": sl.cash, "type": mt,
                    "qty": h.qty, "avg": h.avg, "ltp": ltp,
                    "pnl": (ltp - h.avg) * h.qty if ltp else None,
                    "pnl_pct": (ltp / h.avg - 1) * 100 if ltp and h.avg else None,
                    "target": tgt,
                    "age": (today - pf._d(h.first_buy_date)).days,
                    "steps": pf.ageing_steps(h.first_buy_date, today, int(s["ageing_days"])),
                    "avg_trigger": pf.avg_down_trigger(h, s) if pf.avg_down_allowed(h, s) else None,
                    "adds": h.adds, "s_level": (w.s_level if w else ""), "s_done": h.s_done,
                    "busy": h.symbol in busy,
                })
            watch = []
            for w in items:
                q = self.quotes.get(w.symbol)
                ltp = q.ltp if q else None
                if w.symbol not in self.index and self.index:
                    state = "NOT FOUND"
                elif w.symbol in self.book.holdings:
                    state = "HELD"
                elif w.symbol in busy:
                    state = "ORDER"
                elif not w.enabled:
                    state = "OFF"
                elif pf.entry_ready(w, ltp, s):
                    state = "READY" if self.book.free_slots() else "READY · no slot"
                else:
                    state = "WAITING"
                watch.append({
                    "symbol": w.symbol, "name": w.name, "buy": w.buy_price, "type": w.market_type,
                    "s_level": w.s_level, "enabled": w.enabled, "ltp": ltp,
                    "open": q.open if q else None, "high": q.high if q else None,
                    "low": q.low if q else None, "prev": q.prev_close if q else None,
                    "rsi": q.rsi if q else None,
                    "dist": (ltp / w.buy_price - 1) * 100 if ltp and w.buy_price else None,
                    "state": state,
                })
            n = self.quotes.get(NIFTY_KEY)
            return {
                "status": self.status, "mode": self.mode, "feed": self.feed_live,
                "ticks": self.ticks, "broker_check": self.broker_check,
                "kill": bool(s.get("kill_switch")),
                "capital": self.book.capital, "reserve": self.book.reserve,
                "slot_cash": self.book.slot_cash, "invested": self.book.invested(),
                "value": self.book.market_value(ltps), "realized": self.book.realized,
                "unrealized": self.book.unrealized(ltps),
                "nifty": {"ltp": n.ltp, "open": n.open, "high": n.high, "low": n.low,
                          "prev": n.prev_close, "rsi": n.rsi} if n else {},
                "holdings": holdings, "watch": watch,
                "orders": self.orders.orders_view() if self.orders else [],
            }
