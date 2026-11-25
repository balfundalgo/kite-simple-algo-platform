"""
reconcile.py
============
Book vs Zerodha, pure functions (tested offline).

Zerodha quantity per share (CNC delivery):
    holdings:  quantity + t1_quantity
               (quantity already drops on an intraday sale of settled shares;
                used_quantity = settled shares sold today)
    positions: today's CNC buys are not in holdings yet -> + buy_quantity
               today's CNC sells beyond used_quantity came out of today's
               buys -> - (sell_quantity - used_quantity)

States per share held in the book:
    OK        Zerodha has at least the book quantity
    EXTRA     Zerodha has more (e.g. the user's own shares) -> fine, only noted
    MISMATCH  Zerodha has LESS than the book -> the share is frozen (no orders)
              until the user chooses Sync to Zerodha or Keep book
    KEPT      user chose Keep book for exactly this Zerodha quantity
    BUSY      an order for it is working, checked again later
"""

from typing import Dict, List


def broker_quantities(holdings: List[dict], day_positions: List[dict]) -> Dict[str, int]:
    held: Dict[str, int] = {}
    used: Dict[str, int] = {}
    for h in holdings or []:
        sym = h.get("tradingsymbol")
        if not sym:
            continue
        held[sym] = held.get(sym, 0) + int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
        used[sym] = used.get(sym, 0) + int(h.get("used_quantity") or 0)
    buys: Dict[str, int] = {}
    sells: Dict[str, int] = {}
    for p in day_positions or []:
        if p.get("product") != "CNC":
            continue
        sym = p.get("tradingsymbol")
        buys[sym] = buys.get(sym, 0) + int(p.get("buy_quantity") or 0)
        sells[sym] = sells.get(sym, 0) + int(p.get("sell_quantity") or 0)
    out: Dict[str, int] = {}
    for sym in set(held) | set(buys) | set(sells):
        q = held.get(sym, 0) + buys.get(sym, 0) - max(0, sells.get(sym, 0) - used.get(sym, 0))
        out[sym] = max(0, q)
    return out


def compare(book_qty: Dict[str, int], broker: Dict[str, int], busy: set,
            kept: Dict[str, int]) -> List[dict]:
    rows = []
    for sym in sorted(book_qty):
        b = int(book_qty[sym])
        z = int(broker.get(sym, 0))
        if sym in busy:
            st = "BUSY"
        elif z >= b:
            st = "OK" if z == b else "EXTRA"
        elif kept.get(sym) == z:
            st = "KEPT"
        else:
            st = "MISMATCH"
        rows.append({"symbol": sym, "book": b, "zerodha": z, "diff": z - b, "state": st})
    return rows
