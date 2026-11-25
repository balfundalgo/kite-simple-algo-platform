"""
instruments.py
==============
The NSE share list behind the stock search box.

  * downloaded from Kite (kite.instruments("NSE")) once a day, NSE cash
    equities only (about 2,000+ shares)
  * cached in state/instruments_nse.json, so search works before login and
    on days the download fails (yesterday's list is used, with a warning)
  * search(text) ranks matches on symbol and company name:
        exact symbol > symbol starts with > name starts with >
        a word in the name starts with > symbol contains > name contains
    "yes" -> YESBANK, "ashok" -> ASHOKLEY, "kalyan" -> KALYANKJIL

Only symbols in this list can be added to the watchlist.
"""

import json
import threading
from typing import Dict, List, Optional

import config

CACHE_FILE = config.STATE_DIR / "instruments_nse.json"

_lock = threading.RLock()
_index: Dict[str, dict] = {}       # tradingsymbol -> row
_date: Optional[str] = None


def _rows_from_kite(raw: List[dict]) -> List[dict]:
    out = []
    for r in raw:
        if r.get("segment") != "NSE" or r.get("instrument_type") != "EQ":
            continue
        sym = r.get("tradingsymbol") or ""
        if not sym:
            continue
        out.append({"tradingsymbol": sym, "name": str(r.get("name") or "").strip(),
                    "instrument_token": int(r["instrument_token"]),
                    "tick_size": float(r.get("tick_size") or 0.05)})
    return out


def _set(rows: List[dict], day: str) -> None:
    global _index, _date
    with _lock:
        _index = {r["tradingsymbol"]: r for r in rows}
        _date = day


def load_cache() -> int:
    if not CACHE_FILE.exists():
        return 0
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        _set(data.get("rows", []), data.get("date"))
    except Exception:
        return 0
    return len(_index)


def set_from_kite(raw: List[dict]) -> int:
    rows = _rows_from_kite(raw)
    if not rows:
        raise RuntimeError("Kite returned no NSE equities")
    day = config.now_ist().date().isoformat()
    _set(rows, day)
    config.atomic_write_json(CACHE_FILE, {"date": day, "rows": rows})
    return len(rows)


def refresh(kite, force: bool = False) -> int:
    """Download today's list unless the cache is already from today."""
    if not force and is_fresh():
        return len(_index)
    return set_from_kite(kite.instruments("NSE"))


def refresh_public(api_key: str = "") -> int:
    """The instrument dump does not need a login; used before the first login."""
    from kiteconnect import KiteConnect
    return set_from_kite(KiteConnect(api_key=api_key or "x").instruments("NSE"))


def is_fresh() -> bool:
    return bool(_index) and _date == config.now_ist().date().isoformat()


def cache_date() -> Optional[str]:
    return _date


def index() -> Dict[str, dict]:
    with _lock:
        return dict(_index)


def get(symbol: str) -> Optional[dict]:
    with _lock:
        return _index.get(symbol)


def count() -> int:
    return len(_index)


def search(text: str, limit: int = 15) -> List[dict]:
    q = " ".join(str(text or "").upper().split())
    if not q:
        return []
    words = q.split()
    scored = []
    with _lock:
        rows = list(_index.values())
    for r in rows:
        sym = r["tradingsymbol"]
        name = r["name"].upper()
        name_words = name.split()
        if sym == q:
            s = 0
        elif sym.startswith(q):
            s = 1
        elif name.startswith(q):
            s = 2
        elif any(w.startswith(q) for w in name_words):
            s = 3
        elif len(words) > 1 and all(any(nw.startswith(w) for nw in name_words) for w in words):
            s = 3
        elif q in sym:
            s = 4
        elif q in name:
            s = 5
        else:
            continue
        if "-" in sym:                 # BE / SM series rank below the main listing
            s += 0.5
        scored.append((s, len(sym), sym, r))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in scored[:limit]]


load_cache()
