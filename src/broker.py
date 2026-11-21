"""
broker.py
=========
Kite session and order handling for NSE cash shares (CNC delivery).

PAPER: every order fills at once at its price; nothing goes to Zerodha.
LIVE : orders go to Zerodha and are tracked until they are final.

Lessons carried over from the Gaurav / POMR builds:
  * UNKNOWN is not FAILED. An order whose state cannot be read (timeout,
    network error) stays WORKING and is polled again; the book is only
    changed by a confirmed fill. A slot is never freed on a guess.
  * place_order() that raises a network error may still have reached the
    exchange, so every order carries a unique tag and the order book is
    searched for that tag before declaring it not placed.
  * Partial fills are applied for exactly the filled quantity.
  * All broker calls run on one worker thread so the engine never blocks
    and orders stay in sequence.
"""

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import config
import kite_auth
from logger import logger, order_row

TAG_PREFIX = "BFSAP"
FINAL = ("COMPLETE", "REJECTED", "CANCELLED")
SELL_KINDS = ("SELL_TARGET", "SELL_SLEVEL")


# ----------------------------------------------------------------------
# session
# ----------------------------------------------------------------------
class Session:
    def __init__(self):
        self.kite = None
        self.user_id = None
        self.error = ""

    @property
    def logged_in(self):
        return self.kite is not None

    def login(self, force=False):
        s = config.SETTINGS
        acct = dict(s["account"])
        acct.setdefault("name", "main")
        try:
            self.kite, profile = kite_auth.login(acct, s["redirect_host"],
                                                 int(s["redirect_port"]), force=force)
            self.user_id = profile["user_id"]
            self.error = ""
        except Exception as e:
            self.kite = None
            self.user_id = None
            self.error = str(e)
            logger.error("Login failed: %s", e)
            raise


SESSION = Session()


# ----------------------------------------------------------------------
# orders
# ----------------------------------------------------------------------
@dataclass
class WorkingOrder:
    seq: int
    action: dict
    txn: str
    order_type: str
    qty: int
    price: Optional[float]
    tag: str
    placed_at: float = field(default_factory=time.time)
    order_id: Optional[str] = None
    status: str = "SENDING"
    filled: int = 0
    avg_price: Optional[float] = None
    message: str = ""
    cancel_sent: bool = False
    done: bool = False
    time: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


def _round_tick(x, tick=0.05):
    return round(round(x / tick) * tick, 2)


class OrderManager:
    """
    on_fill(action, qty, avg_price) is called once per confirmed fill
    (once per order, with the total filled quantity).
    """

    def __init__(self, live: bool, on_fill: Callable, kite=None):
        self.live = live
        self.kite = kite
        self.on_fill = on_fill
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orders")
        self.lock = threading.RLock()
        self.working: Dict[int, WorkingOrder] = {}
        self.history: List[WorkingOrder] = []
        self._seq = itertools.count(1)
        self._run_id = datetime.now().strftime("%H%M%S")
        self._poll_pending = threading.Event()

    # -- state the engine asks about ------------------------------------
    def busy_symbols(self) -> set:
        with self.lock:
            return {w.action["symbol"] for w in self.working.values()}

    def busy_slots(self) -> set:
        with self.lock:
            return {w.action.get("slot_id") for w in self.working.values()
                    if w.action["kind"] == "BUY"}

    def reserve_busy(self) -> bool:
        with self.lock:
            return any(w.action["kind"] == "ADD" for w in self.working.values())

    def orders_view(self) -> List[dict]:
        with self.lock:
            rows = list(self.history)
        return [{"time": w.time, "kind": w.action["kind"], "symbol": w.action["symbol"],
                 "txn": w.txn, "type": w.order_type, "qty": w.qty, "price": w.price,
                 "order_id": w.order_id or "", "status": w.status, "filled": w.filled,
                 "avg": w.avg_price, "msg": w.message} for w in reversed(rows)]

    # -- submit -------------------------------------------------------------
    def submit(self, action: dict, tick: float = 0.05) -> None:
        s = config.SETTINGS
        kind = action["kind"]
        txn = "SELL" if kind in SELL_KINDS else "BUY"
        otype = s["exit_order_type"] if txn == "SELL" else s["entry_order_type"]
        if kind == "ADD":
            otype = s["entry_order_type"]
        price = None if otype == "MARKET" else _round_tick(float(action["price"]), tick)
        seq = next(self._seq)
        w = WorkingOrder(seq=seq, action=action, txn=txn, order_type=otype,
                         qty=int(action["qty"]), price=price,
                         tag=f"{TAG_PREFIX}{self._run_id}{seq:04d}"[:20])
        with self.lock:
            self.working[seq] = w
            self.history.append(w)
        logger.info("ORDER %s %s %s x%d %s%s  | %s", kind, txn, action["symbol"], w.qty, otype,
                    f" @ {price:.2f}" if price else "", action.get("reason", ""))
        if not self.live:
            fill_px = float(action["price"])
            w.order_id = f"PAPER-{seq}"
            self._final(w, "COMPLETE", w.qty, fill_px, "paper fill")
            return
        self.pool.submit(self._live_place, w)

    # -- live ---------------------------------------------------------------
    def _params(self, w: WorkingOrder) -> dict:
        k = self.kite
        p = dict(variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE,
                 tradingsymbol=w.action["symbol"], transaction_type=w.txn,
                 quantity=w.qty, product=k.PRODUCT_CNC, order_type=w.order_type,
                 validity=k.VALIDITY_DAY, tag=w.tag)
        if w.price is not None:
            p["price"] = w.price
        if w.order_type == "MARKET":
            mp = config.SETTINGS.get("market_protection")
            if mp not in (None, 0, "0", ""):
                p["market_protection"] = int(mp)
        return p

    def _find_by_tag(self, tag: str) -> Optional[str]:
        try:
            for o in self.kite.orders() or []:
                if o.get("tag") == tag or tag in (o.get("tags") or []):
                    return o.get("order_id")
        except Exception as e:
            logger.warning("orders() while searching tag %s: %s", tag, e)
            return "UNKNOWN"
        return None

    def _live_place(self, w: WorkingOrder):
        from kiteconnect import exceptions as kex
        try:
            w.order_id = self.kite.place_order(**self._params(w))
            w.status = "OPEN"
            self._log(w, "PLACED")
        except (kex.InputException, kex.OrderException, kex.TokenException,
                kex.PermissionException) as e:
            self._final(w, "REJECTED", 0, None, f"not placed: {e}")
        except Exception as e:
            # network / timeout: it may have reached Zerodha
            time.sleep(1.5)
            oid = self._find_by_tag(w.tag)
            if oid and oid != "UNKNOWN":
                w.order_id = oid
                w.status = "OPEN"
                self._log(w, f"PLACED (recovered by tag after: {e})")
            elif oid is None:
                self._final(w, "REJECTED", 0, None, f"not placed: {e}")
            else:
                w.status = "UNKNOWN"
                w.message = f"place uncertain: {e}"
                self._log(w, "UNKNOWN")
                logger.error("%s %s: order state UNKNOWN (tag %s). Kept working; "
                             "it will be checked again.", w.txn, w.action["symbol"], w.tag)

    def poll(self):
        """LIVE: called by the engine every couple of seconds."""
        if not self.live or self._poll_pending.is_set():
            return
        with self.lock:
            if not self.working:
                return
        self._poll_pending.set()
        self.pool.submit(self._poll)

    def _poll(self):
        try:
            with self.lock:
                todo = list(self.working.values())
            timeout = float(config.SETTINGS.get("limit_order_timeout_s", 60))
            for w in todo:
                if w.order_id is None:
                    if w.status == "UNKNOWN":
                        oid = self._find_by_tag(w.tag)
                        if oid and oid != "UNKNOWN":
                            w.order_id = oid
                            w.status = "OPEN"
                        elif oid is None and time.time() - w.placed_at > 30:
                            self._final(w, "REJECTED", 0, None, "never reached Zerodha")
                    continue
                try:
                    hist = self.kite.order_history(w.order_id)
                except Exception as e:
                    w.message = f"history: {e}"
                    continue                    # unknown != failed, try next poll
                if not hist:
                    continue
                last = hist[-1]
                st = last.get("status", "UNKNOWN")
                filled = int(last.get("filled_quantity") or 0)
                avg = last.get("average_price")
                avg = float(avg) if avg else None
                if st in FINAL:
                    self._final(w, st, filled, avg, last.get("status_message") or "")
                    continue
                w.status = st
                w.filled = filled
                if (w.order_type == "LIMIT" and not w.cancel_sent
                        and time.time() - w.placed_at > timeout):
                    try:
                        self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR,
                                               order_id=w.order_id)
                        w.cancel_sent = True
                        self._log(w, f"CANCEL SENT (unfilled after {timeout:.0f}s)")
                    except Exception as e:
                        logger.warning("cancel %s: %s", w.order_id, e)
        finally:
            self._poll_pending.clear()

    def cancel_all(self, why: str):
        if not self.live:
            return

        def work():
            with self.lock:
                todo = [w for w in self.working.values() if w.order_id and not w.cancel_sent]
            for w in todo:
                try:
                    self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=w.order_id)
                    w.cancel_sent = True
                    self._log(w, f"CANCEL SENT ({why})")
                except Exception as e:
                    logger.warning("cancel %s: %s", w.order_id, e)
        self.pool.submit(work)

    # -- shared ---------------------------------------------------------------
    def _final(self, w: WorkingOrder, status: str, filled: int, avg: Optional[float], msg: str):
        w.status = status
        w.filled = filled
        w.avg_price = avg
        w.message = msg
        w.done = True
        with self.lock:
            self.working.pop(w.seq, None)
        self._log(w, status)
        if status != "COMPLETE":
            logger.warning("%s %s %s x%d: %s %s (filled %d)", w.action["kind"], w.txn,
                           w.action["symbol"], w.qty, status, msg, filled)
        if filled > 0:
            px = avg if avg else (w.price or float(w.action["price"]))
            try:
                self.on_fill(w.action, filled, float(px))
            except Exception as e:
                logger.exception("applying fill to the book failed: %s", e)

    def _log(self, w: WorkingOrder, result: str):
        order_row({
            "kind": w.action["kind"], "symbol": w.action["symbol"], "txn": w.txn,
            "order_type": w.order_type, "qty": w.qty,
            "price": f"{w.price:.2f}" if w.price else "",
            "order_id": w.order_id or "", "status": result, "filled": w.filled,
            "avg_price": f"{w.avg_price:.2f}" if w.avg_price else "",
            "message": w.message, "mode": "LIVE" if self.live else "PAPER",
        })

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=False)
