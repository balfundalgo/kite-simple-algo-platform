"""
engine.py
=========
The running loop.

Every start (each trading day):
  1. NSE share list (today's download, or yesterday's cache with a warning)
  2. the book from disk: slots, holdings, buy dates, average costs, adds,
     cash, reserve -> yesterday's positions are remembered
  3. LIVE: the order journal. Orders that were working when the app last
     stopped are found in Zerodha's order book and settled; previous-day
     orders are settled against holdings
  4. LIVE: reconciliation with Zerodha holdings + today's CNC positions.
     A share where Zerodha has LESS than the book is frozen until the user
     chooses Sync to Zerodha or Keep book (Reconcile tab)
  5. quotes, RSI, WebSocket -> management resumes

While running:
  * every second, inside trading hours, kill switch off:
    portfolio.decide() -> orders (frozen shares are skipped)
  * every 2 s (LIVE): one kite.orders() call settles working orders
  * every `reconcile_seconds` (LIVE): holdings reconciliation again
  * the book is saved after every fill; a fill whose Zerodha order id is
    already in the book is never applied twice

The book lives in state/book_paper.json or state/book_live.json, so paper
testing can never touch the live book.
"""

import json
import threading
import time
from datetime import timedelta
from typing import Dict

from kiteconnect import KiteTicker

import config
import instruments
import portfolio as pf
import reconcile as rc
import watchlist
from broker import SESSION, OrderManager
from indicators import live_rsi
from logger import logger, trade_row

NIFTY_KEY = "NSE:NIFTY 50"
NIFTY_TOKEN = 256265
MISMATCH_STRIKES = 2          # consecutive checks before a share is frozen


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
        self._sub = set()
        self._last_poll = 0.0
        self._kill_seen = bool(config.SETTINGS.get("kill_switch"))
        # reconciliation
        self.recon_rows = []
        self.recon_time = None
        self.recon_state = "PAPER" if not self.live else "—"
        self.frozen = set()
        self._strikes: Dict[str, int] = {}
        self._recon_lock = threading.Lock()
        self._last_recon = 0.0
        self.holdings_avg: Dict[str, float] = {}

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
        logger.info("Engine stopped. Book saved; working LIVE orders stay in the journal "
                    "and are settled on the next start.")

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
            logger.info("=== START %s | account %s ===", self.mode, SESSION.user_id)
            logger.info("Book restored: capital %.0f, %d slots, reserve %.0f, realized %+.2f, "
                        "%d holdings%s", self.book.capital, len(self.book.slots), self.book.reserve,
                        self.book.realized, len(self.book.holdings),
                        "".join(f"\n      {h.symbol} x{h.qty} avg {h.avg:.2f} since "
                                f"{h.first_buy_date} (slot {h.slot_id}, adds {h.adds})"
                                for h in self.book.holdings.values()))

            self._load_instruments(kite)
            self.orders = OrderManager(self.live, self._on_fill, kite=kite, mode=self.mode)
            recovered = 0
            if self.live:
                journal = self.orders.load_journal()
                recovered = len(journal)
                if journal:
                    logger.warning("Order journal: %d order(s) were working when the app last "
                                   "stopped; settling them with Zerodha", recovered)
                    self.orders.adopt(journal)
                    self.orders.sync_now()

            self._refresh_symbols()
            self._snapshot_quotes(kite)
            threading.Thread(target=self._load_rsi, args=(kite,), daemon=True,
                             name="rsi").start()
            if self.live:
                self._reconcile(startup=True)
            logger.info("Start-up summary: %d holdings restored, %d journal order(s) recovered, "
                        "%d still working, reconciliation %s%s",
                        len(self.book.holdings), recovered, self.orders.working_count(),
                        self.recon_state,
                        f" (FROZEN: {', '.join(sorted(self.frozen))})" if self.frozen else "")
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
        try:
            n = instruments.refresh(kite)
            logger.info("NSE share list: %d shares (%s)", n, instruments.cache_date())
        except Exception as e:
            if instruments.count():
                logger.warning("NSE share list download failed (%s); using cached list from %s",
                               e, instruments.cache_date())
            else:
                raise RuntimeError(f"NSE share list unavailable: {e}")
        self.index = instruments.index()
        changed, missing = watchlist.resolve_all(self.index)
        for old, new in changed:
            logger.info("Watchlist: %s -> NSE symbol %s", old, new)
        for m in missing:
            logger.error("Watchlist: '%s' is not an NSE share. Pick it with the search box on "
                         "the Watchlist tab; it is skipped until then.", m)
        for sym in self.book.holdings:
            if sym not in self.index:
                logger.error("Held share %s is not in today's NSE list (renamed/suspended?). "
                             "It is still in the book; check it in Kite.", sym)

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

    # ==================================================================
    # reconciliation with Zerodha
    # ==================================================================
    def _broker_snapshot(self):
        kite = SESSION.kite
        holdings = kite.holdings() or []
        day = (kite.positions() or {}).get("day", [])
        self.holdings_avg = {h["tradingsymbol"]: float(h.get("average_price") or 0)
                             for h in holdings if h.get("tradingsymbol")}
        return rc.broker_quantities(holdings, day)

    def _settle_previous_day(self, bq):
        """Journal orders from an earlier day are not in today's order book:
        decide them from holdings."""
        for w in self.orders.previous_day():
            sym, kind = w.action["symbol"], w.action["kind"]
            h = self.book.holdings.get(sym)
            have = h.qty if h else 0
            z = bq.get(sym, 0)
            if kind in ("BUY", "ADD"):
                if z - have >= w.qty:
                    px = self.holdings_avg.get(sym) or w.price or float(w.action["price"])
                    logger.warning("Previous-day %s %s x%d found in Zerodha holdings: applied "
                                   "at %.2f", kind, sym, w.qty, px)
                    self.orders.settle_external(w, w.qty, px, "previous-day fill found in holdings")
                else:
                    logger.warning("Previous-day %s %s x%d not in holdings: treated as unfilled",
                                   kind, sym, w.qty)
                    self.orders.settle_external(w, 0, None, "previous-day order not filled")
            else:
                if have - z >= w.qty:
                    q = self.quotes.get(sym)
                    px = w.price or (q.ltp if q and q.ltp else float(w.action["price"]))
                    logger.warning("Previous-day SELL %s x%d confirmed by holdings: applied at %.2f",
                                   sym, w.qty, px)
                    self.orders.settle_external(w, w.qty, px, "previous-day sale confirmed by holdings")
                else:
                    logger.warning("Previous-day SELL %s x%d not reflected in holdings: "
                                   "treated as unfilled", sym, w.qty)
                    self.orders.settle_external(w, 0, None, "previous-day sale not filled")

    def _reconcile(self, startup=False):
        if not self.live or not self._recon_lock.acquire(blocking=False):
            return
        try:
            try:
                bq = self._broker_snapshot()
            except Exception as e:
                self.recon_state = "CHECK FAILED"
                logger.warning("Reconciliation: could not read Zerodha holdings/positions: %s", e)
                return
            if self.orders.previous_day():
                self._settle_previous_day(bq)
            with self.lock:
                book_qty = {s: h.qty for s, h in self.book.holdings.items()}
                rows = rc.compare(book_qty, bq, self.orders.busy_symbols(), self.book.recon_kept)
                changed = False
                for r in rows:
                    sym, st = r["symbol"], r["state"]
                    if st == "MISMATCH":
                        self._strikes[sym] = self._strikes.get(sym, 0) + 1
                        if (startup or self._strikes[sym] >= MISMATCH_STRIKES) and sym not in self.frozen:
                            self.frozen.add(sym)
                            logger.error("RECONCILE: %s book %d but Zerodha %d -> FROZEN (no orders). "
                                         "Reconcile tab: Sync to Zerodha or Keep book.",
                                         sym, r["book"], r["zerodha"])
                        if sym not in self.frozen:
                            r["state"] = "CHECKING"
                    else:
                        self._strikes.pop(sym, None)
                        if sym in self.frozen and st != "BUSY":
                            self.frozen.discard(sym)
                            logger.info("RECONCILE: %s back in line (%s), unfrozen", sym, st)
                        if st in ("OK", "EXTRA") and sym in self.book.recon_kept:
                            self.book.recon_kept.pop(sym)
                            changed = True
                for sym in list(self.book.recon_kept):
                    if sym not in book_qty:
                        self.book.recon_kept.pop(sym)
                        changed = True
                self.frozen &= set(book_qty)
                if changed:
                    save_book(self.mode, self.book)
                self.recon_rows = rows
                self.recon_time = config.now_ist().strftime("%H:%M:%S")
                bad = [r for r in rows if r["state"] == "MISMATCH"]
                self.recon_state = f"MISMATCH ({len(bad)})" if bad else "OK"
            if startup:
                for r in rows:
                    logger.info("  reconcile %-12s book %6d  Zerodha %6d  %s",
                                r["symbol"], r["book"], r["zerodha"], r["state"])
        finally:
            self._recon_lock.release()

    def reconcile_now(self):
        threading.Thread(target=self._reconcile, daemon=True, name="recon").start()

    def recon_sync(self, sym: str):
        """Book takes Zerodha's quantity for `sym` (shortfall booked as sold)."""
        if not self.live:
            return
        bq = self._broker_snapshot()
        with self.lock:
            h = self.book.holdings.get(sym)
            if h is None:
                return
            z = bq.get(sym, 0)
            short = h.qty - z
            if short <= 0:
                logger.info("Sync %s: Zerodha already has %d >= book %d", sym, z, h.qty)
            else:
                px, src = self._sale_price(sym)
                pnl = pf.apply_sell(self.book, sym, short, px)
                self.book.recon_kept.pop(sym, None)
                save_book(self.mode, self.book)
                trade_row({"kind": "RECONCILE", "symbol": sym, "slot": h.slot_id, "qty": short,
                           "price": f"{px:.2f}", "amount": f"{short * px:.2f}",
                           "avg_cost": f"{h.avg:.2f}", "realized_pnl": f"{pnl:.2f}",
                           "reason": f"synced to Zerodha qty {z} (price from {src})",
                           "mode": self.mode})
                logger.warning("SYNC %s: book %d -> %d, %d booked as sold at %.2f (%s), P/L %+.2f%s",
                               sym, z + short, z, short, px, src, pnl,
                               "; slot freed" if z == 0 else "")
            self.frozen.discard(sym)
            self._strikes.pop(sym, None)
        self._reconcile()

    def recon_keep(self, sym: str):
        """User confirms the book is right for the current Zerodha quantity."""
        with self.lock:
            row = next((r for r in self.recon_rows if r["symbol"] == sym), None)
            if row is None:
                return
            self.book.recon_kept[sym] = row["zerodha"]
            save_book(self.mode, self.book)
            self.frozen.discard(sym)
            self._strikes.pop(sym, None)
            row["state"] = "KEPT"
        logger.warning("KEEP BOOK %s: book %d kept although Zerodha shows %d. Trading resumes "
                       "for it; it re-freezes if Zerodha's quantity changes.",
                       sym, row["book"], row["zerodha"])

    def _sale_price(self, sym):
        try:
            sells = [t for t in (SESSION.kite.trades() or [])
                     if t.get("tradingsymbol") == sym and t.get("transaction_type") == "SELL"]
            if sells:
                q = sum(int(t["quantity"]) for t in sells)
                return sum(float(t["average_price"]) * int(t["quantity"]) for t in sells) / q, \
                    "today's Zerodha sell trades"
        except Exception as e:
            logger.warning("trades(): %s", e)
        q = self.quotes.get(sym)
        if q and q.ltp:
            return q.ltp, "LTP"
        return self.book.holdings[sym].avg, "average cost"

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

        if self.live:
            if time.time() - self._last_poll > 2:
                self._last_poll = time.time()
                self.orders.poll()
            every = max(15, int(s.get("reconcile_seconds", 60)))
            if time.time() - self._last_recon > every:
                self._last_recon = time.time()
                self.reconcile_now()

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
                                 busy=self.orders.busy_symbols() | self.frozen,
                                 reserve_busy=self.orders.reserve_busy(),
                                 ticks=self.ticksize,
                                 busy_slots=self.orders.busy_slots())
                for a in acts:
                    self.orders.submit(a, self.ticksize.get(a["symbol"], 0.05))

    def _on_fill(self, action, qty, price, order_id=""):
        today = config.now_ist().date()
        with self.lock:
            if self.book.was_applied(order_id):
                logger.warning("Fill for order %s is already in the book; not applied again",
                               order_id)
                return
            kind, sym = action["kind"], action["symbol"]
            pnl = ""
            slot = action.get("slot_id")
            if kind == "BUY":
                h = pf.apply_buy(self.book, sym, qty, price, "SLOT", today,
                                 slot_id=action["slot_id"], market_type=action["market_type"])
            elif kind == "ADD":
                if sym not in self.book.holdings:
                    logger.error("ADD fill for %s but it is no longer held; booked as a new "
                                 "holding is not possible. Check Kite.", sym)
                    return
                h = pf.apply_buy(self.book, sym, qty, price, "RESERVE", today)
                slot = h.slot_id
            else:
                h = self.book.holdings.get(sym)
                if h is None:
                    logger.error("SELL fill for %s but it is not in the book. Check Kite.", sym)
                    self.book.mark_applied(order_id)
                    save_book(self.mode, self.book)
                    return
                slot = h.slot_id
                pnl = pf.apply_sell(self.book, sym, qty, price)
                h = self.book.holdings.get(sym)
                if kind == "SELL_SLEVEL" and h is not None:
                    h.s_done = action["level"]
            self.book.mark_applied(order_id)
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
            index = self.index or instruments.index()
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
                    "busy": h.symbol in busy, "frozen": h.symbol in self.frozen,
                })
            watch = []
            for w in items:
                q = self.quotes.get(w.symbol)
                ltp = q.ltp if q else None
                if index and w.symbol not in index:
                    state = "NOT FOUND"
                elif w.symbol in self.frozen:
                    state = "FROZEN"
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
                "ticks": self.ticks, "kill": bool(s.get("kill_switch")),
                "recon_state": self.recon_state, "recon_time": self.recon_time,
                "recon": [dict(r) for r in self.recon_rows], "frozen": sorted(self.frozen),
                "working": self.orders.working_count() if self.orders else 0,
                "capital": self.book.capital, "reserve": self.book.reserve,
                "slot_cash": self.book.slot_cash, "invested": self.book.invested(),
                "value": self.book.market_value(ltps), "realized": self.book.realized,
                "unrealized": self.book.unrealized(ltps),
                "nifty": {"ltp": n.ltp, "open": n.open, "high": n.high, "low": n.low,
                          "prev": n.prev_close, "rsi": n.rsi} if n else {},
                "holdings": holdings, "watch": watch,
                "orders": self.orders.orders_view() if self.orders else [],
            }
