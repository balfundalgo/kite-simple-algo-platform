"""
portfolio.py
============
All the trading rules and the money book. No broker, network or GUI in
here, so every rule is tested offline (test_logic.py).

Money
-----
  capital x equity%  -> split equally into N slots (one share per slot)
  the rest           -> RESERVE, used only for averaging down (futures parked)

  A slot holds one share at a time. When its share is sold, the proceeds go
  back to that slot and buy the next share from the watchlist.

Rules (client spec + clarifications 21-11-2026)
-----------------------------------------------
  ENTRY     a free slot buys the first watchlist share whose LTP reaches its
            sheet buy price (LTP <= buy x (1 + tolerance%)).
            qty = floor(slot cash / price)
  TARGET    sell all when LTP >= target
            target = multiplier[market type] x average cost
            BULL 1.15 · BEAR 1.09 · BUY ON DIP 1.05 · SELL ON RALLY 1.05
  AGEING    every `ageing_days` (30) the holding stays unsold, the target is
            multiplied by factor[market type] again (repeats):
            BULL 0.95 · BEAR 0.80 · others 0.70
  AVERAGE   LTP <= last buy price x (1 - 5%) and reserve free ->
            buy more with 50% of the free reserve. Repeats from the new
            buy price; optional max adds per holding.
  S-LEVEL   S1..S5 set on a row sells 20/40/60/80/100% (configurable) of the
            holding, only while it is at a loss or close to cost
            (LTP <= avg x (1 + near%)). Fires once per level setting.
  KILL      kill switch on -> no orders at all.

On a sale, proceeds are split between the slot and the reserve in the same
ratio as each funded the holding, so averaging money returns to the reserve.
"""

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List, Optional

from config import MARKET_TYPES, S_LEVELS


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------
def round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


def floor_qty(cash: float, price: float) -> int:
    if price <= 0 or cash <= 0:
        return 0
    return int(math.floor(cash / price + 1e-9))


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def norm_market_type(text) -> str:
    t = str(text or "").strip().upper().replace(" ", "_").replace("-", "_")
    alias = {"BUYONDIP": "BUY_ON_DIP", "BUY_ON_DIPS": "BUY_ON_DIP",
             "SELLONRALLY": "SELL_ON_RALLY", "SELL_ON_RALLIES": "SELL_ON_RALLY"}
    t = alias.get(t, t)
    return t if t in MARKET_TYPES else ""


def norm_s_level(text) -> str:
    t = str(text or "").strip().upper()
    return t if t in S_LEVELS else ""


# ----------------------------------------------------------------------
# data
# ----------------------------------------------------------------------
@dataclass
class WatchItem:
    symbol: str                    # NSE trading symbol, e.g. YESBANK
    buy_price: float
    market_type: str = "BULL"
    s_level: str = ""              # "" or S1..S5
    enabled: bool = True
    name: str = ""                 # as typed in the client's sheet


@dataclass
class Holding:
    symbol: str
    slot_id: int
    market_type: str
    first_buy_date: str            # ISO date, ageing clock starts here
    qty: int = 0
    cost: float = 0.0              # total rupees paid for the qty still held
    slot_cost: float = 0.0         # part of cost funded by the slot
    reserve_cost: float = 0.0      # part of cost funded by the reserve
    last_buy_price: float = 0.0    # averaging trigger is measured from here
    adds: int = 0
    s_done: str = ""               # S-level already executed for this holding

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


@dataclass
class Slot:
    id: int
    cash: float
    symbol: Optional[str] = None


@dataclass
class Book:
    capital: float = 0.0
    equity_pct: float = 60.0
    reserve: float = 0.0
    slots: List[Slot] = field(default_factory=list)
    holdings: Dict[str, Holding] = field(default_factory=dict)
    realized: float = 0.0
    created: str = ""

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "capital": self.capital, "equity_pct": self.equity_pct,
            "reserve": self.reserve, "realized": self.realized, "created": self.created,
            "slots": [asdict(s) for s in self.slots],
            "holdings": {k: asdict(h) for k, h in self.holdings.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        b = cls(capital=float(d.get("capital", 0)), equity_pct=float(d.get("equity_pct", 60)),
                reserve=float(d.get("reserve", 0)), realized=float(d.get("realized", 0)),
                created=d.get("created", ""))
        b.slots = [Slot(**s) for s in d.get("slots", [])]
        b.holdings = {k: Holding(**h) for k, h in d.get("holdings", {}).items()}
        return b

    # -- views --------------------------------------------------------------
    @property
    def initialised(self) -> bool:
        return bool(self.slots)

    def free_slots(self) -> List[Slot]:
        return [s for s in self.slots if s.symbol is None]

    def slot(self, sid: int) -> Slot:
        return next(s for s in self.slots if s.id == sid)

    @property
    def slot_cash(self) -> float:
        return sum(s.cash for s in self.slots)

    def invested(self) -> float:
        return sum(h.cost for h in self.holdings.values())

    def market_value(self, ltps: Dict[str, float]) -> float:
        return sum(h.qty * (ltps.get(h.symbol) or h.avg) for h in self.holdings.values())

    def unrealized(self, ltps: Dict[str, float]) -> float:
        return sum(h.qty * ((ltps.get(h.symbol) or h.avg) - h.avg) for h in self.holdings.values())


# ----------------------------------------------------------------------
# set-up
# ----------------------------------------------------------------------
def new_book(capital: float, equity_pct: float, n: int, today: date) -> Book:
    if capital <= 0 or n < 1 or not (0 < equity_pct <= 100):
        raise ValueError("capital > 0, N >= 1 and 0 < equity% <= 100 required")
    per = capital * equity_pct / 100.0 / n
    return Book(capital=capital, equity_pct=equity_pct,
                reserve=capital * (100.0 - equity_pct) / 100.0,
                slots=[Slot(id=i + 1, cash=per) for i in range(n)],
                created=_d(today).isoformat())


# ----------------------------------------------------------------------
# rule maths
# ----------------------------------------------------------------------
def entry_ready(item: WatchItem, ltp: Optional[float], s: dict) -> bool:
    if not ltp or item.buy_price <= 0:
        return False
    return ltp <= item.buy_price * (1 + float(s["entry_tolerance_pct"]) / 100.0)


def ageing_steps(first_buy: str, today: date, ageing_days: int) -> int:
    if ageing_days <= 0:
        return 0
    days = (_d(today) - _d(first_buy)).days
    return max(0, days // int(ageing_days))


def target_price(h: Holding, market_type: str, today: date, s: dict, tick: float = 0.05) -> float:
    mt = market_type if market_type in MARKET_TYPES else h.market_type
    base = float(s["target_mult"][mt]) * h.avg
    steps = ageing_steps(h.first_buy_date, today, int(s["ageing_days"]))
    return round_tick(base * float(s["ageing_factor"][mt]) ** steps, tick)


def avg_down_trigger(h: Holding, s: dict) -> float:
    return h.last_buy_price * (1 - float(s["avg_down_drop_pct"]) / 100.0)


def avg_down_allowed(h: Holding, s: dict) -> bool:
    mx = int(s.get("avg_down_max_adds", 0) or 0)
    return mx <= 0 or h.adds < mx


def avg_down_qty(reserve: float, price: float, s: dict) -> int:
    return floor_qty(reserve * float(s["avg_down_reserve_pct"]) / 100.0, price)


def s_level_applies(h: Holding, ltp: float, s: dict) -> bool:
    """At a loss, or within near% above cost."""
    return ltp <= h.avg * (1 + float(s["s_level_near_pct"]) / 100.0)


def s_level_qty(h: Holding, level: str, s: dict) -> int:
    pct = float(s["s_level_pct"].get(level, 0))
    if pct <= 0 or h.qty <= 0:
        return 0
    if pct >= 100:
        return h.qty
    return min(h.qty, max(1, int(math.floor(h.qty * pct / 100.0))))


# ----------------------------------------------------------------------
# fills -> book
# ----------------------------------------------------------------------
def apply_buy(book: Book, symbol: str, qty: int, price: float, source: str,
              today: date, slot_id: Optional[int] = None, market_type: str = "BULL") -> Holding:
    """source = SLOT (new holding) or RESERVE (averaging an existing one)."""
    amount = qty * price
    if source == "SLOT":
        slot = book.slot(slot_id)
        h = book.holdings.get(symbol)
        if h is None:
            h = Holding(symbol=symbol, slot_id=slot_id, market_type=market_type,
                        first_buy_date=_d(today).isoformat())
            book.holdings[symbol] = h
        slot.symbol = symbol
        slot.cash -= amount
        h.slot_cost += amount
    else:
        h = book.holdings[symbol]
        book.reserve -= amount
        h.reserve_cost += amount
        h.adds += 1
    h.qty += qty
    h.cost += amount
    h.last_buy_price = price
    return h


def apply_sell(book: Book, symbol: str, qty: int, price: float) -> float:
    """Returns realized P/L of this sale."""
    h = book.holdings[symbol]
    qty = min(qty, h.qty)
    if qty <= 0:
        return 0.0
    frac = qty / h.qty
    cost_out = h.cost * frac
    slot_out = h.slot_cost * frac
    res_out = h.reserve_cost * frac
    proceeds = qty * price
    res_share = (res_out / cost_out) if cost_out > 0 else 0.0
    book.reserve += proceeds * res_share
    book.slot(h.slot_id).cash += proceeds * (1 - res_share)
    h.qty -= qty
    h.cost -= cost_out
    h.slot_cost -= slot_out
    h.reserve_cost -= res_out
    pnl = proceeds - cost_out
    book.realized += pnl
    if h.qty <= 0:
        book.slot(h.slot_id).symbol = None
        del book.holdings[symbol]
    return pnl


# ----------------------------------------------------------------------
# the decision step
# ----------------------------------------------------------------------
def decide(book: Book, watch: List[WatchItem], ltps: Dict[str, float], today: date,
           s: dict, busy: set, reserve_busy: bool = False, ticks: Dict[str, float] = None,
           busy_slots: set = frozenset()) -> list:
    """
    One pass over the book. Returns actions:
      {"kind": SELL_TARGET | SELL_SLEVEL | ADD | BUY, "symbol", "qty",
       "price", "reason", ["slot_id"], ["market_type"], ["level"]}
    `busy`: symbols with an order still working (nothing new for them).
    `reserve_busy`: an averaging order is working (one at a time).
    `busy_slots`: slot ids with a BUY still working (not reused meanwhile).
    """
    if s.get("kill_switch") or not book.initialised:
        return []
    ticks = ticks or {}
    by_sym = {w.symbol: w for w in watch}
    acts = []
    taken = set(busy)

    # 1) exits on target
    for sym, h in book.holdings.items():
        if sym in taken:
            continue
        ltp = ltps.get(sym)
        if not ltp:
            continue
        w = by_sym.get(sym)
        mt = (w.market_type if w else "") or h.market_type
        tgt = target_price(h, mt, today, s, ticks.get(sym, 0.05))
        if ltp >= tgt:
            acts.append({"kind": "SELL_TARGET", "symbol": sym, "qty": h.qty, "price": ltp,
                         "reason": f"target {tgt:.2f} ({mt}, ageing x"
                                   f"{ageing_steps(h.first_buy_date, today, int(s['ageing_days']))})"})
            taken.add(sym)

    # 2) S-level partial / full sells
    for sym, h in book.holdings.items():
        if sym in taken:
            continue
        w = by_sym.get(sym)
        lvl = norm_s_level(w.s_level) if w else ""
        ltp = ltps.get(sym)
        if not lvl or lvl == h.s_done or not ltp:
            continue
        if not s_level_applies(h, ltp, s):
            continue
        q = s_level_qty(h, lvl, s)
        if q > 0:
            acts.append({"kind": "SELL_SLEVEL", "symbol": sym, "qty": q, "price": ltp,
                         "level": lvl, "reason": f"{lvl} {s['s_level_pct'][lvl]:g}% "
                                                 f"(LTP {ltp:.2f} vs avg {h.avg:.2f})"})
            taken.add(sym)

    # 3) averaging down from the reserve (one order at a time)
    if not reserve_busy:
        for sym, h in book.holdings.items():
            if sym in taken:
                continue
            ltp = ltps.get(sym)
            if not ltp or not avg_down_allowed(h, s):
                continue
            if ltp <= avg_down_trigger(h, s):
                q = avg_down_qty(book.reserve, ltp, s)
                if q > 0:
                    acts.append({"kind": "ADD", "symbol": sym, "qty": q, "price": ltp,
                                 "reason": f"LTP {ltp:.2f} <= {avg_down_trigger(h, s):.2f} "
                                           f"({s['avg_down_drop_pct']:g}% below {h.last_buy_price:.2f})"})
                    taken.add(sym)
                    break

    # 4) new entries for free slots, in watchlist order
    free = [sl for sl in book.free_slots() if sl.id not in busy_slots]
    for w in watch:
        if not free:
            break
        if not w.enabled or w.symbol in taken or w.symbol in book.holdings:
            continue
        ltp = ltps.get(w.symbol)
        if not entry_ready(w, ltp, s):
            continue
        slot = free[0]
        if s["entry_order_type"] == "MARKET":
            px = ltp
            q = floor_qty(slot.cash, ltp * (1 + float(s["market_buffer_pct"]) / 100.0))
        else:
            px = round_tick(min(ltp, w.buy_price), ticks.get(w.symbol, 0.05))
            q = floor_qty(slot.cash, px)
        if q <= 0:
            continue
        acts.append({"kind": "BUY", "symbol": w.symbol, "qty": q, "price": px,
                     "slot_id": slot.id, "market_type": norm_market_type(w.market_type) or "BULL",
                     "reason": f"LTP {ltp:.2f} <= buy {w.buy_price:.2f} · slot {slot.id}"})
        taken.add(w.symbol)
        free.pop(0)
    return acts
