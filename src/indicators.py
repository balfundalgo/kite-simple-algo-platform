"""
indicators.py
=============
Daily RSI (Wilder, 14) for display: NIFTY 50 and every watchlist share.
History comes from Kite's daily candles once at start; the live value
replaces today's close with the current LTP.
"""

from typing import List, Optional


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def live_rsi(prev_closes: List[float], ltp: Optional[float], period: int = 14) -> Optional[float]:
    """prev_closes = completed daily closes (excluding today)."""
    if ltp is None:
        return rsi(prev_closes, period)
    return rsi(list(prev_closes) + [ltp], period)
