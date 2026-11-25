"""
broker.py
=========
Kite session and order handling for NSE cash shares (CNC delivery).

PAPER: every order fills at once at its price; nothing goes to Zerodha.
LIVE : orders go to Zerodha and are tracked until they are final.

Crash safety (multi-day holding):
  * WRITE-AHEAD JOURNAL. Every LIVE order is written to
    state/orders_live.json (with its unique tag) BEFORE it is sent, and
    removed only after its fill is in the book. If the app or PC dies at any
    moment, the next start finds the order again in Zerodha's order book by
    id or tag and settles it: filled -> into the book, open -> tracked,
    rejected/cancelled -> dropped.
  * Tags carry the date and time (BF + MMDDHHMMSS + seq), unique across days.
  * One kite.orders() call per poll covers every working order; the book is
    changed only by a confirmed fill, and the engine skips any order id it has
    already applied (no double counting).

Lessons carried over from the Gaurav / POMR builds:
  * UNKNOWN is not FAILED: an order whose state cannot be read stays working.
  * A network error on place_order() may still have reached Zerodha: the order
    book is searched for the tag before declaring it not placed.
  * Partial fills are applied for exactly the filled quantity.
"""

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import config
import kite_auth
from logger import logger, order_row

TAG_PREFIX = "BF"
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
    day: str = field(default_factory=lambda: config.now_ist().date().isoformat())
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


def journal_path(mode: str):
    return config.STATE_DIR / f"orders_{mode.lower()}.json"


class OrderManager:
    """
    on_fill(action, qty, avg_price, order_id) is called once per order with
    the total confirmed filled quantity.
    """

    def __init__(self, live: bool, on_fill: Callable, kite=None, mode: str = "PAPER"):
        self.live = live
        self.kite = kite
        self.on_fill = on_fill
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orders")
        self.lock = threading.RLock()
        self.working: Dict[int, WorkingOrder] = {}
        self.history: List[WorkingOrder] = []
        self._seq = itertools.count(1)
        self._poll_pending = threading.Event()
        self.journal_file = journal_path(mode)
        self.last_orders: List[dict] = []          # last kite.orders() result

    # -- journal ------------------------------------------------------------
    def _journal_save(self):
        if not self.live:
            return
        with self.lock:
            rows = [asdict(w) for w in self.working.values()]
        config.atomic_write_json(self.journal_file, rows)

    def load_journal(self) -> List[WorkingOrder]:
        """Orders that were working when the app last stopped."""
        import json
        if not self.journal_file.exists():
            return []
        try:
            rows = json.loads(self.journal_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Order journal unreadable (%s): %s", self.journal_file.name, e)
            return []
        out = []
        for r in rows:
            try:
                out.append(WorkingOrder(**r))
            except TypeError:
                logger.error("Journal entry skipped: %s", r)
        return out

    def adopt(self, orders: List[WorkingOrder]) -> None:
        """Put recovered journal orders back under tracking."""
        with self.lock:
            top = max([w.seq for w in orders] + [0])
            self._seq = itertools.count(top + 1000)
            for w in orders:
                w.done = False
                self.working[w.seq] = w
                self.history.append(w)
        self._journal_save()

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

    def working_count(self) -> int:
        with self.lock:
            return len(self.working)

    def orders_view(self) -> List[dict]:
        with self.lock:
            rows = list(self.history)
        return [{"time": w.time, "kind": w.action["kind"], "symbol": w.action["symbol"],
                 "txn": w.txn, "type": w.order_type, "qty": w.qty, "price": w.price,
                 "order_id": w.order_id or "", "status": w.status, "filled": w.filled,
                 "avg": w.avg_price, "msg": w.message} for w in reversed(rows)]

    # -- submit -------------------------------------------------------------
    def _new_tag(self, seq: int) -> str:
        return f"{TAG_PREFIX}{config.now_ist().strftime('%m%d%H%M%S')}{seq % 10000:04d}"[:20]

    def submit(self, action: dict, tick: float = 0.05) -> None:
        s = config.SETTINGS
        kind = action["kind"]
        txn = "SELL" if kind in SELL_KINDS else "BUY"
        otype = s["exit_order_type"] if txn == "SELL" else s["entry_order_type"]
        price = None if otype == "MARKET" else _round_tick(float(action["price"]), tick)
        seq = next(self._seq)
        w = WorkingOrder(seq=seq, action=action, txn=txn, order_type=otype,
                         qty=int(action["qty"]), price=price, tag=self._new_tag(seq))
        with self.lock:
            self.working[seq] = w
            self.history.append(w)
        logger.info("ORDER %s %s %s x%d %s%s  | %s  [tag %s]", kind, txn, action["symbol"], w.qty,
                    otype, f" @ {price:.2f}" if price else "", action.get("reason", ""), w.tag)
        if not self.live:
            w.order_id = f"PAPER-{w.tag}"
            self._final(w, "COMPLETE", w.qty, float(action["price"]), "paper fill")
            return
        self._journal_save()                       # write-ahead: before it is sent
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

    def _fetch_orders(self) -> Optional[List[dict]]:
        try:
            rows = self.kite.orders() or []
            self.last_orders = rows
            return rows
        except Exception as e:
            logger.warning("orders(): %s", e)
            return None

    @staticmethod
    def _match(w: WorkingOrder, rows: List[dict]) -> Optional[dict]:
        hit = None
        for o in rows:
            if (w.order_id and o.get("order_id") == w.order_id) or \
                    o.get("tag") == w.tag or w.tag in (o.get("tags") or []):
                hit = o                       # orders() lists updates oldest first
        return hit

    def _live_place(self, w: WorkingOrder):
        from kiteconnect import exceptions as kex
        try:
            w.order_id = self.kite.place_order(**self._params(w))
            w.status = "OPEN"
            self._journal_save()
            self._log(w, "PLACED")
        except (kex.InputException, kex.OrderException, kex.TokenException,
                kex.PermissionException) as e:
            self._final(w, "REJECTED", 0, None, f"not placed: {e}")
        except Exception as e:
            # network / timeout: it may have reached Zerodha
            time.sleep(1.5)
            rows = self._fetch_orders()
            o = self._match(w, rows) if rows is not None else None
            if o:
                w.order_id = o.get("order_id")
                w.status = "OPEN"
                self._journal_save()
                self._log(w, f"PLACED (found by tag after: {e})")
            elif rows is not None:
                self._final(w, "REJECTED", 0, None, f"not placed: {e}")
            else:
                w.status = "UNKNOWN"
                w.message = f"place uncertain: {e}"
                self._journal_save()
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

    def sync_now(self) -> None:
        """Blocking settle of every working order (used at start-up)."""
        if self.live:
            self._poll(startup=True)

    def _poll(self, startup: bool = False):
        try:
            rows = self._fetch_orders()
            if rows is None:
                return                                   # unknown != failed
            with self.lock:
                todo = list(self.working.values())
            timeout = float(config.SETTINGS.get("limit_order_timeout_s", 60))
            today = config.now_ist().date().isoformat()
            for w in todo:
                o = self._match(w, rows)
                if o is None:
                    if w.day != today:
                        # a previous day's order: not in today's order book.
                        # The engine settles it against holdings.
                        if startup:
                            w.status = "PREVIOUS DAY"
                        continue
                    if w.order_id is None and time.time() - w.placed_at > 30:
                        self._final(w, "REJECTED", 0, None, "never reached Zerodha")
                    continue
                w.order_id = o.get("order_id") or w.order_id
                st = o.get("status", "UNKNOWN")
                filled = int(o.get("filled_quantity") or 0)
                avg = o.get("average_price")
                avg = float(avg) if avg else None
                if st in FINAL:
                    self._final(w, st, filled, avg, o.get("status_message") or "")
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
            self._journal_save()
        finally:
            self._poll_pending.clear()

    def previous_day(self) -> List[WorkingOrder]:
        with self.lock:
            return [w for w in self.working.values() if w.status == "PREVIOUS DAY"]

    def settle_external(self, w: WorkingOrder, filled: int, price: Optional[float], why: str):
        """Close a journal order from outside (previous-day settlement)."""
        self._final(w, "COMPLETE" if filled > 0 else "CANCELLED", filled, price, why)

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
        self._log(w, status)
        if status != "COMPLETE":
            logger.warning("%s %s %s x%d: %s %s (filled %d)", w.action["kind"], w.txn,
                           w.action["symbol"], w.qty, status, msg, filled)
        if filled > 0:
            px = avg if avg else (w.price or float(w.action["price"]))
            try:
                self.on_fill(w.action, filled, float(px), w.order_id or w.tag)
            except Exception as e:
                # keep it in the journal so the next start retries it
                logger.exception("applying fill to the book failed (kept in journal): %s", e)
                return
        with self.lock:
            self.working.pop(w.seq, None)
        self._journal_save()

    def _log(self, w: WorkingOrder, result: str):
        order_row({
            "kind": w.action["kind"], "symbol": w.action["symbol"], "txn": w.txn,
            "order_type": w.order_type, "qty": w.qty,
            "price": f"{w.price:.2f}" if w.price else "",
            "order_id": w.order_id or "", "tag": w.tag, "status": result, "filled": w.filled,
            "avg_price": f"{w.avg_price:.2f}" if w.avg_price else "",
            "message": w.message, "mode": "LIVE" if self.live else "PAPER",
        })

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=False)
