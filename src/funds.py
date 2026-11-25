"""
funds.py
========
Zerodha cash for delivery buys, pure functions (tested offline).

Source: kite.margins("equity")["available"]["live_balance"], the cash
available for trading during the day (opening balance + pay-in - used).
"net" is NOT used: it includes collateral (pledged shares), which cannot
pay for delivery (CNC) buys.

Zerodha's live_balance can lag a few minutes behind executions, so the
free cash the app works with is:

    free = live_balance
           - money held by our BUY / ADD orders still working
           - our buy fills from the last `lag_s` seconds (may not be in
             live_balance yet)

This errs on the safe side for a few minutes after a fill. Sale proceeds
are not added until Zerodha shows them.
"""

import time
from typing import List, Optional, Tuple


def live_cash(margins_equity: dict) -> Optional[float]:
    """Cash available for delivery buys from kite.margins('equity')."""
    if not margins_equity:
        return None
    av = margins_equity.get("available") or {}
    for key in ("live_balance", "cash"):
        v = av.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def free_cash(live_balance: Optional[float], committed: float,
              recent_fills: List[Tuple[float, float]], lag_s: float = 300,
              now: Optional[float] = None) -> Optional[float]:
    """recent_fills = [(epoch_time, amount)] of our buy fills."""
    if live_balance is None:
        return None
    now = time.time() if now is None else now
    recent = sum(a for t, a in recent_fills if now - t <= lag_s)
    return live_balance - committed - recent


def buy_amount(action: dict, market_buffer_pct: float, market: bool) -> float:
    px = float(action["price"])
    if market:
        px *= 1 + market_buffer_pct / 100.0
    return int(action["qty"]) * px
